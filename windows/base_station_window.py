"""Base station panel.

Shows and changes the settings of the base station (the Arduino that triggers
the receivers and drives the transmitter station over the radio) through the
ground station program (UltraGpsXY), and explains in plain language what each
one does and what it means for range, update rate and calibration:

* trigger rate ``F`` and sampling window ``H``: read whenever the panel is
  shown, changed after a confirmation; the baud rate ``B`` is read-only;
* the burst delay: the ``D`` sweep (about 16 s, during which the ground
  station answers nothing else) and the ``E`` default.  The firmware cannot be
  asked which delay is in force, so the panel shows the last one reported in
  this session, and warns that a new delay shifts every receiver's distances
  (recalibrate);
* reset transmitter ``R`` and reset base-station settings ``M``;
* a test pulse ``P``, a simulated pulse ``S`` and a live view (``C``) with
  each receiver's raw ticks and the distance the saved calibration makes of
  them;
* what the settings imply: update rate, the farthest distance the sampling
  window can time against the size of the arena, a timeline of one trigger
  period, how long a sweep takes.

Every command runs on a worker thread (``widgets.run_in_thread``).  The panel
runs one at a time and disables its command buttons while one runs or while
the link to the ground station is down.  A sweep cannot be interrupted: when
the panel is left during one, it finishes in the background and its result is
shown when the panel is next visible.
"""

from __future__ import annotations

import functools
import logging
import math
import time
from collections import deque

from PyQt6 import sip
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QSpinBox,
    QVBoxLayout, QWidget,
)

from matplotlib.figure import Figure
from matplotlib.transforms import blended_transform_factory

from ultragps_client import (
    CommMode, DEFAULT_BURST_DELAY_US, DEFAULT_SAMPLING_TIME_MS, DEFAULT_TRIGGER_RATE_HZ,
    SAMPLING_TIME_MAX_MS, SAMPLING_TIME_MIN_MS, TRIGGER_RATE_MAX_HZ, TRIGGER_RATE_MIN_HZ,
    max_sampling_time_ms, trigger_top,
)
from ultragps_position import coverage

from windows import ui_scale, widgets
from windows.widgets import AMBER, CYAN, DIM, GOLD, GREEN, MAGENTA, ORANGE, RED

log = logging.getLogger(__name__)

#: Receivers in every reading (R1..R6).
RECEIVER_COUNT = 6

#: Receivers a position needs.
MIN_RECEIVERS_FOR_FIX = 3

#: The base station's factory baud rate ('M' restores it).
DEFAULT_BAUD_RATE = 38400

#: One Timer1 count (µs): the trigger clock's unit, and a tick on the newest firmware.
TIMER1_US = 16

#: Timing model behind "farthest timeable distance": the HC-SR04 datasheet's
#: hard limits, i.e. (min(window, 41 ms) - 4 ms) x speed of sound.  The 4 ms
#: burst lead assumes the default 2 ms burst delay plus radio latency, so it is
#: conservative after a sweep found a shorter delay.
TIMING_MODEL = coverage.DATASHEET

#: The HC-SR04 gives up this long after its trigger (ready time + module
#: timeout, ms): a longer sampling window adds no range.
MODULE_GIVE_UP_MS = TIMING_MODEL.rx_ready_ms + TIMING_MODEL.timeout_ms

#: Speed of sound in cm per microsecond: a burst-delay change of N us moves
#: every arrival by N us, which the calibration turns into N x this many cm.
SOUND_CM_PER_US = TIMING_MODEL.sound_speed / 1e4

#: Spare range (share of the arena size) below which the window counts as tight.
TIGHT_MARGIN = 0.1

#: Font sizes (design px, scaled with the window): explanations, captions /
#: values / buttons, and the numbers of the tick table.
NOTE_PX = 12
TEXT_PX = 13
CELL_PX = 15

#: Housekeeping timer while the panel is shown (link state, busy time), ms.
TICK_MS = 200

#: Live view: wait this long for each reading (s); show at most one reading per
#: interval (s), and the rate every LIVE_IDLE_SHOW_S without readings; measure
#: the rate over LIVE_RATE_SPAN_S; re-send 'C' when nothing arrived for
#: LIVE_RESEND_PERIODS trigger periods (and at least LIVE_RESEND_MIN_S).
LIVE_WAIT_S = 0.2
LIVE_SHOW_EVERY_S = 0.05
LIVE_IDLE_SHOW_S = 0.5
LIVE_RATE_SPAN_S = 2.0
LIVE_RESEND_PERIODS = 5
LIVE_RESEND_MIN_S = 1.0

#: Longest wait for the live-view thread when the panel is hidden (ms).
LIVE_STOP_WAIT_MS = 3000

#: What each of the panel's jobs is called in its messages.
_JOB_NAMES = {
    "refresh": "Reading the settings", "F": "Setting the trigger rate",
    "H": "Setting the sampling window", "D": "The burst-delay sweep",
    "E": "Restoring the default burst delay", "R": "Resetting the transmitter",
    "M": "Resetting the base-station settings", "P": "The pulse", "S": "The simulated pulse",
}

_SETTINGS_NOTE = (
    "Every trigger fires the transmitter once and times all six receivers, so the trigger rate "
    "(F) is how many readings per second the system takes. After each trigger the base station "
    "listens for the sampling window (H): sound that arrives later is lost and that receiver "
    "reads 0. Neither setting affects the calibration.")

_DELAY_NOTE = (
    "The transmitter fires this long after each trigger, so that the receivers are already "
    "listening when its sound arrives. The sweep (D) tries 0 to 500 µs and keeps the delay with "
    "the most readings; E restores the 2000 µs default. The delay is part of every reading: "
    "changing it shifts every receiver's distances, so recalibrate afterwards.")

_PULSE_NOTE = (
    "Pulse (P) fires the transmitter once and shows each receiver's raw tick count (0 = it heard "
    "nothing) and the distance the saved calibration makes of it: ticks × slope + intercept. "
    "Simulated (S) shows the base station's made-up test ticks, to check the link without the "
    "sensors. Live (C) streams one reading per trigger.")

_MAINTENANCE_NOTE = (
    "Reset transmitter (R) reboots the transmitter station over the radio; it misses a few "
    "readings. Try it when every receiver reads 0. Reset base-station settings (M) restores the "
    "factory defaults: 10 Hz, 80 ms window, 2000 µs burst delay, 38400 baud.")

_FIRMWARE_NOTE = (
    "Firmware limits: the burst delay in force cannot be read back (it is known only after D, E "
    "or M in this session), and there is no query for the firmware version or its tick unit. "
    "Ticks are 16 µs Timer1 counts on the newest firmware and loop counts on older firmware: "
    "after flashing firmware with another tick unit every slope is wrong, so recalibrate.")

_SWEEP_DETAILS = (
    "How the sweep picks a delay: for every delay from 0 to 500 µs (10 µs steps) the base station "
    "fires 3 pulses and counts the receivers that heard each one. The delay with the most valid "
    "readings wins; on a tie the one with the lowest mean tick count (the shortest) wins. A 0 µs "
    "winner is stored as 1 µs (zero is reserved by the radio protocol).\n\n"
    "Receivers only start listening a few milliseconds after each trigger, so a delay that is too "
    "short can make a receiver miss a transmitter that is very close to it. With the transmitter "
    "far from every receiver the sweep favours the shortest delay. If readings close to a "
    "receiver drop out afterwards, restore the default (E) or repeat the sweep with the "
    "transmitter nearer a receiver.\n\n"
    "If no step gets a single valid reading, nothing is stored and the delay in force stays.")

# Style-sheet arrows need images, so the spin boxes have none: _stepper puts
# "-" and "+" buttons beside them (the wheel and the arrow keys work as well).
_SPIN_CSS = f"""
    QSpinBox {{ background-color: #222222; color: white; border: 1px solid #444444;
               border-radius: 3px; padding: 3px 6px; font: {TEXT_PX}px Arial; }}
    QSpinBox:focus {{ border-color: {CYAN}; }}
    QSpinBox:disabled {{ background-color: #151515; color: #555555; border-color: #2A2A2A; }}
"""


# ---------------------------------------------------------------------------
# What the settings imply (pure helpers)
# ---------------------------------------------------------------------------

def period_ms(hz: int) -> float:
    """Exact trigger period (ms) at ``hz``, as the firmware's Timer1 runs it."""
    return (trigger_top(hz) + 1) * TIMER1_US / 1000.0


def timing_range_cm(window_ms: float) -> float:
    """Farthest receiver-to-transmitter distance (cm) a sampling window of
    ``window_ms`` can time: ``coverage.DATASHEET.copy(window_ms=...)
    .timing_range_cm``, i.e. (min(window, 41 ms) - 4 ms) x speed of sound;
    0 when the window ends before the transmitter fires."""
    if window_ms is None or window_ms <= 0:
        return 0.0
    return TIMING_MODEL.copy(window_ms=float(window_ms)).timing_range_cm


def arrival_ms(distance_cm: float) -> float:
    """When (ms after the trigger) the transmitter's sound reaches a receiver
    ``distance_cm`` away, in the timing model."""
    return TIMING_MODEL.burst_lead_ms + distance_cm * 10.0 / TIMING_MODEL.sound_speed


def window_needed_ms(distance_cm: float) -> int | None:
    """Shortest whole-millisecond sampling window that times a receiver
    ``distance_cm`` from the transmitter, or None beyond what the HC-SR04 can
    time at all (its own 41 ms limit)."""
    needed = arrival_ms(distance_cm)
    if needed > MODULE_GIVE_UP_MS:
        return None
    return max(SAMPLING_TIME_MIN_MS, math.ceil(needed - 1e-9))


def fastest_rate_hz(window_ms: int) -> int | None:
    """Highest trigger rate whose longest sampling window (80 % of the
    period) is at least ``window_ms``, or None if even 1 Hz is too fast."""
    for hz in range(TRIGGER_RATE_MAX_HZ, TRIGGER_RATE_MIN_HZ - 1, -1):
        if max_sampling_time_ms(hz) >= window_ms:
            return hz
    return None


def _finite_xy(point) -> tuple[float, float] | None:
    """``point`` as finite ``(x, y)`` floats, or None when it is not a pair of
    finite numbers (a receiver not placed yet, or a hand-edited config)."""
    try:
        x, y = float(point[0]), float(point[1])
    except (TypeError, ValueError, IndexError):
        return None
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def arena_extent(receivers, cal_points=()) -> tuple[float, str, str] | None:
    """The farthest the transmitter can be from a receiver in the arena:
    ``(distance_cm, label_a, label_b)`` of the farthest pair of a receiver
    and a receiver or calibration point, or None with fewer than two placed
    receivers (all of them on one spot, as in an uninitialised config, count
    as one).  Positions that are not pairs of finite numbers are skipped.

    The arena lies within the receivers (and the calibration points), and the
    farthest point of such an area from a receiver is one of its corners, so
    these pairs are enough.  ``receivers`` = ``[(id, (x, y))]``; the labels
    are R1.. and P1..
    """
    placed = []
    for rid, position in receivers:
        xy = _finite_xy(position)
        if xy is not None:
            placed.append((f"R{int(rid) + 1}", *xy))
    if len(placed) < 2:
        return None
    targets = list(placed)
    for k, point in enumerate(cal_points):
        xy = _finite_xy(point)
        if xy is not None:
            targets.append((f"P{k + 1}", *xy))
    best = None
    for name_a, ax, ay in placed:
        for name_b, bx, by in targets:
            d = math.hypot(bx - ax, by - ay)
            if best is None or d > best[0] + 1e-9:
                best = (d, name_a, name_b)
    return best if best[0] > 0 else None


def delay_shift_cm(old_us: float, new_us: float) -> float:
    """How far (cm) every calibrated distance moves when the burst delay goes
    from ``old_us`` to ``new_us``: positive = the receivers now read long.

    The burst fires (new - old) us later, every arrival moves by as much, and
    a sound calibration maps time to distance at the speed of sound, whatever
    the tick unit.
    """
    return (new_us - old_us) * SOUND_CM_PER_US


def calibrated_distance(tick: int, offset) -> float | None:
    """``tick * slope + intercept`` with ``offset`` = (slope, intercept), or
    None when there is no echo (0) or no calibration."""
    if not tick or offset is None:
        return None
    slope, intercept = offset
    return tick * slope + intercept


def _cm(value: float) -> str:
    return f"{value:.0f} cm"


def _ms(value: float) -> str:
    return f"{widgets.format_number(value, 1)} ms"


def _now() -> str:
    return time.strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Worker-thread functions (no Qt)
# ---------------------------------------------------------------------------

def _send(client, command, *args):
    """Run one client command (a bound method): ``(result, None)``, or
    ``(None, why it failed)`` when it answered None or False (timeout, ERR
    reply, link down)."""
    result = command(*args)
    if result is None or result is False:
        return None, client.last_error or "no reply from the ground station"
    return result, None


def _read_settings(client):
    """Query the trigger rate, the sampling window and the baud rate (``F 0``,
    ``H 0``, ``B 0``: out-of-range values change nothing on the firmware).

    Returns ``({'F': TriggerRate|None, 'H': SamplingTime|None, 'B': int|None},
    [error, ...])``.  Stops early when the link drops, and after two queries
    in a row without an answer (the base station is not answering), so a
    dead base station costs two timeouts, not three.
    """
    values, errors = {"F": None, "H": None, "B": None}, []
    misses = 0
    for letter, query in (("F", client.trigger_rate), ("H", client.sampling_time),
                          ("B", client.baud_rate)):
        if not client.connected:
            errors.append(f"{letter} not sent: the link to the ground station is down")
            break
        values[letter] = query()
        if values[letter] is not None:
            misses = 0
            continue
        errors.append(f"{letter}: {client.last_error or 'no reply'}")
        misses += 1
        if misses == 2:
            break
    return values, errors


def _live_view(client, generation, cancel, on_progress):
    """Continuous mode ('C') until ``cancel()``: report the latest reading and
    the reading rate as ``on_progress(generation, ticks or None,
    readings_per_s, resends)``, at most every LIVE_SHOW_EVERY_S (every
    LIVE_IDLE_SHOW_S while no reading arrives).

    Returns ``(generation, reason, error)``; reason is "stopped", "link" (the
    link dropped), "interrupted" (another command ended continuous mode) or
    "not_started".
    """
    if not client.continuous():
        return generation, "not_started", client.last_error
    count = client.reading_count
    started = last_arrival = last_resend = time.monotonic()
    arrivals = deque()
    latest = None
    last_shown = 0.0
    resends = 0
    while not cancel():
        if not client.connected:
            return generation, "link", client.last_error
        item = client.wait_for_reading(count, LIVE_WAIT_S)
        now = time.monotonic()
        if item is not None:
            count, latest = item
            last_arrival = now
            arrivals.append(now)
        elif client.mode is not CommMode.CONTINUOUS:
            if not client.connected:
                return generation, "link", client.last_error
            return generation, "interrupted", None
        else:
            # 'C' sent is not 'C' running (the ground station drops commands
            # while its serial link is not ready): re-send it after a silence
            hz = client.trigger_rate_hz or DEFAULT_TRIGGER_RATE_HZ
            silence = max(LIVE_RESEND_MIN_S, LIVE_RESEND_PERIODS / hz)
            if now - last_arrival > silence and now - last_resend > silence:
                last_resend = now
                resends += 1
                if not client.continuous():
                    reason = "link" if not client.connected else "not_started"
                    return generation, reason, client.last_error
        while arrivals and now - arrivals[0] > LIVE_RATE_SPAN_S:
            arrivals.popleft()
        span = min(LIVE_RATE_SPAN_S, now - started)
        rate = len(arrivals) / span if span >= LIVE_IDLE_SHOW_S else 0.0
        due = LIVE_SHOW_EVERY_S if latest is not None else LIVE_IDLE_SHOW_S
        if now - last_shown >= due:
            on_progress(generation, latest, rate, resends)
            latest = None
            last_shown = now
    return generation, "stopped", None


# ---------------------------------------------------------------------------
# Small widget helpers
# ---------------------------------------------------------------------------

def _guarded(method):
    """Wrap a slot so that no exception escapes it (PyQt6 aborts the whole
    process on one): it is logged and shown in the panel's status line.  The
    slot is skipped once the panel's C++ object is gone (a worker finishing
    after the window closed)."""

    @functools.wraps(method)
    def wrapper(self, *args):
        if sip.isdeleted(self):
            return None
        try:
            return method(self, *args)
        except Exception as exc:                  # reported in the panel, never raised
            log.exception("Base station panel: %s failed", method.__name__)
            try:
                self._set_status(f"Unexpected error: {exc}", RED)
            except Exception:                     # the panel is being torn down
                pass
            return None
    return wrapper


def _label_css(color: str, px: int, bold: bool = False) -> str:
    return f"color: {color}; font: {'bold ' if bold else ''}{px}px Arial; background: transparent;"


def _label(text: str = "", color: str = "white", px: int = TEXT_PX, bold: bool = False,
           wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    ui_scale.style(lbl, _label_css(color, px, bold))
    lbl.setWordWrap(wrap)
    return lbl


def _note(text: str = "") -> QLabel:
    """A dim explanatory paragraph."""
    return _label(text, DIM, NOTE_PX, wrap=True)


def _banner(color: str, background: str) -> QLabel:
    """A framed warning paragraph (hidden until it has something to say)."""
    lbl = QLabel()
    lbl.setWordWrap(True)
    ui_scale.style(lbl, f"QLabel {{ color: {color}; background-color: {background}; "
                        f"border: 1px solid {color}; border-radius: 4px; padding: 6px; "
                        f"font: bold {TEXT_PX}px Arial; }}")
    lbl.hide()
    return lbl


def _button(text: str, bg: str, fg: str = "black", tooltip: str | None = None,
            checkable: bool = False):
    return widgets.neon_button(text, bg, fg, font_px=TEXT_PX, padding=(5, 12),
                               checkable=checkable, tooltip=tooltip)


def _spin(low: int, high: int, value: int, suffix: str) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(low, high)
    spin.setValue(value)
    spin.setSuffix(suffix)
    spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
    spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
    ui_scale.style(spin, _SPIN_CSS)
    ui_scale.min_width(spin, 76)
    return spin


def _stepper(spin: QSpinBox) -> QHBoxLayout:
    """``spin`` between a "-" and a "+" button (held down, they repeat)."""
    minus, plus = (widgets.neon_button(text, "#333333", "white", font_px=TEXT_PX + 2,
                                       padding=(2, 9)) for text in ("−", "+"))
    for button, step in ((minus, spin.stepDown), (plus, spin.stepUp)):
        button.setAutoRepeat(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(step)
    row = QHBoxLayout()
    ui_scale.spacing(row, 3)
    row.addWidget(minus)
    row.addWidget(spin)
    row.addWidget(plus)
    return row


# ---------------------------------------------------------------------------
# Timeline of one trigger period
# ---------------------------------------------------------------------------

class TimingDiagram:
    """One trigger period as a timeline: the sampling window (H), when the
    HC-SR04 receivers listen, and when the transmitter's sound reaches
    receivers across the arena, green where it can be timed and red where it
    comes too late.  A matplotlib figure in a ``ScalingCanvas`` (its text
    follows the window size); the bars' labels sit in a column on the right.
    """

    REF_SIZE = (560, 150)                  # canvas size (design px) at factor 1.0
    AXES = (0.015, 0.31, 0.52, 0.64)       # left, bottom, width, height (figure fractions)
    KEY_X = 0.555                          # left edge of the labels (figure fraction)
    FONT_PT = 8.5
    BACKGROUND = "#0D0D0D"                 # the background of a widgets.Section

    def __init__(self):
        self.figure = Figure(facecolor=self.BACKGROUND)
        self.canvas = ui_scale.ScalingCanvas(self.figure, ref_size=self.REF_SIZE)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        ui_scale.fixed_height(self.canvas, self.REF_SIZE[1])
        ui_scale.min_width(self.canvas, 260)
        self.ax = self.figure.add_axes(self.AXES)
        self._shown = ()

    def plot(self, hz: int | None, window_ms: int | None, arena_cm: float | None) -> None:
        """Draw the period for these settings (nothing is redrawn when they
        did not change); ``None`` values are unknown."""
        state = (hz, window_ms, None if arena_cm is None else round(arena_cm, 1))
        if state == self._shown:
            return
        self._shown = state
        ax = self.ax
        ax.clear()
        ax.set_facecolor(self.BACKGROUND)
        for side in ("left", "right", "top"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color("#555555")
        ax.tick_params(axis="x", colors=DIM, labelsize=self.FONT_PT - 1, length=3)
        ax.set_yticks([])
        if window_ms is None:
            ax.set_xticks([])
            ax.spines["bottom"].set_visible(False)
            ax.text(0.5, 0.55, "One trigger period is shown here once the sampling window is read.",
                    transform=self.figure.transFigure, ha="center", va="center", color=DIM,
                    fontsize=self.FONT_PT)
            self.canvas.draw_idle()
            return

        lead, ready = TIMING_MODEL.burst_lead_ms, TIMING_MODEL.rx_ready_ms
        period = period_ms(hz) if hz else None
        far = arrival_ms(arena_cm) if arena_cm is not None else None
        timed_end = min(window_ms, MODULE_GIVE_UP_MS)
        content = max(window_ms, MODULE_GIVE_UP_MS, far or 0.0)
        xmax = period if period is not None and period <= 2.5 * content else 1.12 * content
        key = blended_transform_factory(self.figure.transFigure, ax.transData)

        def bar(y, start, end, color):
            end = min(end, xmax)
            if end > start:
                ax.barh(y, end - start, left=start, height=0.6, color=color, linewidth=0)

        def label(y, text, color):
            ax.text(self.KEY_X, y, text, transform=key, color=color, fontsize=self.FONT_PT,
                    ha="left", va="center")

        bar(2, 0.0, window_ms, CYAN)
        label(2, f"sampling window (H): 0–{window_ms} ms", CYAN)
        bar(1, ready, MODULE_GIVE_UP_MS, MAGENTA)
        label(1, f"HC-SR04 listens: {ready:g}–{MODULE_GIVE_UP_MS:g} ms", MAGENTA)
        if far is None:
            label(0, f"sound leaves at ~{lead:g} ms (no receivers placed)", DIM)
        else:
            bar(0, lead, min(far, timed_end), GREEN)
            if far > timed_end:
                bar(0, max(lead, timed_end), far, RED)
                label(0, f"sound arrives {lead:g}–{far:.1f} ms: late after {timed_end:g} ms",
                      RED)
            else:
                label(0, f"sound arrives {lead:g}–{far:.1f} ms ({arena_cm:.0f} cm)", GREEN)
        if period is not None and period <= xmax:
            ax.axvline(period, color="#888888", linestyle="--", linewidth=1.0)
        ax.set_xlim(0.0, xmax)
        ax.set_ylim(-0.55, 2.55)
        text = "ms after each trigger"
        if period is not None:
            text += f" · next trigger at {_ms(period)}"
        ax.set_xlabel(text, color=DIM, fontsize=self.FONT_PT - 1, labelpad=2)
        self.canvas.draw_idle()


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class BaseStationPanel(QWidget):
    """Full-page panel for the base station's settings, what they mean, and
    its test and maintenance commands (see the module docstring).

    Args:
        client:          The connected ``UltraGPSClient`` shared by the panels.
        settings_module: ``SettingsModule``: receiver positions, calibration
                         offsets and points (read only).
        main_window:     The main window: ``show_panel(name)`` and the
                         ``config_changed`` / ``link_changed(bool)`` signals.
    """

    def __init__(self, client, settings_module, main_window):
        super().__init__()
        self._client = client
        self._settings = settings_module
        self._main_window = main_window

        self._active = False                      # between on_panel_show and on_panel_hide
        self._link_up = bool(client.connected)

        # The one command running (a widgets.Worker), what it is and since when
        self._worker = None
        self._job: str | None = None
        self._job_context: dict = {}
        self._job_started = 0.0
        self._job_expected = 0.0
        self._refresh_pending = False             # read the settings once the command ends
        # A result that arrived while the panel was hidden: (text, colour,
        # tooltip), shown on the next show and kept over the refresh it starts
        self._away_status = None
        self._keep_status = False

        # Live view (continuous mode); the generation tells a stopped view's late signals apart
        self._live = None
        self._live_gen = 0
        # The line under the table once the live view ends: its last reading,
        # or what the line said before it started (no reading came)
        self._live_end_info = ("", DIM)

        # What the base station reported; the client tracks rate, window and delay
        self._baud: int | None = None
        self._read_at: str | None = None
        self._delay_source = ""                   # how client.burst_delay_us became known
        self._sweep = None                        # (DelaySearchResult, "HH:MM:SS")
        self._recal_offsets = None                # offsets when the recalibration warning went up

        # The reading shown in the table
        self._ticks: list[int] | None = None
        self._ticks_info = ("", DIM)

        # From the settings
        self._offsets: list = [None] * RECEIVER_COUNT
        self._extent = None
        self._sanity_top: float | None = None

        self._label_styles: dict[int, tuple] = {}  # id(label) -> its current style
        self._build_ui()

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(TICK_MS)
        self._tick_timer.timeout.connect(self._on_tick)

        main_window.config_changed.connect(self._on_config_changed)
        main_window.link_changed.connect(self._on_link_changed)

        self._load_settings()
        self._sync_spins()
        self._render()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @_guarded
    def on_panel_show(self) -> None:
        """Read the settings from the base station (unless a command still
        runs: then once it ends) and follow the link state."""
        self._active = True
        self._keep_status = False                 # a new visit: only its own away note is kept
        self._load_settings()
        self._set_link(bool(self._client.connected))
        if not self._link_up:
            self._announce_link_down()
        self._tick_timer.start()
        if self._away_status is not None:
            self._set_status(*self._away_status)
            self._away_status = None
            self._keep_status = True
        if self._worker is not None:
            self._refresh_pending = self._job != "refresh"
        elif self._link_up:
            self.refresh()
        self._render()
        self._update_progress()

    @_guarded
    def on_panel_hide(self) -> None:
        """Stop the live view and the timers.  A command that is running (a
        delay sweep cannot be interrupted) finishes in the background; its
        result is shown when the panel is next visible."""
        self._active = False
        self._tick_timer.stop()
        self._stop_live(wait=True)
        self._update_controls()

    def refresh(self) -> bool:
        """Read the trigger rate, sampling window and baud rate from the base
        station on a worker thread.  False (nothing sent) while a command or
        the live view runs, or while the link is down."""
        started = self._start_job("refresh", _read_settings,
                                  text="Reading the base-station settings…")
        if started:
            self._refresh_pending = False
        return started

    def is_busy(self) -> bool:
        """True while one of the panel's commands or its live view runs."""
        return self._worker is not None or self._live is not None

    def delay_search_running(self) -> bool:
        """True while a burst-delay sweep ('D') started here runs; the ground
        station answers no other command meanwhile."""
        return self._job == "D"

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setObjectName("baseStationPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        ui_scale.style(self, "QWidget#baseStationPanel { background-color: black; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_top_bar())
        root.addWidget(self._build_status_row())

        body = QWidget()
        body.setObjectName("baseStationBody")
        ui_scale.style(body, "QWidget#baseStationBody { background-color: black; }")
        columns = QHBoxLayout(body)
        ui_scale.margins(columns, 12, 6, 12, 12)
        ui_scale.spacing(columns, 14)

        left = QVBoxLayout()
        ui_scale.spacing(left, 12)
        left.addWidget(self._build_settings_section())
        left.addWidget(self._build_implications_section())
        left.addWidget(_note(_FIRMWARE_NOTE))
        left.addStretch(1)

        right = QVBoxLayout()
        ui_scale.spacing(right, 12)
        right.addWidget(self._build_delay_section())
        right.addWidget(self._build_pulse_section())
        right.addWidget(self._build_maintenance_section())
        right.addStretch(1)

        columns.addLayout(left, 1)
        columns.addLayout(right, 1)
        root.addWidget(widgets.scroll_area(body), 1)

        self._command_buttons = [
            self._refresh_btn, self._rate_btn, self._window_btn, self._sweep_btn,
            self._default_delay_btn, self._pulse_btn, self._simulate_btn,
            self._reset_tx_btn, self._reset_settings_btn,
        ]

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        ui_scale.margins(layout, 10, 5, 10, 5)
        ui_scale.spacing(layout, 12)
        title = QLabel("Base Station")
        ui_scale.style(title, f"color: {GOLD}; background: transparent;")
        ui_scale.font(title, 16, bold=True)
        layout.addWidget(title)
        layout.addWidget(_label("settings, tests and maintenance of the receivers' base station",
                                DIM, NOTE_PX))
        layout.addStretch(1)
        back = _button("← Back", GREEN)
        back.clicked.connect(self._on_back)
        layout.addWidget(back)
        return bar

    def _build_status_row(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        ui_scale.margins(layout, 12, 0, 12, 2)
        ui_scale.spacing(layout, 10)
        self._status = _label("", DIM, TEXT_PX, bold=True, wrap=True)
        self._status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._status, 1)
        self._progress = widgets.progress_bar(GOLD)
        ui_scale.fixed_width(self._progress, 240)
        policy = self._progress.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self._progress.setSizePolicy(policy)
        self._progress.hide()
        layout.addWidget(self._progress, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    @staticmethod
    def _value_grid(captions) -> tuple[QGridLayout, list[QLabel]]:
        """A two-column grid of dim captions and bold white values."""
        grid = QGridLayout()
        ui_scale.spacing(grid, 6)
        values = []
        for r, caption in enumerate(captions):
            grid.addWidget(_label(caption, DIM), r, 0, Qt.AlignmentFlag.AlignTop)
            value = _label("", "white", bold=True, wrap=True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(value, r, 1)
            values.append(value)
        grid.setColumnStretch(1, 1)
        return grid, values

    def _build_settings_section(self) -> QWidget:
        sec = widgets.Section("Trigger rate & sampling window", CYAN)
        sec.add(_note(_SETTINGS_NOTE))
        grid, (self._rate_value, self._window_value, self._baud_value) = self._value_grid(
            ("Trigger rate (F)", "Sampling window (H)", "Baud rate (B)"))
        self._baud_value.setToolTip(
            "Serial speed between the base station and the ground-station computer. Changing it "
            "reboots the base station and the ground station has to follow, so Control does not "
            "offer it; use the ground station's console (B <baud>) if you must.")
        sec.add_layout(grid)

        row = QHBoxLayout()
        self._read_at_label = _label("", DIM, NOTE_PX)
        row.addWidget(self._read_at_label, 1)
        self._refresh_btn = _button(
            "Refresh", CYAN,
            tooltip="Read the settings from the base station again (F 0, H 0, B 0).")
        self._refresh_btn.clicked.connect(self._on_refresh)
        row.addWidget(self._refresh_btn)
        sec.add_layout(row)

        grid = QGridLayout()
        ui_scale.spacing(grid, 8)
        self._rate_spin = _spin(TRIGGER_RATE_MIN_HZ, TRIGGER_RATE_MAX_HZ, DEFAULT_TRIGGER_RATE_HZ,
                                " Hz")
        self._rate_spin.valueChanged.connect(self._on_spin_changed)
        self._rate_btn = _button("Set rate…", CYAN,
                                 tooltip="F <Hz> on both stations: the transmitter reboots when "
                                         "the rate changes, and the sampling window goes to its "
                                         "maximum.")
        self._rate_btn.clicked.connect(self._on_set_rate)
        self._window_spin = _spin(SAMPLING_TIME_MIN_MS,
                                  max_sampling_time_ms(DEFAULT_TRIGGER_RATE_HZ),
                                  DEFAULT_SAMPLING_TIME_MS, " ms")
        self._window_spin.valueChanged.connect(self._on_spin_changed)
        self._window_btn = _button("Set window…", CYAN,
                                   tooltip="H <ms>: 1 ms up to 80 % of the trigger period.")
        self._window_btn.clicked.connect(self._on_set_window)
        self._window_max_btn = _button("Max", "#333333", "white",
                                       tooltip="Put the longest window the trigger rate in force "
                                               "allows in the box (nothing is sent).")
        self._window_max_btn.clicked.connect(self._on_window_max)
        self._rate_preview = _note()
        self._window_preview = _note()
        for r, (caption, spin, buttons, preview) in enumerate((
                ("New trigger rate", self._rate_spin, (self._rate_btn,), self._rate_preview),
                ("New sampling window", self._window_spin,
                 (self._window_btn, self._window_max_btn), self._window_preview))):
            grid.addWidget(_label(caption, DIM), 2 * r, 0)
            grid.addLayout(_stepper(spin), 2 * r, 1)
            for c, button in enumerate(buttons):
                grid.addWidget(button, 2 * r, 2 + c)
            grid.addWidget(preview, 2 * r + 1, 0, 1, 5)
        grid.setColumnStretch(4, 1)
        sec.add_layout(grid)
        return sec

    def _build_implications_section(self) -> QWidget:
        sec = widgets.Section("What these settings mean", GREEN)
        grid, (self._update_value, self._range_value, self._arena_value,
               self._sweep_time_value) = self._value_grid(
            ("Update rate", "Farthest timeable distance", "Your arena", "Delay sweep takes"))
        self._range_value.setToolTip(
            "A receiver can only be timed if the transmitter's sound reaches it before the base "
            "station stops sampling: (window − about 4 ms until the transmitter fires) × speed of "
            "sound, and never beyond 41 ms after the trigger, when the HC-SR04 itself gives up "
            "(the coverage model's datasheet timing). This is the timing limit only: the signal "
            "itself reaches about 7.5 m on a receiver's axis (simulator estimate), 4 m by the "
            "HC-SR04 datasheet's echo-ranging figures.")
        self._arena_value.setToolTip(
            "The farthest the transmitter can be from a receiver inside the arena: the longest "
            "distance between a receiver and another receiver or a calibration point.")
        sec.add_layout(grid)
        self._range_warning = _banner(RED, "#1A0000")
        sec.add(self._range_warning)
        self._timing = TimingDiagram()
        self._timing.canvas.setToolTip(
            "One trigger period: the base station samples for the window (cyan); each HC-SR04 "
            "listens from about 3 ms to 41 ms after its trigger (magenta); the transmitter fires "
            "about 4 ms after the trigger and its sound reaches the receivers across your arena "
            "in the green span. Sound that comes after sampling stops, or after the HC-SR04 gives "
            "up, is lost (red).")
        sec.add(self._timing.canvas)
        return sec

    def _build_delay_section(self) -> QWidget:
        sec = widgets.Section("Burst delay", GOLD)
        sec.add(_note(_DELAY_NOTE))
        grid, (self._delay_value, self._sweep_value) = self._value_grid(
            ("Delay in force", "Last sweep"))
        sec.add_layout(grid)

        row = QHBoxLayout()
        ui_scale.spacing(row, 8)
        self._sweep_btn = _button("Run delay sweep (D)…", GOLD,
                                  tooltip="Try 0 to 500 µs and store the best delay; about 16 s "
                                          "at 10 Hz.")
        self._sweep_btn.clicked.connect(self._on_delay_search)
        row.addWidget(self._sweep_btn)
        self._default_delay_btn = _button(f"Restore {DEFAULT_BURST_DELAY_US} µs (E)…", GOLD,
                                          tooltip="Store the default burst delay again (E).")
        self._default_delay_btn.clicked.connect(self._on_default_delay)
        row.addWidget(self._default_delay_btn)
        row.addStretch(1)
        sec.add_layout(row)

        self._sweep_running = _label("", GOLD, bold=True, wrap=True)
        self._sweep_running.hide()
        sec.add(self._sweep_running)

        self._recal_box = QFrame()
        self._recal_box.setObjectName("recalBox")
        ui_scale.style(self._recal_box, f"QFrame#recalBox {{ background-color: #1A1400; "
                                        f"border: 1px solid {GOLD}; border-radius: 4px; }}")
        box = QVBoxLayout(self._recal_box)
        ui_scale.margins(box, 8, 6, 8, 6)
        ui_scale.spacing(box, 6)
        self._recal_text = _label("", GOLD, bold=True, wrap=True)
        box.addWidget(self._recal_text)
        row = QHBoxLayout()
        ui_scale.spacing(row, 8)
        self._recal_btn = _button("Recalibrate…", MAGENTA, "white",
                                  tooltip="Open the calibration panel.")
        self._recal_btn.clicked.connect(self._on_recalibrate)
        row.addWidget(self._recal_btn)
        self._dismiss_btn = _button("Dismiss", "#555555", "white")
        self._dismiss_btn.clicked.connect(self._on_dismiss_recal)
        row.addWidget(self._dismiss_btn)
        row.addStretch(1)
        box.addLayout(row)
        self._recal_box.hide()
        sec.add(self._recal_box)
        return sec

    def _build_pulse_section(self) -> QWidget:
        sec = widgets.Section("Test pulse", MAGENTA)
        sec.add(_note(_PULSE_NOTE))
        row = QHBoxLayout()
        ui_scale.spacing(row, 8)
        self._pulse_btn = _button("Pulse (P)", MAGENTA, "white",
                                  tooltip="Fire one real pulse and show the ticks.")
        self._pulse_btn.clicked.connect(self._on_pulse)
        row.addWidget(self._pulse_btn)
        self._simulate_btn = _button("Simulated (S)", CYAN,
                                     tooltip="The base station reports made-up ticks (351 to 450).")
        self._simulate_btn.clicked.connect(self._on_simulate)
        row.addWidget(self._simulate_btn)
        self._live_btn = _button("Live (C)", GREEN, checkable=True,
                                 tooltip="Continuous mode: a reading every trigger, with the "
                                         "reading rate.")
        self._live_btn.clicked.connect(self._on_live_clicked)
        row.addWidget(self._live_btn)
        row.addStretch(1)
        sec.add_layout(row)

        table = QFrame()
        table.setObjectName("tickTable")
        ui_scale.style(table, "QFrame#tickTable { background-color: #111111; "
                              "border: 1px solid #333333; border-radius: 4px; }")
        grid = QGridLayout(table)
        ui_scale.margins(grid, 10, 6, 10, 6)
        ui_scale.spacing(grid, 6)
        for i in range(RECEIVER_COUNT):
            head = _label(f"R{i + 1}", MAGENTA, bold=True)
            head.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(head, 0, i + 1)
        caption = _label("Raw ticks", DIM, NOTE_PX)
        caption.setToolTip("Timer1 counts from the trigger to the moment the receiver heard the "
                           "transmitter (16 µs each on the newest firmware); 0 = no echo.")
        grid.addWidget(caption, 1, 0)
        caption = _label("Distance (cm)", DIM, NOTE_PX)
        caption.setToolTip("ticks × slope + intercept with the receiver's saved calibration.")
        grid.addWidget(caption, 2, 0)
        self._tick_cells, self._dist_cells = [], []
        for i in range(RECEIVER_COUNT):
            for row_index, cells in ((1, self._tick_cells), (2, self._dist_cells)):
                cell = QLabel("—")
                cell.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                ui_scale.min_width(cell, 58)
                grid.addWidget(cell, row_index, i + 1)
                cells.append(cell)
        grid.setColumnStretch(0, 1)
        sec.add(table)
        self._reading_info = _note()
        sec.add(self._reading_info)
        return sec

    def _build_maintenance_section(self) -> QWidget:
        sec = widgets.Section("Maintenance", ORANGE)
        sec.add(_note(_MAINTENANCE_NOTE))
        row = QHBoxLayout()
        ui_scale.spacing(row, 8)
        self._reset_tx_btn = _button("Reset transmitter (R)…", ORANGE)
        self._reset_tx_btn.clicked.connect(self._on_reset_transmitter)
        row.addWidget(self._reset_tx_btn)
        self._reset_settings_btn = _button("Reset base-station settings (M)…", RED, "white")
        self._reset_settings_btn.clicked.connect(self._on_reset_settings)
        row.addWidget(self._reset_settings_btn)
        row.addStretch(1)
        sec.add_layout(row)
        return sec

    # ------------------------------------------------------------------
    # Settings (receivers, offsets, calibration points) and base-station state
    # ------------------------------------------------------------------

    def _load_settings(self) -> None:
        """Read what the panel needs from the settings: each receiver's
        offset, the arena's extent and the positioning's distance limit."""
        offsets = []
        for rid in range(RECEIVER_COUNT):
            try:
                off = self._settings.get_receiver_offset(rid)
                slope, intercept = float(off["slope"]), float(off["intercept"])
                offsets.append((slope, intercept)
                               if math.isfinite(slope) and math.isfinite(intercept) else None)
            except (TypeError, KeyError, ValueError):
                offsets.append(None)
        self._offsets = offsets

        try:
            receivers = self._settings.get_tower_coordinates()
        except (TypeError, ValueError):
            receivers = []
        try:
            cal_points = self._settings.cal_points
        except (TypeError, ValueError):            # a hand-edited, malformed point
            cal_points = []
        try:
            self._extent = arena_extent(receivers, cal_points)
            spread = arena_extent(receivers)
        except (TypeError, ValueError, IndexError):
            self._extent = spread = None

        # The positioning drops a distance above its top band: max_distance,
        # by default the largest receiver-to-receiver distance
        lib = getattr(self._main_window, "position_lib", None)
        top = getattr(lib, "max_distance", None)
        self._sanity_top = float(top) if top is not None else (spread[0] if spread else None)

    def _rate_hz(self) -> int | None:
        """Trigger rate last reported by the base station, if any."""
        hz = self._client.trigger_rate_hz
        return hz if isinstance(hz, int) and TRIGGER_RATE_MIN_HZ <= hz <= TRIGGER_RATE_MAX_HZ \
            else None

    def _window_ms(self) -> int | None:
        """Sampling window last reported by the base station, if any."""
        ms = self._client.sampling_time_ms
        return ms if isinstance(ms, int) and ms >= SAMPLING_TIME_MIN_MS else None

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self) -> None:
        self._render_settings()
        self._render_previews()
        self._render_implications()
        self._render_delay()
        self._render_ticks()
        self._update_controls()

    def _paint(self, label: QLabel, text: str, color: str, px: int = TEXT_PX,
               bold: bool = False) -> None:
        """Set ``label``'s text and colour; restyled only when the style changes
        (the tick table is repainted at the live view's rate)."""
        label.setText(text)
        style = (color, px, bold)
        if self._label_styles.get(id(label)) != style:
            self._label_styles[id(label)] = style
            ui_scale.style(label, _label_css(color, px, bold))

    def _render_settings(self) -> None:
        hz, ms = self._rate_hz(), self._window_ms()
        if hz:
            self._rate_value.setText(f"{hz} Hz · a reading every {_ms(period_ms(hz))}")
        else:
            self._rate_value.setText("unknown (not read yet)")
        if ms:
            text = f"{ms} ms ({ms * 1000 // TIMER1_US} Timer1 counts)"
            if hz:
                text += f" · at most {max_sampling_time_ms(hz)} ms at {hz} Hz"
            self._window_value.setText(text)
        else:
            self._window_value.setText("unknown (not read yet)")
        if self._baud:
            self._baud_value.setText(f"{self._baud} baud · read only")
        else:
            self._baud_value.setText("unknown (not read yet) · read only")
        self._read_at_label.setText(f"Read from the base station at {self._read_at}"
                                    if self._read_at else "Not read from the base station yet")

    def _render_previews(self) -> None:
        """What the values in the spin boxes would do (nothing is sent)."""
        arena = self._extent[0] if self._extent else None

        hz = self._rate_spin.value()
        longest = max_sampling_time_ms(hz)
        reach = timing_range_cm(longest)
        text = (f"At {hz} Hz: a reading every {_ms(period_ms(hz))}; the sampling window goes to "
                f"its {longest} ms maximum, which times receivers up to {_cm(reach)} away.")
        short = arena is not None and reach < arena
        if short:
            text += f" Too short for your arena ({_cm(arena)})."
        self._paint(self._rate_preview, text, AMBER if short else DIM, NOTE_PX)

        ms = self._window_spin.value()
        reach = timing_range_cm(ms)
        if reach <= 0:
            text = (f"At {ms} ms nothing can be timed: sampling stops before the transmitter fires "
                    f"(about {TIMING_MODEL.burst_lead_ms:g} ms after each trigger).")
            short = True
        else:
            text = f"At {ms} ms: receivers up to {_cm(reach)} from the transmitter can be timed."
            if ms > MODULE_GIVE_UP_MS:
                text += (f" The HC-SR04 gives up {MODULE_GIVE_UP_MS:g} ms after its trigger, so a "
                         f"longer window adds no range.")
            short = arena is not None and reach < arena
            if short:
                text += f" Too short for your arena ({_cm(arena)})."
        self._paint(self._window_preview, text, AMBER if short else DIM, NOTE_PX)

    def _render_implications(self) -> None:
        hz, ms = self._rate_hz(), self._window_ms()
        if hz:
            self._update_value.setText(f"{hz} readings per second (one every {_ms(period_ms(hz))})")
        else:
            self._update_value.setText("unknown until the trigger rate is read")

        reach = timing_range_cm(ms) if ms else None
        if reach is None:
            self._range_value.setText("unknown until the sampling window is read")
        elif reach <= 0:
            self._range_value.setText(
                f"nothing: a {ms} ms window ends before the transmitter fires")
        elif ms >= MODULE_GIVE_UP_MS:
            self._range_value.setText(
                f"{_cm(reach)} · set by the HC-SR04, which gives up {MODULE_GIVE_UP_MS:g} ms after "
                f"its trigger: a window over {MODULE_GIVE_UP_MS:g} ms adds no range")
        else:
            self._range_value.setText(
                f"{_cm(reach)} · set by the {ms} ms window (sound arriving later is lost)")

        warning = ""
        if self._extent is None:
            self._paint(self._arena_value, "unknown (fewer than two receivers placed)", "white",
                        bold=True)
        else:
            arena, a, b = self._extent
            text, color = f"{_cm(arena)} from {a} to {b}", "white"
            if reach is not None:
                spare = reach - arena
                if spare < 0:
                    text += " · too far for the sampling window"
                    color = RED
                    warning = self._too_short_text(reach, arena, a, b, hz)
                elif spare < TIGHT_MARGIN * arena:
                    text += f" · fits, but only {_cm(spare)} to spare"
                    color = AMBER
                else:
                    text += f" · fits, {_cm(spare)} to spare"
                    color = GREEN
            self._paint(self._arena_value, text, color, bold=True)
        self._range_warning.setText(warning)
        self._range_warning.setVisible(bool(warning))

        self._sweep_time_value.setText(
            f"about {self._client.expected_delay_search_s:.0f} s at these settings (51 steps × 3 "
            f"pulses); the ground station answers nothing else meanwhile")
        self._timing.plot(hz, ms, self._extent[0] if self._extent else None)

    @staticmethod
    def _too_short_text(reach: float, arena: float, a: str, b: str, hz: int | None) -> str:
        text = (f"The sampling window is too short for this arena: a receiver more than "
                f"{_cm(reach)} from the transmitter hears it after sampling stops and reads 0 (no "
                f"echo), but {a} and {b} are {_cm(arena)} apart. ")
        needed = window_needed_ms(arena)
        if needed is None:
            return text + ("The arena is wider than the HC-SR04 can time at all "
                           f"({_cm(timing_range_cm(MODULE_GIVE_UP_MS))}).")
        if hz and needed <= max_sampling_time_ms(hz):
            return text + f"Set the window to at least {needed} ms."
        return text + (f"That needs a window of at least {needed} ms: lower the trigger rate to "
                       f"{fastest_rate_hz(needed)} Hz or less (F), which also sets the longest "
                       f"window.")

    def _render_delay(self) -> None:
        delay = self._client.burst_delay_us
        if delay is None:
            self._delay_value.setText(
                f"unknown · the firmware cannot report it; it is known after D, E or M in this "
                f"session (factory default {DEFAULT_BURST_DELAY_US} µs)")
        else:
            text = f"{delay} µs"
            if delay == DEFAULT_BURST_DELAY_US:
                text += " (the default)"
            if self._delay_source:
                text += f" · {self._delay_source}"
            self._delay_value.setText(text)

        if self._sweep is None:
            self._sweep_value.setText("not run in this session")
        else:
            result, when = self._sweep
            text = f"{result.delay_us} µs"
            if result.delay_us == 0 and result.stored:
                text += " (sent and stored as 1 µs)"
            text += f" · {result.valid}/{result.total} readings valid"
            if result.valid:
                text += f", mean {result.mean_ticks} ticks"
            text += f" · {'stored' if result.stored else 'nothing stored'} at {when}"
            self._sweep_value.setText(text)

    def _render_ticks(self) -> None:
        ticks = self._ticks
        for i in range(RECEIVER_COUNT):
            tick_cell, dist_cell = self._tick_cells[i], self._dist_cells[i]
            if ticks is None:
                self._paint(tick_cell, "—", DIM, CELL_PX, bold=True)
                self._paint(dist_cell, "—", DIM, CELL_PX, bold=True)
                dist_cell.setToolTip("")
                continue
            tick = ticks[i]
            self._paint(tick_cell, str(tick), "white" if tick else DIM, CELL_PX, bold=True)
            if tick == 0:
                self._paint(dist_cell, "no echo", DIM, CELL_PX, bold=True)
                dist_cell.setToolTip(f"R{i + 1} did not hear the transmitter.")
                continue
            if self._offsets[i] is None:
                self._paint(dist_cell, "no cal.", AMBER, CELL_PX, bold=True)
                dist_cell.setToolTip(f"R{i + 1} has no slope and intercept in the settings.")
                continue
            d = calibrated_distance(tick, self._offsets[i])
            slope, intercept = self._offsets[i]
            tip = f"{tick} × {slope:.4g} + {intercept:.4g} = {d:.1f} cm"
            outside = d <= 0 or (self._sanity_top is not None and d > self._sanity_top)
            if outside:
                top = f"{self._sanity_top:.0f}" if self._sanity_top is not None else "?"
                tip += (f"\nOutside 0 to {top} cm: positioning ignores this reading. A burst-delay "
                        f"change since the calibration, a reflection or a bad calibration can "
                        f"cause it.")
            self._paint(dist_cell, f"{d:.1f}", AMBER if outside else "white", CELL_PX, bold=True)
            dist_cell.setToolTip(tip)
        text, color = self._ticks_info
        if ticks is None and not text:
            text = "No reading yet: press Pulse (P)."
        self._paint(self._reading_info, text, color, NOTE_PX)

    def _set_status(self, text: str, color: str = DIM, tooltip: str | None = None) -> None:
        self._paint(self._status, text, color, TEXT_PX, bold=True)
        self._status.setToolTip(tooltip or "")

    def _sync_spins(self) -> None:
        """Put the spin boxes on the settings in force (after they were read
        or changed)."""
        hz, ms = self._rate_hz(), self._window_ms()
        self._window_spin.setMaximum(max_sampling_time_ms(hz) if hz else SAMPLING_TIME_MAX_MS)
        if hz:
            self._rate_spin.setValue(hz)
        if ms:
            self._window_spin.setValue(min(ms, self._window_spin.maximum()))

    # ------------------------------------------------------------------
    # Busy state, link state and controls
    # ------------------------------------------------------------------

    def _update_controls(self) -> None:
        idle = self._worker is None
        live = self._live is not None
        can_send = self._link_up and idle and not live
        for button in self._command_buttons:
            button.setEnabled(can_send)
        self._live_btn.setEnabled(self._link_up and idle)
        self._live_btn.setChecked(live)
        self._live_btn.setText("Stop live (C)" if live else "Live (C)")

    def _set_link(self, up: bool, announce: bool = True) -> None:
        """Follow the link state; ``announce`` False keeps the status line (a
        command that failed with the link has already said why)."""
        was = self._link_up
        self._link_up = up
        if was and not up and announce:
            self._announce_link_down()
        elif up and not was:
            self._keep_status = False
            self._set_status("The link to the ground station is back.", GREEN)
            if self._active and self._worker is None and self._live is None:
                self.refresh()
            else:
                self._refresh_pending = True
        self._update_controls()

    def _announce_link_down(self) -> None:
        self._keep_status = False
        self._set_status("The link to the ground station is down: the commands are unavailable "
                         "until it is back.", RED, self._client.last_error)

    def _start_job(self, job: str, fn, *args, context=None, text: str = "",
                   expected_s: float = 0.0) -> bool:
        """Run ``fn(client, *args)`` (one of the worker functions) on a worker
        thread; its ``(value, error)`` comes back in ``_on_job_result``."""
        if self._worker is not None or self._live is not None or not self._link_up:
            return False
        self._job = job
        self._job_context = dict(context or {})
        self._job_started = time.monotonic()
        self._job_expected = float(expected_s)
        self._away_status = None
        if job != "refresh":                      # the user asked for something new
            self._keep_status = False
        self._worker = widgets.run_in_thread(fn, self._client, *args,
                                             on_result=self._on_job_result,
                                             on_error=self._on_job_error)
        if text and not self._keep_status:
            self._set_status(text, DIM)
        self._update_controls()
        self._update_progress()
        return True

    def _start_command(self, job: str, command, *args, context=None, text: str = "",
                       expected_s: float = 0.0) -> bool:
        """``_start_job`` for one client command (a bound method) the user
        asked for; says so when it cannot be sent (the link dropped or another
        command started while a confirmation was open)."""
        if self._start_job(job, _send, command, *args, context=context, text=text,
                           expected_s=expected_s):
            return True
        self._refuse(_JOB_NAMES[job])
        return False

    def _refuse(self, what: str) -> None:
        why = ("the link to the ground station is down" if not self._link_up
               else "another command or the live view is running")
        self._keep_status = False
        self._set_status(f"{what}: not sent, {why}.", RED)

    def _update_progress(self) -> None:
        if self._worker is None:
            self._progress.hide()
            self._sweep_running.hide()
            return
        elapsed = time.monotonic() - self._job_started
        if self._job == "D" and self._job_expected > 0:
            expected = self._job_expected
            if elapsed < expected:
                self._progress.setRange(0, 1000)
                self._progress.setValue(int(1000 * elapsed / expected))
                late = ""
            else:
                self._progress.setRange(0, 0)
                late = " It is taking longer than expected."
            self._progress.setToolTip(f"Sweep: {elapsed:.0f} s of about {expected:.0f} s")
            self._sweep_running.setText(
                f"Sweep running: {elapsed:.0f} s of about {expected:.0f} s. The ground station "
                f"answers nothing else until it ends, and it cannot be interrupted.{late}")
            self._sweep_running.show()
        else:
            self._progress.setRange(0, 0)
            self._progress.setToolTip("Waiting for the base station's answer")
            self._sweep_running.hide()
        self._progress.show()

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    @_guarded
    def _on_back(self, *_args) -> None:
        self._main_window.show_panel("main")

    @_guarded
    def _on_recalibrate(self, *_args) -> None:
        self._main_window.show_panel("calibration")

    @_guarded
    def _on_dismiss_recal(self, *_args) -> None:
        self._recal_offsets = None
        self._recal_box.hide()

    @_guarded
    def _on_refresh(self, *_args) -> None:
        self._keep_status = False
        self.refresh()

    @_guarded
    def _on_spin_changed(self, *_args) -> None:
        self._render_previews()

    @_guarded
    def _on_window_max(self, *_args) -> None:
        self._window_spin.setValue(self._window_spin.maximum())

    @_guarded
    def _on_set_rate(self, *_args) -> None:
        hz = self._rate_spin.value()
        current = self._rate_hz()
        longest = max_sampling_time_ms(hz)
        reach = timing_range_cm(longest)
        if hz == current:                         # the transmitter ignores a rate it already has
            text = (f"The trigger rate stays {hz} Hz and the transmitter keeps running, but the "
                    f"sampling window goes back to its maximum, {longest} ms, which times "
                    f"receivers up to {_cm(reach)} away.")
        else:
            # The rate in force is unknown when it could not be read
            reboots = "reboots" if current else "reboots (unless it already runs at that rate)"
            text = (f"Both stations switch to {hz} triggers per second: a reading every "
                    f"{_ms(period_ms(hz))}. The transmitter station stores the new rate and "
                    f"{reboots}, so it misses a few readings.\n\n"
                    f"The sampling window is set to its maximum at {hz} Hz, {longest} ms, which "
                    f"times receivers up to {_cm(reach)} away.")
        text += " The calibration stays valid."
        if self._extent is not None and reach < self._extent[0]:
            arena, a, b = self._extent
            text += (f"\n\nWarning: your arena needs {_cm(arena)} ({a} to {b}): receivers farther "
                     f"from the transmitter than {_cm(reach)} will read 0 (no echo).")
        if hz == current:
            title = f"Set {hz} Hz again?"
        elif current:
            title = f"Change the trigger rate from {current} Hz to {hz} Hz?"
        else:
            title = f"Set the trigger rate to {hz} Hz?"
        if not widgets.confirm(self, title, text, ok_text=f"Set {hz} Hz"):
            return
        self._start_command("F", self._client.trigger_rate, hz, context={"hz": hz, "old": current},
                            text=f"Setting the trigger rate to {hz} Hz…")

    @_guarded
    def _on_set_window(self, *_args) -> None:
        ms = self._window_spin.value()
        current = self._window_ms()
        reach = timing_range_cm(ms)
        if reach <= 0:
            text = (f"With a {ms} ms window sampling stops before the transmitter fires (about "
                    f"{TIMING_MODEL.burst_lead_ms:g} ms after each trigger): every receiver will "
                    f"read 0 (no echo).")
        else:
            text = (f"After each trigger the base station will listen for {ms} ms: a receiver "
                    f"more than {_cm(reach)} from the transmitter hears it too late and reads 0 "
                    f"(no echo).")
        if self._extent is not None:
            arena, a, b = self._extent
            needed = window_needed_ms(arena)
            if reach >= arena:
                text += f"\n\nYour arena needs {_cm(arena)} ({a} to {b}): that fits."
            elif needed is None:
                text += (f"\n\nWarning: your arena needs {_cm(arena)} ({a} to {b}), more than the "
                         f"HC-SR04 can time at all.")
            else:
                text += (f"\n\nWarning: your arena needs {_cm(arena)} ({a} to {b}): use at least "
                         f"{needed} ms.")
        text += " The calibration stays valid."
        title = (f"Change the sampling window from {current} ms to {ms} ms?" if current
                 else f"Set the sampling window to {ms} ms?")
        if not widgets.confirm(self, title, text, ok_text=f"Set {ms} ms"):
            return
        self._start_command("H", self._client.sampling_time, ms, context={"ms": ms},
                            text=f"Setting the sampling window to {ms} ms…")

    @_guarded
    def _on_delay_search(self, *_args) -> None:
        expected = self._client.expected_delay_search_s
        text = (f"The base station tries every burst delay from 0 to 500 µs, then stores the one "
                f"with the most valid readings and uses it from now on, also after a reboot.\n\n"
                f"It takes about {expected:.0f} s and cannot be interrupted. Meanwhile the ground "
                f"station answers nothing else: positioning and calibration pause.\n\n"
                f"Keep the transmitter still where all six receivers hear it.\n\n"
                f"A new delay shifts every receiver's distances: recalibrate afterwards.")
        if not widgets.confirm(self, "Run the burst-delay sweep?", text, ok_text="Run sweep",
                               details=_SWEEP_DETAILS):
            return
        self._start_command("D", self._client.delay_search,
                            context={"old": self._client.burst_delay_us},
                            text=f"Sweeping burst delays from 0 to 500 µs "
                                 f"(about {expected:.0f} s)…",
                            expected_s=expected)

    @_guarded
    def _on_default_delay(self, *_args) -> None:
        current = self._client.burst_delay_us
        text = (f"Store the default burst delay, {DEFAULT_BURST_DELAY_US} µs, in the base station, "
                f"in place of the delay found by the last sweep; it stays in force after a "
                f"reboot.\n\n")
        if current == DEFAULT_BURST_DELAY_US:
            text += "The delay in force is already the default: nothing changes."
        else:
            text += ("If the receivers were calibrated with another delay, every receiver's "
                     "distances shift: recalibrate afterwards.")
        if not widgets.confirm(self, "Restore the default burst delay?", text,
                               ok_text=f"Restore {DEFAULT_BURST_DELAY_US} µs"):
            return
        self._start_command("E", self._client.default_delay, context={"old": current},
                            text="Restoring the default burst delay…")

    @_guarded
    def _on_reset_transmitter(self, *_args) -> None:
        text = ("The base station sends the transmitter station a reset packet over the radio "
                "(several times: the link has no acknowledgement). The transmitter reboots and "
                "misses a few readings; its settings are kept.\n\n"
                "Use it when every receiver reads 0 (no echo).")
        if not widgets.confirm(self, "Reset the transmitter station?", text,
                               ok_text="Reset transmitter"):
            return
        self._start_command("R", self._client.reset_transmitter,
                            text="Resetting the transmitter station…")

    @_guarded
    def _on_reset_settings(self, *_args) -> None:
        text = ("Every setting stored in the base station goes back to its factory default:\n\n"
                f"• trigger rate {DEFAULT_TRIGGER_RATE_HZ} Hz (sent to the transmitter, which "
                f"reboots if its rate changes)\n"
                f"• sampling window {DEFAULT_SAMPLING_TIME_MS} ms\n"
                f"• burst delay {DEFAULT_BURST_DELAY_US} µs: if another delay was in force, every "
                f"receiver's distances shift and you need to recalibrate\n"
                f"• baud rate {DEFAULT_BAUD_RATE}: if it was different, the base station reboots "
                f"and the ground station needs about 7 s to follow (commands time out "
                f"meanwhile)\n\n"
                "The old values are not kept.")
        if not widgets.confirm(self, "Reset the base-station settings?", text,
                               ok_text="Reset settings", danger=True):
            return
        self._start_command("M", self._client.reset_settings,
                            context={"old": self._client.burst_delay_us, "baud": self._baud},
                            text="Resetting the base-station settings…")

    @_guarded
    def _on_pulse(self, *_args) -> None:
        self._start_command("P", self._client.pulse, text="Firing one pulse…")

    @_guarded
    def _on_simulate(self, *_args) -> None:
        self._start_command("S", self._client.simulate, text="Asking for a simulated pulse…")

    # ------------------------------------------------------------------
    # Command results (GUI thread)
    # ------------------------------------------------------------------

    @_guarded
    def _on_job_result(self, outcome) -> None:
        job, context = self._job, self._job_context
        self._worker = None
        self._job = None
        self._job_context = {}
        try:
            value, error = outcome
            finish = {
                "refresh": self._finish_refresh, "F": self._finish_rate, "H": self._finish_window,
                "D": self._finish_sweep, "E": self._finish_default_delay,
                "R": self._finish_reset_transmitter, "M": self._finish_reset_settings,
                "P": self._finish_pulse, "S": self._finish_pulse,
            }[job]
            finish(job, value, error, context)
        finally:
            self._after_job()

    @_guarded
    def _on_job_error(self, message: str) -> None:
        job = self._job
        self._worker = None
        self._job = None
        self._job_context = {}
        try:
            self._report_failure(_JOB_NAMES.get(job, "The command"), message)
        finally:
            self._after_job()

    def _after_job(self) -> None:
        if not self._client.connected and self._link_up:
            self._set_link(False, announce=False)
        self._render()
        self._update_progress()
        self._refresh_if_pending()

    def _refresh_if_pending(self) -> None:
        """Start the refresh that waited for the command or the live view that
        has just ended (only while the panel is shown and the link is up)."""
        if self._refresh_pending and self._active and self._link_up:
            self.refresh()

    def _report_failure(self, what: str, error, away: bool = True) -> None:
        error = str(error or "no reply")
        text = f"{what} failed: {error}"
        client = self._client
        if client.connected and client.consecutive_timeouts > 0 and "No reply" in error:
            text += (f" — the ground station runs, but the base station is not answering "
                     f"({client.consecutive_timeouts} in a row): check its USB cable, or wait "
                     f"about 7 s if it is rebooting.")
        self._show_result(text, RED, error, away)

    def _show_result(self, text: str, color: str, tooltip: str | None = None,
                     away: bool = True) -> None:
        """Show a command's outcome; with ``away`` it is kept for the next show
        when the panel is hidden now (a sweep that finished in the
        background)."""
        self._keep_status = False
        self._set_status(text, color, tooltip)
        if away and not self._active:
            self._away_status = (f"While you were away: {text}", color, tooltip)

    def _finish_refresh(self, job, values, errors, _context) -> None:
        if values["B"] is not None:
            self._baud = values["B"]
        if any(value is not None for value in values.values()):
            self._read_at = _now()
            self._sync_spins()
        if errors:                                # the next show reads them again anyway
            self._report_failure(_JOB_NAMES[job], "; ".join(errors), away=False)
        elif not self._keep_status and self._active:
            self._set_status(f"Read from the base station: {values['F'].hz} Hz, "
                             f"{values['H'].ms} ms window, {values['B']} baud.", GREEN)

    def _finish_rate(self, job, value, error, context) -> None:
        if value is None:
            self._report_failure(_JOB_NAMES[job], error)
            return
        self._read_at = _now()
        self._sync_spins()
        if value.hz != context["hz"]:
            self._show_result(f"The base station kept {value.hz} Hz (it accepts "
                              f"{TRIGGER_RATE_MIN_HZ} to {TRIGGER_RATE_MAX_HZ} Hz).", AMBER)
            return
        text = (f"Trigger rate set to {value.hz} Hz; the sampling window is now "
                f"{value.sampling_ms} ms, its maximum.")
        old = context.get("old")
        if old is None:
            text += " The transmitter station reboots into it if it ran at another rate."
        elif value.hz != old:
            text += " The transmitter station reboots into the new rate."
        self._show_result(text, GREEN)

    def _finish_window(self, job, value, error, context) -> None:
        if value is None:
            self._report_failure(_JOB_NAMES[job], error)
            return
        self._read_at = _now()
        self._sync_spins()
        if value.ms != context["ms"]:
            hz = self._rate_hz()
            limit = (f"at {hz} Hz it accepts {SAMPLING_TIME_MIN_MS} to "
                     f"{max_sampling_time_ms(hz)} ms" if hz
                     else "it accepts at most 80 % of the trigger period")
            self._show_result(f"Not applied: the base station kept {value.ms} ms ({limit}).", AMBER)
            return
        self._show_result(f"Sampling window set to {value.ms} ms.", GREEN)

    def _finish_sweep(self, job, value, error, context) -> None:
        if value is None:
            self._report_failure(
                _JOB_NAMES[job], f"{error}. The base station may still finish the sweep "
                f"and store a delay: restore the default (E) or run the sweep again to be sure "
                f"which delay is in force")
            return
        when = _now()
        self._sweep = (value, when)
        if not value.stored:
            self._show_result("Sweep finished, but no delay got a single valid reading: nothing "
                              "was stored and the delay in force stays. Check that the transmitter "
                              "is on and that the receivers can hear it.", AMBER)
            return
        self._delay_source = f"found by the sweep at {when}"
        self._show_result(f"Sweep finished: {value.delay_us} µs stored "
                          f"({value.valid}/{value.total} readings valid). Recalibrate if the "
                          f"delay changed.", GREEN)
        self._warn_delay_change(context.get("old"), self._client.burst_delay_us)

    def _finish_default_delay(self, job, value, error, context) -> None:
        if value is None:
            self._report_failure(_JOB_NAMES[job], error)
            return
        self._delay_source = f"restored at {_now()}"
        self._show_result(f"Burst delay set to {value} µs.", GREEN)
        self._warn_delay_change(context.get("old"), value)

    def _finish_reset_transmitter(self, job, _value, error, _context) -> None:
        if error:
            self._report_failure(_JOB_NAMES[job], error)
            return
        self._show_result("Reset sent to the transmitter station. The radio link has no "
                          "acknowledgement, so this does not prove that it arrived: a test pulse "
                          "will tell.", GREEN)

    def _finish_reset_settings(self, job, _value, error, context) -> None:
        if error:
            self._report_failure(_JOB_NAMES[job], error)
            return
        old_baud = context.get("baud")
        self._baud = DEFAULT_BAUD_RATE
        self._delay_source = f"factory default, reset at {_now()}"
        self._sync_spins()
        text = (f"Base-station settings reset: {DEFAULT_TRIGGER_RATE_HZ} Hz, "
                f"{DEFAULT_SAMPLING_TIME_MS} ms window, {DEFAULT_BURST_DELAY_US} µs burst delay, "
                f"{DEFAULT_BAUD_RATE} baud.")
        if old_baud != DEFAULT_BAUD_RATE:
            text += (" If the baud rate was different, the base station is rebooting: the ground "
                     "station follows in about 7 s.")
        self._show_result(text, GREEN)
        self._warn_delay_change(context.get("old"), DEFAULT_BURST_DELAY_US)

    def _finish_pulse(self, job, value, error, _context) -> None:
        if value is None:
            self._report_failure(_JOB_NAMES[job], error)
            return
        when = _now()
        if job == "P":
            heard = sum(1 for tick in value if tick)
            text = (f"Pulse at {when}: {heard} of {RECEIVER_COUNT} receivers heard the "
                    f"transmitter.")
            color = GREEN
            if heard < MIN_RECEIVERS_FOR_FIX:
                text += f" At least {MIN_RECEIVERS_FOR_FIX} are needed for a position."
                color = AMBER
            self._show_result(text, color)
            info = (text, color)
        else:
            self._show_result("Simulated pulse received: the link to the base station works.",
                              GREEN)
            info = (f"Simulated pulse at {when}: the base station's made-up test ticks, not "
                    f"measurements.", CYAN)
        self._show_ticks(value, info)

    def _show_ticks(self, ticks, info) -> None:
        self._ticks = [int(tick) for tick in ticks[:RECEIVER_COUNT]]
        self._ticks_info = info
        self._render_ticks()

    def _warn_delay_change(self, old: int | None, new: int | None) -> None:
        """Raise the recalibration warning after D, E or M set the burst delay
        to ``new`` (``old``: the delay known before, None if unknown)."""
        if new is None or (old is not None and old == new):
            return                                # unchanged: the calibration stays valid
        if old is None and new == DEFAULT_BURST_DELAY_US:
            text = (f"Burst delay now {new} µs. The delay in force before was not known (the "
                    f"firmware cannot report it): if the receivers were calibrated with another "
                    f"delay, recalibrate.")
        else:
            base = old if old is not None else DEFAULT_BURST_DELAY_US
            shift = delay_shift_cm(base, new)
            amount = f"{abs(shift):.1f}" if abs(shift) < 10 else f"{abs(shift):.0f}"
            text = (f"Burst delay changed {f'from {old} µs ' if old is not None else ''}to "
                    f"{new} µs: every receiver now reads about {amount} cm too "
                    f"{'long' if shift > 0 else 'short'} until you recalibrate.")
            if old is None:
                text += (f" (Assuming the receivers were calibrated with the {base} µs default: "
                         f"the delay before was not known.)")
        self._recal_text.setText(text)
        self._recal_offsets = list(self._offsets)
        self._recal_box.show()

    # ------------------------------------------------------------------
    # Live view
    # ------------------------------------------------------------------

    @_guarded
    def _on_live_clicked(self, *_args) -> None:
        if self._live is not None:
            self._stop_live(wait=False)
        else:
            self._start_live()
        self._update_controls()

    def _start_live(self) -> bool:
        if self._live is not None or self._worker is not None or not self._link_up:
            self._refuse("The live view")
            return False
        self._live_gen += 1
        self._away_status = None
        self._keep_status = False
        self._live_end_info = self._ticks_info
        self._live = widgets.run_in_thread(
            _live_view, self._client, self._live_gen, pass_cancel=True, pass_progress=True,
            on_result=self._on_live_done, on_error=self._on_live_failed,
            on_progress=self._on_live_reading)
        self._set_status("Live view: continuous mode, one reading per trigger…", DIM)
        self._update_controls()
        return True

    def _stop_live(self, wait: bool) -> None:
        """Ask the live view to stop.  With ``wait`` (the panel is being
        hidden) wait for its thread and finish it here; its late signals are
        then ignored.  The base station keeps streaming until its next
        command, which is up to the panel shown next."""
        worker = self._live
        if worker is None:
            return
        worker.cancel()
        if not wait:
            return
        if not worker.wait(LIVE_STOP_WAIT_MS):
            log.warning("The live view did not stop within %d ms", LIVE_STOP_WAIT_MS)
        self._live = None
        self._live_gen += 1
        self._show_live_ended()
        self._set_status("Live view stopped.", DIM)
        self._update_controls()

    def _show_live_ended(self) -> None:
        """The live view ended: the line under the table stops reporting a
        reading rate and says which reading the table shows."""
        self._ticks_info = self._live_end_info
        self._render_ticks()

    @_guarded
    def _on_live_reading(self, payload) -> None:
        generation, ticks, rate, resends = payload
        if generation != self._live_gen or self._live is None:
            return
        hz = self._rate_hz()
        text = f"Live: {rate:.1f} readings per second"
        if hz:
            text += f" (trigger rate {hz} Hz)"
        if resends:
            text += f" · no readings came, C re-sent {resends}×"
        if ticks is not None:
            heard = sum(1 for tick in ticks if tick)
            text += f" · {heard} of {RECEIVER_COUNT} receivers hear the transmitter"
            color = GREEN if heard >= MIN_RECEIVERS_FOR_FIX else AMBER
            self._live_end_info = (f"Last live reading at {_now()}: {heard} of "
                                   f"{RECEIVER_COUNT} receivers heard the transmitter.", color)
            self._show_ticks(ticks, (text, color))
        else:
            self._ticks_info = (text, AMBER if resends else DIM)
            self._render_ticks()

    @_guarded
    def _on_live_done(self, outcome) -> None:
        generation, reason, error = outcome
        if generation != self._live_gen:
            return                                # already finished when the panel was hidden
        self._live = None
        self._show_live_ended()
        if reason == "stopped":
            self._set_status("Live view stopped.", DIM)
            if self._client.mode is CommMode.CONTINUOUS:
                # The base station streams until its next command: reading the
                # settings again ends continuous mode (the status stays)
                self._refresh_pending = True
                self._keep_status = True
        elif reason == "interrupted":
            self._set_status("Live view stopped: another command ended continuous mode.", AMBER)
        elif reason == "link":
            self._report_failure("The live view", error or "the link to the ground station dropped")
        else:
            self._report_failure("Starting the live view", error)
        if not self._client.connected and self._link_up:
            self._set_link(False, announce=reason == "stopped")
        self._update_controls()
        self._refresh_if_pending()

    @_guarded
    def _on_live_failed(self, message: str) -> None:
        self._live = None
        self._live_gen += 1
        self._show_live_ended()
        self._report_failure("The live view", message)
        self._update_controls()
        self._refresh_if_pending()

    # ------------------------------------------------------------------
    # Main-window signals and housekeeping
    # ------------------------------------------------------------------

    @_guarded
    def _on_link_changed(self, up) -> None:
        self._set_link(bool(up))

    @_guarded
    def _on_config_changed(self, *_args) -> None:
        self._load_settings()
        if self._recal_offsets is not None and self._recal_offsets != self._offsets:
            # New offsets were saved after the delay changed: recalibrated
            self._recal_offsets = None
            self._recal_box.hide()
        self._render()

    @_guarded
    def _on_tick(self, *_args) -> None:
        connected = bool(self._client.connected)
        if connected != self._link_up:            # in case link_changed comes late
            self._set_link(connected)
        if self._worker is not None:
            self._update_progress()

