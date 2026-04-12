"""
position-lib-test.py
====================
Performance and accuracy test for ultragps_position.UltraGPSPositionLib.

Two test modes:

  TCP mode (default)
      Sends repeated "P" poll commands over TCP and measures the full
      round-trip + solve time for a configurable number of iterations.

  UDP continuous mode  (-u / --udp)
      Sends a single "C" command over TCP to start the Arduino's continuous
      streaming, then receives the tick stream over UDP for 20 seconds
      (configurable with --udp-duration).  Reports per-packet timing and
      position statistics over the entire capture window.

Usage examples
--------------
    # TCP mode, defaults
    python position-lib-test.py

    # TCP mode, CEP solver, 200 iterations against a remote server
    python position-lib-test.py --cep --iterations 200 --ip 192.168.1.50

    # UDP continuous mode, default 20 s
    python position-lib-test.py -u --ip 192.168.1.50

    # UDP continuous mode, CEP solver, 30 s window
    python position-lib-test.py -u --cep --udp-duration 30

    # Point at a different config file
    python position-lib-test.py --config /path/to/config.toml

Options
-------
    --config          Path to config.toml              (default: ../config.toml)
    --ip              UltraGPS-Ground server IP         (default: 127.0.0.1)
    --tcp-port        TCP command port                  (default: 9000)
    --udp-port        UDP continuous-stream port        (default: 9001)
    --iterations      Poll cycles for TCP mode          (default: 100)
    -u / --udp        Use UDP continuous mode
    --udp-duration    Capture window in seconds         (default: 20)
    --cep             Use CEP subset-selection solver
    --max-subsets     Receiver subsets for CEP          (default: 15)
"""

import argparse
import os
import socket
import statistics
import time

from ultragps_position import UltraGPSPositionLib

# Project root — one level up from tests/
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Server protocol constants ────────────────────────────────────────────────
TCP_POLL_CMD       = b"P\n"   # single-shot poll (Normal command)
TCP_CONTINUOUS_CMD = b"C\n"   # start continuous streaming (Continuous command)
TCP_STOP_CMD       = b"P\n"   # sending any Normal command exits continuous mode
RECV_BUFSIZE       = 4096


# ─────────────────────────────────────────────────────────────────────────────
# Low-level network helpers
# ─────────────────────────────────────────────────────────────────────────────

def open_tcp(ip: str, port: int, timeout: float = 5.0) -> socket.socket:
    """Open a persistent TCP connection to the UltraGPS-Ground server."""
    sock = socket.create_connection((ip, port), timeout=timeout)
    sock.settimeout(2.0)
    return sock


def tcp_poll(sock: socket.socket) -> str | None:
    """Send "P" and return the raw response, or None on error/timeout."""
    try:
        sock.sendall(TCP_POLL_CMD)
        data = sock.recv(RECV_BUFSIZE)
        return data.decode("utf-8") if data else None
    except (socket.timeout, OSError):
        return None


def open_udp(ip: str, udp_port: int) -> socket.socket:
    """Create a UDP socket and register with the server.

    The UltraGPS-Ground server only sends data to UDP clients that have
    previously sent it *any* datagram.  This function creates the socket,
    binds it to an OS-assigned ephemeral port, then sends a single
    registration datagram so the server adds us to its client list.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))          # ephemeral local port
    sock.settimeout(1.0)
    sock.sendto(b"register", (ip, udp_port))
    return sock


# ─────────────────────────────────────────────────────────────────────────────
# Shared statistics printer
# ─────────────────────────────────────────────────────────────────────────────

def _print_stats(
    lib: UltraGPSPositionLib,
    iter_times:  list[float],
    solve_times: list[float],
    sane_counts: list[int],
    successes:   int,
    positions:   list,
    total_wall:  float,
    first_raw:   str | None,
    last_raw:    str | None,
    use_cep:     bool,
) -> None:
    n = len(iter_times)
    if not n:
        print("No packets processed.")
        return

    def _row(data: list[float], label: str, unit: str = "ms") -> None:
        scale = 1000.0 if unit == "ms" else 1.0
        avg = statistics.mean(data)
        med = statistics.median(data)
        mn  = min(data)
        mx  = max(data)
        sd  = statistics.stdev(data) if len(data) > 1 else 0.0
        print(
            f"  {label:<22} mean={avg*scale:7.2f}  median={med*scale:7.2f}"
            f"  min={mn*scale:7.2f}  max={mx*scale:7.2f}"
            f"  std={sd*scale:6.2f}  [{unit}]"
        )

    print(f"Packets processed     : {n}")
    print(f"Successful positions  : {successes}  ({100*successes/n:.1f} %)")
    print(f"Total wall time       : {total_wall:.3f} s")
    print(f"Avg sane receivers    : {statistics.mean(sane_counts):.1f} / {lib.receiver_count}")
    print()
    _row(iter_times,  "Round-trip time")
    _row(solve_times, "Solve time only")
    print(
        f"\n  Throughput (solve)   : {1/statistics.mean(solve_times):.1f} pos/s"
        "  (theoretical max)"
    )
    print(
        f"  Throughput (total)   : {n/total_wall:.1f} pos/s"
        "  (actual, includes I/O)"
    )

    if positions:
        xs = [float(p[0]) for p in positions]
        ys = [float(p[1]) for p in positions]
        print(f"\nPosition statistics ({lib.units}):")
        print(
            f"  X — mean={statistics.mean(xs):8.2f}  "
            f"std={statistics.stdev(xs) if len(xs)>1 else 0.0:6.2f}  "
            f"min={min(xs):8.2f}  max={max(xs):8.2f}"
        )
        print(
            f"  Y — mean={statistics.mean(ys):8.2f}  "
            f"std={statistics.stdev(ys) if len(ys)>1 else 0.0:6.2f}  "
            f"min={min(ys):8.2f}  max={max(ys):8.2f}"
        )

    if first_raw:
        print(f"\nSample message (first) : {first_raw}")
    if last_raw and last_raw != first_raw:
        print(f"Sample message (last)  : {last_raw}")


# ─────────────────────────────────────────────────────────────────────────────
# TCP polling test
# ─────────────────────────────────────────────────────────────────────────────

def run_tcp_test(
    lib:           UltraGPSPositionLib,
    ip:            str,
    tcp_port:      int,
    num_iterations: int,
    use_cep:       bool,
    max_subsets:   int,
) -> None:
    print(f"Connecting to {ip}:{tcp_port} (TCP) ...", end=" ", flush=True)
    try:
        sock = open_tcp(ip, tcp_port)
    except (OSError, ConnectionRefusedError) as exc:
        print(f"FAILED\n  {exc}")
        return
    print("OK")

    # Warm-up — prime the TCP connection and the differential filter
    print("Warming up (3 cycles) ...", end=" ", flush=True)
    for _ in range(3):
        raw = tcp_poll(sock)
        if raw:
            lib.get_position(UltraGPSPositionLib.parse_message(raw))
        time.sleep(0.05)
    lib.reset_state()
    print("done\n")

    method = f"CEP subset (max_subsets={max_subsets})" if use_cep else "OLS + Levenberg-Marquardt"
    print(f"Running {num_iterations} TCP poll iterations  [{method}]")
    print("-" * 65)

    iter_times:  list[float] = []
    solve_times: list[float] = []
    positions:   list        = []
    sane_counts: list[int]   = []
    successes:   int         = 0
    first_raw:   str | None  = None
    last_raw:    str | None  = None

    total_start = time.time()

    for i in range(num_iterations):
        t0 = time.time()

        raw = tcp_poll(sock)
        if raw is None:
            print(f"  [iter {i+1:4d}] No response — skipping")
            continue

        ticks = UltraGPSPositionLib.parse_message(raw)

        t_solve = time.time()
        result  = lib.get_position_cep(ticks, max_subsets=max_subsets) if use_cep \
                  else lib.get_position(ticks)
        solve_elapsed = time.time() - t_solve

        elapsed = time.time() - t0
        iter_times.append(elapsed)
        solve_times.append(solve_elapsed)
        sane_counts.append(len(result["sane_indices"]))

        if result["success"] and result["position"] is not None:
            successes += 1
            positions.append(result["position"].copy())

        if i == 0:
            first_raw = raw.strip()
        last_raw = raw.strip()

        if (i + 1) % 10 == 0:
            avg10 = statistics.mean(iter_times[-10:])
            pos   = result["position"]
            pos_s = (
                f"({pos[0]:7.1f}, {pos[1]:7.1f})"
                if pos is not None else "(    None    )"
            )
            extra = (
                f"  CEP={result.get('cep', 0.0):5.1f} cm"
                if use_cep else
                f"  rms={result.get('residual_rms') or 0.0:5.1f} cm"
            )
            print(
                f"  iter {i+1:4d}/{num_iterations}  "
                f"total={elapsed*1000:6.1f} ms  "
                f"solve={solve_elapsed*1000:5.1f} ms  "
                f"sane={len(result['sane_indices'])}/{lib.receiver_count}  "
                f"pos={pos_s}{extra}"
            )

    sock.close()
    total_wall = time.time() - total_start

    print("\n" + "=" * 65)
    print("TCP TEST RESULTS")
    print("=" * 65)
    _print_stats(
        lib, iter_times, solve_times, sane_counts,
        successes, positions, total_wall, first_raw, last_raw, use_cep,
    )


# ─────────────────────────────────────────────────────────────────────────────
# UDP continuous-stream test
# ─────────────────────────────────────────────────────────────────────────────

def run_udp_test(
    lib:          UltraGPSPositionLib,
    ip:           str,
    tcp_port:     int,
    udp_port:     int,
    duration:     float,
    use_cep:      bool,
    max_subsets:  int,
) -> None:
    # ── Open TCP connection and start continuous mode ─────────────────────────
    print(f"Connecting to {ip}:{tcp_port} (TCP) ...", end=" ", flush=True)
    try:
        tcp_sock = open_tcp(ip, tcp_port)
    except (OSError, ConnectionRefusedError) as exc:
        print(f"FAILED\n  {exc}")
        return
    print("OK")

    # ── Register with UDP server BEFORE sending C, so no packets are missed ──
    print(f"Registering UDP socket on {ip}:{udp_port} ...", end=" ", flush=True)
    try:
        udp_sock = open_udp(ip, udp_port)
    except OSError as exc:
        print(f"FAILED\n  {exc}")
        tcp_sock.close()
        return
    local_port = udp_sock.getsockname()[1]
    print(f"OK  (local port {local_port})")

    # ── Send "C" to start continuous streaming ────────────────────────────────
    print("Sending continuous command (C) ...", end=" ", flush=True)
    try:
        tcp_sock.sendall(TCP_CONTINUOUS_CMD)
    except OSError as exc:
        print(f"FAILED\n  {exc}")
        tcp_sock.close()
        udp_sock.close()
        return
    # The server does NOT echo a TCP response for the C command — continuous
    # data flows via UDP only.  Give the Arduino a moment to start streaming.
    time.sleep(0.3)
    print("OK")

    method = f"CEP subset (max_subsets={max_subsets})" if use_cep else "OLS + Levenberg-Marquardt"
    print(f"\nCapturing UDP stream for {duration:.0f} s  [{method}]")
    print("-" * 65)

    iter_times:  list[float] = []
    solve_times: list[float] = []
    positions:   list        = []
    sane_counts: list[int]   = []
    successes:   int         = 0
    first_raw:   str | None  = None
    last_raw:    str | None  = None
    pkt_count:   int         = 0

    capture_start = time.time()
    next_report   = capture_start + 2.0   # print a status line every 2 s

    while True:
        now = time.time()
        elapsed_total = now - capture_start
        if elapsed_total >= duration:
            break

        # ── Receive one UDP datagram ──────────────────────────────────────────
        t0 = now
        try:
            data, _ = udp_sock.recvfrom(RECV_BUFSIZE)
        except socket.timeout:
            continue          # no packet in the last 1 s — keep waiting
        except OSError as exc:
            print(f"\n  UDP receive error: {exc}")
            break

        raw = data.decode("utf-8", errors="replace")
        pkt_count += 1

        ticks = UltraGPSPositionLib.parse_message(raw)
        if not ticks:
            continue          # malformed packet

        # ── Solve position ────────────────────────────────────────────────────
        t_solve = time.time()
        result  = lib.get_position_cep(ticks, max_subsets=max_subsets) if use_cep \
                  else lib.get_position(ticks)
        solve_elapsed = time.time() - t_solve

        iter_elapsed = time.time() - t0
        iter_times.append(iter_elapsed)
        solve_times.append(solve_elapsed)
        sane_counts.append(len(result["sane_indices"]))

        if result["success"] and result["position"] is not None:
            successes += 1
            positions.append(result["position"].copy())

        if first_raw is None:
            first_raw = raw.strip()
        last_raw = raw.strip()

        # ── Periodic status line ──────────────────────────────────────────────
        if time.time() >= next_report:
            pos   = result["position"]
            pos_s = (
                f"({pos[0]:7.1f}, {pos[1]:7.1f})"
                if pos is not None else "(    None    )"
            )
            extra = (
                f"  CEP={result.get('cep', 0.0):5.1f} cm"
                if use_cep else
                f"  rms={result.get('residual_rms') or 0.0:5.1f} cm"
            )
            rate = pkt_count / elapsed_total if elapsed_total > 0 else 0
            print(
                f"  t={elapsed_total:5.1f}s  "
                f"pkts={pkt_count:5d}  "
                f"rate={rate:5.1f} pkt/s  "
                f"solve={solve_elapsed*1000:5.1f} ms  "
                f"sane={len(result['sane_indices'])}/{lib.receiver_count}  "
                f"pos={pos_s}{extra}"
            )
            next_report += 2.0

    total_wall = time.time() - capture_start

    # ── Send "P" to take server out of continuous mode ────────────────────────
    try:
        tcp_sock.sendall(TCP_STOP_CMD)
    except OSError:
        pass
    tcp_sock.close()
    udp_sock.close()

    print(f"\n  Capture complete — {pkt_count} packets received in {total_wall:.1f} s")

    print("\n" + "=" * 65)
    print("UDP CONTINUOUS TEST RESULTS")
    print("=" * 65)
    _print_stats(
        lib, iter_times, solve_times, sane_counts,
        successes, positions, total_wall, first_raw, last_raw, use_cep,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="UltraGPS ultragps_position performance and accuracy test",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=os.path.join(_PROJECT_ROOT, "config.toml"),
        help="Path to config.toml",
    )
    parser.add_argument(
        "--ip",
        default="127.0.0.1",
        help="IP address of the UltraGPS-Ground server",
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        default=9000,
        dest="tcp_port",
        help="TCP command port",
    )
    parser.add_argument(
        "--udp-port",
        type=int,
        default=9001,
        dest="udp_port",
        help="UDP continuous-stream port",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=100,
        help="Poll cycles (TCP mode only)",
    )
    parser.add_argument(
        "-u", "--udp",
        action="store_true",
        help="Use UDP continuous-stream mode instead of TCP polling",
    )
    parser.add_argument(
        "--udp-duration",
        type=float,
        default=20.0,
        dest="udp_duration",
        help="Capture window in seconds (UDP mode only)",
    )
    parser.add_argument(
        "--cep",
        action="store_true",
        help="Use CEP subset-selection solver instead of plain OLS+LM",
    )
    parser.add_argument(
        "--max-subsets",
        type=int,
        default=15,
        dest="max_subsets",
        help="Maximum receiver subsets to evaluate (CEP mode only)",
    )
    args = parser.parse_args()

    # ── Load library ──────────────────────────────────────────────────────────
    print(f"Loading config: {args.config}")
    lib = UltraGPSPositionLib(args.config)
    coords = lib.receiver_coords
    print(f"  Receivers : {lib.receiver_count}")
    print(f"  Units     : {lib.units}")
    print(
        f"  Arena     : x=[{coords[:, 0].min():.1f}, {coords[:, 0].max():.1f}]  "
        f"y=[{coords[:, 1].min():.1f}, {coords[:, 1].max():.1f}] {lib.units}"
    )
    print()

    if args.udp:
        run_udp_test(
            lib=lib,
            ip=args.ip,
            tcp_port=args.tcp_port,
            udp_port=args.udp_port,
            duration=args.udp_duration,
            use_cep=args.cep,
            max_subsets=args.max_subsets,
        )
    else:
        run_tcp_test(
            lib=lib,
            ip=args.ip,
            tcp_port=args.tcp_port,
            num_iterations=args.iterations,
            use_cep=args.cep,
            max_subsets=args.max_subsets,
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
