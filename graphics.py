"""UltraGPS Control — entry point.

Starts in this order:

1. Parse the command line and create the QApplication.  The UI scale is set
   from the screen first, so the startup dialog already has the size the main
   window will have.
2. Connect to the ground station program (UltraGpsXY).  Control can't run
   without it: when it is not running, cannot be reached, or another program
   holds its only client slot, the reason and how to start it are printed to
   stderr and shown in a dialog with Retry / Quit.  Quit exits with status 1.
3. Start the position-feed server (TCP 8000 and 8004, UDP 8001-8003).  When
   a port is taken -- usually by another UltraGPS Control -- that is shown and
   Control exits with status 1.
4. Open the main window with the connected client.  A config.toml that
   cannot be read (missing, unreadable or malformed) is reported the same way.

Ctrl+C in the terminal (SIGINT) or SIGTERM closes the window cleanly, as its
close button does.
"""

import argparse
import errno
import html
import logging
import signal
import socket
import sys
import time

import matplotlib
matplotlib.use('QtAgg')

from PyQt6.QtCore import QSocketNotifier, Qt, QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QVBoxLayout

from ultragps_client import GroundStationBusy, GroundStationError, UltraGPSClient
from ultragps_server import UltraGPSServer
from windows import ui_scale, widgets
from windows.main_window import UltraGPSMainWindow

log = logging.getLogger(__name__)

SERVER_CMD_TCP     = "cmd"
SERVER_POS_UDP     = "pos"
SERVER_NMEA_UDP    = "nmea"
SERVER_BARRIER_TCP = "barrier_tcp"
SERVER_BARRIER_UDP = "barrier"

#: Ports of the position feed this program serves: (name used in the code,
#: port, protocol).  The TCP ports come first: a second UltraGPS Control on
#: the same computer fails there.
SERVER_PORTS = (
    (SERVER_CMD_TCP,     8000, "tcp"),
    (SERVER_BARRIER_TCP, 8004, "tcp"),
    (SERVER_POS_UDP,     8001, "udp"),
    (SERVER_NMEA_UDP,    8002, "udp"),
    (SERVER_BARRIER_UDP, 8003, "udp"),
)

#: The ground station's default ports (UltraGpsXY -t / -u).
GROUND_TCP_PORT = 9000
GROUND_UDP_PORT = 9001

#: Seconds allowed for each attempt to connect to the ground station.
CONNECT_TIMEOUT_S = 3.0

#: Exit status when UltraGPS Control cannot start.
EXIT_FAILURE = 1

LEAD = "UltraGPS Control can't run without the ground station program (UltraGpsXY)."


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _port(text: str) -> int:
    """argparse type: a TCP/UDP port number."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 1..65535, got {port}")
    return port


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UltraGPS positioning system")
    parser.add_argument("--ip", default="127.0.0.1",
                        help="IP address (or host name) of the ground station program "
                             "UltraGpsXY (default 127.0.0.1)")
    parser.add_argument("--ground-tcp-port", type=_port, default=GROUND_TCP_PORT, metavar="PORT",
                        help=f"its TCP command port, UltraGpsXY -t (default {GROUND_TCP_PORT})")
    parser.add_argument("--ground-udp-port", type=_port, default=GROUND_UDP_PORT, metavar="PORT",
                        help=f"its UDP stream port, UltraGpsXY -u (default {GROUND_UDP_PORT})")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.toml (default: search ~/.config/ultragps-control, "
             "./config, then /etc/ultragps-control)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug messages")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Startup check: the ground station
# ---------------------------------------------------------------------------

def _start_command(client: UltraGPSClient) -> str:
    """Example command line that starts UltraGpsXY on the ports Control expects."""
    return f"./build/UltraGpsXY -s /dev/ttyACM0 -t {client.tcp_port} -u {client.udp_port}"


def ground_station_problem(error: GroundStationError, client: UltraGPSClient) -> dict:
    """What went wrong connecting to the ground station and what to do, as
    plain text: ``where``, ``reason`` and ``advice``; when UltraGpsXY is not
    running there also an example ``command`` that starts it and a ``hint``
    with the ``options`` that point UltraGPS Control elsewhere ("" otherwise)."""
    where = (f"Ground station: {client.host}:{client.tcp_port} (TCP commands), "
             f"UDP stream on port {client.udp_port}")
    reason = error.reason or str(error)
    if isinstance(error, GroundStationBusy):
        return {
            'where': where,
            'reason': f"Busy ({reason}): UltraGpsXY is running, but another program holds "
                      f"its only client slot.",
            'advice': "Close the other program (for example another UltraGPS Control), or "
                      "restart UltraGpsXY with a higher --max-clients, then retry.",
            'command': "", 'hint': "", 'options': "",
        }
    return {
        'where': where,
        'reason': f"Reason: {reason}.",
        'advice': "Start UltraGpsXY on the ground-station computer first, for example:",
        'command': _start_command(client),
        'hint': "If it runs on another computer or on other ports, start UltraGPS Control with",
        'options': "--ip HOST --ground-tcp-port PORT --ground-udp-port PORT",
    }


def report_ground_station_problem(error: GroundStationError, client: UltraGPSClient) -> None:
    """Explain on stderr why UltraGPS Control cannot start."""
    problem = ground_station_problem(error, client)
    lines = [LEAD, "  " + problem['where'], "  " + problem['reason'], "  " + problem['advice']]
    if problem['command']:
        lines.append("      " + problem['command'])
    if problem['hint']:
        lines.append(f"  {problem['hint']} {problem['options']}.")
    print("\n".join(lines), file=sys.stderr, flush=True)


def _try_connect(client: UltraGPSClient):
    """Connect ``client``; the GroundStationError, or None once connected
    (runs on a worker thread)."""
    try:
        client.connect(timeout=CONNECT_TIMEOUT_S)
        return None
    except GroundStationError as exc:
        return exc


class GroundStationDialog(QDialog):
    """UltraGPS Control can't start: the ground station did not answer.

    Shows why and how to start it.  Retry connects again on a worker thread
    (the dialog stays responsive and shows that it is trying) and accepts the
    dialog once connected; Quit -- or closing the dialog, Escape, Ctrl+C --
    rejects it.  A request to quit during an attempt takes effect when the
    attempt ends.
    """

    def __init__(self, client: UltraGPSClient, error: GroundStationError, parent=None):
        super().__init__(parent)
        self._client = client
        self._attempt = None              # the connection attempt in progress (a Worker)
        self._attempts = 1
        self._quit_requested = False
        self.setWindowTitle("UltraGPS Control: no ground station")
        self.setModal(True)
        ui_scale.style(self, "QDialog { background-color: black; }")

        layout = QVBoxLayout(self)
        ui_scale.margins(layout, 22, 18, 22, 16)
        ui_scale.spacing(layout, 10)

        self._heading = self._text_label(widgets.RED, 17, bold=True)
        self._heading.setText("No ground station")
        layout.addWidget(self._heading)
        lead = self._text_label("white", 13, bold=True)
        lead.setText(LEAD)
        ui_scale.min_width(lead, 480)
        layout.addWidget(lead)

        self._where = self._text_label(widgets.DIM, 12)
        self._reason = self._text_label(widgets.GOLD, 12, bold=True)
        self._advice = self._text_label("white", 12)
        self._advice.setTextFormat(Qt.TextFormat.RichText)
        for label in (self._where, self._reason, self._advice):
            layout.addWidget(label)

        self._status = self._text_label(widgets.DIM, 11)
        self._status.setVisible(False)
        layout.addWidget(self._status)
        self._busy = widgets.progress_bar(widgets.CYAN, busy=True)
        self._busy.setVisible(False)
        layout.addWidget(self._busy)

        row = QHBoxLayout()
        row.addStretch(1)
        self.retry_button = widgets.neon_button("Retry", widgets.GREEN, padding=(7, 26))
        self.retry_button.clicked.connect(self.retry)
        self.retry_button.setDefault(True)
        self.quit_button = widgets.neon_button("Quit", "#555555", "white", padding=(7, 26))
        self.quit_button.clicked.connect(self.reject)
        row.addWidget(self.retry_button)
        row.addWidget(self.quit_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.show_error(error)
        self.retry_button.setFocus()

    @staticmethod
    def _text_label(color: str, px: int, bold: bool = False) -> QLabel:
        label = QLabel()
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        ui_scale.style(label, f"color: {color}; font: {'bold ' if bold else ''}{px}px Arial; "
                              f"background: transparent;")
        return label

    def show_error(self, error: GroundStationError) -> None:
        """Explain ``error`` (the latest failed attempt)."""
        problem = ground_station_problem(error, self._client)
        busy = isinstance(error, GroundStationBusy)
        self._heading.setText("Ground station busy" if busy else "No ground station")
        self._where.setText(problem['where'])
        self._reason.setText(problem['reason'])
        code = f"<br><code style='color: {widgets.CYAN}; white-space: nowrap'>{{}}</code>"
        advice = html.escape(problem['advice'])
        if problem['command']:
            advice += code.format(html.escape(problem['command']))
        if problem['hint']:
            advice += f"<br>{html.escape(problem['hint'])}" + code.format(
                html.escape(problem['options']))
        self._advice.setText(advice)

    def is_trying(self) -> bool:
        """True while a connection attempt runs."""
        return self._attempt is not None

    def retry(self) -> None:
        """Try to connect again, on a worker thread."""
        if self._attempt is not None:
            return
        self._set_busy(True, f"Connecting to {self._client.host}:{self._client.tcp_port}…")
        self._attempt = widgets.run_in_thread(_try_connect, self._client,
                                              on_result=self._on_attempt_done,
                                              on_error=self._on_attempt_failed)

    def reject(self) -> None:
        if self._attempt is not None:        # quit once the attempt is over
            self._quit_requested = True
            self._set_status("Quitting when this attempt ends…")
            return
        super().reject()

    def _on_attempt_done(self, error) -> None:
        self._attempt = None
        if error is None:                    # connected
            if self._quit_requested:
                self._client.disconnect()
                super().reject()
            else:
                self.accept()
            return
        self._attempts += 1
        report_ground_station_problem(error, self._client)
        self.show_error(error)
        self._set_busy(False, f"Attempt {self._attempts} failed at {time.strftime('%H:%M:%S')}.")
        if self._quit_requested:
            super().reject()

    def _on_attempt_failed(self, message: str) -> None:
        # connect() raises nothing else for network problems; this is a bug
        self._attempt = None
        log.error("Connecting to the ground station failed unexpectedly: %s", message)
        self._set_busy(False, f"Attempt failed: {message}")
        if self._quit_requested:
            super().reject()

    def _set_busy(self, busy: bool, status: str) -> None:
        self._busy.setVisible(busy)
        self.retry_button.setEnabled(not busy)
        self._set_status(status)

    def _set_status(self, text: str) -> None:
        self._status.setText(text)
        self._status.setVisible(bool(text))


def connect_ground_station(client: UltraGPSClient, interrupts=None) -> bool:
    """Connect ``client`` to the ground station.  While that fails, say why
    (stderr and a dialog) and let the user retry.  True once connected, False
    if the user quit (or interrupted)."""
    try:
        client.connect(timeout=CONNECT_TIMEOUT_S)
        return True
    except GroundStationError as exc:
        error = exc
    report_ground_station_problem(error, client)
    if interrupts is not None and interrupts.requested:
        return False
    dialog = GroundStationDialog(client, error)
    if interrupts is not None:
        interrupts.target = dialog
    try:
        return dialog.exec() == int(QDialog.DialogCode.Accepted.value) and client.connected
    finally:
        if interrupts is not None:
            interrupts.target = None
        dialog.deleteLater()


# ---------------------------------------------------------------------------
# Position-feed server
# ---------------------------------------------------------------------------

class ServerStartError(RuntimeError):
    """The position-feed server could not open one of its ports."""


def start_server() -> UltraGPSServer:
    """Start the position-feed server on ``SERVER_PORTS``.

    Raises:
        ServerStartError: a port could not be opened (the message says which
            and why); the ports opened before it are closed again.
    """
    server = UltraGPSServer()
    for name, port, proto in SERVER_PORTS:
        try:
            if proto == "tcp":
                server.start_tcp_port(port, name=name)
            else:
                server.start_udp_port(port, name=name)
        except OSError as exc:
            server.stop_all()
            message = (f"UltraGPS Control could not open its {proto.upper()} port {port} "
                       f"on {server.bind_ip} for the position feed: {exc.strerror or exc}.")
            if exc.errno == errno.EADDRINUSE:
                message += (" Another UltraGPS Control (or another program) is already using "
                            "it on this computer: close it, then start UltraGPS Control again.")
            raise ServerStartError(message) from exc
    return server


# ---------------------------------------------------------------------------
# Ctrl+C / SIGTERM
# ---------------------------------------------------------------------------

class _Interrupts:
    """Close UltraGPS Control cleanly on Ctrl+C (SIGINT) or SIGTERM.

    Python runs a signal handler only when the interpreter gets control, which
    it does not while Qt's event loop waits in C++.  The signal module's wakeup
    file descriptor wakes the event loop (a QSocketNotifier), so the handler
    runs at once.  It closes the active modal dialog, if any, and ``target``
    (the startup dialog, then the main window) -- as their close buttons would.
    """

    SIGNALS = (signal.SIGINT, signal.SIGTERM)

    def __init__(self):
        self.target = None
        self.signum: int | None = None
        self._uninstalled = False
        self._handlers = {}
        self._old_wakeup_fd = None
        self._reader, self._writer = socket.socketpair()
        self._reader.setblocking(False)
        self._writer.setblocking(False)
        self._notifier = QSocketNotifier(self._reader.fileno(), QSocketNotifier.Type.Read)
        self._notifier.activated.connect(self._drain)
        try:
            self._old_wakeup_fd = signal.set_wakeup_fd(self._writer.fileno())
            for signum in self.SIGNALS:
                self._handlers[signum] = signal.signal(signum, self._on_signal)
        except ValueError:                   # not the main thread: keep Python's defaults
            log.debug("Signal handlers not installed (not on the main thread)")

    @property
    def requested(self) -> bool:
        """True once a signal asked UltraGPS Control to stop."""
        return self.signum is not None

    def exit_status(self, status: int) -> int:
        """The process exit status: 128 + the signal number after one, else ``status``."""
        return 128 + self.signum if self.signum is not None else status

    def uninstall(self) -> None:
        # A _close still queued (a signal while no event loop ran) does nothing
        self.target = None
        self._uninstalled = True
        for signum, handler in self._handlers.items():
            signal.signal(signum, handler)
        self._handlers.clear()
        if self._old_wakeup_fd is not None:
            signal.set_wakeup_fd(self._old_wakeup_fd)
            self._old_wakeup_fd = None
        self._notifier.setEnabled(False)
        self._notifier.deleteLater()
        self._reader.close()
        self._writer.close()

    def _drain(self) -> None:
        try:
            while self._reader.recv(256):
                pass
        except OSError:
            pass

    def _on_signal(self, signum, _frame) -> None:
        if self.signum is None:
            self.signum = signum
            print(f"\n{signal.Signals(signum).name}: closing UltraGPS Control…",
                  file=sys.stderr, flush=True)
        # Close from the event loop, not from whatever code the signal interrupted
        QTimer.singleShot(0, self._close)

    def _close(self) -> None:
        if self._uninstalled:
            return
        modal = QApplication.activeModalWidget()
        if modal is not None and modal is not self.target:
            modal.close()
        if self.target is not None:
            self.target.close()
        elif modal is None:
            QApplication.quit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _config_problem(path: str | None, exc: Exception) -> str:
    """Why the main window could not be built -- in practice a config.toml
    that is missing, unreadable or malformed (an empty file, a receiver
    without a position ...) -- for the user."""
    where = f" {path}" if path else ""
    if isinstance(exc, (OSError, ValueError)):          # TOMLDecodeError is a ValueError
        reason = str(exc) or type(exc).__name__
    else:                                               # e.g. KeyError: 'receivers'
        reason = f"{type(exc).__name__}: {exc}"
    return (f"UltraGPS Control could not read its configuration{where}: {reason.rstrip('.')}. "
            "Correct the file, or start UltraGPS Control with --config <another config.toml>.")


def _fail(message: str, interrupts: _Interrupts) -> int:
    """Report why UltraGPS Control cannot start (stderr and a dialog)."""
    print(message, file=sys.stderr, flush=True)
    if not interrupts.requested:
        widgets.error(None, "UltraGPS Control can't start", message)
    return EXIT_FAILURE


def _run(app: QApplication, args: argparse.Namespace, interrupts: _Interrupts) -> int:
    """Start everything in order; returns the exit status."""
    client = UltraGPSClient(host=args.ip, tcp_port=args.ground_tcp_port,
                            udp_port=args.ground_udp_port)
    if not connect_ground_station(client, interrupts):
        print("UltraGPS Control: quitting, the ground station is not available.",
              file=sys.stderr, flush=True)
        return EXIT_FAILURE

    try:
        server = start_server()
    except ServerStartError as exc:
        client.disconnect()
        return _fail(str(exc), interrupts)

    try:
        window = UltraGPSMainWindow(client, config_path=args.config, server=server)
    except Exception as exc:             # a missing, unreadable or malformed config.toml
        client.disconnect()
        server.stop_all()
        log.debug("Could not build the main window", exc_info=True)
        return _fail(_config_problem(args.config, exc), interrupts)
    except BaseException:
        client.disconnect()
        server.stop_all()
        raise

    interrupts.target = window
    status = 0
    if not interrupts.requested:         # else interrupted while the window was being built
        window.show()
        status = app.exec()
    window.close()           # stops everything, if the event loop ended another way
    return interrupts.exit_status(status)


def main(argv=None) -> None:
    """Run UltraGPS Control; exits the process with its status (1 when it
    cannot start)."""
    args = parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    app = QApplication.instance() or QApplication(sys.argv)
    # Dialogs shown before the main window exists get the scale it will have
    ui_scale.instance().set_factor(ui_scale.screen_factor())
    interrupts = _Interrupts()
    try:
        status = _run(app, args, interrupts)
    finally:
        widgets.shutdown_workers()
        interrupts.uninstall()
    sys.exit(status)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    main()
