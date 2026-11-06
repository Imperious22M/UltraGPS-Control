"""Main window: the shared objects, panel navigation and the link monitor.

``UltraGPSMainWindow`` owns what every panel shares -- the ground-station
client (already connected by graphics.py's startup check), the settings, the
position and calibration libraries, the position-feed server and the UI scale
-- and shows one panel at a time in a QStackedWidget.

Contract for the panels:

* attributes ``client``, ``settings_module``, ``position_lib``, ``cal``,
  ``server`` and ``ui_scale``;
* ``show_panel(name)`` with the names ``'main'``, ``'position'``,
  ``'calibration'``, ``'arena_maker'``, ``'barrier_drawer'`` and
  ``'base_station'``: hides the current panel (``on_panel_hide()``, which
  stops everything it runs) and shows the next (``on_panel_show()``);
* ``notify_config_changed()`` after every save that changes receivers,
  facings, offsets, calibration points or the origin: reloads the settings
  and both libraries, then emits ``config_changed`` (the receivers'
  ``origin_distance`` is kept by the SettingsModule setters, and synced once
  at startup);
* ``link_changed(bool)`` when the ground-station TCP link goes down (False)
  or comes back (True).

The status bar is the link monitor, visible on every panel.  When the link
drops it reconnects in the background, waiting 1 s, 2 s, 5 s and then 10 s
between attempts (never two at once), so Control rides out a ground station
that stops or restarts.
"""

import logging
import math
import time

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMainWindow, QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

from SettingsModule import SettingsModule
from ultragps_calibration import UltraGPSCalibration
from ultragps_position import UltraGPSPositionLib
from windows import ui_scale, widgets
from windows.ui_scale import UiScale

log = logging.getLogger(__name__)

#: The window's style sheet: the black background every panel is designed on,
#: and readable tooltips (white on black; without the QToolTip rule their
#: text would keep the palette's black).  The black is scoped to the window
#: and its page stack by name: an unscoped rule cascades into every child,
#: dialogs included (Qt's own file dialog came out black on black).
WINDOW_CSS = ("QMainWindow#ultraGpsMainWindow, QStackedWidget#panelStack "
              "{ background-color: black; } "
              "QToolTip { color: white; background-color: black; "
              "border: 1px solid #555555; padding: 2px; }")


def _log_reload_error(exc: Exception, message: str, path: str) -> None:
    """Log a failed reload: one line for the expected failures (a file that
    cannot be read or parsed), the traceback for anything else (a bug)."""
    if isinstance(exc, (OSError, ValueError)):        # TOMLDecodeError is a ValueError
        log.error(message + ": %s", path, exc)
    else:
        log.exception(message, path)


class UltraGPSMainWindow(QMainWindow):
    """Top-level application window.  Owns shared resources and manages panel navigation.

    Args:
        client:      An ``UltraGPSClient`` that is already connected (graphics.py
                     connects it before anything else starts).
        config_path: config.toml to use; None searches the usual places
                     (see ``SettingsModule``).
        server:      The position-feed ``UltraGPSServer``, or None.

    Signals:
        config_changed:      emitted at the end of ``notify_config_changed()``.
        link_changed(bool):  the ground-station link went up (True) or down (False).
    """

    config_changed = pyqtSignal()
    link_changed = pyqtSignal(bool)

    #: Seconds before each background reconnect attempt: 1 s, 2 s, 5 s, then every 10 s.
    RECONNECT_BACKOFF_S = (1.0, 2.0, 5.0, 10.0)

    #: Seconds allowed for the TCP connect of each reconnect attempt.
    RECONNECT_TIMEOUT_S = 3.0

    #: How often the link state is checked (ms).
    LINK_POLL_MS = 250

    def __init__(self, client, config_path: str = None, server=None):
        super().__init__()
        self.setWindowTitle("UltraGPS Control")
        self.setObjectName("ultraGpsMainWindow")
        ui_scale.style(self, WINDOW_CSS)
        self.client = client
        self.server = server

        # Size the window from its screen, then let the UI scale follow it so
        # the panels built below already get the right factor.
        ui_scale.fit_to_screen(self)
        self.ui_scale = UiScale(self)
        self.ui_scale.attach(self)

        # SettingsModule resolves the config location when config_path is None;
        # reuse whatever it settled on for the other consumers so all four
        # (settings, position, calibration, barriers) read the same file.
        self.settings_module = SettingsModule(config_path)
        # Bring each receiver's origin_distance (its distance to (0, 0) on the
        # arena map) in step with its position; writes only when one changed.
        # Before reading config_path: a read-only config dir switches to ~/.config.
        try:
            if self.settings_module.sync_origin_distances():
                log.info("Updated the receivers' origin distances in %s",
                         self.settings_module.config_path)
        except (OSError, ValueError) as exc:
            log.error("Could not update the receivers' origin distances in %s: %s",
                      self.settings_module.config_path, exc)
        config_path = self.settings_module.config_path
        self.position_lib = UltraGPSPositionLib(config_path)
        self.cal = UltraGPSCalibration(config_path, self.client)

        # Central stacked widget — each panel occupies a full page
        self._stack = QStackedWidget()
        self._stack.setObjectName("panelStack")
        self.setCentralWidget(self._stack)

        self._link_indicator = LinkIndicator()
        self._link_indicator.reconnect_requested.connect(self._start_reconnect)
        status_bar = QStatusBar()
        status_bar.setSizeGripEnabled(False)
        ui_scale.style(status_bar, "QStatusBar { background-color: #0D0D0D; "
                                   "border-top: 1px solid #333333; } "
                                   "QStatusBar::item { border: none; }")
        status_bar.addWidget(self._link_indicator, 1)
        self.setStatusBar(status_bar)

        self._panels: dict[str, QWidget] = {}
        self._current: str | None = None
        self._closed = False
        self._init_panels()

        # Link monitor: the client has no callback for a lost link, so poll it
        self._link_up = True                    # graphics.py hands over a connected client
        self._reconnect_attempt = 0             # attempts since the link went down
        self._reconnect_worker = None           # the attempt in progress (a widgets.Worker)
        self._next_attempt_at: float | None = None
        self._sweep_since: float | None = None  # when a burst-delay sweep was first seen
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._start_reconnect)
        self._link_timer = QTimer(self)
        self._link_timer.setInterval(self.LINK_POLL_MS)
        self._link_timer.timeout.connect(self._poll_link)
        self._link_timer.start()
        self._poll_link()

        self.show_panel('main')

    def _init_panels(self) -> None:
        from windows.position_window import PositionPanel
        from windows.calibration_window import CalibrationPanel
        from windows.arena_maker_window import ArenaMakerPanel
        from windows.barrier_drawer_window import BarrierDrawerPanel
        from windows.base_station_window import BaseStationPanel

        panels = [
            ('main',           MainMenuPanel(main_window=self, server=self.server)),
            ('position',       PositionPanel(
                                   client=self.client,
                                   position_lib=self.position_lib,
                                   settings_module=self.settings_module,
                                   main_window=self,
                                   server=self.server)),
            ('calibration',    CalibrationPanel(
                                   cal=self.cal,
                                   settings_module=self.settings_module,
                                   main_window=self)),
            ('arena_maker',    ArenaMakerPanel(
                                   settings_module=self.settings_module,
                                   main_window=self)),
            ('barrier_drawer', BarrierDrawerPanel(
                                   settings_module=self.settings_module,
                                   main_window=self)),
            ('base_station',   BaseStationPanel(
                                   client=self.client,
                                   settings_module=self.settings_module,
                                   main_window=self)),
        ]

        for name, panel in panels:
            self._stack.addWidget(panel)
            self._panels[name] = panel

    # ------------------------------------------------------------------
    # Panel contract
    # ------------------------------------------------------------------

    def show_panel(self, name: str) -> None:
        """Hide the current panel (stopping what it runs) and show ``name``.

        An unknown name is logged and ignored, and so is the panel already
        shown; a panel whose show or hide fails is reported, never fatal.
        """
        panel = self._panels.get(name)
        if panel is None:
            log.error("show_panel: no panel named %r", name)
            return
        if name == self._current or self._closed:
            return
        previous = self._panels.get(self._current)
        if previous is not None:
            self._call_panel(self._current, 'on_panel_hide')
        self._current = name
        self._stack.setCurrentWidget(panel)
        self._call_panel(name, 'on_panel_show')

    def current_panel(self) -> str | None:
        """Name of the panel on show."""
        return self._current

    def notify_config_changed(self) -> None:
        """Reload the configuration everywhere after a save, then emit
        ``config_changed``.

        Call it after every save that changes receivers, facings, offsets,
        calibration points or the origin.  The settings are re-read from disk,
        the calibration library follows the settings' (possibly switched)
        config path and the position library reloads its geometry and
        calibration.  A file that cannot be read is reported; the panels are
        told either way, so they redraw from what is loaded.
        """
        settings = self.settings_module
        problems = []
        try:
            settings.reload_config()
        except Exception as exc:          # unreadable or invalid file: keep what is loaded
            _log_reload_error(exc, "Could not reload %s", settings.config_path)
            problems.append(f"settings: {exc}")
        self.cal.config_path = settings.config_path
        try:
            self.position_lib.reload(settings.config_path)
        except Exception as exc:
            _log_reload_error(exc, "Could not reload the position library from %s",
                              settings.config_path)
            problems.append(f"position library: {exc}")
        if problems:
            widgets.error(self, "Configuration error",
                          f"Could not reload {settings.config_path}:\n" + "\n".join(problems))
        self.config_changed.emit()

    def _call_panel(self, name: str, method: str, report: bool = True) -> bool:
        """Call a panel's ``on_panel_show`` / ``on_panel_hide``; an exception is
        logged (and shown when ``report``) instead of aborting the application."""
        fn = getattr(self._panels[name], method, None)
        if fn is None:
            return True
        try:
            fn()
            return True
        except Exception as exc:
            log.exception("Panel %r: %s() failed", name, method)
            if report:
                widgets.error(self, "Panel error",
                              f"The {name.replace('_', ' ')} panel failed in {method}(): {exc}")
            return False

    # ------------------------------------------------------------------
    # Ground-station link monitor
    # ------------------------------------------------------------------

    def ground_address(self) -> str:
        """host:port of the ground station's command channel."""
        return f"{self.client.host}:{self.client.tcp_port}"

    def _poll_link(self) -> None:
        """Follow the client's link state (timer slot): emit ``link_changed`` on
        a change, start reconnecting when the link went down, refresh the
        indicator."""
        if self._closed:
            return
        up = bool(self.client.connected)
        if up != self._link_up:
            self._link_up = up
            if up:
                self._retry_timer.stop()
                self._reconnect_attempt = 0
                self._next_attempt_at = None
                log.info("Ground station link at %s is up again", self.ground_address())
            else:              # the client has logged why
                log.info("Reconnecting to the ground station at %s in the background",
                         self.ground_address())
                self._schedule_reconnect()
            self.link_changed.emit(up)
        self._update_link_indicator()

    def _schedule_reconnect(self) -> None:
        """Arm the next background reconnect attempt, backing off."""
        if self._closed or self._link_up or self._reconnect_worker is not None:
            return
        delays = self.RECONNECT_BACKOFF_S
        delay = delays[min(self._reconnect_attempt, len(delays) - 1)]
        self._next_attempt_at = time.monotonic() + delay
        self._retry_timer.start(int(delay * 1000))

    def _start_reconnect(self) -> None:
        """Try to reconnect on a worker thread (timer slot and "Reconnect now");
        never two attempts at once."""
        if self._closed or self._reconnect_worker is not None or self.client.connected:
            return
        self._retry_timer.stop()
        self._next_attempt_at = None
        self._reconnect_attempt += 1
        log.info("Reconnecting to the ground station at %s (attempt %d)",
                 self.ground_address(), self._reconnect_attempt)
        self._reconnect_worker = widgets.run_in_thread(
            self.client.reconnect, self.RECONNECT_TIMEOUT_S,
            on_error=self._on_reconnect_error, on_finished=self._on_reconnect_finished)
        self._update_link_indicator()

    def _on_reconnect_error(self, message: str) -> None:
        # reconnect() never raises for network problems; anything else is a bug
        log.error("Reconnect attempt failed unexpectedly: %s", message)

    def _on_reconnect_finished(self) -> None:
        self._reconnect_worker = None
        if self._closed:
            return
        if not self.client.connected:
            self._schedule_reconnect()
        self._poll_link()

    def _sweep_running(self) -> bool:
        """True while the Base Station panel's burst-delay sweep (D) runs: the
        ground station answers no other command until it ends."""
        running = getattr(self._panels.get('base_station'), 'delay_search_running', None)
        try:
            return bool(running()) if running is not None else False
        except Exception:                 # a panel bug must not stop the link monitor
            log.exception("delay_search_running() failed")
            return False

    def _update_link_indicator(self) -> None:
        client = self.client
        where = self.ground_address()
        sweeping = client.connected and self._sweep_running()
        if not sweeping:
            self._sweep_since = None
        elif self._sweep_since is None:
            self._sweep_since = time.monotonic()
        if sweeping:
            elapsed = time.monotonic() - self._sweep_since
            expected = client.expected_delay_search_s
            self._link_indicator.set_state(
                f"Ground station {where} connected — base station busy: burst-delay sweep "
                f"({elapsed:.0f} s of about {expected:.0f} s); other commands wait",
                widgets.AMBER,
                "The burst-delay sweep (D) started on the Base Station panel blocks the ground "
                "station until it ends: positioning, calibration and the other panels' commands "
                "wait meanwhile and may time out.")
            return
        if client.connected:
            timeouts = client.consecutive_timeouts
            if timeouts:
                plural = "" if timeouts == 1 else "s"
                self._link_indicator.set_state(
                    f"Ground station {where} connected — base station not answering "
                    f"({timeouts} timeout{plural})",
                    widgets.AMBER, client.last_error or "")
            else:
                self._link_indicator.set_state(
                    f"Ground station {where} connected", widgets.GREEN,
                    f"UltraGpsXY at {where} (TCP commands), UDP stream on port {client.udp_port}")
            return
        if self._reconnect_worker is not None:
            text = f"Ground station {where} LOST — reconnecting (attempt {self._reconnect_attempt})…"
        elif self._next_attempt_at is not None:
            wait = max(0, math.ceil(self._next_attempt_at - time.monotonic()))
            text = (f"Ground station {where} LOST — reconnecting "
                    f"(attempt {self._reconnect_attempt + 1} in {wait} s)…")
        else:
            text = f"Ground station {where} LOST"
        self._link_indicator.set_state(text, widgets.RED, client.last_error or "",
                                       can_reconnect=self._reconnect_worker is None)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._shutdown()
        event.accept()

    def _shutdown(self) -> None:
        """Stop everything the window started (once): the current panel's
        work, the link monitor, the ground-station link, background workers
        and the position-feed server."""
        if self._closed:
            return
        self._closed = True
        self._link_timer.stop()
        self._retry_timer.stop()
        if self._current is not None:
            self._call_panel(self._current, 'on_panel_hide', report=False)
        try:
            # Wakes any command still waiting for a reply (a D sweep, say), so
            # the workers waiting on one can end; waits for a reconnect attempt
            self.client.disconnect()
            worker = self._reconnect_worker
            if worker is not None:
                worker.wait(int((self.RECONNECT_TIMEOUT_S + 5.0) * 1000))
                self.client.disconnect()       # in case that attempt connected after all
        except Exception:
            log.exception("Error while disconnecting from the ground station")
        if not widgets.shutdown_workers():
            log.warning("Some background tasks did not stop in time")
        if self.server is not None:
            try:
                self.server.stop_all()
            except Exception:
                log.exception("Error while stopping the position-feed server")


class LinkIndicator(QWidget):
    """The ground-station link state for the status bar: a coloured dot, a
    line of text (details in its tooltip) and, while the link is down, a
    "Reconnect now" button."""

    reconnect_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        ui_scale.margins(layout, 10, 3, 10, 3)
        ui_scale.spacing(layout, 8)
        self._dot = QLabel("●")
        self._text = QLabel()
        self._reconnect = widgets.neon_button("Reconnect now", widgets.RED, "white",
                                              font_px=10, padding=(2, 10))
        self._reconnect.clicked.connect(self.reconnect_requested)
        self._reconnect.setVisible(False)
        layout.addWidget(self._dot)
        layout.addWidget(self._text, 1)
        layout.addWidget(self._reconnect)
        self._color = None
        self.set_state("", widgets.DIM)

    def set_state(self, text: str, color: str, tooltip: str = "",
                  can_reconnect: bool = False) -> None:
        """Show ``text`` in ``color``; the style is only re-applied when the
        colour changes (this is called on every link poll)."""
        if color != self._color:
            self._color = color
            ui_scale.style(self._dot, f"color: {color}; font: bold 15px Arial; "
                                      f"background: transparent;")
            ui_scale.style(self._text, f"color: {color}; font: bold 12px Arial; "
                                       f"background: transparent;")
        if text != self._text.text():
            self._text.setText(text)
        if tooltip != self.toolTip():
            self.setToolTip(tooltip)
        if can_reconnect != self._reconnect.isVisibleTo(self):
            self._reconnect.setVisible(can_reconnect)

    def text(self) -> str:
        return self._text.text()

    def color(self) -> str | None:
        return self._color


class ServerStatusWidget(QFrame):
    """A compact status panel that shows UltraGPSServer state."""

    def __init__(self, server=None):
        super().__init__()
        self._server = server
        self.setObjectName("serverStatus")
        ui_scale.style(self, "QFrame#serverStatus { background-color: #111111; "
                             "border: 1px solid #333333; border-radius: 4px; }")
        ui_scale.min_width(self, 420)

        layout = QVBoxLayout(self)
        ui_scale.margins(layout, 12, 8, 12, 8)
        ui_scale.spacing(layout, 4)

        title_lbl = QLabel("Server Status")
        ui_scale.style(title_lbl, "color: #FF8800; font: bold 13px Arial; background: transparent;")
        layout.addWidget(title_lbl)

        self._ip_label = QLabel("")
        self._tcp_label = QLabel("")
        self._udp_label = QLabel("")
        self._clients_label = QLabel("")
        for lbl in (self._ip_label, self._tcp_label, self._udp_label, self._clients_label):
            ui_scale.style(lbl, "color: #AAAAAA; font: 12px Arial; background: transparent;")
            layout.addWidget(lbl)
        self._streaming = None

        self._refresh()

        if self._server is not None:
            self._timer = QTimer(self)
            self._timer.setInterval(1500)
            self._timer.timeout.connect(self._refresh)
            self._timer.start()

    def _refresh(self) -> None:
        if self._server is None:
            self._ip_label.setText("Server not running")
            for lbl in (self._tcp_label, self._udp_label, self._clients_label):
                lbl.setVisible(False)
            return

        status = self._server.get_status()

        streaming = status['streaming']
        self._ip_label.setText(f"IP: {status['ip']}  |  Streaming: {'ON' if streaming else 'OFF'}")
        if streaming != self._streaming:
            self._streaming = streaming
            color = "#39FF14" if streaming else "#AAAAAA"
            ui_scale.style(self._ip_label, f"color: {color}; font: 12px Arial; background: transparent;")

        for proto, lbl in (('tcp', self._tcp_label), ('udp', self._udp_label)):
            parts = [f"{p['name']}:{p['port']} ({p['clients']} clients)"
                     for p in status['ports'] if p['proto'] == proto]
            lbl.setText(f"{proto.upper()}  " + "  |  ".join(parts) if parts else "")
            lbl.setVisible(bool(parts))

        self._clients_label.setText(f"Total connected: {status['total_clients']}")


class MainMenuPanel(QWidget):
    """Main menu with navigation buttons."""

    #: (label, panel name, button colour, text colour), top to bottom.
    BUTTONS = (
        ("Position",     'position',       widgets.GREEN,   'black'),
        ("Calibration",  'calibration',    widgets.MAGENTA, 'white'),
        ("Setup",        'arena_maker',    widgets.CYAN,    'black'),
        ("Barriers",     'barrier_drawer', widgets.ORANGE,  'black'),
        ("Base Station", 'base_station',   widgets.GOLD,    'black'),
    )

    def __init__(self, main_window: UltraGPSMainWindow, server=None):
        super().__init__()
        self._main_window = main_window
        self._server = server
        self.setObjectName("mainMenuPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("QWidget#mainMenuPanel { background-color: black; }")
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ui_scale.spacing(layout, 12)

        title = QLabel("Ultra GPS Control")
        ui_scale.font(title, 32, bold=True)
        title.setStyleSheet("color: #39FF14;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        status_widget = ServerStatusWidget(self._server)
        layout.addWidget(status_widget, alignment=Qt.AlignmentFlag.AlignCenter)

        for text, panel_name, bg, fg in self.BUTTONS:
            btn = widgets.neon_button(text, bg, fg, font_px=16, padding=(15, 40), radius=5)
            ui_scale.min_width(btn, 300)
            btn.clicked.connect(
                lambda checked=False, p=panel_name: self._main_window.show_panel(p))
            layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
