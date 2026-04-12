"""
ultragps-client-test.py
=======================
Comprehensive benchmark and functional test for the ultragps_client library.

Tests every command the UltraGPS-Ground server supports and measures timing
for each, producing per-command and aggregate throughput statistics.

Sections
--------
  1. Connection test   — connect / disconnect lifecycle
  2. pulse() bench     — TCP P command, N iterations
  3. simulate() bench  — TCP S command, N iterations
  4. continuous() bench — C command + UDP stream, measured over --udp-duration s
  5. Mode transitions   — NORMAL → CONTINUOUS → NORMAL round-trip
  6. Summary table      — all commands side-by-side

Usage examples
--------------
    # All tests against local server
    python ultragps-client-test.py

    # Remote server, more iterations
    python ultragps-client-test.py --ip 192.168.1.50 --iterations 200

    # Skip UDP test
    python ultragps-client-test.py --no-udp

    # Longer UDP capture window
    python ultragps-client-test.py --udp-duration 30

Options
-------
    --ip            UltraGPS-Ground server IP      (default: 127.0.0.1)
    --tcp-port      TCP command port               (default: 9000)
    --udp-port      UDP stream port                (default: 9001)
    --iterations    Cycles for pulse/simulate      (default: 100)
    --udp-duration  UDP capture window (seconds)   (default: 20)
    --no-udp        Skip the UDP continuous test
"""

import argparse
import statistics
import sys
import time

from ultragps_client import UltraGPSClient, CommMode

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _timing_stats(label: str, data: list[float]) -> None:
    """Print a single formatted timing row."""
    if not data:
        print(f"  {label:<24} no data")
        return
    avg = statistics.mean(data) * 1000
    med = statistics.median(data) * 1000
    mn  = min(data) * 1000
    mx  = max(data) * 1000
    sd  = (statistics.stdev(data) if len(data) > 1 else 0.0) * 1000
    print(
        f"  {label:<24} mean={avg:7.2f}  median={med:7.2f}"
        f"  min={mn:7.2f}  max={mx:7.2f}  std={sd:6.2f}  [ms]"
    )


def _check(name: str, passed: bool, note: str = "") -> bool:
    marker = PASS if passed else FAIL
    print(f"  {marker}  {name}" + (f"  — {note}" if note else ""))
    return passed


def _connect(ip: str, tcp_port: int, udp_port: int) -> UltraGPSClient | None:
    """Create and connect a client, printing the outcome."""
    client = UltraGPSClient(host=ip, tcp_port=tcp_port, udp_port=udp_port)
    try:
        client.connect()
        return client
    except ConnectionError as exc:
        print(f"  {FAIL}  connect() — {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 1. Connection lifecycle
# ─────────────────────────────────────────────────────────────────────────────

def test_connection(ip: str, tcp_port: int, udp_port: int) -> bool:
    print("=" * 65)
    print("1. CONNECTION LIFECYCLE")
    print("=" * 65)

    ok = True

    # connect()
    client = UltraGPSClient(host=ip, tcp_port=tcp_port, udp_port=udp_port)
    try:
        client.connect()
        ok &= _check("connect() succeeds", True)
    except ConnectionError as exc:
        ok &= _check("connect() succeeds", False, str(exc))
        return False

    ok &= _check("mode == NORMAL after connect()",
                 client.mode == CommMode.NORMAL, str(client.mode))

    # disconnect()
    client.disconnect()

    # disconnect() is idempotent
    try:
        client.disconnect()
        ok &= _check("disconnect() is idempotent (no exception)", True)
    except Exception as exc:
        ok &= _check("disconnect() is idempotent (no exception)", False, str(exc))

    # Bad host raises ConnectionError
    bad = UltraGPSClient(host="192.0.2.1", tcp_port=tcp_port, udp_port=udp_port)
    bad._timeout = 0.5
    try:
        bad.connect()
        bad.disconnect()
        ok &= _check("bad host raises ConnectionError", False, "no exception raised")
    except ConnectionError:
        ok &= _check("bad host raises ConnectionError", True)
    except Exception as exc:
        ok &= _check("bad host raises ConnectionError", False,
                     f"wrong exception: {type(exc).__name__}: {exc}")

    return ok


# ─────────────────────────────────────────────────────────────────────────────
# 2. pulse() benchmark
# ─────────────────────────────────────────────────────────────────────────────

def bench_pulse(
    ip: str, tcp_port: int, udp_port: int, iterations: int
) -> tuple[list[float], int]:
    """
    Returns (times_list, success_count).
    times_list is empty on connection failure.
    """
    print("\n" + "=" * 65)
    print("2. pulse()  —  TCP P command")
    print("=" * 65)

    client = _connect(ip, tcp_port, udp_port)
    if client is None:
        return [], 0

    # Warm up
    print("  Warming up (3 cycles) ...", end=" ", flush=True)
    for _ in range(3):
        client.pulse()
    print("done")

    print(f"  Running {iterations} iterations ...")
    print("  " + "-" * 61)

    times: list[float] = []
    successes = 0

    wall_start = time.time()

    for i in range(iterations):
        t0 = time.time()
        result = client.pulse()
        elapsed = time.time() - t0

        times.append(elapsed)
        if result is not None:
            successes += 1

        if (i + 1) % 10 == 0:
            avg10 = statistics.mean(times[-10:]) * 1000
            vals  = str(result) if result else "None"
            print(
                f"  iter {i+1:4d}/{iterations}  "
                f"time={elapsed*1000:6.2f} ms  "
                f"avg10={avg10:6.2f} ms  "
                f"ok={result is not None}  ticks={vals}"
            )

    total_wall = time.time() - wall_start
    client.disconnect()

    print(f"\n  {iterations} iterations in {total_wall:.3f} s")
    print(f"  Successes: {successes}/{iterations}  ({100*successes/iterations:.1f} %)")
    _timing_stats("pulse() round-trip", times)
    print(f"  Throughput: {iterations/total_wall:.1f} cmd/s  (actual, includes I/O)")
    print(f"            : {1/statistics.mean(times):.1f} cmd/s  (theoretical max)")

    return times, successes


# ─────────────────────────────────────────────────────────────────────────────
# 3. simulate() benchmark
# ─────────────────────────────────────────────────────────────────────────────

def bench_simulate(
    ip: str, tcp_port: int, udp_port: int, iterations: int
) -> tuple[list[float], int]:
    """Returns (times_list, success_count)."""
    print("\n" + "=" * 65)
    print("3. simulate()  —  TCP S command")
    print("=" * 65)

    client = _connect(ip, tcp_port, udp_port)
    if client is None:
        return [], 0

    print("  Warming up (3 cycles) ...", end=" ", flush=True)
    for _ in range(3):
        client.simulate()
    print("done")

    print(f"  Running {iterations} iterations ...")
    print("  " + "-" * 61)

    times: list[float] = []
    successes = 0

    wall_start = time.time()

    for i in range(iterations):
        t0 = time.time()
        result = client.simulate()
        elapsed = time.time() - t0

        times.append(elapsed)
        if result is not None:
            successes += 1

        if (i + 1) % 10 == 0:
            avg10 = statistics.mean(times[-10:]) * 1000
            vals  = str(result) if result else "None"
            print(
                f"  iter {i+1:4d}/{iterations}  "
                f"time={elapsed*1000:6.2f} ms  "
                f"avg10={avg10:6.2f} ms  "
                f"ok={result is not None}  ticks={vals}"
            )

    total_wall = time.time() - wall_start
    client.disconnect()

    print(f"\n  {iterations} iterations in {total_wall:.3f} s")
    print(f"  Successes: {successes}/{iterations}  ({100*successes/iterations:.1f} %)")
    _timing_stats("simulate() round-trip", times)
    print(f"  Throughput: {iterations/total_wall:.1f} cmd/s  (actual, includes I/O)")
    print(f"            : {1/statistics.mean(times):.1f} cmd/s  (theoretical max)")

    return times, successes


# ─────────────────────────────────────────────────────────────────────────────
# 4. continuous() / UDP stream benchmark
# ─────────────────────────────────────────────────────────────────────────────

def bench_continuous(
    ip: str, tcp_port: int, udp_port: int, duration: float
) -> list[float]:
    """
    Measures the inter-arrival time of UDP readings for `duration` seconds.
    Returns a list of inter-arrival times.
    """
    print("\n" + "=" * 65)
    print("4. continuous()  —  UDP stream")
    print("=" * 65)

    client = _connect(ip, tcp_port, udp_port)
    if client is None:
        return []

    print("  Sending continuous command ...", end=" ", flush=True)
    client.continuous()

    # Wait for the first reading to confirm streaming has started
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if client.get_latest_reading() is not None:
            break
        time.sleep(0.01)

    if client.get_latest_reading() is None:
        print("TIMEOUT — no UDP data received")
        client.disconnect()
        return []
    print("OK")

    print(f"  Measuring UDP stream for {duration:.0f} s ...")
    print("  " + "-" * 61)

    inter_arrival: list[float] = []
    pkt_count = 0
    prev_count = client.reading_count
    prev_time = None
    next_report = time.time() + 2.0

    capture_start = time.time()

    while time.time() - capture_start < duration:
        cur_count = client.reading_count
        reading   = client.get_latest_reading()

        if cur_count != prev_count and reading is not None:
            now = time.time()
            if prev_time is not None:
                inter_arrival.append(now - prev_time)
            prev_count = cur_count
            prev_time  = now
            pkt_count += 1

            if time.time() >= next_report:
                elapsed = time.time() - capture_start
                rate = pkt_count / elapsed if elapsed > 0 else 0
                avg_ia = statistics.mean(inter_arrival) * 1000 if inter_arrival else 0
                print(
                    f"  t={elapsed:5.1f}s  "
                    f"readings={pkt_count:5d}  "
                    f"rate={rate:6.1f}/s  "
                    f"avg_interval={avg_ia:.1f} ms  "
                    f"latest={reading}"
                )
                next_report += 2.0

        time.sleep(0.001)   # 1 ms poll — finer than the ~20 ms stream interval

    total_wall = time.time() - capture_start

    # Return to normal mode
    result = client.pulse()
    client.disconnect()

    print(f"\n  Capture complete — {pkt_count} distinct readings in {total_wall:.1f} s")

    if not inter_arrival:
        print("  No inter-arrival data collected.")
        return []

    avg_rate = pkt_count / total_wall
    print(f"  Stream rate     : {avg_rate:.1f} readings/s  (actual)")
    _timing_stats("UDP inter-arrival", inter_arrival)
    print(
        f"  Effective rate  : "
        f"{1/statistics.mean(inter_arrival):.1f} readings/s  (from inter-arrival mean)"
    )

    return inter_arrival


# ─────────────────────────────────────────────────────────────────────────────
# 5. Mode transition test
# ─────────────────────────────────────────────────────────────────────────────

def test_mode_transitions(ip: str, tcp_port: int, udp_port: int) -> bool:
    print("\n" + "=" * 65)
    print("5. MODE TRANSITIONS")
    print("=" * 65)

    client = _connect(ip, tcp_port, udp_port)
    if client is None:
        return False

    ok = True

    # Initial state
    ok &= _check("mode == NORMAL after connect()",
                 client.mode == CommMode.NORMAL, str(client.mode))

    # NORMAL → CONTINUOUS
    client.continuous()
    ok &= _check("mode == CONTINUOUS after continuous()",
                 client.mode == CommMode.CONTINUOUS, str(client.mode))

    # Wait for data to arrive
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if client.get_latest_reading() is not None:
            break
        time.sleep(0.01)
    ok &= _check("get_latest_reading() returns data in CONTINUOUS mode",
                 client.get_latest_reading() is not None)

    # CONTINUOUS → NORMAL via pulse()
    result = client.pulse()
    ok &= _check("pulse() returns data after leaving CONTINUOUS",
                 result is not None, str(result))
    ok &= _check("mode == NORMAL after pulse()",
                 client.mode == CommMode.NORMAL, str(client.mode))

    # get_latest_reading() returns None in NORMAL mode
    ok &= _check("get_latest_reading() returns None in NORMAL mode",
                 client.get_latest_reading() is None)

    # NORMAL → CONTINUOUS → NORMAL via simulate()
    client.continuous()
    time.sleep(0.3)
    result = client.simulate()
    ok &= _check("simulate() exits CONTINUOUS and returns data",
                 result is not None, str(result))
    ok &= _check("mode == NORMAL after simulate()",
                 client.mode == CommMode.NORMAL, str(client.mode))

    # Return value structure
    result = client.pulse()
    if result is not None:
        ok &= _check("pulse() returns list",          isinstance(result, list))
        ok &= _check("pulse() returns 6 values",      len(result) == 6,
                     str(len(result)))
        ok &= _check("pulse() values are integers",
                     all(isinstance(v, int) for v in result))
        ok &= _check("pulse() values are non-negative",
                     all(v >= 0 for v in result), str(result))
    else:
        _check("pulse() return value checks", False, "got None")
        ok = False

    client.disconnect()
    return ok


# ─────────────────────────────────────────────────────────────────────────────
# 6. Summary table
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(
    pulse_times:    list[float],
    simulate_times: list[float],
    udp_intervals:  list[float],
) -> None:
    print("\n" + "=" * 65)
    print("SUMMARY")
    print("=" * 65)

    def _row(label: str, data: list[float]) -> None:
        if not data:
            print(f"  {label:<22} — no data")
            return
        avg  = statistics.mean(data) * 1000
        med  = statistics.median(data) * 1000
        mn   = min(data) * 1000
        mx   = max(data) * 1000
        rate = 1.0 / statistics.mean(data)
        print(
            f"  {label:<22} "
            f"mean={avg:7.2f} ms  median={med:7.2f} ms  "
            f"min={mn:7.2f} ms  max={mx:7.2f} ms  "
            f"→ {rate:6.1f} cmd/s"
        )

    _row("pulse()  (TCP)",    pulse_times)
    _row("simulate()  (TCP)", simulate_times)
    if udp_intervals:
        avg_interval = statistics.mean(udp_intervals) * 1000
        rate = 1000.0 / avg_interval if avg_interval > 0 else 0
        mn   = min(udp_intervals) * 1000
        mx   = max(udp_intervals) * 1000
        print(
            f"  {'continuous()  (UDP)':<22} "
            f"mean={avg_interval:7.2f} ms  "
            f"min={mn:7.2f} ms  max={mx:7.2f} ms  "
            f"→ {rate:6.1f} readings/s"
        )
    else:
        print(f"  {'continuous()  (UDP)':<22} — not run")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="UltraGPS client library benchmark and functional test",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--ip",           default="127.0.0.1",
                        help="UltraGPS-Ground server IP")
    parser.add_argument("--tcp-port",     type=int, default=9000, dest="tcp_port",
                        help="TCP command port")
    parser.add_argument("--udp-port",     type=int, default=9001, dest="udp_port",
                        help="UDP stream port")
    parser.add_argument("--iterations",   type=int, default=100,
                        help="Cycles for pulse/simulate benchmarks")
    parser.add_argument("--udp-duration", type=float, default=20.0, dest="udp_duration",
                        help="UDP continuous capture window in seconds")
    parser.add_argument("--no-udp",       action="store_true", dest="no_udp",
                        help="Skip the UDP continuous benchmark")
    args = parser.parse_args()

    print(f"UltraGPS Client Test  —  {args.ip}  TCP:{args.tcp_port}  UDP:{args.udp_port}")
    print()

    # 1. Connection lifecycle
    conn_ok = test_connection(args.ip, args.tcp_port, args.udp_port)
    if not conn_ok:
        print("\n[ABORT] Connection test failed — cannot run benchmarks.")
        sys.exit(1)

    # 2. pulse() benchmark
    pulse_times, _ = bench_pulse(args.ip, args.tcp_port, args.udp_port, args.iterations)

    # 3. simulate() benchmark
    sim_times, _ = bench_simulate(args.ip, args.tcp_port, args.udp_port, args.iterations)

    # 4. UDP continuous benchmark
    udp_intervals: list[float] = []
    if not args.no_udp:
        udp_intervals = bench_continuous(
            args.ip, args.tcp_port, args.udp_port, args.udp_duration
        )
    else:
        print("\n[SKIP]  UDP continuous benchmark (--no-udp)")

    # 5. Mode transitions
    test_mode_transitions(args.ip, args.tcp_port, args.udp_port)

    # 6. Summary
    print_summary(pulse_times, sim_times, udp_intervals)

    print("\nDone.")


if __name__ == "__main__":
    main()
