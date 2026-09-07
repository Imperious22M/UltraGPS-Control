from __future__ import annotations

import logging
import queue
import socket
import threading
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class _TcpClient:
    conn: socket.socket
    addr: tuple
    send_queue: queue.Queue = field(default_factory=queue.Queue)
    alive: bool = True


@dataclass
class _PortState:
    name: str
    port: int
    proto: str   # 'tcp' or 'udp'
    sock: socket.socket
    running: bool = True
    # TCP
    clients: list = field(default_factory=list)
    clients_lock: threading.Lock = field(default_factory=threading.Lock)
    # UDP
    send_queue: queue.Queue = field(default_factory=queue.Queue)
    udp_clients: set = field(default_factory=set)
    udp_lock: threading.Lock = field(default_factory=threading.Lock)


class UltraGPSServer:
    """Multi-port TCP/UDP server for the UltraGPS positioning system."""

    def __init__(self) -> None:
        self._bind_ip: str = self._find_local_ip()
        self._streaming: bool = False
        self._streaming_lock: threading.Lock = threading.Lock()
        self._ports: dict[str, _PortState] = {}
        self._ports_lock: threading.Lock = threading.Lock()
        self._command_handlers: dict[str, Callable[[str], None]] = {}
        logger.info("UltraGPSServer initialised on IP %s", self._bind_ip)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def bind_ip(self) -> str:
        return self._bind_ip

    @property
    def streaming(self) -> bool:
        with self._streaming_lock:
            return self._streaming

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_device(self, ip: str) -> None:
        """Change the bind IP (affects new ports only)."""
        logger.info("UltraGPSServer: bind IP changed from %s to %s", self._bind_ip, ip)
        self._bind_ip = ip

    def start_tcp_port(self, port: int, name: str = None) -> str:
        """Bind a TCP port, start listening, and spawn an accept thread.

        Returns the port name. Raises ValueError if the name is already in use.
        """
        if name is None:
            name = f"tcp:{port}"
        with self._ports_lock:
            if name in self._ports:
                raise ValueError(f"Port name '{name}' is already registered")

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)
        sock.bind((self._bind_ip, port))
        sock.listen(5)

        state = _PortState(name=name, port=port, proto='tcp', sock=sock)

        with self._ports_lock:
            self._ports[name] = state

        t = threading.Thread(target=self._tcp_accept_loop, args=(state,), daemon=True,
                              name=f"ultragps-tcp-accept-{name}")
        t.start()

        logger.info("TCP port %d started (name=%s, ip=%s)", port, name, self._bind_ip)
        return name

    def start_udp_port(self, port: int, name: str = None) -> str:
        """Bind a UDP port and spawn sender + receiver threads.

        Returns the port name. Raises ValueError if the name is already in use.
        """
        if name is None:
            name = f"udp:{port}"
        with self._ports_lock:
            if name in self._ports:
                raise ValueError(f"Port name '{name}' is already registered")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)
        sock.bind((self._bind_ip, port))

        state = _PortState(name=name, port=port, proto='udp', sock=sock)

        with self._ports_lock:
            self._ports[name] = state

        ts = threading.Thread(target=self._udp_send_loop, args=(state,), daemon=True,
                               name=f"ultragps-udp-send-{name}")
        ts.start()

        tr = threading.Thread(target=self._udp_recv_loop, args=(state,), daemon=True,
                               name=f"ultragps-udp-recv-{name}")
        tr.start()

        logger.info("UDP port %d started (name=%s, ip=%s)", port, name, self._bind_ip)
        return name

    def send_message(self, port_name: str, message: str) -> None:
        """Send a message on the named port.

        For TCP: fans out to all connected clients.
        For UDP: enqueues for broadcast to all known UDP clients.
        Appends a newline if the message does not end with one.
        Silently no-ops for unknown port names.
        """
        with self._ports_lock:
            state = self._ports.get(port_name)
        if state is None:
            return

        if not message.endswith('\n'):
            message = message + '\n'

        data = message.encode('utf-8')

        if state.proto == 'tcp':
            with state.clients_lock:
                clients = list(state.clients)
            for client in clients:
                if client.alive:
                    client.send_queue.put(data)
        else:
            state.send_queue.put(data)

    def register_command(self, command: str, handler: Callable[[str], None]) -> None:
        """Register a handler for a named command (the part after 'N: ')."""
        self._command_handlers[command] = handler
        logger.debug("Registered command handler for '%s'", command)

    def get_status(self) -> dict:
        """Return a status dictionary describing the server state."""
        with self._ports_lock:
            ports_snapshot = list(self._ports.values())

        port_entries = []
        total_clients = 0
        for state in ports_snapshot:
            if state.proto == 'tcp':
                with state.clients_lock:
                    n = len(state.clients)
            else:
                with state.udp_lock:
                    n = len(state.udp_clients)
            total_clients += n
            port_entries.append({
                'name': state.name,
                'port': state.port,
                'proto': state.proto,
                'clients': n,
            })

        return {
            'ip': self._bind_ip,
            'streaming': self.streaming,
            'ports': port_entries,
            'total_clients': total_clients,
        }

    def stop_all(self) -> None:
        """Stop all ports and close all connections."""
        logger.info("UltraGPSServer: stopping all ports")
        with self._ports_lock:
            states = list(self._ports.values())

        for state in states:
            state.running = False
            if state.proto == 'tcp':
                with state.clients_lock:
                    clients = list(state.clients)
                for client in clients:
                    client.alive = False
                    client.send_queue.put(None)
                    try:
                        client.conn.close()
                    except Exception:
                        pass
            else:
                # Wake the send loop so it exits cleanly
                state.send_queue.put(None)
            try:
                state.sock.close()
            except Exception:
                pass

        with self._ports_lock:
            self._ports.clear()

        logger.info("UltraGPSServer: all ports stopped")

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_local_ip() -> str:
        """Determine the local outbound IP address."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            pass
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return '127.0.0.1'

    # ------------------------------------------------------------------
    # TCP internals
    # ------------------------------------------------------------------

    def _tcp_accept_loop(self, state: _PortState) -> None:
        logger.debug("TCP accept loop started for port %s", state.name)
        while state.running:
            try:
                conn, addr = state.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            client = _TcpClient(conn=conn, addr=addr)
            with state.clients_lock:
                state.clients.append(client)
            logger.info("TCP client connected: %s on port %s", addr, state.name)

            sender_thread = threading.Thread(
                target=self._tcp_client_sender, args=(state, client), daemon=True,
                name=f"ultragps-tcp-sender-{state.name}-{addr}")
            sender_thread.start()

            handler_thread = threading.Thread(
                target=self._tcp_client_handler, args=(state, client), daemon=True,
                name=f"ultragps-tcp-handler-{state.name}-{addr}")
            handler_thread.start()

        logger.debug("TCP accept loop ended for port %s", state.name)

    def _tcp_client_handler(self, state: _PortState, client: _TcpClient) -> None:
        buf = b''
        try:
            while client.alive and state.running:
                try:
                    chunk = client.conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break

                if not chunk:
                    break

                buf += chunk
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    raw = line.decode('utf-8', errors='replace').strip()
                    if raw.startswith('N: '):
                        cmd = raw[3:]
                        reply_fn = lambda msg, q=client.send_queue: q.put(
                            msg.encode('utf-8'))
                        self._handle_command(cmd, client.addr, reply_fn)
        finally:
            client.alive = False
            client.send_queue.put(None)
            with state.clients_lock:
                try:
                    state.clients.remove(client)
                except ValueError:
                    pass
            try:
                client.conn.close()
            except Exception:
                pass
            logger.info("TCP client disconnected: %s on port %s", client.addr, state.name)

    def _tcp_client_sender(self, state: _PortState, client: _TcpClient) -> None:
        while True:
            try:
                data = client.send_queue.get()
            except Exception:
                break

            if data is None:
                break

            try:
                client.conn.sendall(data)
            except OSError as exc:
                logger.debug("TCP send error to %s: %s", client.addr, exc)
                client.alive = False
                break

        logger.debug("TCP sender thread exited for %s on port %s", client.addr, state.name)

    # ------------------------------------------------------------------
    # UDP internals
    # ------------------------------------------------------------------

    def _udp_send_loop(self, state: _PortState) -> None:
        logger.debug("UDP send loop started for port %s", state.name)
        while state.running:
            try:
                data = state.send_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if data is None:
                break

            with state.udp_lock:
                clients = set(state.udp_clients)

            stale = set()
            for addr in clients:
                try:
                    state.sock.sendto(data, addr)
                except OSError as exc:
                    logger.debug("UDP send error to %s: %s", addr, exc)
                    stale.add(addr)

            if stale:
                with state.udp_lock:
                    state.udp_clients -= stale

        logger.debug("UDP send loop ended for port %s", state.name)

    def _udp_recv_loop(self, state: _PortState) -> None:
        logger.debug("UDP recv loop started for port %s", state.name)
        while state.running:
            try:
                data, addr = state.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            with state.udp_lock:
                state.udp_clients.add(addr)

            raw = data.decode('utf-8', errors='replace').strip()
            if raw.startswith('N: '):
                cmd = raw[3:]
                reply_fn = lambda msg, a=addr, s=state.sock: s.sendto(
                    msg.encode('utf-8'), a)
                self._handle_command(cmd, addr, reply_fn)

        logger.debug("UDP recv loop ended for port %s", state.name)

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def _handle_command(self, cmd: str, addr: tuple, reply_fn=None) -> None:
        handler = self._command_handlers.get(cmd, None)
        cmd_success = False

        if cmd == 'Ready':
            with self._streaming_lock:
                self._streaming = True
                cmd_success = True
            logger.info("Streaming enabled by client %s (cmd='Ready')", addr)

        if handler is not None:
            try:
                handler(cmd)
                cmd_success = True
            except Exception as exc:
                cmd_success = False
                logger.error("Command handler error for '%s': %s", cmd, exc)

        if reply_fn is not None:
            try:
                if cmd_success:
                    reply_fn("<Ok>\n")
                else:
                    reply_fn("<Error>\n")
            except Exception as exc:
                logger.debug("Reply error to %s: %s", addr, exc)
