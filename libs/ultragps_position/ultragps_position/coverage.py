"""
UltraGPS Position Library — coverage module
===========================================
Which receivers can hear the transmitter where, so that a receiver layout can
be judged before it is built.

For any point of the arena this module predicts which receivers detect the
transmitter's direct sound in time to be timed, and from that how many
receivers cover the point (``coverage_map``).  It is a port of the UltraGPS
Simulator's direct-sound detection model (``engine/acoustics.py`` and
``engine/maps.py::_hears_direct`` / ``coverage``) for an open room:
no walls or obstacles, a transmitter that radiates all round, and nominal
modules (no module-to-module gain or aim spread).  Pure numpy/scipy,
vectorised over points × receivers: a coverage map on a 5 cm grid over a
4 × 4 m arena with six receivers takes about 10 ms.

The model
---------
One ping, as the firmware runs it (times from the base station's trigger):

1. The base station triggers every receiver's HC-SR04 at t = 0.  Each one
   listens from ``rx_ready_ms`` (3 ms), once its own burst has gone.
2. The transmitter fires ``burst_lead_ms`` later (4 ms: the firmware's 2 ms
   burst delay plus the radio and processing latency, as the calibration
   intercepts show).
3. Its direct sound reaches a receiver ``d`` cm away ``d / c`` later, with the
   speed of sound ``c`` from the air's temperature and humidity (344 m/s at
   20 °C and 50 %).
4. The burst arrives ``snr_db`` (30 dB) over the receiver's comparator
   threshold on axis at 1 m, less 1/r spreading, ISO 9613-1 air absorption at
   40 kHz (1.3 dB/m at 20 °C and 50 %) and the receiver's beam pattern: a
   circular piston, −6 dB at ±27.5° off axis, side lobes filled in, −30 dB
   behind (``directivity``).
5. The burst rings up through the resonant transducers as
   1 − exp(−t / ``rise_cycles``), and the comparator fires on the first
   40 kHz cycle over the threshold (``detect_cycle``).  A burst that does not
   cross it within its 8 cycles (less than 2.7 dB over the threshold) is lost.
6. The detection must come before the HC-SR04 gives up (``rx_ready_ms`` +
   ``timeout_ms`` = 41 ms) and before the base station stops sampling
   (``window_ms``: 80 ms at the default 10 Hz; the ground station's ``H`` and
   ``F`` commands set it).

``hears`` applies exactly that, as the simulator's ``_hears_direct`` does.  A
model with ``threshold=False`` replaces steps 4 and 5 with a hard range limit
(``max_range_cm``), and ``fov_half_deg`` and ``min_range_cm`` add a hard
field of view and minimum range.  Two models are provided:

    SIMULATOR   the simulator's "Realistic" receiver (the default)
    DATASHEET   the HC-SR04 datasheet's hard limits: 2-400 cm within ±15°

Checked against the HC-SR04
---------------------------
The receivers are HC-SR04 ranging modules used one way: the base station
triggers them all and each one times the transmitter's burst instead of the
echo of its own.  The datasheets (ElecFreaks "Ultrasonic Ranging Module
HC-SR04"; ITead Studio "Ultrasonic ranging module: HC-SR04", 2010) give,
for echo ranging off a flat target of at least 0.5 m²:

    ranging distance    2 cm - 400 cm (ITead: 2 cm - 500 cm)
    measuring angle     15° (ITead: "effectual angle < 15°", and its measured
                        pattern is "best in 30 degree angle"): a ±15° cone
    carrier and burst   40 kHz, 8 cycles, after a 10 µs trigger pulse
    echo pulse          150 µs - 25 ms, or 38 ms high when nothing is
                        detected (ITead)
    resolution          0.3 cm (ITead); "accuracy can reach to 3mm"
    measurement cycle   at least 60 ms (ITead: at least 50 ms)

Those are echo-ranging limits.  The one-way link reaches farther and wider,
for three reasons:

* Path.  An echo travels out and back, so 4 m of range is 8 m of spreading
  and absorption, plus the loss at the target: the datasheet's 0.5 m² smooth
  plane is close to a mirror, and anything smaller or tilted returns far less.
  One way, a receiver 4 m away hears sound that has travelled 4 m and never
  bounced.
* Beam.  An echo passes through the module's transmit and receive beams, so
  its angular response is the beam pattern squared: the piston's −6 dB
  half-angle shrinks from 27.5° to about 20°, and the datasheet quotes ±15°.
  One way, only the receiver's beam counts, as the transmitter radiates all
  round.
* Timing.  The 38 ms timeout is 6.5 m of echo round trip.  One way, the
  receiver times out 41 ms after the trigger, and the transmitter fires 4 ms
  in, leaving 37 ms: 12.7 m.

So DATASHEET (2-400 cm within ±15°) is the conservative bound: a receiver
should hear the transmitter at least there.  (Only where a sampling window
shorter than about 16 ms binds does it reach a little beyond SIMULATOR, by
the one or two cycles, 1-2 cm, that the threshold model takes to detect the
burst; hard limits time it on arrival, as in the simulator.)

SIMULATOR is the best estimate: with the simulator's 30 dB of signal over
the threshold on axis at 1 m, a nominal receiver hears the transmitter out to
about 7.5 m on its axis, 3.5 m at 45° and 2.8 m at 60° off it (the
simulator's MANUAL, section 13), and about 0.6 m behind it, so in an arena a
few metres across it is the angle, not the distance, that loses a receiver.
The 30 dB is an estimate, not a measurement: lower ``snr_db`` to model
weaker modules (at 24 dB the reach falls to 5.3 m on axis and 2.2 m at 45°).
The transmitter is assumed to radiate all round; a directional transmitter's
own beam would cut the reach further.  Echo ranging's 2 cm minimum (the
module's own burst ringing down) has no one-way counterpart, since the
transmitter fires after the receivers listen.

The base station's sampling window bounds both models, whatever the signal:
a burst must be detected before sampling stops, so the farthest receiver it
can time is about (``window_ms`` − ``burst_lead_ms``) × c, less a cycle or
two of detection (``SensorModel.timing_range_cm``).  At the default 10 Hz
the window is 80 ms (26 m), and the module's 41 ms timeout (12.7 m) binds
first; at 50 Hz the window is at most 16 ms (80 % of the 20 ms period), and
nothing beyond about 4.1 m can be timed.

Quick-start
-----------
    from ultragps_position import coverage

    coords = [[-118, -118], [-118, 0], [-118, 118],
              [118, -118], [118, 0], [118, 118]]       # receivers, id order
    facing = [None] * 6                                 # None: aim at the centroid

    xs, ys, count = coverage.coverage_map(coords, facing)   # receivers per point
    print(coverage.summary(coords, facing))                 # share with >= 3, ...

    # The base station sampling for 16 ms (50 Hz), against the datasheet:
    fast = coverage.SIMULATOR.copy(window_ms=16.0)
    heard = coverage.hears([[0, 0], [300, 50]], coords, facing, fast)   # (2, 6)
    heard = coverage.hears([[0, 0], [300, 50]], coords, facing, coverage.DATASHEET)

Conventions
-----------
* Centimetres, milliseconds, degrees and dB throughout.
* ``coords`` is an (R, 2) array of receiver positions in id order; ``points``
  is an (M, 2) array (a single ``(x, y)`` counts as M = 1).
* Facing: degrees, 0 = +x, 90 = +y.  ``facing_deg`` is a sequence with one
  entry per receiver; ``None`` (or NaN) entries, or ``facing_deg=None``, aim
  that receiver at the centroid of all receivers (``default_facing``).
* Maps return ``(xs, ys, Z)`` with ``Z[j, i]`` at ``(xs[i], ys[j])``, on a
  grid over the receivers' box plus ``margin`` (``grid``), as in the
  simulator.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Callable, Iterator, Optional, Sequence

import numpy as np
from scipy.special import j1


# ─────────────────────────────────────────────────────────────────────────────
# Constants (the simulator's engine/acoustics.py)
# ─────────────────────────────────────────────────────────────────────────────

FREQ_HZ       = 40_000.0       # HC-SR04 carrier
CYCLE_MS      = 1000.0 / FREQ_HZ
BURST_CYCLES  = 8              # HC-SR04 sends an 8-cycle burst
X_6DB         = 2.215089       # 2 J1(x) / x = 0.5  (−6 dB point of a piston)
BACK_LEVEL    = 0.03           # response behind the transducer (−30 dB)
ENVELOPE      = 2.0 * math.sqrt(2.0 / math.pi)
X_ENVELOPE    = 2.4444576      # main lobe meets the side-lobe envelope here, without a step
BACK_FROM     = 90.0           # off-axis angle where the response starts falling to BACK_LEVEL ...
BACK_TO       = 120.0          # ... which it reaches here
NEAR_FIELD_CM = 5.0            # spreading is capped inside the transducer's near field

MARGIN_CM     = 30.0           # map grids reach this far outside the receivers' box

_CHUNK        = 32_768         # points per block: bounds the (points × receivers) temporaries


# ─────────────────────────────────────────────────────────────────────────────
# Acoustics
# ─────────────────────────────────────────────────────────────────────────────

def speed_of_sound(temp_c: float, humidity_pct: float = 0.0) -> float:
    """Speed of sound in air, m/s.

    Moist air is lighter and a little less stiff than dry air: saturated air
    is about 1.3 m/s faster than dry air at 20 °C, 2.4 m/s at 30 °C and
    5.6 m/s at 45 °C.  344.05 m/s at 20 °C and 50 %.

    Args:
        temp_c:       Air temperature, °C.
        humidity_pct: Relative humidity, % (0-100).
    """
    x     = vapour_fraction(temp_c, humidity_pct)
    gamma = (3.5 * (1.0 - x) + 4.03 * x) / (2.5 * (1.0 - x) + 3.03 * x)
    molar = 28.97 * (1.0 - x) + 18.02 * x
    return (331.3 + 0.606 * temp_c) * math.sqrt(gamma / 1.4 * 28.97 / molar)


def vapour_fraction(temp_c: float, humidity_pct: float, pressure_kpa: float = 101.325) -> float:
    """Mole fraction of water vapour in air (ISO 9613-1 saturation pressure)."""
    T01 = 273.16
    c   = -6.8346 * (T01 / (temp_c + 273.15)) ** 1.261 + 4.6151
    return min(humidity_pct / 100.0 * 10.0 ** c * (101.325 / pressure_kpa), 1.0)


@lru_cache(maxsize=64)
def absorption_db_per_m(temp_c: float, humidity_pct: float, freq_hz: float = FREQ_HZ,
                        pressure_kpa: float = 101.325) -> float:
    """Atmospheric absorption, dB per metre (ISO 9613-1:1993, equations 3-5).

    About 1.3 dB/m at 40 kHz in a room (20 °C, 50 %): ultrasound fades far
    faster than audible sound.
    """
    T, T0, pr = temp_c + 273.15, 293.15, 101.325
    pa  = pressure_kpa
    h   = 100.0 * vapour_fraction(temp_c, humidity_pct, pa)   # molar water-vapour concentration, %
    frO = (pa / pr) * (24.0 + 4.04e4 * h * (0.02 + h) / (0.391 + h))
    frN = (pa / pr) * (T / T0) ** -0.5 * (
        9.0 + 280.0 * h * math.exp(-4.170 * ((T / T0) ** (-1 / 3) - 1.0)))
    f2  = freq_hz ** 2
    return 8.686 * f2 * (
        1.84e-11 * (pr / pa) * (T / T0) ** 0.5
        + (T / T0) ** -2.5 * (0.01275 * math.exp(-2239.1 / T) / (frO + f2 / frO)
                              + 0.1068 * math.exp(-3352.0 / T) / (frN + f2 / frN)))


def piston_ka(half_angle_deg: float) -> float:
    """``ka`` of a circular piston whose response is −6 dB at ``half_angle_deg`` off axis."""
    return X_6DB / math.sin(math.radians(min(max(half_angle_deg, 1.0), 89.0)))


def directivity(off_axis_deg, half_angle_deg: float):
    """Amplitude response (0..1) of a transducer ``off_axis_deg`` off its axis.

    A circular piston's main lobe ``2 J1(x) / x``, then the upper envelope of
    its side lobes (``2·sqrt(2/π)·x^-1.5``), since real transducers have no
    deep nulls.  Beside the transducer (90°) the response falls smoothly, in
    dB, to ``BACK_LEVEL`` behind it (120° and beyond).

    Args:
        off_axis_deg:   Angle(s) off the axis, degrees (any range: wrapped to
                        ±180°).  A scalar or an array.
        half_angle_deg: −6 dB half-angle of the beam, degrees.

    Returns:
        A float for a scalar angle, else an array of the same shape (NaN for
        a non-finite angle).
    """
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        off  = np.abs((np.asarray(off_axis_deg, dtype=float) + 180.0) % 360.0 - 180.0)
        x    = piston_ka(half_angle_deg) * np.sin(np.radians(np.minimum(off, BACK_FROM)))
        lobe = np.where(x <= X_ENVELOPE, 2.0 * j1(x) / x, ENVELOPE * x ** -1.5)
        d    = np.where(x < 1e-9, 1.0, np.maximum(lobe, BACK_LEVEL))
        f    = (off - BACK_FROM) / (BACK_TO - BACK_FROM)
        side = d ** (1.0 - f) * BACK_LEVEL ** f                          # straight line in dB
        out  = np.where(off >= BACK_TO, BACK_LEVEL, np.where(off <= BACK_FROM, d, side))
    return float(out) if out.ndim == 0 else out


def spreading(length_cm, temp_c: float, humidity_pct: float):
    """Amplitude after ``length_cm`` of travel, relative to 1 m.

    1/r spreading times atmospheric absorption over the whole length (so 1 m
    itself loses about 1.3 dB).  Inside the transducer's near field
    (``NEAR_FIELD_CM``) the amplitude stops growing.

    Args:
        length_cm:    Path length(s), cm.  A scalar or an array.
        temp_c:       Air temperature, °C.
        humidity_pct: Relative humidity, %.

    Returns:
        A float for a scalar length, else an array of the same shape.
    """
    length  = np.maximum(np.asarray(length_cm, dtype=float), NEAR_FIELD_CM)
    loss_db = absorption_db_per_m(temp_c, humidity_pct) * length / 100.0
    out     = (100.0 / length) * 10.0 ** (-loss_db / 20.0)
    return float(out) if out.ndim == 0 else out


def detect_cycle(ratio: float, rise_cycles: float, burst_cycles: int = BURST_CYCLES) -> int | None:
    """First cycle (1, 2, ...) at which a burst crosses the detection threshold.

    The burst's envelope through the resonant transducers rises as
    ``1 − exp(−t / rise_cycles)`` and stops rising when the drive stops after
    ``burst_cycles``.

    Args:
        ratio:        The burst's full amplitude over the threshold (linear).
        rise_cycles:  Ring-up time constant, cycles.
        burst_cycles: Cycles in the burst.

    Returns:
        The cycle, or ``None`` if the burst never crosses the threshold.
    """
    if not ratio > 1.0:                                 # also NaN
        return None
    t = -rise_cycles * math.log(1.0 - 1.0 / ratio)
    k = max(1, math.ceil(t - 1e-9))
    return k if k <= burst_cycles else None


def detect_cycles(ratio, rise_cycles: float, burst_cycles: int = BURST_CYCLES) -> np.ndarray:
    """``detect_cycle`` for an array of ratios, with 0 for "never".

    Returns:
        An int array of the same shape as ``ratio``.
    """
    r  = np.asarray(ratio, dtype=float)
    ok = r > 1.0                                        # also NaN
    with np.errstate(divide="ignore", invalid="ignore"):
        t = -rise_cycles * np.log(1.0 - 1.0 / np.where(ok, r, 2.0))
    k = np.maximum(1.0, np.ceil(t - 1e-9))
    return np.where(ok & (k <= burst_cycles), k, 0.0).astype(int)


# ─────────────────────────────────────────────────────────────────────────────
# Sensor model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SensorModel:
    """What a receiver can hear: the limits ``hears`` applies to the direct sound.

    With ``threshold=True`` (the simulator's physical model) the signal decides
    range and angle: the burst's strength over the comparator threshold from
    ``snr_db``, spreading, absorption and the receiver's beam, and the cycle
    the comparator fires on.  With ``threshold=False`` the receiver hears
    everything within ``max_range_cm`` (no limit when ``None``), whatever its
    strength.  The timing limits (``burst_lead_ms``, ``rx_ready_ms``,
    ``timeout_ms``, ``window_ms``) and the hard limits that are set
    (``max_range_cm``, ``fov_half_deg``, ``min_range_cm``) apply to both.

    Immutable and hashable; ``copy(**changes)`` makes a changed one, e.g.
    ``SIMULATOR.copy(window_ms=16.0)`` for the base station's sampling window
    at 50 Hz.

    Attributes:
        name:          Label for menus and messages.
        threshold:     Physical threshold detection (True) or hard range only.
        snr_db:        Burst over the comparator threshold on axis at 1 m,
                       before air absorption, dB.
        rx_beam_deg:   Receiver beam's −6 dB half-angle, degrees.
        rise_cycles:   Transducer ring-up time constant, cycles of 40 kHz.
        burst_cycles:  Cycles in the transmitter's burst.
        temperature_c: Air temperature, °C (speed of sound, absorption).
        humidity_pct:  Relative humidity, %.
        burst_lead_ms: The transmitter fires this long after the receivers'
                       trigger: the base station's burst delay (2 ms by
                       default; the ground station's ``D`` and ``E``
                       commands change it) plus, as the calibration
                       intercepts suggest, about 2 ms of radio and
                       processing latency.  A lead shorter than
                       ``rx_ready_ms`` leaves a blind zone round each
                       receiver.
        rx_ready_ms:   The receivers listen from this long after their trigger.
        timeout_ms:    The HC-SR04 gives up this long after it starts listening.
        window_ms:     The base station stops sampling this long after the
                       trigger (the ground station's ``H`` setting).
        max_range_cm:  Nothing farther is heard (cm), or ``None``: no hard
                       range limit.
        fov_half_deg:  Nothing farther off the receiver's axis is heard
                       (degrees), or ``None``: no hard field of view.
        min_range_cm:  Nothing closer is heard (cm); 0 for no limit.
    """

    name:          str             = "Custom"
    threshold:     bool            = True
    snr_db:        float           = 30.0
    rx_beam_deg:   float           = 27.5
    rise_cycles:   float           = 6.0
    burst_cycles:  int             = BURST_CYCLES
    temperature_c: float           = 20.0
    humidity_pct:  float           = 50.0
    burst_lead_ms: float           = 4.0
    rx_ready_ms:   float           = 3.0
    timeout_ms:    float           = 38.0
    window_ms:     float           = 80.0
    max_range_cm:  Optional[float] = None
    fov_half_deg:  Optional[float] = None
    min_range_cm:  float           = 2.0

    def __post_init__(self):
        problems = []
        if not math.isfinite(self.snr_db):
            problems.append("snr_db must be finite")
        if not 0.0 < self.rx_beam_deg <= 90.0:
            problems.append("rx_beam_deg must be in (0, 90]")
        if not 0.0 < self.rise_cycles < math.inf:
            problems.append("rise_cycles must be > 0 and finite")
        if not (1 <= self.burst_cycles < math.inf and float(self.burst_cycles).is_integer()):
            problems.append("burst_cycles must be a whole number >= 1")
        if not -273.15 < self.temperature_c < math.inf:
            problems.append("temperature_c must be finite and above absolute zero")
        if not 0.0 <= self.humidity_pct <= 100.0:
            problems.append("humidity_pct must be in [0, 100]")
        for name in ("burst_lead_ms", "rx_ready_ms"):
            if not 0.0 <= getattr(self, name) < math.inf:
                problems.append(f"{name} must be >= 0")
        for name in ("timeout_ms", "window_ms"):
            if not 0.0 < getattr(self, name) < math.inf:
                problems.append(f"{name} must be > 0")
        if self.max_range_cm is not None and not self.max_range_cm > 0.0:
            problems.append("max_range_cm must be > 0 or None")
        if self.fov_half_deg is not None and not 0.0 < self.fov_half_deg <= 180.0:
            problems.append("fov_half_deg must be in (0, 180] or None")
        if not 0.0 <= self.min_range_cm < math.inf:
            problems.append("min_range_cm must be >= 0")
        if problems:
            raise ValueError(f"SensorModel {self.name!r}: " + "; ".join(problems))

    def copy(self, **changes) -> "SensorModel":
        """A copy with ``changes`` applied (validated like a new model)."""
        return replace(self, **changes)

    @property
    def sound_speed(self) -> float:
        """Speed of sound in this model's air, m/s."""
        return speed_of_sound(self.temperature_c, self.humidity_pct)

    @property
    def give_up_ms(self) -> float:
        """When a reading is lost: the module times out or sampling stops, ms after the trigger."""
        return min(self.rx_ready_ms + self.timeout_ms, self.window_ms)

    @property
    def timing_range_cm(self) -> float:
        """Farthest distance (cm) whose direct sound can still be timed.

        The detection, on the burst's first cycle at the earliest with the
        threshold model, must come before ``give_up_ms``: (give_up −
        burst_lead − one cycle) × c.  This is the timing limit alone; the
        signal (``reach``) usually limits the range first, except with a short
        sampling window.  0 when nothing can be timed.
        """
        first = CYCLE_MS if self.threshold else 0.0
        return max(self.give_up_ms - self.burst_lead_ms - first, 0.0) * self.sound_speed / 10.0

    def describe(self) -> str:
        """One line on what this model's receiver hears, for labels and tooltips."""
        limit = ("sampling window" if self.window_ms < self.rx_ready_ms + self.timeout_ms
                 else "module timeout")
        timed = f"timing allows {self.timing_range_cm / 100.0:.1f} m ({limit})"
        if self.threshold:
            r0, r45, r60 = reach([0.0, 45.0, 60.0], self)
            text = (f"{self.snr_db:g} dB at 1 m, beam ±{self.rx_beam_deg:g}° at −6 dB: "
                    f"hears {r0 / 100.0:.1f} m on axis, {r45 / 100.0:.1f} m at 45°, "
                    f"{r60 / 100.0:.1f} m at 60°")
        else:
            far  = ("any distance" if self.max_range_cm is None or math.isinf(self.max_range_cm)
                    else f"{self.max_range_cm:g} cm")
            text = f"hears {self.min_range_cm:g} cm to {far}"
        if self.fov_half_deg is not None:
            text += f" within ±{self.fov_half_deg:g}°"
        return f"{self.name}: {text}; {timed}"


#: The simulator's "Realistic" receiver: physical beam and threshold model,
#: nominal module, transmitter radiating all round.  No minimum range, as in
#: the simulator.
SIMULATOR = SensorModel(name="Simulator", threshold=True, min_range_cm=0.0)

#: The HC-SR04 datasheet's echo-ranging limits as hard limits: 2-400 cm within
#: ±15° of the receiver's axis, with the same timing.  The conservative bound.
DATASHEET = SensorModel(name="HC-SR04 datasheet", threshold=False, max_range_cm=400.0,
                        fov_half_deg=15.0, min_range_cm=2.0)


# ─────────────────────────────────────────────────────────────────────────────
# Receiver facing
# ─────────────────────────────────────────────────────────────────────────────

def default_facing(coords) -> np.ndarray:
    """Facing (degrees, 0 = +x, 90 = +y) that aims every receiver at the
    centroid (mean position) of all the receivers.

    A receiver standing on the centroid faces 0°.

    Args:
        coords: (R, 2) receiver positions, cm.

    Returns:
        (R,) float array.
    """
    C = _as_points(coords, "coords")
    if not len(C):
        return np.empty(0)
    to_c = C.mean(axis=0) - C
    return np.degrees(np.arctan2(to_c[:, 1], to_c[:, 0]))


def resolve_facing(coords, facing: Optional[Sequence[Optional[float]]] = None) -> np.ndarray:
    """Every receiver's facing, with the automatic one where none is given.

    Args:
        coords: (R, 2) receiver positions, cm.
        facing: ``None`` (all automatic), or one entry per receiver: degrees,
                or ``None`` / NaN for automatic (``default_facing``).

    Returns:
        (R,) float array of degrees.

    Raises:
        ValueError: If ``facing`` does not have one entry per receiver.
    """
    out = default_facing(coords)
    if facing is None:
        return out
    entries = list(facing)
    if len(entries) != len(out):
        raise ValueError(f"facing has {len(entries)} entries for {len(out)} receivers")
    for i, value in enumerate(entries):
        if value is not None and math.isfinite(float(value)):
            out[i] = float(value)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────────────────────────────────────

def hears(points, coords, facing_deg=None, model: SensorModel = SIMULATOR) -> np.ndarray:
    """Which receivers detect the transmitter's direct sound from each point.

    The simulator's ``_hears_direct`` for a clear line of sight: the burst
    must arrive once the receiver listens, be strong enough to cross the
    threshold within the burst (threshold model) or be within
    ``max_range_cm`` (hard model), and be detected before the module times
    out and the base station stops sampling; plus the model's hard field of
    view and minimum range, when set.

    Args:
        points:     (M, 2) transmitter positions, cm.
        coords:     (R, 2) receiver positions, cm, in id order.
        facing_deg: Receiver facings (see ``resolve_facing``); ``None``: all
                    aimed at the centroid.
        model:      What a receiver can hear.

    Returns:
        (M, R) bool array: ``[m, r]`` is True when receiver ``r`` times the
        direct sound from point ``m``.  A point with a NaN or infinite
        coordinate is heard by none.
    """
    P, C, facing = _prepare(points, coords, facing_deg)
    return _by_block(P, lambda block: _heard(*_offsets(block, C, facing), model), (len(C),), bool)


def margin_db(points, coords, facing_deg=None, model: SensorModel = SIMULATOR) -> np.ndarray:
    """Strength of the direct sound over each receiver's comparator threshold, dB.

    For display: ``snr_db`` + 20·log10(spreading × beam response), as the
    simulator's ``strength_db`` for a direct path.  It ignores the timing and
    hard limits (``hears`` decides); a burst needs about 2.7 dB to be
    detected within its 8 cycles at all, and more to be detected on its
    first cycle.  Computed from the model's physics even for a model without
    threshold detection.

    Args:
        points, coords, facing_deg, model: As for ``hears``.

    Returns:
        (M, R) float array, dB; NaN for a point with a NaN or infinite
        coordinate.
    """
    P, C, facing = _prepare(points, coords, facing_deg)
    return _by_block(P, lambda block: _strength_db(*_offsets(block, C, facing), model),
                     (len(C),), float)


def reach(off_axis_deg=0.0, model: SensorModel = SIMULATOR):
    """Farthest distance (cm) at which a receiver hears the transmitter
    ``off_axis_deg`` off its axis, with every limit of ``model``.

    Along any one direction the receiver hears the transmitter from close by
    (from its minimum range, or from the edge of its blind zone if it starts
    listening after the transmitter fires) out to this distance, so ``reach``
    is the radius of the receiver's footprint in that direction (see
    ``footprint``).  Found by bisection to well under a micrometre.

    Args:
        off_axis_deg: Angle(s) off the receiver's axis, degrees.  A scalar or
                      an array.
        model:        What a receiver can hear.

    Returns:
        A float for a scalar angle, else an array of the same shape; 0 where
        the receiver hears nothing in that direction (outside a hard field of
        view, or nothing can be timed).
    """
    off  = np.asarray(off_axis_deg, dtype=float)
    c    = model.sound_speed / 10.0                                  # cm per ms
    near = max(model.min_range_cm, (model.rx_ready_ms - model.burst_lead_ms) * c, 0.0) + 1e-6
    far  = model.timing_range_cm
    if model.max_range_cm is not None:
        far = min(far, model.max_range_cm)
    lo    = np.full(off.shape, near)
    hi    = np.full(off.shape, max(far, near) + 1.0)                 # never heard there
    close = _heard(lo, off, model)                                   # not even close: nothing
    for _ in range(64):
        mid = 0.5 * (lo + hi)
        ok  = _heard(mid, off, model)
        lo  = np.where(ok, mid, lo)
        hi  = np.where(ok, hi, mid)
    out = np.where(close, lo, 0.0)
    return float(out) if out.ndim == 0 else out


def footprint(position, facing_deg: float, model: SensorModel = SIMULATOR,
              n: int = 181) -> np.ndarray:
    """Outline of where one receiver hears the transmitter, for drawing.

    The receiver's ``reach`` in ``n`` directions all round it (plus the edges
    of a hard field of view), as a closed polygon; where it hears nothing the
    outline passes through the receiver itself.

    Args:
        position:   (x, y) of the receiver, cm.
        facing_deg: Its facing, degrees.
        model:      What a receiver can hear.
        n:          Directions sampled (at least 3), evenly from −180° to
                    180° off axis.

    Returns:
        (K, 2) float array of polygon vertices, cm; the first equals the last.
    """
    x0, y0 = (float(v) for v in np.asarray(position, dtype=float).reshape(2))
    angles = np.linspace(-180.0, 180.0, max(int(n), 3))
    if model.fov_half_deg is not None and model.fov_half_deg < 180.0:
        angles = np.union1d(angles, [-model.fov_half_deg, model.fov_half_deg])
    r   = reach(angles, model)
    b   = np.radians(float(facing_deg) + angles)
    out = np.column_stack((x0 + r * np.cos(b), y0 + r * np.sin(b)))
    out[-1] = out[0]                                    # −180° and 180° round differently
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Maps
# ─────────────────────────────────────────────────────────────────────────────

def grid(coords, step: float = 5.0, margin: float = MARGIN_CM) -> tuple[np.ndarray, np.ndarray]:
    """Grid lines ``step`` cm apart over the receivers' box plus ``margin``,
    centred on it (the simulator's ``maps.grid``).

    Args:
        coords: (R, 2) receiver positions, cm (at least one, all finite).
        step:   Grid spacing, cm (> 0).
        margin: How far the grid reaches outside the receivers' box, cm.

    Returns:
        ``(xs, ys)``: the x and y grid lines, ascending.

    Raises:
        ValueError: With no receivers, a receiver position that is not
                    finite, a step that is not positive and finite, or a
                    margin that is not finite.
    """
    C = _as_points(coords, "coords")
    if not len(C):
        raise ValueError("a grid needs at least one receiver")
    if not np.all(np.isfinite(C)):
        raise ValueError("a grid needs finite receiver positions")
    if not 0.0 < step < math.inf:
        raise ValueError(f"grid step must be > 0 and finite, not {step!r}")
    if not math.isfinite(margin):
        raise ValueError(f"grid margin must be finite, not {margin!r}")
    lo   = np.min(C, axis=0) - margin
    hi   = np.max(C, axis=0) + margin
    axes = []
    for a, b in zip(lo, hi):
        n = max(math.floor((b - a) / step + 1e-9) + 1, 0)
        axes.append(a + ((b - a) - (n - 1) * step) / 2 + step * np.arange(n))
    return axes[0], axes[1]


def coverage_map(coords, facing_deg=None, model: SensorModel = SIMULATOR, step: float = 5.0,
                 margin: float = MARGIN_CM) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Number of receivers that hear the transmitter's direct sound at each
    point of a grid over the receivers' box (the simulator's ``maps.coverage``).

    Three receivers are needed for a fix, four to reject one bad reading.

    Args:
        coords:     (R, 2) receiver positions, cm, in id order.
        facing_deg: Receiver facings (see ``resolve_facing``).
        model:      What a receiver can hear.
        step:       Grid spacing, cm.
        margin:     How far the grid reaches outside the receivers' box, cm.

    Returns:
        ``(xs, ys, Z)``: ``Z[j, i]`` is the count at ``(xs[i], ys[j])``, as
        floats (whole numbers) so callers can mask points with NaN.

    Raises:
        ValueError: As ``grid`` does, e.g. with no receivers (no grid to
                    centre).
    """
    xs, ys = grid(coords, step, margin)
    count  = hears(_mesh(xs, ys), coords, facing_deg, model).sum(axis=1)
    return xs, ys, count.reshape(len(ys), len(xs)).astype(float)


def summary(coords, facing_deg=None, model: SensorModel = SIMULATOR, step: float = 5.0,
            inside: Optional[Callable[[np.ndarray], np.ndarray]] = None) -> dict:
    """How well a receiver layout covers the arena, in a few numbers.

    Judged on a ``step`` cm grid over the receivers' box (where the position
    library accepts a fix), or over the points ``inside`` selects.

    Args:
        coords:     (R, 2) receiver positions, cm, in id order.
        facing_deg: Receiver facings (see ``resolve_facing``).
        model:      What a receiver can hear.
        step:       Grid spacing, cm.
        inside:     Optional callable taking an (M, 2) array of points and
                    returning an (M,) bool mask of those that belong to the
                    area to judge, e.g. the arena outline's
                    ``matplotlib.path.Path(outline).contains_points``.  It is
                    offered a grid over the receivers' box plus
                    ``MARGIN_CM``.

    Returns:
        dict with
          ``points``      grid points judged (int);
          ``receivers``   R (int);
          ``share_3``     share (0..1) of them heard by at least 3 receivers:
                          a fix is possible;
          ``share_4``     ... by at least 4: one bad reading can be rejected;
          ``share_all``   ... by every receiver;
          ``min_count``   fewest receivers hearing any of them (int).
        With no receivers yet, or no points to judge, the shares and
        ``min_count`` are 0.

    Raises:
        ValueError: As ``grid`` does for a bad step or receiver position, or
                    if ``inside`` returns the wrong number of values.
    """
    C = _as_points(coords, "coords")
    if not len(C):                                      # an arena still being laid out
        P = np.empty((0, 2))
    else:
        xs, ys = grid(C, step, 0.0 if inside is None else MARGIN_CM)
        P      = _mesh(xs, ys)
    if inside is not None and len(P):
        mask = np.asarray(inside(P), dtype=bool).reshape(-1)
        if mask.shape != (len(P),):
            raise ValueError(f"inside() returned {mask.shape[0]} values for {len(P)} points")
        P = P[mask]
    heard = hears(P, C, facing_deg, model)
    count = heard.sum(axis=1)
    n     = len(P)
    share = (lambda k: float(np.mean(count >= k))) if n else (lambda k: 0.0)
    return {
        "points":      n,
        "receivers":   len(C),
        "share_3":     share(3),
        "share_4":     share(4),
        "share_all":   share(max(len(C), 1)),
        "min_count":   int(count.min()) if n else 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _as_points(a, what: str) -> np.ndarray:
    """``a`` as an (N, 2) float array; a single (x, y) becomes N = 1."""
    arr = np.asarray(a, dtype=float)
    if arr.size == 0:
        return arr.reshape(0, 2)
    if arr.ndim == 1 and arr.shape[0] == 2:
        arr = arr[None, :]
    if arr.ndim != 2 or arr.shape[1] != 2:
        raise ValueError(f"{what} must be (x, y) pairs in an (N, 2) array, not shape {arr.shape}")
    return arr


def _prepare(points, coords, facing_deg) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Validated points, receiver positions and facings."""
    C = _as_points(coords, "coords")
    return _as_points(points, "points"), C, resolve_facing(C, facing_deg)


def _mesh(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Every grid point as an (len(ys) × len(xs), 2) array, row by row (y outer)."""
    X, Y = np.meshgrid(xs, ys)
    return np.column_stack((X.ravel(), Y.ravel()))


def _blocks(P: np.ndarray) -> Iterator[np.ndarray]:
    """``P`` in blocks of at most ``_CHUNK`` rows (one empty block if empty)."""
    for s in range(0, max(len(P), 1), _CHUNK):
        yield P[s:s + _CHUNK]


def _by_block(P: np.ndarray, fn, tail: tuple, dtype) -> np.ndarray:
    """``fn`` over ``P`` block by block, stacked: an (len(P), *tail) array."""
    parts = [fn(block) for block in _blocks(P)]
    return np.concatenate(parts).astype(dtype, copy=False).reshape((len(P),) + tail)


def _offsets(P: np.ndarray, C: np.ndarray, facing: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Distance (cm) from each receiver to each point, and how far off the
    receiver's axis the point lies (degrees, unwrapped), both (M, R).

    The direction is the simulator's: the reverse of the unit vector from the
    point to the receiver (so a point on the receiver itself lies at −180°).
    """
    dx   = C[None, :, 0] - P[:, None, 0]
    dy   = C[None, :, 1] - P[:, None, 1]
    dist = np.hypot(dx, dy)
    with np.errstate(divide="ignore", invalid="ignore"):
        ux = np.where(dist > 0.0, dx / dist, 0.0)
        uy = np.where(dist > 0.0, dy / dist, 0.0)
    return dist, np.degrees(np.arctan2(-uy, -ux)) - facing[None, :]


def _strength_db(dist, off, model: SensorModel) -> np.ndarray:
    """Direct sound's strength over the threshold, dB (the simulator's
    ``Hardware.strength_db`` with an omnidirectional transmitter)."""
    a = 10.0 ** (model.snr_db / 20.0) * spreading(dist, model.temperature_c, model.humidity_pct)
    a = a * directivity(off, model.rx_beam_deg)
    with np.errstate(invalid="ignore"):
        return 20.0 * np.log10(np.maximum(a, 1e-12))


def _heard(dist, off, model: SensorModel) -> np.ndarray:
    """``hears`` from distances and off-axis angles (any matching shapes)."""
    dist   = np.asarray(dist, dtype=float)
    off    = np.asarray(off, dtype=float)
    arrive = model.burst_lead_ms + dist / 100.0 / model.sound_speed * 1000.0
    with np.errstate(invalid="ignore"):
        ok = arrive >= model.rx_ready_ms                  # earlier: not listening yet
        if model.threshold:
            ratio = 10.0 ** (_strength_db(dist, off, model) / 20.0)
            cycle = detect_cycles(ratio, model.rise_cycles, model.burst_cycles)
            ok   &= cycle > 0
        else:
            cycle = 0
        ok &= arrive + cycle * CYCLE_MS <= model.give_up_ms   # before time-out and end of sampling
        if model.max_range_cm is not None:
            ok &= dist <= model.max_range_cm
        if model.min_range_cm > 0.0:
            ok &= dist >= model.min_range_cm
        if model.fov_half_deg is not None:
            ok &= np.abs((off + 180.0) % 360.0 - 180.0) <= model.fov_half_deg
    return ok
