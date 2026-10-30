"""Measuring where the transmitter is.

``measure_position`` pulses the ground station a number of times with the
transmitter standing still and averages the fixes, for the panels that need a
measured point: the origin tool ("Measure with transmitter" on the physical
(0, 0) mark) and the calibration panel's accuracy check.  ``check_point``
compares such a measurement with where the transmitter really was: the
per-receiver range residuals and the position errors.

No Qt here: the panels run ``measure_position`` on a worker thread
(``windows.widgets.run_in_thread`` with ``pass_cancel`` / ``pass_progress``).
It uses the shared ``UltraGPSPositionLib``, whose filter history it resets
first, so it must not run while the position panel's thread is tracking.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np


@dataclass
class MeasureResult:
    """The averaged outcome of ``measure_position``.

    Means and spreads are over the readings that gave a fix; a spread is the
    sample standard deviation per axis (0 with a single fix).  Per-receiver
    lists are in receiver-id order.
    """

    n_ok: int = 0                     # readings that gave an LM fix
    n_total: int = 0                  # pulses sent
    n_replies: int = 0                # pulses answered with a reading
    lm_mean: tuple[float, float] | None = None
    lm_std: tuple[float, float] | None = None
    n_cep_ok: int = 0                 # readings that gave a valid CEP fix
    cep_mean: tuple[float, float] | None = None
    cep_std: tuple[float, float] | None = None
    distances_mean: list[float] = field(default_factory=list)   # cm; NaN when never sane
    distances_std: list[float] = field(default_factory=list)    # cm; NaN when never sane
    sane_counts: list[int] = field(default_factory=list)        # readings it passed the filter
    ticks_median: list[float] = field(default_factory=list)     # of its non-zero ticks; NaN if none
    lm_fixes: list[tuple[float, float]] = field(default_factory=list)
    cep_fixes: list[tuple[float, float]] = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False
    link_lost: bool = False           # stopped because the ground-station link went down
    elapsed_s: float = 0.0

    @property
    def lm_spread(self) -> float | None:
        """Radial RMS spread of the LM fixes round their mean (cm)."""
        return None if self.lm_std is None else math.hypot(*self.lm_std)

    @property
    def cep_spread(self) -> float | None:
        """Radial RMS spread of the CEP fixes round their mean (cm)."""
        return None if self.cep_std is None else math.hypot(*self.cep_std)

    @property
    def position(self) -> tuple[float, float] | None:
        """The measured position: the LM mean, else the CEP mean, else None."""
        return self.lm_mean if self.lm_mean is not None else self.cep_mean

    @property
    def spread(self) -> float | None:
        """Radial spread of whichever mean ``position`` is."""
        return self.lm_spread if self.lm_mean is not None else self.cep_spread

    def summary(self) -> str:
        """One line for a status label."""
        if self.position is None:
            why = ("cancelled" if self.cancelled else "ground-station link down" if self.link_lost
                   else "timed out" if self.timed_out else "no fix")
            return f"No position ({why}; {self.n_replies}/{self.n_total} replies)"
        x, y = self.position
        return (f"({x:.1f}, {y:.1f}) cm ± {self.spread:.1f} cm from "
                f"{self.n_ok}/{self.n_total} readings")


@dataclass
class PointCheck:
    """A measurement compared with the true position (see ``check_point``)."""

    true_xy: tuple[float, float]
    true_distances: list[float]                 # receiver -> true point, cm
    residuals: list[float]                      # measured - true distance, cm (NaN: never sane)
    rms_residual: float | None                  # over the receivers with a residual
    worst_receiver: int | None                  # index with the largest |residual|
    lm_offset: tuple[float, float] | None       # LM mean - true, cm
    lm_error: float | None                      # |LM mean - true|, cm
    cep_offset: tuple[float, float] | None
    cep_error: float | None

    def summary(self) -> str:
        """One line for a status label (receivers are labelled R1..)."""
        parts = []
        if self.lm_error is not None:
            parts.append(f"position error {self.lm_error:.1f} cm")
        elif self.cep_error is not None:
            parts.append(f"position error {self.cep_error:.1f} cm (CEP)")
        else:
            parts.append("no position fix")
        if self.rms_residual is not None:
            parts.append(f"range RMS {self.rms_residual:.1f} cm")
        if self.worst_receiver is not None:
            r = self.residuals[self.worst_receiver]
            parts.append(f"worst R{self.worst_receiver + 1} {r:+.1f} cm")
        return ", ".join(parts)


def _is_cancelled(cancel) -> bool:
    """``cancel`` may be None, a callable returning bool, or an Event-like object."""
    if cancel is None:
        return False
    if hasattr(cancel, "is_set"):
        return bool(cancel.is_set())
    return bool(cancel())


def _mean_std(points: list) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
    if not points:
        return None, None
    arr = np.asarray(points, dtype=float)
    mean = arr.mean(axis=0)
    std = arr.std(axis=0, ddof=1) if len(arr) > 1 else np.zeros(2)
    return (float(mean[0]), float(mean[1])), (float(std[0]), float(std[1]))


def measure_position(client, position_lib, samples: int = 20, timeout_s: float = 15.0,
                     cancel=None, on_progress=None) -> MeasureResult:
    """Pulse the transmitter until ``samples`` readings have come back (or
    ``timeout_s`` passes, or ``cancel`` says stop) and average the fixes.

    Args:
        client:       A connected ``UltraGPSClient``; ``client.pulse()`` is
                      called repeatedly (a None reply counts as a pulse without
                      a reading).  Its exceptions propagate.  When a pulse
                      fails while the client reports its link down
                      (``connected`` False) the measurement stops at once
                      with ``link_lost`` set and whatever it has so far.
        position_lib: The ``UltraGPSPositionLib``; ``reset_state()`` is called
                      first, then ``get_position_full(ticks)`` per reading.
        samples:      Readings to collect.
        timeout_s:    Give up after this long (``timed_out`` is then set).
        cancel:       None, a callable returning True to stop, or a
                      ``threading.Event`` (``cancelled`` is then set).
        on_progress:  ``on_progress(readings, samples)`` after every pulse.

    A reading counts as an LM (CEP) fix only when the solver succeeded on it,
    never the library's "last good position" fallback.
    """
    if samples < 1:
        raise ValueError("samples must be at least 1")
    n_rx = int(position_lib.receiver_count)
    position_lib.reset_state()

    result = MeasureResult()
    sane = [[] for _ in range(n_rx)]
    ticks_seen = [[] for _ in range(n_rx)]
    start = time.monotonic()
    deadline = start + float(timeout_s)

    while result.n_replies < samples:
        if _is_cancelled(cancel):
            result.cancelled = True
            break
        if time.monotonic() >= deadline:
            result.timed_out = True
            break
        ticks = client.pulse()
        result.n_total += 1
        if _is_cancelled(cancel):             # the pulse in flight when cancelled: keep its
            result.cancelled = True           # reading off the shared position library
            break
        if ticks is None or len(ticks) < n_rx:
            if not getattr(client, "connected", True):
                result.link_lost = True       # pulse() fails at once while the link is down
                break
            time.sleep(0.05)                  # no reading: don't hammer a silent link
        else:
            result.n_replies += 1
            for i in range(n_rx):
                if ticks[i]:
                    ticks_seen[i].append(float(ticks[i]))
            fix = position_lib.get_position_full(ticks)
            distances = fix.get("distances")
            distances = () if distances is None else distances
            sane_indices = fix.get("sane_indices")
            for i in ([] if sane_indices is None else sane_indices):
                i = int(i)
                if 0 <= i < n_rx and i < len(distances):
                    sane[i].append(float(distances[i]))
            if fix.get("lm_success") and fix.get("lm_position") is not None:
                p = fix["lm_position"]
                result.lm_fixes.append((float(p[0]), float(p[1])))
            if fix.get("cep_success") and fix.get("cep_position") is not None:
                p = fix["cep_position"]
                result.cep_fixes.append((float(p[0]), float(p[1])))
        if on_progress is not None:
            on_progress(result.n_replies, samples)

    result.n_ok = len(result.lm_fixes)
    result.n_cep_ok = len(result.cep_fixes)
    result.lm_mean, result.lm_std = _mean_std(result.lm_fixes)
    result.cep_mean, result.cep_std = _mean_std(result.cep_fixes)
    result.sane_counts = [len(d) for d in sane]
    result.distances_mean = [float(np.mean(d)) if d else math.nan for d in sane]
    result.distances_std = [float(np.std(d, ddof=1)) if len(d) > 1 else (0.0 if d else math.nan)
                            for d in sane]
    result.ticks_median = [float(np.median(t)) if t else math.nan for t in ticks_seen]
    result.elapsed_s = time.monotonic() - start
    return result


def check_point(result: MeasureResult, true_xy, coords) -> PointCheck:
    """Compare ``result`` with the true transmitter position ``true_xy``.

    ``coords`` are the receiver positions in id order (an (N, 2) array, e.g.
    ``position_lib.receiver_coords``).  A residual is the receiver's mean
    measured distance minus its true distance (positive: it reads long).
    """
    xy = np.asarray(coords, dtype=float).reshape(-1, 2)
    truth = np.array([float(true_xy[0]), float(true_xy[1])])
    true_d = np.hypot(xy[:, 0] - truth[0], xy[:, 1] - truth[1])
    measured = np.full(len(xy), np.nan)
    m = np.asarray(result.distances_mean, dtype=float)[:len(xy)]
    measured[:len(m)] = m
    residuals = measured - true_d
    finite = np.isfinite(residuals)
    rms = float(np.sqrt(np.mean(residuals[finite] ** 2))) if finite.any() else None
    worst = int(np.nanargmax(np.abs(residuals))) if finite.any() else None

    def offset(mean):
        if mean is None:
            return None, None
        d = (float(mean[0] - truth[0]), float(mean[1] - truth[1]))
        return d, math.hypot(*d)

    lm_offset, lm_error = offset(result.lm_mean)
    cep_offset, cep_error = offset(result.cep_mean)
    return PointCheck(
        true_xy=(float(truth[0]), float(truth[1])),
        true_distances=[float(d) for d in true_d],
        residuals=[float(r) for r in residuals],
        rms_residual=rms,
        worst_receiver=worst,
        lm_offset=lm_offset, lm_error=lm_error,
        cep_offset=cep_offset, cep_error=cep_error)
