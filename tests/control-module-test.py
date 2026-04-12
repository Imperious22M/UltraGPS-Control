"""
control-module-test.py
======================
End-to-end performance test for the UltraGPS control pipeline.

Polls the UltraGPS-Ground server via ControlModule, converts each raw tick
message to a 2D position using UltraGPSPositionLib, and reports timing and position
statistics for the full round-trip.

Usage examples
--------------
    # Defaults: 100 iterations, local server, config.toml beside this script
    python control-module-test.py

    # CEP solver, 200 iterations, remote server
    python control-module-test.py --cep --iterations 200 --ip 192.168.1.50

    # Different config file
    python control-module-test.py --config /path/to/config.toml

Options
-------
    --config        Path to config.toml           (default: ../config.toml)
    --ip            UltraGPS-Ground server IP      (default: 127.0.0.1)
    --iterations    Number of poll cycles          (default: 100)
    --cep           Use CEP subset-selection solver
    --max-subsets   Receiver subsets for CEP       (default: 15)
"""

import argparse
import os
import sys
import statistics
import time

# Add the project root (one level up from tests/) to the path so that
# ControlModule and other project modules can be imported.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from ControlModule import ControlModule
from ultragps_position import UltraGPSPositionLib


# ─────────────────────────────────────────────────────────────────────────────

def run_test(
    lib:            UltraGPSPositionLib,
    control_module: ControlModule,
    num_iterations: int,
    use_cep:        bool,
    max_subsets:    int,
) -> None:
    # Warm up the TCP connection and prime the differential filter
    print("Warming up (3 cycles) ...", end=" ", flush=True)
    for _ in range(3):
        try:
            control_module.update()
            raw = control_module.get_serial_message()
            if raw:
                lib.get_position(UltraGPSPositionLib.parse_message(raw))
        except Exception:
            pass
        time.sleep(0.05)
    lib.reset_state()
    print("done\n")

    method = f"CEP subset (max_subsets={max_subsets})" if use_cep else "OLS + Levenberg-Marquardt"
    print(f"Running {num_iterations} iterations  [{method}]")
    print("-" * 70)

    poll_times:  list[float] = []
    solve_times: list[float] = []
    total_times: list[float] = []
    sane_counts: list[int]   = []
    positions:   list        = []
    successes:   int         = 0
    first_raw:   str | None  = None
    last_raw:    str | None  = None

    wall_start = time.time()

    for i in range(num_iterations):
        t0 = time.time()

        # ── Poll ──────────────────────────────────────────────────────────────
        try:
            control_module.update()
            raw = control_module.get_serial_message()
        except Exception as exc:
            print(f"  [iter {i+1:4d}] Poll error: {exc}")
            continue

        t_after_poll = time.time()
        poll_times.append(t_after_poll - t0)

        if not raw:
            print(f"  [iter {i+1:4d}] Empty message — skipping")
            continue

        ticks = UltraGPSPositionLib.parse_message(raw)
        if not ticks:
            print(f"  [iter {i+1:4d}] Parse error — skipping")
            continue

        # ── Solve ─────────────────────────────────────────────────────────────
        t_solve = time.time()
        result = (
            lib.get_position_cep(ticks, max_subsets=max_subsets)
            if use_cep
            else lib.get_position(ticks)
        )
        solve_elapsed = time.time() - t_solve
        solve_times.append(solve_elapsed)

        total_elapsed = time.time() - t0
        total_times.append(total_elapsed)
        sane_counts.append(len(result["sane_indices"]))

        if result["success"] and result["position"] is not None:
            successes += 1
            positions.append(result["position"].copy())

        if i == 0:
            first_raw = raw.strip()
        last_raw = raw.strip()

        if (i + 1) % 10 == 0:
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
                f"poll={poll_times[-1]*1000:6.1f} ms  "
                f"solve={solve_elapsed*1000:5.1f} ms  "
                f"total={total_elapsed*1000:6.1f} ms  "
                f"sane={len(result['sane_indices'])}/{lib.receiver_count}  "
                f"pos={pos_s}{extra}"
            )

    total_wall = time.time() - wall_start
    n = len(total_times)

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)

    if not n:
        print("No successful iterations recorded.")
        return

    def _row(data: list[float], label: str) -> None:
        avg = statistics.mean(data)
        med = statistics.median(data)
        mn  = min(data)
        mx  = max(data)
        sd  = statistics.stdev(data) if len(data) > 1 else 0.0
        print(
            f"  {label:<22} mean={avg*1000:7.2f}  median={med*1000:7.2f}"
            f"  min={mn*1000:7.2f}  max={mx*1000:7.2f}"
            f"  std={sd*1000:6.2f}  [ms]"
        )

    print(f"Iterations completed  : {n}  (of {num_iterations} requested)")
    print(f"Successful positions  : {successes}  ({100*successes/n:.1f} %)")
    print(f"Total wall time       : {total_wall:.3f} s")
    print(f"Avg sane receivers    : {statistics.mean(sane_counts):.1f} / {lib.receiver_count}")
    print()
    _row(poll_times,  "Poll time")
    _row(solve_times, "Solve time")
    _row(total_times, "Round-trip time")
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

def main() -> None:
    default_config = os.path.join(_PROJECT_ROOT, "config.toml")

    parser = argparse.ArgumentParser(
        description="UltraGPS end-to-end control + position pipeline test",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=default_config,
        help="Path to config.toml",
    )
    parser.add_argument(
        "--ip",
        default="127.0.0.1",
        help="IP address of the UltraGPS-Ground server",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=100,
        help="Number of poll cycles",
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

    # ── Load position library ─────────────────────────────────────────────────
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

    # ── Connect control module ────────────────────────────────────────────────
    print(f"Connecting ControlModule to {args.ip} ...", end=" ", flush=True)
    control_module = ControlModule(ip_address=args.ip)
    print("OK\n")

    try:
        run_test(
            lib=lib,
            control_module=control_module,
            num_iterations=args.iterations,
            use_cep=args.cep,
            max_subsets=args.max_subsets,
        )
    finally:
        try:
            control_module.comms_module.close()
        except Exception:
            pass

    print("\nDone.")


if __name__ == "__main__":
    main()
