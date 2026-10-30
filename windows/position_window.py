"""Position tracking panel.

``NetworkThread`` (a QThread) gets the transmitter's ticks from the ground
station -- every continuous (UDP) reading exactly once, or one pulse (TCP)
after another -- solves each with the position library, checks the barriers,
feeds the position server and the position log, and hands every fix to the
panel through a signal.  It rides out a lost ground-station link: it waits
while the main window reconnects, then starts the continuous stream again (a
reconnected client is back in normal mode), and it re-sends the continuous
command, backing off, while the stream stays silent.

``PositionPanel`` shows the arena (receivers, barriers, image overlays, the
LM and CEP fixes with their trails), each receiver's distance history, the
HDOP of the current fix, how many receivers passed the sanity filter, and the
tracking state.  A timer redraws the canvas, and only when something changed,
so the redraw cost does not follow the reading rate.
"""

import functools
import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

import matplotlib.image as mpl_image
from matplotlib.figure import Figure
from matplotlib.patches import Circle, Polygon as MplPolygon
from matplotlib.transforms import Affine2D

from SettingsModule import RECEIVER_COUNT, SettingsModule
from ultragps_barrier import BarrierEvent, BarrierManager, EventType, load_images
from ultragps_client import DEFAULT_TRIGGER_RATE_HZ, CommMode, UltraGPSClient
from ultragps_position import UltraGPSPositionLib, coverage
from ultragps_server import send_barrier_event, send_nmea, send_position
from windows import ui_scale, widgets
from windows.arena_view import RECEIVER_COLOR, ArenaView
from windows.position_logger import PositionLogger
from windows.ui_scale import ScalingCanvas

log = logging.getLogger(__name__)

_SRV_POS       = "pos"
_SRV_NMEA      = "nmea"
_SRV_BARR_TCP  = "barrier_tcp"
_SRV_BARR      = "barrier"

CEP_COLOR = "#FFFF00"

# Tracking loop timing (seconds)
READING_WAIT_S = 0.25                    # longest wait for a continuous reading between checks
LINK_DOWN_POLL_S = 0.25                  # how often a lost link is checked again
PULSE_RETRY_S = 0.5                      # pause after a pulse that failed at once
STREAM_RETRY_S = (1.0, 2.0, 5.0, 10.0)   # gap before the 1st, 2nd, 3rd, later re-sent 'C'
STALL_PERIODS = 3                        # trigger periods without a reading: the stream stalled
STALL_MIN_S = 1.0                        # ... but never less than this
ERROR_LOG_INTERVAL_S = 5.0               # a repeating error is logged at most this often
STOP_TIMEOUT_MS = 10000                  # stop() waits this long at most (a pulse takes <= 3.3 s)

#: HDOP up to these values is shown green / gold; above, orange.
HDOP_GOOD = 1.5
HDOP_FAIR = 3.0

# Tracking states shown on the panel: (text, colour)
STATE_LINK_DOWN = ("Ground station link down: tracking paused until it reconnects", widgets.RED)
STATE_STARTING = ("Starting the continuous stream…", widgets.AMBER)
STATE_STREAMING = ("Tracking: continuous stream (UDP)", widgets.GREEN)
STATE_POLLING = ("Tracking: pulse mode (TCP)", widgets.GREEN)
STATE_NO_REPLY = ("No reply to the pulses: is the base station running?", widgets.AMBER)


@dataclass(frozen=True)
class Fix:
    """One solved reading.

    Attributes:
        ticks:       The 6 raw tick counts (0 = no echo).
        distances:   Calibrated distance per receiver, cm.
        sane:        Receivers (0-based ids) that passed the sanity filter.
        lm:          LM position (x, y) or None; the library's last good
                     position when this reading gave none.
        lm_success:  The LM solve of this reading converged.
        cep:         CEP position (x, y) or None (last good, as for ``lm``).
        cep_success: The CEP solve of this reading found a valid position.
        hdop:        HDOP of this fix over the sane receivers (inf: no fix).
        continuous:  Read from the continuous stream (else a pulse).
        session:     The thread's run that made it (``NetworkThread.session``).
    """

    ticks: tuple
    distances: tuple
    sane: tuple
    lm: tuple | None
    lm_success: bool
    cep: tuple | None
    cep_success: bool
    hdop: float
    continuous: bool
    session: int = 0


def _xy(position) -> tuple | None:
    """A solver position as a finite (x, y) tuple, or None."""
    if position is None:
        return None
    x, y = float(position[0]), float(position[1])
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def _close_log(logger: PositionLogger) -> None:
    """Close ``logger`` (the CSV, then the spreadsheet); a failure is logged."""
    try:
        logger.close()
    except Exception:
        log.exception("Could not close the position log")


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------

class NetworkThread(QThread):
    """Runs the position loop in a background thread.

    Emits signals for each piece of data so the GUI can update safely on the
    main thread via Qt's queued connection mechanism.  ``stop()`` returns only
    once the thread has ended, so the shared position library is free again
    (the calibration panel's measurements reset it).
    """

    fix_ready         = pyqtSignal(object)     # Fix
    barrier_triggered = pyqtSignal(object)     # BarrierEvent
    rate_updated      = pyqtSignal(float)      # readings solved per second
    state_changed     = pyqtSignal(str, str)   # tracking state: text, colour
    log_failed        = pyqtSignal(str)        # the position log could not be opened

    def __init__(
        self,
        client: UltraGPSClient,
        position_lib: UltraGPSPositionLib,
        barrier_manager: BarrierManager,
        server=None,
    ):
        super().__init__()
        self._client = client
        self._position_lib = position_lib
        self.barrier_manager = barrier_manager   # replaced by the panel while stopped
        self._server = server
        self._use_continuous = True   # True=UDP/continuous, False=TCP/pulse
        self._logging_enabled = True
        self._logger = None
        self._stopping = False
        self._wakeup = threading.Event()
        self.session = 0              # bumped by every start(); stamped on each Fix
        self._state = None
        self._reset_filter = False    # reset the library's filter before the next reading
        self._rate_count = 0
        self._rate_since = 0.0
        self._error_logged_at = -math.inf
        self._errors_suppressed = 0

    # ------------------------------------------------------------------
    # Control (GUI thread)
    # ------------------------------------------------------------------

    def set_mode(self, use_continuous: bool) -> None:
        self._use_continuous = bool(use_continuous)
        self._wakeup.set()

    def set_logging(self, enabled: bool) -> None:
        self._logging_enabled = bool(enabled)

    def start(self, *args) -> None:
        self._stopping = False
        self._wakeup.clear()
        self._state = None
        self.session += 1
        super().start(*args)

    def stop(self) -> bool:
        """End the loop and wait until the thread has finished (at most one
        pulse's reply timeout).  True if it ended."""
        self._stopping = True
        self._wakeup.set()
        if not self.isRunning():
            return True
        ended = self.wait(STOP_TIMEOUT_MS)
        if not ended:
            log.error("The position thread did not stop within %d s", STOP_TIMEOUT_MS // 1000)
        return ended

    def wake(self) -> None:
        """Cut a pause short (e.g. the link is back)."""
        self._wakeup.set()

    # ------------------------------------------------------------------
    # The loop (worker thread)
    # ------------------------------------------------------------------

    def run(self) -> None:
        try:
            self._loop()
        except Exception:      # an exception escaping run() would abort the application
            log.exception("Position tracking stopped by an error")
            self._set_state("Tracking stopped by an error (see the log)", widgets.RED)
        finally:
            self._close_logger()

    def _loop(self) -> None:
        client = self._client
        self._position_lib.reset_state()
        self._rate_count, self._rate_since = 0, time.monotonic()
        count = client.reading_count   # last continuous reading handled
        need_stream = True             # 'C' must be sent before readings can come
        stream_tries = 0               # 'C' sent since the last reading
        next_stream_at = 0.0           # earliest time for the next 'C' (backoff)
        last_reading_at = time.monotonic()

        while not self._stopping:
            self._update_logger()
            self._report_rate()

            if not client.connected:
                # The main window reconnects; a new link starts in normal mode
                need_stream, stream_tries, next_stream_at = True, 0, 0.0
                self._reset_filter = True
                self._set_state(*STATE_LINK_DOWN)
                self._sleep(LINK_DOWN_POLL_S)
                continue

            if not self._use_continuous:
                need_stream, stream_tries, next_stream_at = True, 0, 0.0
                ticks = self._pulse()
                if ticks is not None:
                    self._process(ticks, False)
                continue

            if need_stream or client.mode is not CommMode.CONTINUOUS:
                now = time.monotonic()
                if now < next_stream_at:
                    self._sleep(min(next_stream_at - now, READING_WAIT_S))
                    continue
                count = client.reading_count     # continuous() drops older readings
                stream_tries += 1
                next_stream_at = now + STREAM_RETRY_S[min(stream_tries, len(STREAM_RETRY_S)) - 1]
                if not client.continuous():
                    continue      # link down (handled above) or another command held the lock
                need_stream = False
                last_reading_at = time.monotonic()
                if stream_tries == 1:
                    self._set_state(*STATE_STARTING)
                else:
                    self._set_state(f"No readings: re-sent the continuous command "
                                    f"({stream_tries - 1}×)", widgets.AMBER)

            item = client.wait_for_reading(count, READING_WAIT_S)
            if item is None:
                if client.mode is not CommMode.CONTINUOUS:
                    need_stream = True    # a command or a reconnect ended the stream
                elif time.monotonic() - last_reading_at > self._stall_timeout():
                    need_stream = True    # 'C' sent but no stream (dropped, serial not ready)
                    self._reset_filter = True
                continue

            count, ticks = item
            stream_tries, next_stream_at = 0, 0.0
            last_reading_at = time.monotonic()
            self._set_state(*STATE_STREAMING)
            self._process(ticks, True)

    def _pulse(self) -> list | None:
        """One pulse; None (after a pause if it failed at once) when there is
        no reply.  A timed-out pulse has already waited its reply timeout."""
        started = time.monotonic()
        ticks = self._client.pulse()
        if ticks is not None:
            self._set_state(*STATE_POLLING)
            return ticks
        if self._client.connected:
            self._set_state(*STATE_NO_REPLY)
            if time.monotonic() - started < PULSE_RETRY_S:
                self._sleep(PULSE_RETRY_S)
        return None

    def _stall_timeout(self) -> float:
        """Seconds without a continuous reading after which 'C' is sent again:
        a few trigger periods plus the sampling window."""
        hz = self._client.trigger_rate_hz or DEFAULT_TRIGGER_RATE_HZ
        window_ms = self._client.sampling_time_ms
        window_s = window_ms / 1000.0 if window_ms is not None else 0.8 / hz
        return max(STALL_MIN_S, STALL_PERIODS / hz + window_s)

    def _sleep(self, seconds: float) -> None:
        """Pause that ``stop()``, ``wake()`` and a mode change cut short."""
        self._wakeup.wait(seconds)
        self._wakeup.clear()

    # ------------------------------------------------------------------
    # One reading
    # ------------------------------------------------------------------

    def _process(self, ticks, continuous: bool) -> None:
        """Solve one reading, log it, check the barriers, stream it and hand
        the fix to the GUI.  A failure is logged; the loop carries on."""
        try:
            if self._reset_filter:        # after a gap the differential filter is stale
                self._reset_filter = False
                self._position_lib.reset_state()
            result = self._position_lib.get_position_full(ticks)
            fix = self._make_fix(ticks, result, continuous)
        except Exception as exc:
            self._log_error("Could not solve a reading", exc)
            return

        if self._logger is not None:
            try:
                self._logger.log(ticks, result, continuous)
            except Exception as exc:
                self._log_error("Position log error", exc)

        try:
            self._publish(fix)
        except Exception as exc:
            self._log_error("Could not check barriers or stream the position", exc)

        self._rate_count += 1
        self.fix_ready.emit(fix)

    def _make_fix(self, ticks, result: dict, continuous: bool) -> Fix:
        sane = tuple(int(i) for i in result.get("sane_indices", ()))
        lm, cep = _xy(result.get("lm_position")), _xy(result.get("cep_position"))
        lm_ok, cep_ok = bool(result.get("lm_success")), bool(result.get("cep_success"))
        current = lm if lm_ok else cep if cep_ok else None
        return Fix(
            ticks=tuple(int(t) for t in ticks),
            distances=tuple(float(d) for d in (result.get("distances") or ())),
            sane=sane, lm=lm, lm_success=lm_ok and lm is not None,
            cep=cep, cep_success=cep_ok and cep is not None,
            hdop=self._hdop(current, sane), continuous=continuous, session=self.session,
        )

    def _hdop(self, point, sane) -> float:
        """HDOP at ``point`` over the receivers that passed the sanity filter."""
        if point is None or len(sane) < 3:
            return math.inf
        coords = self._position_lib.receiver_coords
        heard = np.zeros((1, len(coords)), dtype=bool)
        heard[0, [i for i in sane if 0 <= i < len(coords)]] = True
        return float(coverage.hdop([point], coords, heard)[0])

    def _publish(self, fix: Fix) -> None:
        """Barrier events for both positions, and the position feed."""
        manager = self.barrier_manager
        server = self._server
        streaming = server is not None and server.streaming
        for source, xy in (("lm", fix.lm), ("cep", fix.cep)):
            if xy is None:
                continue
            events = manager.check_position(xy[0], xy[1], source) if manager is not None else []
            for ev in events:
                self.barrier_triggered.emit(ev)
            if streaming:
                send_position(server, _SRV_POS, xy[0], xy[1], source=source)
                if source == "lm":
                    send_nmea(server, _SRV_NMEA, num_satellites=len(fix.sane))
                for ev in events:
                    send_barrier_event(server, _SRV_BARR_TCP, _SRV_BARR, ev)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _update_logger(self) -> None:
        """Open or close the log files to match the logging toggle."""
        if self._logging_enabled and self._logger is None:
            try:
                self._logger = PositionLogger()
            except Exception as exc:
                log.warning("Could not start the position log: %s", exc)
                self._logging_enabled = False
                self.log_failed.emit(str(exc))
        elif not self._logging_enabled and self._logger is not None:
            self._close_logger()

    def _close_logger(self) -> None:
        """Close the position log.  Saving the spreadsheet of a long session
        takes seconds, so that runs on a thread of its own: ``stop()``, and
        with it the panel's hide, only waits for the loop."""
        logger, self._logger = self._logger, None
        if logger is None:
            return
        if logger.xlsx_path is None:              # CSV only: closing it is instant
            _close_log(logger)
        else:
            threading.Thread(target=_close_log, args=(logger,), name="position-log-save").start()

    def _report_rate(self) -> None:
        """Emit the readings solved per second about once a second."""
        now = time.monotonic()
        elapsed = now - self._rate_since
        if elapsed >= 1.0:
            self.rate_updated.emit(self._rate_count / elapsed)
            self._rate_count, self._rate_since = 0, now

    def _set_state(self, text: str, color: str) -> None:
        if (text, color) != self._state:
            self._state = (text, color)
            self.state_changed.emit(text, color)

    def _log_error(self, what: str, exc: Exception) -> None:
        """Log a failure, at most once per ERROR_LOG_INTERVAL_S (with a count
        of the ones left out), so a persistent fault does not flood the log."""
        now = time.monotonic()
        if now - self._error_logged_at < ERROR_LOG_INTERVAL_S:
            self._errors_suppressed += 1
            return
        more = f" ({self._errors_suppressed} more errors not shown)" if self._errors_suppressed else ""
        log.warning("%s: %s%s", what, exc, more, exc_info=exc)
        self._error_logged_at, self._errors_suppressed = now, 0


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------

def _label_css(color: str, px: int, bold: bool = False) -> str:
    return f"color: {color}; font: {'bold ' if bold else ''}{px}px Arial; background: transparent;"


def _label(text: str = "", color: str = "white", px: int = 12, bold: bool = False) -> QLabel:
    lbl = QLabel(text)
    ui_scale.style(lbl, _label_css(color, px, bold))
    return lbl


def _checkbox(text: str, color: str, checked: bool = True, tooltip: str = "") -> QCheckBox:
    """A check box in ``color`` with a scaled indicator."""
    cb = QCheckBox(text)
    cb.setChecked(checked)
    ui_scale.style(cb, f"""
        QCheckBox {{ color: {color}; font: bold 12px Arial; spacing: 6px; background: transparent; }}
        QCheckBox::indicator {{ width: 12px; height: 12px; border-radius: 3px; }}
        QCheckBox::indicator:unchecked {{ background-color: #222222; border: 2px solid #666666; }}
        QCheckBox::indicator:checked {{ background-color: {color}; border: 2px solid {color}; }}
    """)
    if tooltip:
        cb.setToolTip(tooltip)
    return cb


def _apply_led(color: str, size: int, led: QFrame, factor: float) -> None:
    """Size and style a status light for the UI scale ``factor``.  The radius
    is half the scaled size: scaled on its own it could round above that, and
    Qt then draws a square.  The rule names the light (``#led``) so that its
    tooltip does not turn into a coloured bubble as well."""
    px = ui_scale.scale_length(size, factor)
    led.setFixedSize(px, px)
    led.setStyleSheet(f"QFrame#led {{ background-color: {color}; border-radius: {px // 2}px; "
                      f"border: {ui_scale.scale_length(2, factor)}px solid white; }}")


class _Led(QFrame):
    """A round status light."""

    def __init__(self, color: str = widgets.GREEN, size: int = 18, tooltip: str = ""):
        super().__init__()
        self.setObjectName("led")
        self._size = size
        self._color = None
        self.set_color(color)
        if tooltip:
            self.setToolTip(tooltip)

    def set_color(self, color: str) -> None:
        if color != self._color:
            self._color = color
            ui_scale.bind(self, functools.partial(_apply_led, color, self._size), key="led")

    def color(self) -> str:
        return self._color


class _StateLabel(QLabel):
    """"● text" in a state's colour."""

    def __init__(self):
        super().__init__()
        self._color = None

    def set_state(self, text: str, color: str, tooltip: str = "") -> None:
        self.setText(f"● {text}")
        self.setToolTip(tooltip)
        if color != self._color:
            self._color = color
            ui_scale.style(self, _label_css(color, 12, bold=True))

    def color(self) -> str | None:
        return self._color


class _DistancePlot:
    """One receiver's distance history: the line, the current distance and a
    light that is green while the receiver passes the sanity filter.

    The plots flank the arena; ``outer_side`` ('left' or 'right') is where
    their tick labels go, away from the arena's own labels.
    """

    def __init__(self, fig, cell, rid: int, history: int, outer_side: str):
        self.ax = ax = fig.add_subplot(cell)
        ax.set_facecolor('black')
        ax.set_title(f"R{rid + 1} distance (cm)", fontsize=9, fontweight='bold',
                     color=RECEIVER_COLOR, pad=3)
        ax.tick_params(axis='both', labelsize=7, colors='white')
        ax.tick_params(axis='x', labelbottom=False)
        ax.set_xlim(0, history - 1)
        ax.set_ylim(0, 300)
        if outer_side == 'right':
            ax.yaxis.tick_right()
            text_x, text_ha, led_x = 0.03, 'left', 0.92
        else:
            text_x, text_ha, led_x = 0.97, 'right', 0.08
        ax.grid(True, alpha=0.3, color='gray')
        for spine in ax.spines.values():
            spine.set_color('white')

        self.line, = ax.plot([], [], color=widgets.GREEN, linewidth=1)
        self.text = ax.text(text_x, 0.94, '---', ha=text_ha, va='top', fontsize=11,
                            fontweight='bold', transform=ax.transAxes, color='white', zorder=10,
                            bbox=dict(boxstyle='round', facecolor='#222222', alpha=0.8))
        self.led, = ax.plot([led_x], [0.86], 'o', transform=ax.transAxes, markersize=9,
                            markerfacecolor=widgets.GREEN, markeredgecolor='white',
                            markeredgewidth=1.5, clip_on=False, zorder=10)

    def update(self, history, sane: bool) -> None:
        values = np.asarray(history, dtype=float)
        self.line.set_data(np.arange(len(values)), values)
        finite = values[np.isfinite(values)]
        if len(finite):
            lo, hi = float(finite.min()), float(finite.max())
            pad = max(5.0, 0.15 * (hi - lo))
            self.ax.set_ylim(lo - pad, hi + pad)
        last = values[-1] if len(values) else math.nan
        self.text.set_text(f'{last:.1f} cm' if math.isfinite(last) else '---')
        self.led.set_markerfacecolor(widgets.GREEN if sane else widgets.RED)

    def clear(self) -> None:
        self.line.set_data([], [])
        self.ax.set_ylim(0, 300)
        self.text.set_text('---')
        self.led.set_markerfacecolor(widgets.GREEN)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class PositionPanel(QWidget):
    """Full-page position tracking panel.

    Owns the matplotlib figure (arena + 6 distance plots) and the
    NetworkThread.  The canvas is refreshed by a 20 Hz QTimer, only when
    something changed; the timer and the thread run while the panel is shown.
    """

    #: Positions kept in each trail and readings in each distance plot.
    HISTORY = 50

    #: Canvas refresh interval (ms).
    REFRESH_MS = 50

    #: How long a barrier stays highlighted after its last inside/outside event (ms).
    BARRIER_HIGHLIGHT_MS = 300

    def __init__(
        self,
        client: UltraGPSClient,
        position_lib: UltraGPSPositionLib,
        settings_module: SettingsModule,
        main_window,
        server=None,
    ):
        super().__init__()
        self._client = client
        self._position_lib = position_lib
        self._settings = settings_module
        self._main_window = main_window
        self._server = server
        # Scoped: an unscoped background would cascade into the panel's dialogs
        self.setObjectName("positionPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("QWidget#positionPanel { background-color: black; }")

        self._barrier_dir = self._settings.config_dir
        self._barrier_manager = BarrierManager(self._barrier_dir)
        self._barrier_artists: dict[str, dict] = {}
        self._barrier_timers: dict[str, QTimer] = {}
        self._barrier_alert: str | None = None     # the barrier the alert label names
        self._image_artists: list = []
        self._warned_images: set = set()

        # Display toggles (mirrored by QCheckBox widgets)
        self.show_multilateration = True
        self.show_cep = True

        self._fix: Fix | None = None
        self._lm_history = deque(maxlen=self.HISTORY)
        self._cep_history = deque(maxlen=self.HISTORY)
        self._distance_histories = [deque(maxlen=self.HISTORY) for _ in range(RECEIVER_COUNT)]
        self._shown = False
        self._dirty = False
        self._draws = 0
        self._draws_since = time.monotonic()

        self._build_figure()
        self._build_ui()
        try:
            self._view.set_receivers(self._settings.get_tower_coordinates())
            self._view.set_limits()
        except Exception:                 # drawn again (and reported) on show
            log.exception("Could not draw the receivers")

        # NetworkThread
        self._net_thread = NetworkThread(
            self._client, self._position_lib, self._barrier_manager, server=self._server)
        self._net_thread.fix_ready.connect(self._on_fix)
        self._net_thread.barrier_triggered.connect(self._handle_barrier_event)
        self._net_thread.rate_updated.connect(self._on_rate_updated)
        self._net_thread.state_changed.connect(self._on_state_changed)
        self._net_thread.log_failed.connect(self._on_log_failed)
        self._mode_cb.toggled.connect(self._on_mode_toggled)
        self._log_cb.toggled.connect(self._on_log_toggled)

        # Canvas refresh, and the redraw-rate read-out (started/stopped with the panel)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(self.REFRESH_MS)
        self._refresh_timer.timeout.connect(self._on_refresh_timer)
        self._fps_timer = QTimer(self)
        self._fps_timer.setInterval(1000)
        self._fps_timer.timeout.connect(self._update_fps)
        self._canvas.mpl_connect('draw_event', self._on_draw)

        main_window.config_changed.connect(self._on_config_changed)
        main_window.link_changed.connect(self._on_link_changed)

    # ------------------------------------------------------------------
    # Figure construction
    # ------------------------------------------------------------------

    def _build_figure(self) -> None:
        self.fig = Figure(facecolor='black')
        gs = self.fig.add_gridspec(3, 3, width_ratios=[1, 4, 1], hspace=0.35, wspace=0.3,
                                   left=0.04, right=0.96, top=0.93, bottom=0.07)

        # Main arena: receivers, outline, origin and compass from ArenaView;
        # the legend sits above the arena, clear of the receivers in the corners
        self.ax = self.fig.add_subplot(gs[:, 1])
        self._view = ArenaView(self.ax, legend_loc='lower center', legend_anchor=(0.5, 1.0),
                               legend_ncol=4)
        self._lm_trail, = self.ax.plot([], [], '-', color=widgets.GREEN, linewidth=1,
                                       alpha=0.5, zorder=6)
        self._cep_trail, = self.ax.plot([], [], '-', color=CEP_COLOR, linewidth=1,
                                        alpha=0.5, zorder=6)

        # Distance plots — left column: receivers 3, 2, 1 from the top,
        #                  right column: receivers 6, 5, 4
        self._distance_plots: list[_DistancePlot | None] = [None] * RECEIVER_COUNT
        for row, rid in enumerate((2, 1, 0)):
            self._distance_plots[rid] = _DistancePlot(self.fig, gs[row, 0], rid, self.HISTORY,
                                                      outer_side='left')
        for row, rid in enumerate((5, 4, 3)):
            self._distance_plots[rid] = _DistancePlot(self.fig, gs[row, 2], rid, self.HISTORY,
                                                      outer_side='right')

    # ------------------------------------------------------------------
    # Qt widget layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Top bar: title, tracking state, barrier alert, back ---
        top_bar = QWidget()
        top_layout = QHBoxLayout(top_bar)
        ui_scale.margins(top_layout, 10, 5, 10, 5)
        ui_scale.spacing(top_layout, 16)

        title = QLabel("Position Tracking")
        ui_scale.font(title, 16, bold=True)
        title.setStyleSheet("color: #39FF14;")
        top_layout.addWidget(title)

        self._state_label = _StateLabel()
        self._state_label.set_state("Stopped", widgets.DIM)
        top_layout.addWidget(self._state_label)

        # Barrier alerts and configuration problems; a long message is clipped
        # (its tooltip has it all) instead of widening the panel past the screen
        self._barrier_status_label = _label("", widgets.RED, 13, bold=True)
        self._barrier_status_label.setMinimumWidth(1)
        top_layout.addWidget(self._barrier_status_label)
        top_layout.addStretch(1)

        back_btn = widgets.neon_button("← Back", widgets.GREEN)
        back_btn.clicked.connect(lambda: self._main_window.show_panel('main'))
        top_layout.addWidget(back_btn)
        layout.addWidget(top_bar)

        # --- Canvas (ref_size: its size in a 1200 x 900 window) ---
        self._canvas = ScalingCanvas(self.fig, ref_size=(1200, 770))
        layout.addWidget(self._canvas, stretch=1)

        # --- Control bar ---
        ctrl = QWidget()
        ctrl_layout = QHBoxLayout(ctrl)
        ui_scale.margins(ctrl_layout, 16, 4, 16, 8)
        ui_scale.spacing(ctrl_layout, 20)
        ctrl_layout.addStretch(1)

        # Multilateration
        self.multilat_label = QLabel("Multilat: (---, ---)")
        ui_scale.font(self.multilat_label, 14, bold=True)
        self.multilat_label.setStyleSheet("color: #39FF14;")
        ui_scale.min_width(self.multilat_label, 250)
        ml_cb = _checkbox("Show Multilateration", widgets.GREEN)
        ml_cb.toggled.connect(self._on_show_multilateration)
        ctrl_layout.addLayout(self._column(self.multilat_label, ml_cb))

        # CEP
        self.cep_label = QLabel("CEP: (---, ---)")
        ui_scale.font(self.cep_label, 14, bold=True)
        self.cep_label.setStyleSheet(f"color: {CEP_COLOR};")
        ui_scale.min_width(self.cep_label, 215)
        self.cep_validity_led = _Led(tooltip="Green: this reading gave a valid CEP position")
        cep_row = QHBoxLayout()
        ui_scale.spacing(cep_row, 8)
        cep_row.addWidget(self.cep_validity_led)
        cep_row.addWidget(self.cep_label)
        cep_cb = _checkbox("Show CEP", CEP_COLOR)
        cep_cb.toggled.connect(self._on_show_cep)
        ctrl_layout.addLayout(self._column(cep_row, cep_cb))

        # Fix quality: HDOP and the receivers that passed the sanity filter
        self._hdop_label = QLabel("HDOP: ---")
        ui_scale.font(self._hdop_label, 14, bold=True)
        self._hdop_label.setStyleSheet(f"color: {widgets.DIM};")
        ui_scale.min_width(self._hdop_label, 140)
        self._hdop_label.setToolTip(
            "Horizontal dilution of precision of the current fix, from the receivers that "
            "passed the sanity filter: cm of position error per cm of range error.\n"
            f"Up to {HDOP_GOOD:g} is good (green), up to {HDOP_FAIR:g} fair (gold); "
            "higher means the receivers see the transmitter from too few directions.")
        self.insuff_led = _Led(tooltip="Red: fewer than 3 receivers passed the sanity filter "
                                       "(no new fix)")
        self._sane_label = _label("Receivers: -/-", "white", 12, bold=True)
        sane_row = QHBoxLayout()
        ui_scale.spacing(sane_row, 6)
        sane_row.addWidget(self.insuff_led)
        sane_row.addWidget(self._sane_label)
        sane_row.addStretch(1)
        ctrl_layout.addLayout(self._column(self._hdop_label, sane_row))

        # Mode and logging
        self._mode_cb = _checkbox(
            "Continuous (UDP)", widgets.CYAN,
            tooltip="Checked: the base station streams every reading over UDP.\n"
                    "Unchecked: one pulse at a time over TCP.")
        self._log_cb = _checkbox("Log to file", widgets.DIM,
                                 tooltip="Write every position to log/positions_<date>.csv/.xlsx")
        ctrl_layout.addLayout(self._column(self._mode_cb, self._log_cb))

        # Performance counters
        self._fps_label = _label("FPS: --", widgets.DIM, 13, bold=True)
        self._pos_sec_label = _label("Pos/s: --", widgets.DIM, 13, bold=True)
        for lbl in (self._fps_label, self._pos_sec_label):
            ui_scale.min_width(lbl, 95)
        self._fps_label.setToolTip("Canvas redraws per second")
        self._pos_sec_label.setToolTip("Readings solved per second")
        ctrl_layout.addLayout(self._column(self._fps_label, self._pos_sec_label))

        # Navigation buttons
        cal_btn = widgets.neon_button("Calibration", widgets.MAGENTA, "white", padding=(6, 15))
        cal_btn.clicked.connect(lambda: self._main_window.show_panel('calibration'))
        arena_btn = widgets.neon_button("Arena Maker", widgets.CYAN, "black", padding=(6, 15))
        arena_btn.clicked.connect(lambda: self._main_window.show_panel('arena_maker'))
        ctrl_layout.addLayout(self._column(cal_btn, arena_btn))
        ctrl_layout.addStretch(1)

        layout.addWidget(ctrl)

    @staticmethod
    def _column(*items) -> QVBoxLayout:
        """A vertical group of widgets / layouts for the control bar."""
        col = QVBoxLayout()
        ui_scale.spacing(col, 4)
        col.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        for item in items:
            if isinstance(item, QWidget):
                col.addWidget(item)
            else:
                col.addLayout(item)
        return col

    # ------------------------------------------------------------------
    # Arena contents
    # ------------------------------------------------------------------

    def _refresh_from_settings(self) -> None:
        """Receivers, barriers and image overlays from the configuration (on
        show, and after a save while shown).  Problems are reported, never
        raised."""
        problems = []
        self._barrier_alert = None
        self._set_alert("")
        try:
            self._position_lib.reload(self._settings.config_path)
        except Exception as exc:
            problems.append(f"position library: {exc}")
        try:
            self._view.set_receivers(self._settings.get_tower_coordinates())
            self._view.set_limits()
        except Exception as exc:
            problems.append(f"receivers: {exc}")

        if self._settings.config_dir != self._barrier_dir:
            self._rebuild_barrier_manager()
        try:
            self._barrier_manager.reload_barriers()
        except Exception as exc:          # unreadable barriers.toml: track without barriers
            self._barrier_manager.barriers = []
            problems.append(f"barriers.toml: {exc}")
        try:
            self._draw_barriers()
        except Exception as exc:
            problems.append(f"barriers: {exc}")

        try:
            failed = self._draw_images()
        except Exception as exc:
            failed = []
            problems.append(f"image overlays: {exc}")

        for problem in problems:
            log.warning("Position panel: %s", problem)
        if problems:
            self._set_alert("\u26a0 " + "; ".join(problems))
        new_failures = set(failed) - self._warned_images
        if new_failures:
            self._warned_images |= new_failures
            widgets.warning(self, "Image Error",
                            "These image overlays could not be shown (missing or corrupted "
                            "files in the resources folder):\n" + "\n".join(sorted(new_failures)))

    def _rebuild_barrier_manager(self) -> None:
        """A fresh BarrierManager for the settings' config directory (the
        thread must be stopped)."""
        self._barrier_dir = self._settings.config_dir
        self._barrier_manager = BarrierManager(self._barrier_dir)
        self._net_thread.barrier_manager = self._barrier_manager

    def _get_resources_dir(self) -> str:
        return os.path.join(self._settings.config_dir, 'resources')

    def _draw_images(self) -> list[str]:
        """Load and render image overlays from barriers.toml onto the arena;
        returns the files that could not be shown."""
        for im in self._image_artists:
            try:
                im.remove()
            except (ValueError, NotImplementedError):
                pass
        self._image_artists.clear()

        images = load_images(os.path.join(self._settings.config_dir, 'barriers.toml'))
        resources_dir = self._get_resources_dir()
        xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
        failed = []
        for overlay in images:
            try:
                img_data = mpl_image.imread(os.path.join(resources_dir, overlay.filename))
                half_w = overlay.width_cm / 2.0
                half_h = overlay.height_cm / 2.0
                cx, cy = overlay.center_x, overlay.center_y
                im = self.ax.imshow(
                    img_data,
                    extent=[cx - half_w, cx + half_w, cy - half_h, cy + half_h],
                    origin='upper',
                    zorder=1.6,           # over the grid, under barriers and the outline
                    interpolation='bilinear',
                )
                im.set_transform(
                    Affine2D().rotate_around(cx, cy, math.radians(overlay.rotation_deg))
                    + self.ax.transData)
                self._image_artists.append(im)
            except Exception:
                failed.append(overlay.filename)
        # imshow must never resize the arena
        self.ax.set_xlim(xlim)
        self.ax.set_ylim(ylim)
        return failed

    def _draw_barriers(self) -> None:
        for artists in self._barrier_artists.values():
            for artist in artists['shapes'] + artists['labels']:
                try:
                    artist.remove()
                except (ValueError, NotImplementedError):
                    pass
        self._barrier_artists.clear()

        for barrier in self._barrier_manager.barriers:
            shapes, labels = [], []
            color, alpha = barrier.color, barrier.alpha
            kind = barrier.barrier_type.value

            if kind == 'polygon' and barrier.vertices:
                patch = MplPolygon(barrier.vertices, closed=True, facecolor=color, alpha=alpha,
                                   edgecolor=color, linewidth=2, zorder=3)
                self.ax.add_patch(patch)
                shapes.append(patch)
            elif kind == 'circle' and barrier.center and barrier.radius:
                patch = Circle(barrier.center, barrier.radius, facecolor=color, alpha=alpha,
                               edgecolor=color, linewidth=2, zorder=3)
                self.ax.add_patch(patch)
                shapes.append(patch)
            elif kind == 'line' and barrier.point1 and barrier.point2:
                (x1, y1), (x2, y2) = barrier.point1, barrier.point2
                line, = self.ax.plot([x1, x2], [y1, y2], color=color, alpha=alpha,
                                     linewidth=max(2, barrier.thickness or 5), zorder=3,
                                     solid_capstyle='round')
                shapes.append(line)

            centre = None
            if shapes and barrier.vertices:
                centre = (sum(v[0] for v in barrier.vertices) / len(barrier.vertices),
                          sum(v[1] for v in barrier.vertices) / len(barrier.vertices))
            elif shapes and barrier.center:
                centre = barrier.center
            if centre is not None:
                labels.append(self.ax.text(          # names are plain text, never mathtext
                    centre[0], centre[1], barrier.name, color='white', fontsize=8,
                    ha='center', va='center', zorder=4, parse_math=False,
                    bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.5)))

            self._barrier_artists[barrier.name] = {'barrier': barrier, 'shapes': shapes,
                                                   'labels': labels}
        self._dirty = True

    # ------------------------------------------------------------------
    # Barrier events
    # ------------------------------------------------------------------

    def _highlight_barrier(self, barrier_name: str) -> None:
        for artist in self._barrier_artists.get(barrier_name, {}).get('shapes', []):
            if hasattr(artist, 'set_edgecolor'):
                artist.set_edgecolor('#FF0000')
                artist.set_linewidth(3)
            else:
                artist.set_color('#FF0000')
        self._dirty = True

    def _reset_barrier_visual(self, barrier_name: str) -> None:
        entry = self._barrier_artists.get(barrier_name)
        if entry is not None:
            for artist in entry['shapes']:
                if hasattr(artist, 'set_edgecolor'):
                    artist.set_edgecolor(entry['barrier'].color)
                    artist.set_linewidth(2)
                else:
                    artist.set_color(entry['barrier'].color)
        if barrier_name == self._barrier_alert:
            self._barrier_alert = None
            self._set_alert("")
        self._dirty = True

    def _get_reset_timer(self, barrier_name: str) -> QTimer:
        if barrier_name not in self._barrier_timers:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(self.BARRIER_HIGHLIGHT_MS)
            timer.timeout.connect(lambda: self._reset_barrier_visual(barrier_name))
            self._barrier_timers[barrier_name] = timer
        return self._barrier_timers[barrier_name]

    def _handle_barrier_event(self, event: BarrierEvent) -> None:
        if event.event_type == EventType.EXIT:
            self._reset_barrier_visual(event.barrier_name)
            return
        self._highlight_barrier(event.barrier_name)
        self._barrier_alert = event.barrier_name
        self._set_alert(f"\u26a0 BARRIER: {event.barrier_name} ({event.event_type.value})")
        if event.event_type in (EventType.INSIDE, EventType.OUTSIDE):
            self._get_reset_timer(event.barrier_name).start()   # restarts it

    def _set_alert(self, text: str) -> None:
        """Show a barrier alert or the configuration problems in the top bar."""
        self._barrier_status_label.setText(text)
        self._barrier_status_label.setToolTip(text)

    # ------------------------------------------------------------------
    # NetworkThread signal handlers  (run on main thread via queued conn)
    # ------------------------------------------------------------------

    def _on_fix(self, fix: Fix) -> None:
        if fix.session != self._net_thread.session:
            return                      # queued by a run that has been stopped since
        self._fix = fix
        if fix.lm is not None:
            self._lm_history.append(fix.lm)
        if fix.cep is not None:
            self._cep_history.append(fix.cep)
        for history, distance in zip(self._distance_histories, fix.distances):
            history.append(distance)
        self._dirty = True

    def _on_rate_updated(self, rate: float) -> None:
        self._pos_sec_label.setText(f"Pos/s: {rate:.1f}")

    def _on_state_changed(self, text: str, color: str) -> None:
        tooltip = "" if color == widgets.GREEN else (self._client.last_error or "")
        self._state_label.set_state(text, color, tooltip)

    def _on_log_failed(self, message: str) -> None:
        self._log_cb.blockSignals(True)
        self._log_cb.setChecked(False)
        self._log_cb.blockSignals(False)
        self._log_cb.setText("Log to file (failed)")
        self._log_cb.setToolTip(f"The position log could not be opened: {message}")

    def _on_log_toggled(self, checked: bool) -> None:
        self._log_cb.setText("Log to file")
        self._net_thread.set_logging(checked)

    def _on_mode_toggled(self, checked: bool) -> None:
        self._mode_cb.setText("Continuous (UDP)" if checked else "Pulse (TCP)")
        self._net_thread.set_mode(checked)

    def _on_show_multilateration(self, checked: bool) -> None:
        self.show_multilateration = checked
        self._dirty = True

    def _on_show_cep(self, checked: bool) -> None:
        self.show_cep = checked
        self._dirty = True

    def _on_link_changed(self, up: bool) -> None:
        if not self._shown:
            return
        if up:
            self._net_thread.wake()     # restart the stream without waiting for the next check
        else:                           # say so at once; the thread pauses at its next check
            self._on_state_changed(*STATE_LINK_DOWN)

    def _on_config_changed(self) -> None:
        """The configuration was saved (receivers, calibration, origin ...):
        barriers may have moved with the origin and the config directory may
        have changed, so start from a fresh BarrierManager.  Redraw now while
        shown, else on the next show."""
        try:
            if not self._shown:
                self._rebuild_barrier_manager()
                return
            self._net_thread.stop()
            self._rebuild_barrier_manager()
            self._refresh_from_settings()
            self._reset_display()
            if self._shown:               # not hidden during an image warning meanwhile
                self._net_thread.start()
        except Exception:
            log.exception("Position panel: could not apply the new configuration")

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _reset_display(self) -> None:
        """Forget the previous session's fixes and histories."""
        self._fix = None
        self._lm_history.clear()
        self._cep_history.clear()
        for history in self._distance_histories:
            history.clear()
        self.multilat_label.setText("Multilat: (---, ---)")
        self.cep_label.setText("CEP: (---, ---)")
        self._hdop_label.setText("HDOP: ---")
        self._hdop_label.setStyleSheet(f"color: {widgets.DIM};")
        self._sane_label.setText("Receivers: -/-")
        self.cep_validity_led.set_color(widgets.GREEN)
        self.insuff_led.set_color(widgets.GREEN)
        self._pos_sec_label.setText("Pos/s: --")
        self._fps_label.setText("FPS: --")
        for plot in self._distance_plots:
            plot.clear()
        self._view.clear_points('lm')
        self._view.clear_points('cep')
        self._lm_trail.set_data([], [])
        self._cep_trail.set_data([], [])
        self._dirty = True

    def _update_display(self) -> None:
        """Push the latest fix and the histories to the labels and the plots."""
        fix = self._fix
        if fix is not None:
            if fix.lm is not None:
                self.multilat_label.setText(f"Multilat: ({fix.lm[0]:.1f}, {fix.lm[1]:.1f})")
            if fix.cep is not None:
                self.cep_label.setText(f"CEP: ({fix.cep[0]:.1f}, {fix.cep[1]:.1f})")
            self.cep_validity_led.set_color(widgets.GREEN if fix.cep_success else widgets.RED)
            n_sane = len(fix.sane)
            self.insuff_led.set_color(widgets.GREEN if n_sane >= 3 else widgets.RED)
            self._sane_label.setText(f"Receivers: {n_sane}/{len(fix.distances)}")
            self._show_hdop(fix.hdop)
            for rid, plot in enumerate(self._distance_plots):
                plot.update(self._distance_histories[rid], rid in fix.sane)

        # The LM marker is the larger one, under CEP's, so both show when they agree
        self._draw_track('lm', self._lm_history, self._lm_trail, self.show_multilateration,
                         widgets.GREEN, 'Multilateration (LM)', size=170, zorder=8.0)
        self._draw_track('cep', self._cep_history, self._cep_trail, self.show_cep,
                         CEP_COLOR, 'CEP', size=70, zorder=8.2)

    def _show_hdop(self, hdop: float) -> None:
        if not math.isfinite(hdop):
            text, color = "HDOP: no fix", widgets.RED
        else:
            text = f"HDOP: {hdop:.2f}"
            color = (widgets.GREEN if hdop <= HDOP_GOOD else
                     widgets.GOLD if hdop <= HDOP_FAIR else widgets.ORANGE)
        if text != self._hdop_label.text():
            self._hdop_label.setText(text)
            self._hdop_label.setStyleSheet(f"color: {color};")

    def _draw_track(self, group: str, history, trail, shown: bool, color: str,
                    legend: str, size: float, zorder: float) -> None:
        """The current position (a point group, so it is in the legend) and the trail."""
        if shown and history:
            self._view.set_points(group, [history[-1]], color=color, marker='o', size=size,
                                  zorder=zorder, legend=legend)
            xs, ys = zip(*history)
            trail.set_data(xs, ys)
        else:
            self._view.clear_points(group)
            trail.set_data([], [])

    def _on_refresh_timer(self) -> None:
        if not self._dirty:
            return
        self._dirty = False
        try:
            self._update_display()
        except Exception:
            log.exception("Position panel: could not update the display")
        self._canvas.draw_idle()

    def _on_draw(self, _event) -> None:
        self._draws += 1

    def _update_fps(self) -> None:
        now = time.monotonic()
        elapsed = now - self._draws_since
        if elapsed > 0:
            self._fps_label.setText(f"FPS: {self._draws / elapsed:.1f}")
        self._draws, self._draws_since = 0, now

    # ------------------------------------------------------------------
    # Panel lifecycle
    # ------------------------------------------------------------------

    def on_panel_show(self) -> None:
        self._shown = True
        self._refresh_from_settings()
        if not self._shown:               # hidden during an image warning (window closed)
            return
        self._reset_display()
        self._barrier_manager.clear_states()
        self._state_label.set_state("Starting…", widgets.AMBER)
        self._draws, self._draws_since = 0, time.monotonic()
        if not self._net_thread.isRunning():
            self._net_thread.start()
        self._refresh_timer.start()
        self._fps_timer.start()

    def on_panel_hide(self) -> None:
        self._shown = False
        self._refresh_timer.stop()
        self._fps_timer.stop()
        self._net_thread.stop()           # synchronous: the position library is free after this
        for timer in self._barrier_timers.values():
            timer.stop()
        self._barrier_manager.clear_states()
        self._state_label.set_state("Stopped", widgets.DIM)
