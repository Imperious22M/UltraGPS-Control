"""
ultragps-position-test.py
=========================
Comprehensive test for the ultragps_position library.

Runs in two stages:

  1. Offline tests (no server required)
       Validates parse_message(), library initialisation, reset_state(),
       and reload() without touching the network.

  2. Live benchmarks (server required, --ip)
       TCP mode  — polls the server via ultragps_client and times all three
                   solvers (get_position, get_position_cep, get_position_full)
                   head-to-head, then prints a comparison table.
       UDP mode  — streams from the server for --udp-duration seconds and
                   benchmarks get_position_full.

Usage examples
--------------
    # Offline tests only (no server needed)
    python ultragps-position-test.py --offline

    # Full test against local server
    python ultragps-position-test.py

    # Full test against remote server, longer UDP capture
    python ultragps-position-test.py --ip 192.168.1.50 --udp-duration 30

    # Skip UDP benchmark
    python ultragps-position-test.py --no-udp

Options
-------
    --config        Path to config.toml           (default: ../config.toml)
    --ip            UltraGPS-Ground server IP      (default: 127.0.0.1)
    --tcp-port      TCP command port               (default: 9000)
    --udp-port      UDP stream port                (default: 9001)
    --iterations    TCP poll cycles per solver     (default: 50)
    --udp-duration  UDP capture window (seconds)   (default: 20)
    --offline       Run offline tests only
    --no-udp        Skip the UDP benchmark
"""

import argparse
import os
import statistics
import sys
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from ultragps_position import UltraGPSPositionLib
from ultragps_client import UltraGPSClient, CommMode

PASS = "PASS"
FAIL = "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _connect(ip: str, tcp_port: int, udp_port: int) -> UltraGPSClient | None:
    """Create and connect a client, printing the outcome."""
    client = UltraGPSClient(host=ip, tcp_port=tcp_port, udp_port=udp_port)
    try:
        client.connect()
        return client
    except ConnectionError as exc:
        print(f"  FAILED — {exc}")
        return None


def _timing_row(label: str, data: list[float]) -> str:
    if not data:
        return f"  {label:<28} no data"
    avg = statistics.mean(data) * 1000
    med = statistics.median(data) * 1000
    mn  = min(data) * 1000
    mx  = max(data) * 1000
    sd  = (statistics.stdev(data) if len(data) > 1 else 0.0) * 1000
    return (
        f"  {label:<28} mean={avg:7.2f}  median={med:7.2f}"
        f"  min={mn:7.2f}  max={mx:7.2f}  std={sd:6.2f}  [ms]"
    )


def _position_row(label: str, positions: list) -> str:
    if not positions:
        return f"  {label:<28} no successful positions"
    xs = [float(p[0]) for p in positions]
    ys = [float(p[1]) for p in positions]
    return (
        f"  {label:<28} "
        f"X mean={statistics.mean(xs):7.1f} std={statistics.stdev(xs) if len(xs)>1 else 0.0:5.1f}  "
        f"Y mean={statistics.mean(ys):7.1f} std={statistics.stdev(ys) if len(ys)>1 else 0.0:5.1f}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Offline tests
# ─────────────────────────────────────────────────────────────────────────────

def run_offline_tests(config_path: str) -> bool:
    print("=" * 70)
    print("OFFLINE TESTS")
    print("=" * 70)

    results: list[tuple[str, str, str]] = []   # (name, PASS/FAIL, note)

    def check(name: str, passed: bool, note: str = "") -> None:
        status = PASS if passed else FAIL
        results.append((name, status, note))
        marker = "  [PASS]" if passed else "  [FAIL]"
        print(f"{marker}  {name}" + (f"  — {note}" if note else ""))

    # ── parse_message ─────────────────────────────────────────────────────────
    print("\n-- parse_message --")

    tcp_msg = "N: 1000, 2000, 3000, 4000, 5000, 6000"
    result  = UltraGPSPositionLib.parse_message(tcp_msg)
    check("TCP prefix stripped",
          result == [1000, 2000, 3000, 4000, 5000, 6000],
          str(result))

    udp_msg = "C: 1000, 2000, 3000, 4000, 5000, 6000"
    result  = UltraGPSPositionLib.parse_message(udp_msg)
    check("UDP prefix stripped",
          result == [1000, 2000, 3000, 4000, 5000, 6000],
          str(result))

    raw_msg = "1000, 2000, 3000, 4000, 5000, 6000"
    result  = UltraGPSPositionLib.parse_message(raw_msg)
    check("No-prefix message",
          result == [1000, 2000, 3000, 4000, 5000, 6000],
          str(result))

    float_msg = "N: 1000.7, 2000.3, 3000.9, 4000.1, 5000.5, 6000.2"
    result    = UltraGPSPositionLib.parse_message(float_msg)
    check("Float values cast to int",
          result == [1000, 2000, 3000, 4000, 5000, 6000],
          str(result))

    result = UltraGPSPositionLib.parse_message("garbage input!!!")
    check("Invalid input returns []", result == [], str(result))

    result = UltraGPSPositionLib.parse_message("")
    check("Empty string returns []", result == [], str(result))

    # ── Initialisation / properties ───────────────────────────────────────────
    print("\n-- Initialisation --")

    try:
        lib = UltraGPSPositionLib(config_path)
        check("Loads config without error", True, f"path={config_path}")
    except Exception as exc:
        check("Loads config without error", False, str(exc))
        print("\n[ABORT] Cannot continue offline tests without a valid config.")
        _print_summary(results)
        return False

    check("receiver_count > 0",
          lib.receiver_count > 0,
          str(lib.receiver_count))

    check("receiver_coords shape",
          lib.receiver_coords.shape == (lib.receiver_count, 2),
          str(lib.receiver_coords.shape))

    check("units is a string",
          isinstance(lib.units, str) and len(lib.units) > 0,
          repr(lib.units))

    check("max_differential default 30",
          lib.max_differential == 30.0,
          str(lib.max_differential))

    # ── reset_state ───────────────────────────────────────────────────────────
    print("\n-- reset_state --")

    # Poison the filter state with a fake distance call
    fake_ticks = [1000] * lib.receiver_count
    lib.get_position(fake_ticks)
    had_state = lib._last_distances is not None

    lib.reset_state()
    check("reset_state clears _last_distances",
          lib._last_distances is None,
          f"had state before: {had_state}")
    check("reset_state clears _lm_last_good_pos",
          lib._lm_last_good_pos is None)
    check("reset_state clears _cep_last_good_pos",
          lib._cep_last_good_pos is None)
    check("receiver_count unchanged after reset",
          lib.receiver_count > 0,
          str(lib.receiver_count))

    # ── reload ────────────────────────────────────────────────────────────────
    print("\n-- reload --")

    original_count = lib.receiver_count
    original_units = lib.units

    try:
        lib.reload()
        check("reload() with no argument succeeds", True)
        check("reload() preserves receiver_count",
              lib.receiver_count == original_count,
              f"{lib.receiver_count} == {original_count}")
        check("reload() preserves units",
              lib.units == original_units,
              repr(lib.units))
        check("reload() resets state (_last_distances is None)",
              lib._last_distances is None)
    except Exception as exc:
        check("reload() with no argument succeeds", False, str(exc))

    try:
        lib.reload(config_path)
        check("reload(explicit_path) succeeds", True)
    except Exception as exc:
        check("reload(explicit_path) succeeds", False, str(exc))

    try:
        lib.reload("/nonexistent/path/config.toml")
        check("reload(bad_path) raises FileNotFoundError", False,
              "no exception raised")
    except FileNotFoundError:
        check("reload(bad_path) raises FileNotFoundError", True)
    except Exception as exc:
        check("reload(bad_path) raises FileNotFoundError", False,
              f"wrong exception: {type(exc).__name__}: {exc}")
    finally:
        lib.reload(config_path)   # restore good state

    # ── get_position return shape ─────────────────────────────────────────────
    print("\n-- Return value structure --")

    ticks = [1000] * lib.receiver_count
    lib.reset_state()

    r = lib.get_position(ticks)
    check("get_position returns dict",          isinstance(r, dict))
    check("get_position has 'distances' key",   "distances"    in r)
    check("get_position has 'sane_indices' key","sane_indices" in r)
    check("get_position has 'success' key",     "success"      in r)
    check("get_position has 'residual_rms' key","residual_rms" in r)
    check("get_position has 'position' key",    "position"     in r)
    check("get_position distances length == receiver_count",
          len(r["distances"]) == lib.receiver_count,
          f"{len(r['distances'])} == {lib.receiver_count}")

    lib.reset_state()
    r = lib.get_position_cep(ticks)
    check("get_position_cep returns dict",           isinstance(r, dict))
    check("get_position_cep has 'best_indices' key", "best_indices" in r)
    check("get_position_cep has 'cep' key",          "cep"          in r)
    check("get_position_cep has 'cov' key",          "cov"          in r)

    lib.reset_state()
    r = lib.get_position_full(ticks)
    check("get_position_full returns dict",             isinstance(r, dict))
    check("get_position_full has 'lm_position' key",   "lm_position"  in r)
    check("get_position_full has 'cep_position' key",  "cep_position" in r)
    check("get_position_full has 'lm_success' key",    "lm_success"   in r)
    check("get_position_full has 'cep_success' key",   "cep_success"  in r)
    check("get_position_full has 'lm_rms' key",        "lm_rms"       in r)
    check("get_position_full has 'best_indices' key",  "best_indices" in r)

    # ── Summary ───────────────────────────────────────────────────────────────
    return _print_summary(results)


def _print_summary(results: list) -> bool:
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = sum(1 for _, s, _ in results if s == FAIL)
    print(f"\n  {passed} passed, {failed} failed")
    return failed == 0


# ─────────────────────────────────────────────────────────────────────────────
# 2. TCP benchmark — all three solvers compared
# ─────────────────────────────────────────────────────────────────────────────

def run_tcp_benchmark(
    lib:        UltraGPSPositionLib,
    ip:         str,
    tcp_port:   int,
    udp_port:   int,
    iterations: int,
) -> None:
    print("\n" + "=" * 70)
    print("TCP BENCHMARK  —  get_position vs get_position_cep vs get_position_full")
    print("=" * 70)

    print(f"Connecting to {ip}:{tcp_port} ...", end=" ", flush=True)
    client = _connect(ip, tcp_port, udp_port)
    if client is None:
        return
    print("OK")

    # Warm up
    print("Warming up (3 cycles) ...", end=" ", flush=True)
    for _ in range(3):
        ticks = client.pulse()
        if ticks:
            lib.get_position(ticks)
        time.sleep(0.05)
    lib.reset_state()
    print("done\n")

    print(f"Running {iterations} iterations per solver ...")
    print("-" * 70)

    # Storage per solver
    solvers = ["get_position", "get_position_cep", "get_position_full"]
    solve_times: dict[str, list[float]] = {s: [] for s in solvers}
    positions:   dict[str, list]        = {s: [] for s in solvers}
    successes:   dict[str, int]         = {s: 0  for s in solvers}
    sane_counts: dict[str, list[int]]   = {s: [] for s in solvers}

    total_start = time.time()

    for i in range(iterations):
        ticks = client.pulse()
        if ticks is None:
            print(f"  [iter {i+1:4d}] No response — skipping")
            continue

        # Run each solver on the same ticks, resetting state between them so
        # the differential filter sees each call independently.
        saved = (
            lib._last_distances.copy() if lib._last_distances is not None else None,
            lib._last_sane_indices.copy() if lib._last_sane_indices is not None else None,
            lib._lm_last_good_pos.copy() if lib._lm_last_good_pos is not None else None,
            lib._cep_last_good_pos.copy() if lib._cep_last_good_pos is not None else None,
        )

        for solver in solvers:
            # Restore identical state before each solver so timings are fair
            lib._last_distances    = saved[0].copy() if saved[0] is not None else None
            lib._last_sane_indices = saved[1].copy() if saved[1] is not None else None
            lib._lm_last_good_pos  = saved[2].copy() if saved[2] is not None else None
            lib._cep_last_good_pos = saved[3].copy() if saved[3] is not None else None

            t0 = time.time()
            if solver == "get_position":
                r = lib.get_position(ticks)
                sane = r["sane_indices"]
                pos  = r["position"]
                ok   = r["success"]
            elif solver == "get_position_cep":
                r = lib.get_position_cep(ticks)
                sane = r["sane_indices"]
                pos  = r["position"]
                ok   = r["success"]
            else:
                r = lib.get_position_full(ticks)
                sane = r["sane_indices"]
                pos  = r["lm_position"]
                ok   = r["lm_success"]
            solve_times[solver].append(time.time() - t0)
            sane_counts[solver].append(len(sane))
            if ok and pos is not None:
                successes[solver] += 1
                positions[solver].append(pos.copy())

        # Advance the library's own state using get_position_full for next iteration
        lib._last_distances    = saved[0].copy() if saved[0] is not None else None
        lib._last_sane_indices = saved[1].copy() if saved[1] is not None else None
        lib.get_position_full(ticks)

        if (i + 1) % 10 == 0:
            row = "  ".join(
                f"{s.replace('get_position','gp')}: {solve_times[s][-1]*1000:.1f}ms"
                for s in solvers
            )
            print(f"  iter {i+1:4d}/{iterations}  {row}  sane={sane_counts['get_position'][-1]}/{lib.receiver_count}")

    client.disconnect()
    total_wall = time.time() - total_start
    n = min(len(solve_times[s]) for s in solvers)

    print(f"\n  {n} iterations completed in {total_wall:.2f} s\n")

    print("Solve time comparison:")
    for s in solvers:
        print(_timing_row(s, solve_times[s]))

    print(f"\n  Throughput (solve, get_position_full):  "
          f"{1/statistics.mean(solve_times['get_position_full']):.1f} pos/s  (theoretical)")
    print(f"  Throughput (total wall):                "
          f"{n/total_wall:.1f} pos/s  (actual, includes I/O)")

    print("\nPosition accuracy:")
    for s in solvers:
        print(_position_row(s, positions[s]))

    print("\nSuccess rate:")
    for s in solvers:
        sc = successes[s]
        pct = 100 * sc / n if n else 0
        avg_sane = statistics.mean(sane_counts[s]) if sane_counts[s] else 0
        print(f"  {s:<28} {sc}/{n} ({pct:.1f} %)  avg sane={avg_sane:.1f}/{lib.receiver_count}")


# ─────────────────────────────────────────────────────────────────────────────
# 3. UDP benchmark
# ─────────────────────────────────────────────────────────────────────────────

def run_udp_benchmark(
    lib:      UltraGPSPositionLib,
    ip:       str,
    tcp_port: int,
    udp_port: int,
    duration: float,
) -> None:
    print("\n" + "=" * 70)
    print("UDP BENCHMARK  —  get_position_full")
    print("=" * 70)

    print(f"Connecting to {ip}:{tcp_port}/{udp_port} ...", end=" ", flush=True)
    client = _connect(ip, tcp_port, udp_port)
    if client is None:
        return
    print("OK")

    print("Sending continuous command ...", end=" ", flush=True)
    client.continuous()

    # Wait for first UDP reading
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if client.get_latest_reading() is not None:
            break
        time.sleep(0.01)

    if client.get_latest_reading() is None:
        print("TIMEOUT — no UDP data received")
        client.disconnect()
        return
    print("OK")

    lib.reset_state()
    print(f"\nCapturing for {duration:.0f} s ...")
    print("-" * 70)

    solve_times:  list[float] = []
    inter_arrival: list[float] = []
    positions:    list         = []
    sane_counts:  list[int]    = []
    successes:    int          = 0
    pkt_count:    int          = 0
    prev_count:   int          = client.reading_count
    prev_time:    float | None = None
    first_ticks               = None
    last_ticks                = None

    capture_start = time.time()
    next_report   = capture_start + 2.0

    while time.time() - capture_start < duration:
        cur_count = client.reading_count
        ticks     = client.get_latest_reading()

        if cur_count != prev_count and ticks is not None:
            now = time.time()
            if prev_time is not None:
                inter_arrival.append(now - prev_time)
            prev_count = cur_count
            prev_time  = now
            pkt_count += 1

            if first_ticks is None:
                first_ticks = ticks
            last_ticks = ticks

            t_solve = time.time()
            r = lib.get_position_full(ticks)
            solve_elapsed = time.time() - t_solve

            solve_times.append(solve_elapsed)
            sane_counts.append(len(r["sane_indices"]))

            if r["lm_success"] and r["lm_position"] is not None:
                successes += 1
                positions.append(r["lm_position"].copy())

            if time.time() >= next_report:
                elapsed = time.time() - capture_start
                rate    = pkt_count / elapsed if elapsed > 0 else 0
                pos     = r["lm_position"]
                pos_s   = f"({pos[0]:.1f}, {pos[1]:.1f})" if pos is not None else "None"
                print(
                    f"  t={elapsed:5.1f}s  pkts={pkt_count:5d}  rate={rate:5.1f}/s  "
                    f"solve={solve_elapsed*1000:5.2f}ms  "
                    f"sane={len(r['sane_indices'])}/{lib.receiver_count}  pos={pos_s}"
                )
                next_report += 2.0

        time.sleep(0.001)   # 1 ms poll — finer than the ~20 ms stream interval

    total_wall = time.time() - capture_start

    # Return server to normal mode then disconnect
    client.pulse()
    client.disconnect()

    n = len(solve_times)
    print(f"\n  Capture complete — {pkt_count} packets in {total_wall:.1f} s")

    print("\n" + "=" * 70)
    print("UDP BENCHMARK RESULTS")
    print("=" * 70)

    if not n:
        print("No packets processed.")
        return

    print(f"Packets processed     : {pkt_count}")
    print(f"Successful positions  : {successes}  ({100*successes/n:.1f} %)")
    print(f"Avg sane receivers    : {statistics.mean(sane_counts):.1f} / {lib.receiver_count}")
    print(f"Packet rate           : {pkt_count/total_wall:.1f} pkt/s")
    print()
    print(_timing_row("Solve time (full)",   solve_times))
    print(_timing_row("Packet inter-arrival", inter_arrival))
    if inter_arrival:
        print(f"\n  Solve throughput: {1/statistics.mean(solve_times):.1f} pos/s  (theoretical max)")
        print(f"  Stream rate     : {1/statistics.mean(inter_arrival):.1f} pkt/s  (from inter-arrival mean)")
    print(_position_row("get_position_full", positions))

    if first_ticks:
        print(f"\nSample ticks (first): {first_ticks}")
    if last_ticks and last_ticks != first_ticks:
        print(f"Sample ticks (last) : {last_ticks}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    default_config = os.path.join(_PROJECT_ROOT, "config.toml")

    parser = argparse.ArgumentParser(
        description="Comprehensive test for the ultragps_position library",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config",       default=default_config,
                        help="Path to config.toml")
    parser.add_argument("--ip",           default="127.0.0.1",
                        help="UltraGPS-Ground server IP")
    parser.add_argument("--tcp-port",     type=int, default=9000, dest="tcp_port",
                        help="TCP command port")
    parser.add_argument("--udp-port",     type=int, default=9001, dest="udp_port",
                        help="UDP stream port")
    parser.add_argument("--iterations",   type=int, default=50,
                        help="TCP poll cycles per solver")
    parser.add_argument("--udp-duration", type=float, default=20.0, dest="udp_duration",
                        help="UDP capture window in seconds")
    parser.add_argument("--offline",      action="store_true",
                        help="Run offline tests only (no server required)")
    parser.add_argument("--no-udp",       action="store_true", dest="no_udp",
                        help="Skip the UDP benchmark")
    args = parser.parse_args()

    # ── Offline tests ─────────────────────────────────────────────────────────
    offline_ok = run_offline_tests(args.config)

    if args.offline:
        sys.exit(0 if offline_ok else 1)

    # ── Load library for live benchmarks ──────────────────────────────────────
    print("\n" + "=" * 70)
    print("LIVE BENCHMARKS")
    print("=" * 70)
    print(f"Config   : {args.config}")
    print(f"Server   : {args.ip}  TCP:{args.tcp_port}  UDP:{args.udp_port}")

    try:
        lib = UltraGPSPositionLib(args.config)
    except Exception as exc:
        print(f"\n[ERROR] Could not load config: {exc}")
        sys.exit(1)

    coords = lib.receiver_coords
    print(f"Receivers: {lib.receiver_count}  units={lib.units}")
    print(
        f"Arena    : x=[{coords[:,0].min():.1f}, {coords[:,0].max():.1f}]  "
        f"y=[{coords[:,1].min():.1f}, {coords[:,1].max():.1f}] {lib.units}"
    )

    # ── TCP benchmark ─────────────────────────────────────────────────────────
    run_tcp_benchmark(lib, args.ip, args.tcp_port, args.udp_port, args.iterations)

    # ── UDP benchmark ─────────────────────────────────────────────────────────
    if not args.no_udp:
        run_udp_benchmark(lib, args.ip, args.tcp_port, args.udp_port, args.udp_duration)

    print("\nDone.")


if __name__ == "__main__":
    main()
