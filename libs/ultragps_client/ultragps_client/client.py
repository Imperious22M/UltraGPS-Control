"""UltraGPS Client Library — core module
=========================================
Standalone TCP/UDP client library for the UltraGPS ground station server.

Provides a threaded client that sends commands via TCP and receives
continuous distance data via UDP, mirroring the protocol used by the
UltraGPS-Ground C++ server.

Quick-start
-----------
    from ultragps_client import UltraGPSClient, CommMode

    client = UltraGPSClient(host='127.0.0.1', tcp_port=9000, udp_port=9001)
    client.connect()

    # One-shot pulse — blocks until response or timeout
    counts = client.pulse()          # list[int] of 6 receiver counts, or None
    counts = client.simulate()       # same, using simulated data

    # Continuous streaming — non-blocking, data arrives via UDP
    client.continuous()
    assert client.mode == CommMode.CONTINUOUS
    latest = client.get_latest_reading()   # list[int] | None

    client.disconnect()

Protocol reference
------------------
Commands are sent as a single ASCII letter + newline over TCP (port 9000):
    'P\\n'  — PULSE      (one-shot, response: "N: <6 counts>")
    'S\\n'  — SIMULATE   (one-shot, response: "N: <6 counts>")
    'C\\n'  — CONTINUOUS (streaming, data arrives via UDP as "C: <6 counts>")

Normal responses arrive via TCP with prefix "N: ".
Continuous data streams via UDP with prefix "C: ".
"""

from __future__ import annotations

import socket
import threading
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

_RESPONSE_PREFIX_NORMAL: str = "N: "
_RESPONSE_PREFIX_CONTINUOUS: str = "C: "
_RESPONSE_MARKER_LEN: int = 3       # len("N: ") == len("C: ") == 3
_RECEIVER_COUNT: int = 6
_DEFAULT_TIMEOUT: float = 2.0       # seconds — generous margin over server's 500ms serial timeout
_UDP_REGISTER_PAYLOAD: bytes = b"register"  # content irrelevant; source addr registers client


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CommandType(Enum):
    """Enumerates every command the UltraGPS-Ground server accepts.

    Maps 1-to-1 with the Arduino base-station GLOBAL_CMD_TABLE:
        PULSE      → 'P'  (trigger one ultrasonic measurement)
        SIMULATE   → 'S'  (simulated measurement, no real hardware needed)
        CONTINUOUS → 'C'  (switch to continuous streaming mode)
    """
    PULSE = "pulse"
    SIMULATE = "simulate"
    CONTINUOUS = "continuous"


class CommMode(Enum):
    """Tracks the current communication mode of the client.

    NORMAL     — one-shot commands (P, S); responses arrive via TCP.
    CONTINUOUS — streaming mode (C); data arrives continuously via UDP.
    """
    NORMAL = "normal"
    CONTINUOUS = "continuous"


# ---------------------------------------------------------------------------
# Private command table  (mirrors Arduino GLOBAL_CMD_TABLE)
# ---------------------------------------------------------------------------

#: Maps each CommandType to the exact wire-format string sent over TCP.
#: The server strips trailing whitespace; the newline acts as the command
#: terminator on the Arduino side (command_control.cpp poll() checks for '\n').
_COMMANDS: dict[CommandType, str] = {
    CommandType.PULSE:      "P\n",
    CommandType.SIMULATE:   "S\n",
    CommandType.CONTINUOUS: "C\n",
}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class UltraGPSClient:
    """Threaded TCP/UDP client for the UltraGPS ground station server.

    Lifecycle is manual: call connect() before use, disconnect() when done.
    disconnect() is idempotent — safe to call before connect() or twice.

    Args:
        host:     IP address of the UltraGPS-Ground server.
        tcp_port: TCP port for command/response channel (default 9000).
        udp_port: UDP port for continuous data stream (default 9001).
        timeout:  Seconds to wait for a normal command response (default 2.0).
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        tcp_port: int = 9000,
        udp_port: int = 9001,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._host = host
        self._tcp_port = tcp_port
        self._udp_port = udp_port
        self._timeout = timeout

        # Sockets — created in connect(), closed in disconnect()
        self._tcp_sock: Optional[socket.socket] = None
        self._udp_sock: Optional[socket.socket] = None

        # Thread control
        self._running: bool = False
        self._tcp_thread: Optional[threading.Thread] = None
        self._udp_thread: Optional[threading.Thread] = None

        # TCP send serialisation
        self._tcp_send_lock = threading.Lock()

        # Normal-response slot: the TCP recv thread deposits a parsed response
        # here; pulse()/simulate() wait on the event then read the slot.
        self._tcp_response: Optional[list[int]] = None
        self._tcp_response_event = threading.Event()

        # Latest continuous reading — written by UDP recv thread, read by caller
        self._latest_reading: Optional[list[int]] = None
        self._latest_reading_lock = threading.Lock()
        self._reading_count: int = 0  # incremented each time a UDP packet is stored

        # Communication mode
        self._mode: CommMode = CommMode.NORMAL

    # -----------------------------------------------------------------------
    # Public lifecycle
    # -----------------------------------------------------------------------

    def connect(self) -> None:
        """Open TCP and UDP connections to the server and start recv threads.

        Raises:
            ConnectionError: If the TCP connection cannot be established.
            RuntimeError:    If already connected.
        """
        if self._running:
            raise RuntimeError("Already connected. Call disconnect() first.")

        # TCP — persistent stream connection
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            tcp.connect((self._host, self._tcp_port))
        except OSError as exc:
            tcp.close()
            raise ConnectionError(
                f"TCP connect to {self._host}:{self._tcp_port} failed: {exc}"
            ) from exc
        self._tcp_sock = tcp

        # UDP — ephemeral local port; first sendto registers us with the server
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock = udp

        self._running = True
        self._mode = CommMode.NORMAL
        self._latest_reading = None
        self._reading_count = 0
        self._tcp_response = None
        self._tcp_response_event.clear()

        # Register with the server's UDP TX list by sending any packet
        self._send_udp(_UDP_REGISTER_PAYLOAD)

        # Start exactly 2 daemon threads
        self._tcp_thread = threading.Thread(
            target=self._tcp_recv_loop, daemon=True, name="ultragps-tcp-recv"
        )
        self._udp_thread = threading.Thread(
            target=self._udp_recv_loop, daemon=True, name="ultragps-udp-recv"
        )
        self._tcp_thread.start()
        self._udp_thread.start()

    def disconnect(self) -> None:
        """Close all connections and stop recv threads. Idempotent."""
        self._running = False

        for sock in (self._tcp_sock, self._udp_sock):
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

        self._tcp_sock = None
        self._udp_sock = None

        for thread in (self._tcp_thread, self._udp_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)

        self._tcp_thread = None
        self._udp_thread = None

        # Unblock any waiting pulse()/simulate() call
        self._tcp_response = None
        self._tcp_response_event.set()

    # -----------------------------------------------------------------------
    # Public command API
    # -----------------------------------------------------------------------

    def pulse(self) -> Optional[list[int]]:
        """Trigger a single ultrasonic measurement and return the 6 counts.

        Blocks until the server responds or the timeout expires.
        If currently in CONTINUOUS mode, transitions back to NORMAL first
        (the server handles this automatically when it receives 'P').

        Returns:
            list[int] of 6 receiver tick counts, or None on timeout/error.

        Raises:
            RuntimeError: If not connected.
        """
        return self._send_normal_command(CommandType.PULSE)

    def simulate(self) -> Optional[list[int]]:
        """Trigger a simulated measurement and return the 6 counts.

        Identical to pulse() but uses the server's simulation mode (no
        real ultrasonic hardware required).

        Returns:
            list[int] of 6 receiver tick counts, or None on timeout/error.

        Raises:
            RuntimeError: If not connected.
        """
        return self._send_normal_command(CommandType.SIMULATE)

    def continuous(self) -> None:
        """Switch the server to continuous streaming mode.

        Non-blocking. After this call, the server streams distance data
        via UDP every ~20 ms. Use get_latest_reading() to access it.

        Raises:
            RuntimeError: If not connected.
        """
        if not self._running:
            raise RuntimeError("Not connected. Call connect() first.")

        self._send_tcp(_COMMANDS[CommandType.CONTINUOUS].encode())
        self._mode = CommMode.CONTINUOUS

    def get_latest_reading(self) -> Optional[list[int]]:
        """Return the most recent UDP reading in CONTINUOUS mode.

        Returns:
            list[int] of 6 receiver tick counts if in CONTINUOUS mode and
            at least one UDP packet has been received, otherwise None.
        """
        if self._mode != CommMode.CONTINUOUS:
            return None
        with self._latest_reading_lock:
            reading = self._latest_reading
        return list(reading) if reading is not None else None

    @property
    def mode(self) -> CommMode:
        """Current communication mode (NORMAL or CONTINUOUS)."""
        return self._mode

    @property
    def reading_count(self) -> int:
        """Total number of UDP packets stored since connect(). Increments on every
        received continuous packet regardless of content, so callers can detect new
        arrivals even when the payload is unchanged."""
        with self._latest_reading_lock:
            return self._reading_count

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _send_normal_command(self, cmd: CommandType) -> Optional[list[int]]:
        """Send a normal (one-shot) command and block for the TCP response."""
        if not self._running:
            raise RuntimeError("Not connected. Call connect() first.")

        # Transition out of continuous mode (server does the same on its side)
        if self._mode == CommMode.CONTINUOUS:
            self._mode = CommMode.NORMAL
            with self._latest_reading_lock:
                self._latest_reading = None

        # Arm the response slot before sending so we don't miss a fast reply
        self._tcp_response = None
        self._tcp_response_event.clear()

        self._send_tcp(_COMMANDS[cmd].encode())

        # Wait for the TCP recv thread to deposit a response
        received = self._tcp_response_event.wait(timeout=self._timeout)
        if not received or self._tcp_response is None:
            return None

        result = self._tcp_response
        self._tcp_response = None
        self._tcp_response_event.clear()
        return result

    def _send_tcp(self, data: bytes) -> None:
        """Send bytes on the TCP socket (thread-safe)."""
        if self._tcp_sock is None:
            return
        with self._tcp_send_lock:
            try:
                self._tcp_sock.sendall(data)
            except OSError:
                pass

    def _send_udp(self, data: bytes) -> None:
        """Send a UDP datagram to the server's UDP port."""
        if self._udp_sock is None:
            return
        try:
            self._udp_sock.sendto(data, (self._host, self._udp_port))
        except OSError:
            pass

    def _parse_response(self, raw: str) -> Optional[list[int]]:
        """Strip the 3-char protocol prefix and parse comma-separated counts.

        Args:
            raw: Wire string, e.g. "N: 4404, 3226, 4063, 4041, 3133, 4109"

        Returns:
            list[int] of length _RECEIVER_COUNT, or None if unparseable.
        """
        s = raw.strip()
        if len(s) > _RESPONSE_MARKER_LEN:
            s = s[_RESPONSE_MARKER_LEN:]
        try:
            values = [int(v.strip()) for v in s.split(",")]
        except ValueError:
            return None
        if len(values) != _RECEIVER_COUNT:
            return None
        return values

    # -----------------------------------------------------------------------
    # Background threads (exactly 2)
    # -----------------------------------------------------------------------

    def _tcp_recv_loop(self) -> None:
        """Daemon thread: read TCP data, detect normal responses, signal caller."""
        buf = ""
        sock = self._tcp_sock

        while self._running and sock is not None:
            try:
                sock.settimeout(0.1)
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            if not chunk:
                break  # server closed connection

            buf += chunk.decode("utf-8", errors="replace")

            # Process all complete lines in the buffer.
            # The server sends bare strings (no trailing newline from serial),
            # but the TCP stack may deliver them with or without '\n'.
            # We look for the known prefixes to identify complete messages.
            while True:
                # Find the earliest occurrence of either known prefix
                n_pos = buf.find(_RESPONSE_PREFIX_NORMAL)
                c_pos = buf.find(_RESPONSE_PREFIX_CONTINUOUS)

                if n_pos == -1 and c_pos == -1:
                    break

                # Pick whichever prefix appears first
                if n_pos != -1 and (c_pos == -1 or n_pos <= c_pos):
                    start = n_pos
                    prefix = _RESPONSE_PREFIX_NORMAL
                else:
                    start = c_pos
                    prefix = _RESPONSE_PREFIX_CONTINUOUS

                # Find the end of this message (next newline or next prefix)
                end = len(buf)
                for delim in ("\n", _RESPONSE_PREFIX_NORMAL, _RESPONSE_PREFIX_CONTINUOUS):
                    pos = buf.find(delim, start + len(prefix))
                    if pos != -1 and pos < end:
                        end = pos

                message = buf[start:end].strip()
                buf = buf[end:]

                if not message:
                    continue

                if prefix == _RESPONSE_PREFIX_NORMAL:
                    parsed = self._parse_response(message)
                    if parsed is not None:
                        self._tcp_response = parsed
                        self._tcp_response_event.set()
                # C: responses on TCP are ignored — continuous data comes via UDP

    def _udp_recv_loop(self) -> None:
        """Daemon thread: receive UDP datagrams and store the latest reading."""
        sock = self._udp_sock

        while self._running and sock is not None:
            try:
                sock.settimeout(0.1)
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            if not data:
                continue

            raw = data.decode("utf-8", errors="replace")
            if not raw.startswith(_RESPONSE_PREFIX_CONTINUOUS):
                continue

            parsed = self._parse_response(raw)
            if parsed is not None:
                with self._latest_reading_lock:
                    self._latest_reading = parsed
                    self._reading_count += 1
