"""UltraGPS Client Library — core module
=========================================
Standalone TCP/UDP client library for the UltraGPS ground station server
(``UltraGpsXY``, the UltraGPS-Ground program).

Provides a threaded client that sends commands via TCP and receives
continuous distance data via UDP, mirroring the protocol used by the
UltraGPS-Ground C++ server and the Arduino base-station firmware behind it.

Quick-start
-----------
    from ultragps_client import (UltraGPSClient, CommMode,
                                 GroundStationBusy, GroundStationUnavailable)

    client = UltraGPSClient(host='127.0.0.1', tcp_port=9000, udp_port=9001)
    try:
        client.connect(timeout=3.0)
    except GroundStationBusy:            # another program holds the only slot
        ...
    except GroundStationUnavailable:     # UltraGpsXY not running / unreachable
        ...

    # One-shot pulse — blocks until response or timeout
    counts = client.pulse()          # list[int] of 6 receiver counts, or None
    counts = client.simulate()       # same, using simulated data

    # Base-station settings (no argument = query)
    rate = client.trigger_rate()     # TriggerRate(hz=10, top=6249, sampling_ms=80)
    window = client.sampling_time(60)    # SamplingTime(ms=60, counts=3750)

    # Continuous streaming — data arrives via UDP
    client.continuous()
    assert client.mode == CommMode.CONTINUOUS
    latest = client.get_latest_reading()   # list[int] | None
    count = client.reading_count
    item = client.wait_for_reading(count, timeout=1.0)   # (count, ticks) | None

    if not client.connected:         # the ground station went away
        client.reconnect()

    client.disconnect()

Protocol reference
------------------
TCP (port 9000) carries commands and their replies:

* The server accepts ONE TCP client by default.  Any further connection
  receives ``"E: server full\\n"`` and is closed at once; ``connect()`` then
  raises :class:`GroundStationBusy`.
* A command is one ASCII letter, optionally followed by a space and an
  integer argument, terminated by ``'\\n'`` (``"P\\n"``, ``"H 80\\n"``).  The
  server treats each TCP segment it reads as ONE command, so two commands
  that arrive together are both discarded: the client runs one exchange at
  a time and spaces its sends at least 50 ms apart.
* Replies are bare: the base-station line is stripped of ``"\\r\\n"`` and
  sent with one ``send()``, WITHOUT a newline.  Several replies can arrive
  concatenated (e.g. after a late reply to an earlier command) and, rarely,
  one can be split across ``recv()`` calls.  The client therefore matches
  each reply against the pattern its command expects, drops any other text,
  and waits a short grace period before accepting a reply that ends in a
  number at the very end of the received data.  Replies are sent to every
  connected TCP client.
* Invalid commands get no reply, and the server drops every command while
  its serial link is not ready (for ~7 s after the port is (re)opened, e.g.
  after a baud change reboots the base station), so every call has a
  timeout.  A reply ``"ERR: <text>"`` comes from the server itself.

    ========  =================================  =========================================
    Command   Meaning                            Reply
    ========  =================================  =========================================
    P         one pulse                          N: t0, t1, t2, t3, t4, t5  (0 = no echo)
    S         one simulated pulse                N: t0, t1, t2, t3, t4, t5
    C         continuous mode                    none on TCP; "C: t0, ..., t5" on UDP
    R         reset the transmitter (radio)      N: R
    D         burst-delay sweep, ~16 s at        D: <delay_us>, <mean_ticks>, <valid>/<total>
              10 Hz; the server blocks
              meanwhile and returns only the
              final line
    E         store the default burst delay      E: <delay_us>                 (2000)
    M         reset all base-station settings    M: OK
    H <ms>    sampling window, 1 ms .. 80 % of   H: <ms>, <timer1_counts>      (ms*1000/16)
              the trigger period (out of range,
              e.g. "H 0", changes nothing = query)
    F <Hz>    trigger rate 1..50 Hz on both      F: <hz>, <timer1_top>, <sampling_ms>
              stations; sets H to its maximum
              (out of range, e.g. "F 0", = query)
    B <baud>  base-station baud rate             B: <baud>
              ("B 0" = query)                    or ERR: B <baud> unsupported by host
    ========  =================================  =========================================

Every command except ``C`` ends continuous mode.  A burst-delay change
(``D``, ``E``, ``M``) shifts every receiver's calibration intercept, so
recalibrate afterwards.  No command reads back the burst delay in force or
reports the firmware version / tick unit.

UDP (port 9001) carries the continuous stream: the client registers by
sending any datagram from its UDP socket, and while in continuous mode the
server sends one ``"C: t0, ..., t5"`` datagram per trigger period (10 Hz by
default, ``F`` changes it) to every registered peer.
"""

from __future__ import annotations

import codecs
import logging
import math
import operator
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

_RECEIVER_COUNT: int = 6
_DEFAULT_TIMEOUT: float = 2.0       # seconds — generous margin over server's 500ms serial timeout
_DEFAULT_CONNECT_TIMEOUT: float = 3.0
_DEFAULT_REPLY_GRACE: float = 0.04  # wait for more bytes when a reply may have been cut short
_UDP_REGISTER_PAYLOAD: bytes = b"register"  # content irrelevant; source addr registers client
_SERVER_FULL_MARKER: bytes = b"server full"  # the server sends "E: server full\n", then closes
_ERROR_MARKER: str = "ERR:"         # replies generated by the server itself
_BUSY_PROBE_S: float = 0.3          # connect(): listen this long for a "server full" rejection
_BUSY_PROBE_MAX_S: float = 1.0      # ... longer on a slow link (it arrives ~1 round trip later)
_MIN_COMMAND_GAP_S: float = 0.05    # the server reads one command per TCP segment
_SOCKET_POLL_S: float = 0.1         # socket timeout: recv-thread poll interval
_UDP_REREGISTER_MIN_S: float = 1.0  # first UDP re-registration after this long without data
_UDP_REREGISTER_MAX_S: float = 30.0 # ... backing off (doubling) up to this interval
_READING_BACKLOG: int = 128         # continuous readings kept for wait_for_reading()
_RX_BUFFER_LIMIT: int = 65536       # characters of unanswered TCP text kept at most
_THREAD_JOIN_S: float = 1.0
_RECONNECT_SETTLE_S: float = 0.05   # reconnect(): let the server notice the old link close
_RECONNECT_BUSY_RETRIES: int = 2    # ... it may still count the old link as a client
_RECONNECT_BUSY_WAIT_S: float = 0.3
_REPLY_MARGIN_S: float = 0.5        # serial and network slack on top of the trigger timing
_DELAY_SEARCH_MIN_TIMEOUT: float = 35.0
_DELAY_SEARCH_MARGIN_S: float = 15.0
_ARG_MAX: int = 999_999_999         # the server accepts at most 9 argument digits
_PORT_MAX: int = 65_535
_MAX_WAIT_S: float = threading.TIMEOUT_MAX   # longest wait the threading primitives accept


# ---------------------------------------------------------------------------
# Base-station firmware limits and defaults
# (base-station/src/settings-eeprom.h, ultrasonic-sampling.h, shared/radio/radio.h)
# ---------------------------------------------------------------------------

TRIGGER_RATE_MIN_HZ: int = 1         #: slowest trigger rate 'F' accepts
TRIGGER_RATE_MAX_HZ: int = 50        #: fastest trigger rate 'F' accepts
DEFAULT_TRIGGER_RATE_HZ: int = 10    #: trigger rate after 'M' / on a fresh board
SAMPLING_TIME_MIN_MS: int = 1        #: shortest sampling window 'H' accepts
SAMPLING_TIME_MAX_MS: int = 1000     #: absolute cap; 80 % of the trigger period is usually lower
DEFAULT_SAMPLING_TIME_MS: int = 80   #: sampling window after 'M' / on a fresh board
DEFAULT_BURST_DELAY_US: int = 2000   #: burst delay stored by 'E' and 'M'

_TIMER1_COUNTS_PER_S: int = 62_500   # 16 MHz / prescaler 256: one Timer1 count = 16 us
_TIMER1_US_PER_COUNT: int = 16
_SAMPLING_WINDOW_MAX_PERCENT: int = 80
_DELAY_SEARCH_STEPS: int = 51        # 0..500 us in 10 us steps
_DELAY_SEARCH_SAMPLES_PER_STEP: int = 3


def trigger_top(hz: int) -> int:
    """Return the Timer1 TOP value the firmware uses for a trigger rate.

    TOP = round(62500 / hz) - 1, so the trigger period is (TOP + 1) x 16 us.

    Args:
        hz: Trigger rate in Hz, TRIGGER_RATE_MIN_HZ..TRIGGER_RATE_MAX_HZ.

    Raises:
        ValueError: If *hz* is outside the accepted range.
    """
    hz = _check_int("hz", hz, TRIGGER_RATE_MIN_HZ, TRIGGER_RATE_MAX_HZ)
    return (_TIMER1_COUNTS_PER_S + hz // 2) // hz - 1


def max_sampling_time_ms(hz: int) -> int:
    """Return the longest sampling window 'H' accepts at a trigger rate.

    The window may last at most 80 % of the trigger period (80 ms at the
    default 10 Hz, 16 ms at 50 Hz) and never more than SAMPLING_TIME_MAX_MS.
    'F' sets the window to exactly this value.

    Args:
        hz: Trigger rate in Hz, TRIGGER_RATE_MIN_HZ..TRIGGER_RATE_MAX_HZ.

    Raises:
        ValueError: If *hz* is outside the accepted range.
    """
    cap_counts = (trigger_top(hz) + 1) * _SAMPLING_WINDOW_MAX_PERCENT // 100
    return min(SAMPLING_TIME_MAX_MS, cap_counts * _TIMER1_US_PER_COUNT // 1000)


def _delay_search_seconds(hz: int, sampling_ms: int) -> float:
    """Expected duration of a 'D' sweep, computed the way the server does.

    Each of the 51 steps measures 3 pulses; every pulse waits for a trigger
    edge and then samples for the sampling window, rounded up to whole
    trigger periods.
    """
    period_ms = (1000 + hz - 1) // hz
    sample_ms = math.ceil(sampling_ms / period_ms) * period_ms
    step_ms = _DELAY_SEARCH_SAMPLES_PER_STEP * max(sample_ms, period_ms)
    return _DELAY_SEARCH_STEPS * step_ms / 1000.0


def _check_int(name: str, value: object, low: int, high: int) -> int:
    """Return *value* as an int if it is an integer within [low, high].

    bool is rejected even though it is an int subclass; any other integer
    type (e.g. numpy.int64) is accepted.

    Raises:
        ValueError: If *value* is not an integer or is out of range.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    try:
        number = operator.index(value)
    except TypeError:
        raise ValueError(f"{name} must be an integer, got {value!r}") from None
    if not low <= number <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {number}")
    return number


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class GroundStationError(ConnectionError):
    """The ground station program (UltraGpsXY) cannot be used.

    Base class of the errors ``UltraGPSClient.connect()`` raises; ``str(exc)``
    is a complete, user-presentable message that names host:port.

    Attributes:
        host:   Host the client tried to reach.
        port:   TCP command port the client tried to reach.
        reason: Short reason, e.g. ``"connection refused"`` or ``"server full"``.
    """

    def __init__(self, message: str, host: str = "", port: int = 0, reason: str = "") -> None:
        super().__init__(message)
        self.host = host
        self.port = port
        self.reason = reason


class GroundStationUnavailable(GroundStationError):
    """The ground station refused the connection, is unreachable, or did not
    answer within the connect timeout — UltraGpsXY is not running there.
    Also raised when the host name cannot be resolved or is malformed, or a
    port is outside 1..65535, so one ``except`` covers every bad address."""


class GroundStationBusy(GroundStationError):
    """The ground station turned the connection away ("E: server full"):
    another client already holds its only TCP slot."""


# ---------------------------------------------------------------------------
# Parsed replies
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DelaySearchResult:
    """Result of a burst-delay sweep ('D').

    Attributes:
        delay_us:   Best burst delay found, in microseconds (most valid
                    readings, then lowest mean).  0 means the 1 us step won
                    (a zero delay is reserved, so 1 us was sent and stored).
        mean_ticks: Mean tick count of the valid readings at that delay.
        valid:      Valid readings (echoes heard) at that delay.
        total:      Readings taken per step (3 pulses x 6 receivers = 18).
    """

    delay_us: int
    mean_ticks: int
    valid: int
    total: int

    @property
    def stored(self) -> bool:
        """True if the firmware stored and applied the delay.

        Nothing is stored when no step produced a single valid reading.
        """
        return self.valid > 0


@dataclass(frozen=True)
class SamplingTime:
    """Sampling window in force, as reported by 'H'.

    Attributes:
        ms:     Sampling window in milliseconds.
        counts: The same window in Timer1 counts (16 us each).
    """

    ms: int
    counts: int


@dataclass(frozen=True)
class TriggerRate:
    """Trigger rate in force, as reported by 'F'.

    Attributes:
        hz:          Trigger rate in Hz (pulses per second on both stations).
        top:         Timer1 TOP; the trigger period is (top + 1) x 16 us.
        sampling_ms: Sampling window in force, in milliseconds.
    """

    hz: int
    top: int
    sampling_ms: int

    @property
    def period_ms(self) -> float:
        """Exact trigger period in milliseconds."""
        return (self.top + 1) * _TIMER1_US_PER_COUNT / 1000.0


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CommandType(Enum):
    """Enumerates every command the UltraGPS-Ground server accepts.

    Maps 1-to-1 with the Arduino base-station command table:
        PULSE          → 'P'  (trigger one ultrasonic measurement)
        SIMULATE       → 'S'  (simulated measurement, no real hardware needed)
        CONTINUOUS     → 'C'  (switch to continuous streaming mode)
        RESET          → 'R'  (reset the transmitter station over the radio)
        DELAY_SEARCH   → 'D'  (sweep the burst delay, store the best one)
        DELAY_DEFAULT  → 'E'  (store the default burst delay)
        SETTINGS_RESET → 'M'  (reset every base-station setting to its default)
        SAMPLING_TIME  → 'H'  ('H <ms>' set / query the sampling window)
        TRIGGER_RATE   → 'F'  ('F <Hz>' set / query the trigger rate)
        BAUD_RATE      → 'B'  ('B <baud>' set / query the base-station baud rate)
    """
    PULSE = "pulse"
    SIMULATE = "simulate"
    CONTINUOUS = "continuous"
    RESET = "reset"
    DELAY_SEARCH = "delay_search"
    DELAY_DEFAULT = "delay_default"
    SETTINGS_RESET = "settings_reset"
    SAMPLING_TIME = "sampling_time"
    TRIGGER_RATE = "trigger_rate"
    BAUD_RATE = "baud_rate"

    @property
    def letter(self) -> str:
        """Command letter sent on the wire, e.g. ``'P'``."""
        return _COMMANDS[self].letter


class CommMode(Enum):
    """Tracks the current communication mode of the client.

    NORMAL     — one-shot commands; responses arrive via TCP.
    CONTINUOUS — streaming mode (C); data arrives continuously via UDP.
    """
    NORMAL = "normal"
    CONTINUOUS = "continuous"


# ---------------------------------------------------------------------------
# Private command table  (mirrors the base-station command table)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _CommandSpec:
    """Wire format of one command and of the reply it expects."""

    letter: str                       # command letter sent over TCP
    marker: Optional[str] = None      # reply marker, e.g. "N:"; None = no TCP reply
    reply: Optional[re.Pattern] = None  # expected reply, or "ERR: <text>" (group "err")


def _spec(letter: str, marker: Optional[str] = None, body: str = "") -> _CommandSpec:
    """Build a command spec whose reply is "<marker> <body>" or "ERR: <text>"."""
    if marker is None:
        return _CommandSpec(letter)
    pattern = re.compile(
        rf"(?:{re.escape(marker)} *{body})|(?:{_ERROR_MARKER} *(?P<err>[^\r\n]*))",
        re.ASCII,
    )
    return _CommandSpec(letter, marker, pattern)


_TICKS_BODY: str = r" *, *".join([r"(-?\d+)"] * _RECEIVER_COUNT)

#: Maps each CommandType to its wire letter and expected reply.  Commands are
#: sent as "<letter>[ <int>]\n"; the newline acts as the command terminator on
#: the Arduino side, and the server strips trailing whitespace before checking.
_COMMANDS: dict[CommandType, _CommandSpec] = {
    CommandType.PULSE:          _spec("P", "N:", _TICKS_BODY),
    CommandType.SIMULATE:       _spec("S", "N:", _TICKS_BODY),
    CommandType.CONTINUOUS:     _spec("C"),
    CommandType.RESET:          _spec("R", "N:", r"R"),
    CommandType.DELAY_SEARCH:   _spec("D", "D:", r"(\d+) *, *(\d+) *, *(\d+) */ *(\d+)"),
    CommandType.DELAY_DEFAULT:  _spec("E", "E:", r"(\d+)"),
    CommandType.SETTINGS_RESET: _spec("M", "M:", r"OK"),
    CommandType.SAMPLING_TIME:  _spec("H", "H:", r"(\d+) *, *(\d+)"),
    CommandType.TRIGGER_RATE:   _spec("F", "F:", r"(\d+) *, *(\d+) *, *(\d+)"),
    CommandType.BAUD_RATE:      _spec("B", "B:", r"(\d+)"),
}

#: One continuous-mode UDP datagram.
_CONTINUOUS_DATAGRAM = re.compile(r"C: *" + _TICKS_BODY, re.ASCII)


def _ints(match: re.Match, count: int) -> list[int]:
    """Return the first *count* groups of a reply match as ints."""
    return [int(match.group(i)) for i in range(1, count + 1)]


def _close_quietly(sock: Optional[socket.socket]) -> None:
    """Shut down and close a socket, ignoring errors (it may be dead already)."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _enable_keepalive(sock: socket.socket) -> None:
    """Make a silently vanished server (host off, Wi-Fi gone) show up as a
    socket error within about 10 s instead of never.  Best effort; options
    that the platform lacks are skipped."""
    options = (
        (socket.SOL_SOCKET, "SO_KEEPALIVE", 1),
        (socket.IPPROTO_TCP, "TCP_KEEPIDLE", 5),       # idle seconds before probing
        (socket.IPPROTO_TCP, "TCP_KEEPINTVL", 2),      # seconds between probes
        (socket.IPPROTO_TCP, "TCP_KEEPCNT", 3),        # failed probes before giving up
        (socket.IPPROTO_TCP, "TCP_USER_TIMEOUT", 10_000),  # ms unacknowledged data may wait
    )
    for level, name, value in options:
        option = getattr(socket, name, None)
        if option is None:
            continue
        try:
            sock.setsockopt(level, option, value)
        except OSError:
            pass


def _describe_connect_error(exc: Exception, timeout: float) -> str:
    """Turn a socket.create_connection() error into a short reason."""
    if isinstance(exc, UnicodeError):
        # The IDNA codec rejects an empty or overlong label, e.g. "192.168..1"
        return f"invalid host name ({exc})"
    if isinstance(exc, socket.gaierror):
        return f"cannot resolve the host name ({exc.strerror or exc})"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused (UltraGpsXY is not running there)"
    if isinstance(exc, TimeoutError):
        return f"no answer within {timeout:g} s"
    return exc.strerror or str(exc)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class UltraGPSClient:
    """Threaded TCP/UDP client for the UltraGPS ground station server.

    Lifecycle is manual: call connect() before use, disconnect() when done.
    disconnect() is idempotent — safe to call before connect() or twice.
    If the server goes away, ``connected`` turns False and reconnect()
    re-establishes the link.

    Every public method is thread-safe.  Only one command/reply exchange
    runs at a time: a call waits for its turn, and the wait counts towards
    its timeout.

    Failure model: calling a command before connect() ever succeeded raises
    RuntimeError, as it always did.  After that, commands never raise on I/O
    problems: they return None (False for the bool commands) on a timeout,
    on an ``"ERR: ..."`` reply or while the link is down — at once in the
    last case — and record why in ``last_error``.

    Args:
        host:        IP address (or host name) of the UltraGPS-Ground server.
        tcp_port:    TCP port for command/response channel (default 9000).
        udp_port:    UDP port for continuous data stream (default 9001).
        timeout:     Seconds to wait for a normal command response (default
                     2.0).  The default wait is never shorter than two
                     trigger periods + the sampling window + 0.5 s (0.78 s at
                     the default 10 Hz / 80 ms; the settings last reported by
                     F, H or M are used), so it stays long enough at slow
                     trigger rates.  Per-call timeouts override it.
        reply_grace: Seconds to wait for more bytes before accepting a reply
                     that might have been cut short (default 0.04).
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        tcp_port: int = 9000,
        udp_port: int = 9001,
        timeout: float = _DEFAULT_TIMEOUT,
        reply_grace: float = _DEFAULT_REPLY_GRACE,
    ) -> None:
        self._host = host
        self._tcp_port = int(tcp_port)
        self._udp_port = int(udp_port)
        self._timeout = float(timeout)
        self._reply_grace = max(0.0, float(reply_grace))

        # Sockets — created in connect(), closed in disconnect()
        self._tcp_sock: Optional[socket.socket] = None
        self._udp_sock: Optional[socket.socket] = None
        self._udp_addr: Optional[tuple] = None   # where UDP registrations go
        # Local UDP port, reused by every reconnect: the server never forgets a
        # UDP peer, so a new port per link would leave it a dead peer each time
        self._udp_local_port: int = 0

        # Thread control.  _link_gen is bumped by every connect()/disconnect();
        # a recv thread only acts while the generation it was started with is
        # current, so a thread of an old link can never touch a newer one.
        self._running: bool = False   # sockets/threads allocated
        self._tcp_thread: Optional[threading.Thread] = None
        self._udp_thread: Optional[threading.Thread] = None
        self._lifecycle_lock = threading.RLock()   # connect/disconnect/reconnect
        self._state_lock = threading.Lock()        # _connected + _link_gen
        self._link_gen: int = 0

        # Link state.  _link_error says why the link is down (None while up).
        self._connected: bool = False
        self._ever_connected: bool = False
        self._last_error: Optional[str] = None
        self._link_error: Optional[str] = None

        # Command exchange.  The command lock admits one exchange at a time.
        # While an exchange is active the TCP recv thread appends incoming text
        # to _rx_buf and notifies _rx_cond (bumping _rx_seq); text arriving
        # between exchanges is unsolicited and dropped.
        self._command_lock = threading.Lock()
        self._rx_cond = threading.Condition()
        self._rx_buf: str = ""
        self._rx_active: bool = False
        self._rx_seq: int = 0
        self._last_send: float = 0.0               # monotonic time of the last command
        self._consecutive_timeouts: int = 0

        # Continuous readings — written by the UDP recv thread, read by callers.
        # _readings keeps (count, ticks) for wait_for_reading(); _reading_count
        # numbers readings over the client's lifetime and is never reset.
        self._reading_cond = threading.Condition()
        self._latest_reading: Optional[list[int]] = None
        self._readings: deque[tuple[int, list[int]]] = deque(maxlen=_READING_BACKLOG)
        self._reading_count: int = 0
        self._last_data_time: float = 0.0          # last reading or (re)registration
        self._reregister_interval: float = _UDP_REREGISTER_MIN_S

        # Communication mode
        self._mode: CommMode = CommMode.NORMAL

        # Base-station settings as last reported in a reply (None = not yet)
        self._trigger_rate_hz: Optional[int] = None
        self._sampling_time_ms: Optional[int] = None
        self._burst_delay_us: Optional[int] = None

    # -----------------------------------------------------------------------
    # Public lifecycle
    # -----------------------------------------------------------------------

    def connect(self, timeout: float = _DEFAULT_CONNECT_TIMEOUT) -> None:
        """Connect to the ground station and start the recv threads.

        Opens the TCP command link (waiting at most *timeout* seconds), then
        listens for 0.3 s for an immediate ``"E: server full"`` or a closed
        connection — the server accepts one TCP client by default and turns
        any other away like that.  On a slow link (the TCP connect took more
        than ~0.1 s) it listens up to 1 s, since the rejection arrives about
        one round trip later.  Only then does it register the UDP socket for
        the continuous stream and start the two recv threads.  The client
        starts in NORMAL mode, and ``last_error`` is cleared.

        After a lost link (``connected`` is False) connect() may be called
        again directly; the old sockets are cleaned up first.

        Args:
            timeout: Seconds allowed for the TCP connect (default 3.0).  The
                     "server full" check comes on top.

        Raises:
            GroundStationUnavailable: Connection refused, host unreachable,
                unknown or malformed, a port outside 1..65535, or no answer
                within *timeout* (UltraGpsXY is not running there).
            GroundStationBusy: The server answered "E: server full" or closed
                the connection at once: another client holds its only slot.
            RuntimeError: If already connected.
            ValueError:   If *timeout* is not positive.
        """
        timeout = float(timeout)
        if not timeout > 0:
            raise ValueError(f"timeout must be positive, got {timeout}")

        with self._lifecycle_lock:
            if self._connected:
                raise RuntimeError("Already connected. Call disconnect() first.")
            if self._running:
                self._teardown()   # clean up after a lost link

            for name, port in (("TCP", self._tcp_port), ("UDP", self._udp_port)):
                if not 0 < port <= _PORT_MAX:
                    raise self._unavailable(f"invalid {name} port {port} (must be 1..{_PORT_MAX})")
            started = time.monotonic()
            try:
                tcp = socket.create_connection((self._host, self._tcp_port), timeout=timeout)
            except (OSError, UnicodeError) as exc:
                raise self._unavailable(_describe_connect_error(exc, timeout)) from exc
            # "server full" comes about one round trip after the connect returns
            probe_s = min(_BUSY_PROBE_MAX_S,
                          max(_BUSY_PROBE_S, 2.0 * (time.monotonic() - started) + 0.05))
            udp: Optional[socket.socket] = None
            try:
                self._refuse_if_busy(tcp, probe_s)
                # The short timeout lets the recv thread notice disconnect();
                # sends of a few bytes never wait for buffer space that long
                tcp.settimeout(_SOCKET_POLL_S)
                tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                _enable_keepalive(tcp)
                peer = tcp.getpeername()
                # Register from a UDP socket of the same family, sending to the
                # address the TCP link resolved to (works with host names too)
                udp = socket.socket(tcp.family, socket.SOCK_DGRAM)
                udp.settimeout(_SOCKET_POLL_S)
                if self._udp_local_port:
                    try:
                        udp.bind(("", self._udp_local_port))
                    except OSError:
                        pass   # taken meanwhile: the first send picks a new port
            except GroundStationError:
                _close_quietly(tcp)
                raise
            except OSError as exc:
                _close_quietly(tcp)
                _close_quietly(udp)
                raise self._unavailable(f"cannot set up the link ({exc.strerror or exc})") from exc

            with self._rx_cond:
                self._rx_buf = ""
                self._rx_active = False
            with self._reading_cond:
                self._mode = CommMode.NORMAL
                self._latest_reading = None
                self._readings.clear()
            with self._state_lock:
                self._link_gen += 1
                gen = self._link_gen
                self._tcp_sock = tcp
                self._udp_sock = udp
                self._udp_addr = (peer[0], self._udp_port) + tuple(peer[2:])
                self._running = True
                self._connected = True
            self._ever_connected = True
            self._last_error = None
            self._link_error = None
            self._consecutive_timeouts = 0
            self._last_send = 0.0

            # Register with the server's UDP TX list by sending any packet
            self._register_udp()
            try:
                self._udp_local_port = udp.getsockname()[1]
            except OSError:
                pass

            # Start exactly 2 daemon threads
            self._tcp_thread = threading.Thread(
                target=self._tcp_recv_loop, args=(tcp, gen),
                daemon=True, name="ultragps-tcp-recv",
            )
            self._udp_thread = threading.Thread(
                target=self._udp_recv_loop, args=(udp, gen),
                daemon=True, name="ultragps-udp-recv",
            )
            self._tcp_thread.start()
            self._udp_thread.start()
            logger.info("Connected to the ground station at %s", self._where())

    def disconnect(self) -> None:
        """Close all connections and stop recv threads. Idempotent.

        A command waiting for its reply returns None at once.  The mode goes
        back to NORMAL; ``reading_count`` keeps its value.
        """
        with self._lifecycle_lock:
            self._teardown()

    def reconnect(self, timeout: float = _DEFAULT_CONNECT_TIMEOUT) -> bool:
        """Drop the current link (if any) and connect again.

        Unlike connect() this never raises for connection problems.  Right
        after the old link closes the server may still count it as a client,
        so a "server full" answer is retried twice, 0.3 s apart.  The client
        comes back in NORMAL mode: call continuous() again to resume
        streaming.

        Args:
            timeout: Seconds allowed for each TCP connect (default 3.0).

        Returns:
            True if connected, False otherwise (the reason is in last_error).
        """
        with self._lifecycle_lock:
            was_connected = self._connected
            self._teardown()
            if was_connected:
                time.sleep(_RECONNECT_SETTLE_S)
            for attempt in range(1 + _RECONNECT_BUSY_RETRIES):
                if attempt:
                    time.sleep(_RECONNECT_BUSY_WAIT_S)
                try:
                    self.connect(timeout)
                    return True
                except GroundStationBusy:
                    continue
                except GroundStationError:
                    return False
            return False

    # -----------------------------------------------------------------------
    # Public command API
    # -----------------------------------------------------------------------

    def pulse(self, timeout: Optional[float] = None) -> Optional[list[int]]:
        """Trigger a single ultrasonic measurement and return the 6 counts.

        Blocks until the server responds or the timeout expires.
        If currently in CONTINUOUS mode, transitions back to NORMAL first
        (the server handles this automatically when it receives 'P').

        Args:
            timeout: Seconds to wait (default: the client's reply timeout).

        Returns:
            list[int] of 6 receiver tick counts (0 = no echo), or None on
            timeout/error.

        Raises:
            RuntimeError: If connect() never succeeded.
        """
        match = self._command(CommandType.PULSE, timeout=timeout)
        return None if match is None else _ints(match, _RECEIVER_COUNT)

    def simulate(self, timeout: Optional[float] = None) -> Optional[list[int]]:
        """Trigger a simulated measurement and return the 6 counts.

        Identical to pulse() but uses the server's simulation mode (no
        real ultrasonic hardware required).

        Args:
            timeout: Seconds to wait (default: the client's reply timeout).

        Returns:
            list[int] of 6 receiver tick counts, or None on timeout/error.

        Raises:
            RuntimeError: If connect() never succeeded.
        """
        match = self._command(CommandType.SIMULATE, timeout=timeout)
        return None if match is None else _ints(match, _RECEIVER_COUNT)

    def continuous(self, timeout: Optional[float] = None) -> bool:
        """Switch the server to continuous streaming mode.

        Does not wait for data (there is no TCP reply).  After this call the
        server streams one reading per trigger period via UDP; read them
        with wait_for_reading() (every reading, in order) or
        get_latest_reading().  The UDP registration is re-sent first, and
        readings from before this call are discarded.

        Args:
            timeout: Seconds to wait for a command in progress to finish
                     (default: the client's reply timeout).

        Returns:
            True if the command was sent, False if the link is down or
            another command did not finish in time.

        Raises:
            RuntimeError: If connect() never succeeded.
        """
        self._require_connected_once()
        line = CommandType.CONTINUOUS.letter
        budget = self._reply_timeout(timeout)
        if not self._command_lock.acquire(timeout=budget):
            self._fail(f"'{line}' not sent: another command was still running after {budget:g} s")
            return False
        try:
            gen = self._current_link()
            if gen is None:
                self._fail(f"'{line}' not sent: {self._link_down_reason()}")
                return False
            self._switch_mode(CommMode.CONTINUOUS)
            self._register_udp()
            if not self._send_line(line, gen):
                self._switch_mode(CommMode.NORMAL)
                return False
            return True
        finally:
            self._command_lock.release()

    def reset_transmitter(self, timeout: Optional[float] = None) -> bool:
        """Reset the transmitter station over the radio link ('R').

        The reset packet is repeated because the radio link has no
        acknowledgement, so a True result confirms the base station sent it,
        not that the transmitter received it.

        Args:
            timeout: Seconds to wait (default: the client's reply timeout).

        Returns:
            True on the reply "N: R", False on timeout/error.

        Raises:
            RuntimeError: If connect() never succeeded.
        """
        return self._command(CommandType.RESET, timeout=timeout) is not None

    def delay_search(self, timeout: Optional[float] = None) -> Optional[DelaySearchResult]:
        """Run the burst-delay sweep ('D') and return its result.

        The base station tries every burst delay from 0 to 500 us in 10 us
        steps (3 pulses each), then stores and applies the delay with the
        most valid readings (ties: lowest mean tick count).  The sweep takes
        51 x 3 trigger periods (~16 s at 10 Hz, see expected_delay_search_s)
        and the server answers nothing else meanwhile, so other commands
        time out.  Ends continuous mode.  Changing the burst delay shifts
        every receiver's calibration intercept: recalibrate afterwards.

        Args:
            timeout: Seconds to wait.  Default: 35 s, or longer when the last
                     known trigger rate makes the sweep slower (expected
                     duration + 15 s).

        Returns:
            DelaySearchResult, or None on timeout/error.

        Raises:
            RuntimeError: If connect() never succeeded.
        """
        if timeout is None:
            timeout = max(_DELAY_SEARCH_MIN_TIMEOUT,
                          self.expected_delay_search_s + _DELAY_SEARCH_MARGIN_S)
        match = self._command(CommandType.DELAY_SEARCH, timeout=timeout)
        if match is None:
            return None
        result = DelaySearchResult(*_ints(match, 4))
        if result.stored:
            # A winning 0 us step is stored as 1 us (zero is reserved)
            self._burst_delay_us = max(1, result.delay_us)
        return result

    def default_delay(self, timeout: Optional[float] = None) -> Optional[int]:
        """Store the default burst delay ('E') and return the delay in force.

        Replaces a delay found by delay_search(); the default (2000 us) stays
        in force across reboots until the next sweep.  Shifts every
        receiver's calibration intercept like any burst-delay change.

        Args:
            timeout: Seconds to wait (default: the client's reply timeout).

        Returns:
            The burst delay now in force, in microseconds, or None on
            timeout/error.

        Raises:
            RuntimeError: If connect() never succeeded.
        """
        match = self._command(CommandType.DELAY_DEFAULT, timeout=timeout)
        if match is None:
            return None
        self._burst_delay_us = _ints(match, 1)[0]
        return self._burst_delay_us

    def sampling_time(self, ms: Optional[int] = None,
                      timeout: Optional[float] = None) -> Optional[SamplingTime]:
        """Set or query the echo sampling window ('H').

        The firmware accepts 1 ms up to 80 % of the trigger period (see
        max_sampling_time_ms()).  A value out of that range — including 0,
        which is what None sends — changes nothing, and the reply reports the
        window still in force; compare it with *ms* to see whether the change
        was applied.  The window bounds the farthest measurable distance.

        Args:
            ms:      New sampling window in milliseconds, or None to query.
            timeout: Seconds to wait (default: the client's reply timeout).

        Returns:
            SamplingTime in force, or None on timeout/error.

        Raises:
            ValueError:   If *ms* is not an integer >= 0 (nothing is sent).
            RuntimeError: If connect() never succeeded.
        """
        arg = 0 if ms is None else _check_int("ms", ms, 0, _ARG_MAX)
        match = self._command(CommandType.SAMPLING_TIME, arg, timeout)
        if match is None:
            return None
        result = SamplingTime(*_ints(match, 2))
        self._sampling_time_ms = result.ms
        return result

    def trigger_rate(self, hz: Optional[int] = None,
                     timeout: Optional[float] = None) -> Optional[TriggerRate]:
        """Set or query the trigger rate ('F').

        A rate of 1..50 Hz is applied on both stations (the transmitter
        station reboots into it) and sets the sampling window to its maximum,
        80 % of the new period.  In continuous mode the stream then arrives
        at the new rate.  A value out of range — including 0, which is what
        None sends — changes nothing, and the reply reports the settings in
        force.

        Args:
            hz:      New trigger rate in Hz, or None to query.
            timeout: Seconds to wait (default: the client's reply timeout).

        Returns:
            TriggerRate in force, or None on timeout/error.

        Raises:
            ValueError:   If *hz* is not an integer >= 0 (nothing is sent).
            RuntimeError: If connect() never succeeded.
        """
        arg = 0 if hz is None else _check_int("hz", hz, 0, _ARG_MAX)
        match = self._command(CommandType.TRIGGER_RATE, arg, timeout)
        if match is None:
            return None
        result = TriggerRate(*_ints(match, 3))
        self._trigger_rate_hz = result.hz
        self._sampling_time_ms = result.sampling_ms
        return result

    def baud_rate(self, baud: Optional[int] = None,
                  timeout: Optional[float] = None) -> Optional[int]:
        """Set or query the base station's serial baud rate ('B').

        A rate the firmware accepts is stored and the base station reboots
        into it; the server follows on its side and drops commands for ~7 s
        while the board boots.  The server refuses rates its host cannot open
        (it supports 9600, 19200, 38400, 57600 and 115200) with
        ``"ERR: B <baud> unsupported by host"``.  Any other value — including
        0, which is what None sends — is a query.

        Args:
            baud:    New baud rate, or None to query.
            timeout: Seconds to wait (default: the client's reply timeout).

        Returns:
            The baud rate the base station uses after the command, or None on
            timeout/error.  On an ``ERR:`` reply ``last_error`` holds the
            whole reply text, e.g. "ERR: B 250000 unsupported by host".

        Raises:
            ValueError:   If *baud* is not an integer >= 0 (nothing is sent).
            RuntimeError: If connect() never succeeded.
        """
        arg = 0 if baud is None else _check_int("baud", baud, 0, _ARG_MAX)
        match = self._command(CommandType.BAUD_RATE, arg, timeout)
        return None if match is None else _ints(match, 1)[0]

    def reset_settings(self, timeout: Optional[float] = None) -> bool:
        """Reset every base-station setting stored in EEPROM to its default ('M').

        Restores 10 Hz, an 80 ms sampling window, the 2000 us burst delay
        (which shifts the calibration intercepts) and 38400 baud; if the baud
        rate changes the base station reboots and the server drops commands
        for ~7 s.

        Args:
            timeout: Seconds to wait (default: the client's reply timeout).

        Returns:
            True on the reply "M: OK", False on timeout/error.

        Raises:
            RuntimeError: If connect() never succeeded.
        """
        if self._command(CommandType.SETTINGS_RESET, timeout=timeout) is None:
            return False
        self._trigger_rate_hz = DEFAULT_TRIGGER_RATE_HZ
        self._sampling_time_ms = DEFAULT_SAMPLING_TIME_MS
        self._burst_delay_us = DEFAULT_BURST_DELAY_US
        return True

    # -----------------------------------------------------------------------
    # Continuous readings
    # -----------------------------------------------------------------------

    def get_latest_reading(self) -> Optional[list[int]]:
        """Return the most recent UDP reading in CONTINUOUS mode.

        Returns:
            list[int] of 6 receiver tick counts if in CONTINUOUS mode, the
            link is up and at least one UDP packet has been received since
            continuous() was called, otherwise None.
        """
        with self._reading_cond:
            if (self._mode is not CommMode.CONTINUOUS or not self._connected
                    or self._latest_reading is None):
                return None
            return list(self._latest_reading)

    def wait_for_reading(self, last_count: int,
                         timeout: float) -> Optional[tuple[int, list[int]]]:
        """Block until a continuous reading newer than *last_count* arrives.

        Readings are numbered like ``reading_count`` (1, 2, 3, ... over the
        client's lifetime).  The call returns the OLDEST kept reading whose
        number is greater than *last_count*, so a loop that feeds each
        returned number back in processes every reading exactly once and in
        order, at whatever trigger rate is set — even if it falls behind for
        a moment (the last 128 readings are kept)::

            count = client.reading_count
            while running:
                item = client.wait_for_reading(count, timeout=0.5)
                if item is None:
                    continue            # see Returns
                count, ticks = item

        Args:
            last_count: Number of the last reading already processed (start
                        with ``reading_count``).
            timeout:    Seconds to wait at most.

        Returns:
            ``(count, ticks)`` — the reading's number and its 6 tick counts —
            or None: on timeout; at once when not in CONTINUOUS mode (also
            when the mode changes while waiting, e.g. by a command,
            disconnect() or reconnect()).  While the link is down no reading
            is returned and the call waits out its timeout, so a polling
            loop does not spin; check ``connected`` to tell the cases apart.
        """
        deadline = time.monotonic() + min(_MAX_WAIT_S, max(0.0, float(timeout)))
        with self._reading_cond:
            while True:
                if self._mode is not CommMode.CONTINUOUS:
                    return None
                if self._connected and self._reading_count > last_count:
                    for count, ticks in self._readings:
                        if count > last_count:
                            return count, list(ticks)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._reading_cond.wait(remaining)

    # -----------------------------------------------------------------------
    # Properties
    # -----------------------------------------------------------------------

    @property
    def mode(self) -> CommMode:
        """Current communication mode (NORMAL or CONTINUOUS)."""
        return self._mode

    @property
    def reading_count(self) -> int:
        """Total number of continuous readings received by this client.

        Increments on every stored UDP packet regardless of content, so
        callers can detect new arrivals even when the payload is unchanged.
        Never reset — not by connect(), reconnect() or continuous() — so it
        can be passed to wait_for_reading() at any time."""
        with self._reading_cond:
            return self._reading_count

    @property
    def connected(self) -> bool:
        """True while the TCP link to the ground station is up.

        Turns False when the server closes the connection, on a socket
        error, and after disconnect()."""
        return self._connected

    @property
    def host(self) -> str:
        """Host of the ground station server."""
        return self._host

    @property
    def tcp_port(self) -> int:
        """TCP command port of the ground station server."""
        return self._tcp_port

    @property
    def udp_port(self) -> int:
        """UDP stream port of the ground station server."""
        return self._udp_port

    @property
    def last_error(self) -> Optional[str]:
        """Why the most recent failure happened, or None.

        Set by a failed connect(), a lost link, a command timeout, a command
        refused while the link is down, or an ``"ERR: ..."`` reply (then the
        whole reply text).  Successful commands leave it alone; a successful
        connect() clears it."""
        return self._last_error

    @property
    def consecutive_timeouts(self) -> int:
        """Commands in a row that got no reply (0 after any reply).

        A link that is up while this keeps growing means the server runs but
        the base station does not answer (serial link down or booting)."""
        return self._consecutive_timeouts

    @property
    def trigger_rate_hz(self) -> Optional[int]:
        """Trigger rate last reported by the base station (F or M reply),
        or None if not reported yet in this session."""
        return self._trigger_rate_hz

    @property
    def sampling_time_ms(self) -> Optional[int]:
        """Sampling window last reported by the base station (H, F or M
        reply), or None if not reported yet in this session."""
        return self._sampling_time_ms

    @property
    def burst_delay_us(self) -> Optional[int]:
        """Burst delay last reported by the base station (D, E or M reply),
        or None if unknown.  The firmware has no query for it, so this is
        only known after one of those commands in this session."""
        return self._burst_delay_us

    @property
    def expected_delay_search_s(self) -> float:
        """Expected duration of delay_search() in seconds, from the last
        reported trigger rate and sampling window (defaults when unknown:
        15.3 s at 10 Hz / 80 ms)."""
        hz = self._trigger_rate_hz or DEFAULT_TRIGGER_RATE_HZ
        sampling_ms = (self._sampling_time_ms if self._sampling_time_ms is not None
                       else DEFAULT_SAMPLING_TIME_MS)
        return _delay_search_seconds(hz, sampling_ms)

    # -----------------------------------------------------------------------
    # Private helpers — connection
    # -----------------------------------------------------------------------

    def _where(self) -> str:
        """host:port of the TCP command channel, for messages."""
        return f"{self._host}:{self._tcp_port}"

    def _unavailable(self, reason: str) -> GroundStationUnavailable:
        """Record a connect failure in last_error and return the exception to raise."""
        message = (f"Cannot connect to the ground station (UltraGpsXY) at "
                   f"{self._where()}: {reason}")
        self._last_error = message
        self._link_error = message
        return GroundStationUnavailable(message, self._host, self._tcp_port, reason)

    def _refuse_if_busy(self, tcp: socket.socket, probe_s: float) -> None:
        """Listen *probe_s* seconds for the server turning us away; raise
        GroundStationBusy if it does.

        A full server sends "E: server full\\n" right after accepting and
        closes the connection; a server with a free slot sends nothing.
        """
        deadline = time.monotonic() + probe_s
        received = b""
        reason: Optional[str] = None
        while reason is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                tcp.settimeout(remaining)
                chunk = tcp.recv(4096)
            except socket.timeout:
                return
            except OSError as exc:
                reason = f"it reset the connection ({exc.strerror or exc})"
                break
            if not chunk:
                reason = "it closed the connection at once"
                break
            received = (received + chunk)[-256:]
            if _SERVER_FULL_MARKER in received:
                reason = "server full"
        message = (f"The ground station (UltraGpsXY) at {self._where()} is busy ({reason}): "
                   f"it accepts one client at a time and another program is connected. "
                   f"Close the other program (e.g. another UltraGPS Control) or start "
                   f"UltraGpsXY with a higher --max-clients.")
        self._last_error = message
        self._link_error = message
        raise GroundStationBusy(message, self._host, self._tcp_port, reason)

    def _teardown(self) -> None:
        """Close sockets, stop threads and wake every waiter (lifecycle lock held)."""
        with self._state_lock:
            if self._connected:
                self._link_error = f"disconnected from the ground station at {self._where()}"
            self._connected = False
            self._link_gen += 1
            tcp, udp = self._tcp_sock, self._udp_sock
            self._tcp_sock = None
            self._udp_sock = None
        _close_quietly(tcp)
        _close_quietly(udp)

        current = threading.current_thread()
        for thread in (self._tcp_thread, self._udp_thread):
            if thread is not None and thread.is_alive() and thread is not current:
                thread.join(timeout=_THREAD_JOIN_S)
        self._tcp_thread = None
        self._udp_thread = None
        self._running = False

        # Unblock any waiting command or wait_for_reading()
        with self._rx_cond:
            self._rx_cond.notify_all()
        self._switch_mode(CommMode.NORMAL)

    def _link_lost(self, gen: int, reason: str) -> None:
        """Mark the link of generation *gen* as down (no-op for an old link)."""
        message = f"Lost the connection to the ground station at {self._where()}: {reason}"
        with self._state_lock:
            if gen != self._link_gen or not self._connected:
                return
            self._connected = False
            self._link_error = message
        self._last_error = message
        logger.warning(message)
        with self._rx_cond:
            self._rx_cond.notify_all()
        with self._reading_cond:
            self._reading_cond.notify_all()

    def _link_down_reason(self, gen: Optional[int] = None) -> str:
        """Explain why the link (of generation *gen*, if given) is not usable,
        for last_error."""
        if gen is not None and self._connected and gen != self._link_gen:
            return "the connection was re-established meanwhile"
        return self._link_error or f"not connected to the ground station at {self._where()}"

    def _current_link(self) -> Optional[int]:
        """Generation of the current link if it is up, else None."""
        with self._state_lock:
            return self._link_gen if self._connected else None

    def _link_is_up(self, gen: int) -> bool:
        """True while the link of generation *gen* is current and up.

        A command started on a link that reconnect() replaced must not wait
        on (or send through) the new one: its reply can never arrive there.
        """
        return self._connected and gen == self._link_gen

    def _register_udp(self) -> None:
        """Send a datagram so the server streams continuous data to our UDP socket."""
        sock, addr = self._udp_sock, self._udp_addr
        if sock is None or addr is None:
            return
        try:
            sock.sendto(_UDP_REGISTER_PAYLOAD, addr)
        except OSError as exc:
            logger.debug("UDP registration to %s failed: %s", addr, exc)
        self._last_data_time = time.monotonic()

    def _maybe_reregister(self) -> None:
        """Re-send the UDP registration while a stream is expected but silent.

        The server forgets UDP peers when its operator clears them, and a
        lost datagram would leave us unregistered.  The interval doubles
        (1 s .. 30 s) while no reading arrives, so a stalled server is not
        flooded.
        """
        if self._mode is not CommMode.CONTINUOUS or not self._connected:
            return
        silent_for = time.monotonic() - self._last_data_time
        hz = self._trigger_rate_hz or DEFAULT_TRIGGER_RATE_HZ
        if silent_for >= max(self._reregister_interval, 2.5 / hz):
            logger.debug("No continuous data for %.1f s, re-registering UDP", silent_for)
            self._register_udp()
            self._reregister_interval = min(2 * self._reregister_interval, _UDP_REREGISTER_MAX_S)

    # -----------------------------------------------------------------------
    # Private helpers — commands
    # -----------------------------------------------------------------------

    def _require_connected_once(self) -> None:
        """Raise RuntimeError if connect() never succeeded (a programming error)."""
        if not self._ever_connected:
            raise RuntimeError("Not connected. Call connect() first.")

    def _fail(self, message: str) -> None:
        """Record why a command failed."""
        self._last_error = message
        logger.debug(message)

    def _reply_timeout(self, timeout: Optional[float]) -> float:
        """Seconds a command may take: *timeout*, or the client default.

        The default covers what the server needs in the worst case: wait for
        the current measurement to end (one trigger period), wait for the
        next trigger edge (another period), sample, plus slack.
        """
        if timeout is not None:
            return min(_MAX_WAIT_S, max(0.0, float(timeout)))
        hz = self._trigger_rate_hz or DEFAULT_TRIGGER_RATE_HZ
        sampling_ms = (self._sampling_time_ms if self._sampling_time_ms is not None
                       else DEFAULT_SAMPLING_TIME_MS)
        return max(self._timeout, 2.0 / hz + sampling_ms / 1000.0 + _REPLY_MARGIN_S)

    def _switch_mode(self, mode: CommMode) -> None:
        """Set the mode, drop readings from before the switch, wake waiters."""
        with self._reading_cond:
            self._mode = mode
            self._latest_reading = None
            self._readings.clear()
            self._reregister_interval = _UDP_REREGISTER_MIN_S
            self._reading_cond.notify_all()

    def _send_line(self, line: str, gen: int) -> bool:
        """Send one command line (command lock held).

        Spaces commands at least _MIN_COMMAND_GAP_S apart: the server reads
        one command per TCP segment and drops two that arrive together.
        Received text still buffered is discarded right before sending.

        Returns:
            True if sent, False if the link is down or the send failed.
        """
        wait = self._last_send + _MIN_COMMAND_GAP_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        with self._state_lock:
            sock = self._tcp_sock if self._link_is_up(gen) else None
        if sock is None:
            self._fail(f"'{line}' not sent: {self._link_down_reason(gen)}")
            return False
        with self._rx_cond:
            self._rx_buf = ""      # stale text (late replies, broadcasts) is never a reply to this
        try:
            sock.sendall(f"{line}\n".encode("ascii"))
        except OSError as exc:
            self._link_lost(gen, f"sending '{line}' failed ({exc.strerror or exc})")
            self._fail(f"'{line}' not sent: {self._link_down_reason()}")
            return False
        self._last_send = time.monotonic()
        return True

    def _command(self, cmd: CommandType, arg: Optional[int] = None,
                 timeout: Optional[float] = None) -> Optional[re.Match]:
        """Run one command/reply exchange.

        Takes the command lock (waiting counts towards the timeout), drops
        stale received text, sends the command, switches to NORMAL mode (the
        firmware leaves continuous mode on any command but 'C') and waits for
        the expected reply.

        Returns:
            The reply match, or None on timeout, link loss or an ERR reply
            (last_error says which).

        Raises:
            RuntimeError: If connect() never succeeded.
        """
        self._require_connected_once()
        spec = _COMMANDS[cmd]
        line = spec.letter if arg is None else f"{spec.letter} {arg}"
        budget = self._reply_timeout(timeout)
        deadline = time.monotonic() + budget
        if not self._command_lock.acquire(timeout=budget):
            self._fail(f"'{line}' not sent: another command was still running after {budget:g} s")
            return None
        try:
            gen = self._current_link()
            if gen is None:
                self._fail(f"'{line}' not sent: {self._link_down_reason()}")
                return None
            with self._rx_cond:
                self._rx_active = True     # _send_line() drops stale text before sending
            if not self._send_line(line, gen):
                return None
            self._switch_mode(CommMode.NORMAL)
            return self._await_reply(line, spec, gen, deadline, budget)
        finally:
            with self._rx_cond:
                self._rx_active = False
                self._rx_buf = ""
            self._command_lock.release()

    def _await_reply(self, line: str, spec: _CommandSpec, gen: int, deadline: float,
                     budget: float) -> Optional[re.Match]:
        """Wait for the reply to *line*, sent on link *gen* (command lock held,
        exchange active).

        Takes the LAST complete match in the received text — a stale reply of
        the same kind arrives before the real one — and gives a match that
        may still grow (see _may_grow) a grace period to do so.  Any other
        text is dropped.
        """
        assert spec.reply is not None
        with self._rx_cond:
            while True:
                match = self._last_match(spec)
                if match is not None:
                    if not self._may_grow(match, spec):
                        break
                    seq = self._rx_seq
                    grace_end = time.monotonic() + self._reply_grace
                    while self._rx_seq == seq and self._link_is_up(gen):
                        remaining = grace_end - time.monotonic()
                        if remaining <= 0:
                            break
                        self._rx_cond.wait(remaining)
                    if self._rx_seq == seq:
                        break          # quiet: accept it
                    if time.monotonic() > deadline + self._reply_grace:
                        match = self._last_match(spec) or match
                        break          # text keeps coming but time is up
                    continue           # more text arrived: look again
                if not self._link_is_up(gen):
                    self._fail(f"No reply to '{line}': {self._link_down_reason(gen)}")
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._consecutive_timeouts += 1
                    self._fail(f"No reply to '{line}' from the ground station "
                               f"at {self._where()} within {budget:g} s")
                    return None
                self._rx_cond.wait(remaining)

        text = match.string
        dropped = (text[:match.start()] + text[match.end():]).strip()
        if dropped:
            logger.debug("Dropped unexpected text while waiting for '%s': %r", line, dropped)
        self._consecutive_timeouts = 0
        if match.group("err") is not None:
            self._fail(match.group(0).strip())
            return None
        return match

    def _last_match(self, spec: _CommandSpec) -> Optional[re.Match]:
        """Last complete reply of *spec*'s kind (or ERR reply) in the received text."""
        match = None
        for match in spec.reply.finditer(self._rx_buf):
            pass
        return match

    @staticmethod
    def _may_grow(match: re.Match, spec: _CommandSpec) -> bool:
        """True if more received bytes could still change or replace *match*.

        A bare reply that ends in a number (or free ERR text) at the very end
        of the data may have been split ("..., 41" + "09").  Text after the
        match that starts like another reply of the same kind may be a newer
        reply still arriving.  A match followed by a newline or by unrelated
        text is complete.
        """
        tail = match.string[match.end():]
        if not tail:
            return match.group("err") is not None or match.group(0)[-1].isdigit()
        for marker in (spec.marker, _ERROR_MARKER):
            if marker in tail:
                return True
            if any(tail.endswith(marker[:k]) for k in range(1, len(marker))):
                return True
        return False

    # -----------------------------------------------------------------------
    # Background threads (exactly 2)
    # -----------------------------------------------------------------------

    def _tcp_recv_loop(self, sock: socket.socket, gen: int) -> None:
        """Daemon thread: collect TCP text for the active exchange, detect link loss."""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        tail = b""   # last bytes received, to recognise a "server full" that came late
        while gen == self._link_gen:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                self._link_lost(gen, f"connection error ({exc.strerror or exc})")
                break
            if not chunk:
                if _SERVER_FULL_MARKER in tail:
                    # The rejection arrived after connect()'s check (very slow link)
                    self._link_lost(gen, "the ground station answered \"server full\" and "
                                         "closed the connection: another client holds its "
                                         "only slot")
                else:
                    self._link_lost(gen, "the ground station closed the connection")
                break
            tail = (tail + chunk)[-64:]

            text = decoder.decode(chunk)
            if not text:
                continue
            with self._rx_cond:
                if not self._rx_active or gen != self._link_gen:
                    logger.debug("Dropping unsolicited TCP text: %r", text)
                    continue
                self._rx_buf = (self._rx_buf + text)[-_RX_BUFFER_LIMIT:]
                self._rx_seq += 1
                self._rx_cond.notify_all()

    def _udp_recv_loop(self, sock: socket.socket, gen: int) -> None:
        """Daemon thread: receive UDP datagrams and store continuous readings."""
        while gen == self._link_gen:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                self._maybe_reregister()
                continue
            except (ConnectionResetError, ConnectionRefusedError):
                continue   # ICMP "port unreachable" for a registration (Windows reports it)
            except OSError:
                break

            raw = data.decode("utf-8", errors="replace").strip()
            parsed = _CONTINUOUS_DATAGRAM.fullmatch(raw)
            if parsed is None:
                logger.debug("Ignoring UDP datagram: %r", raw)
                continue
            ticks = _ints(parsed, _RECEIVER_COUNT)

            with self._reading_cond:
                if self._mode is not CommMode.CONTINUOUS or gen != self._link_gen:
                    continue   # late datagram after leaving continuous mode
                self._reading_count += 1
                self._latest_reading = ticks
                self._readings.append((self._reading_count, ticks))
                self._last_data_time = time.monotonic()
                self._reregister_interval = _UDP_REREGISTER_MIN_S
                self._reading_cond.notify_all()
