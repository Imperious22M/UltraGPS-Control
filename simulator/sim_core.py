"""
UltraGPS simulator — core (no GUI)
==================================
Two pieces, both deliberately small:

  • ``SensorModel`` turns a true transmitter position into the six tick counts
    the base station would report, with optional faults:

        - Gaussian range noise                 (timing jitter, air turbulence)
        - dropouts                             (no echo heard → tick 0)
        - spurious readings                    (false echo / multipath → random range)
        - towers switched off                  (always tick 0)

    Tick 0 for a missing echo matches the base-station firmware, which leaves
    a receiver's tick at 0 when it never sees its echo line fall
    (UltraGPS-Arduino ``measure_echo_times``).

  • ``solve_all`` feeds those ticks to the real ``ultragps_position`` library —
    the same one ultragps-control uses — and returns the estimate from each
    algorithm it implements:

        - ``OLS``     linear least-squares (the seed the library computes first)
        - ``OLS+LM``  OLS seed refined by Levenberg-Marquardt   (``lm_position``)
        - ``CEP``     best receiver subset by CEP radius         (``cep_position``)

Run ``python simulator/sim_core.py`` for a headless accuracy benchmark.
"""

from __future__ import annotations

import os
import sys

import numpy as np

# Use the library from this checkout when it is not installed (./install.sh
# installs it as an editable package, in which case this is a no-op).
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "libs", "ultragps_position"))

from ultragps_position import UltraGPSPositionLib, load_config  # noqa: E402

DEFAULT_CONFIG = os.path.join(_REPO, "config", "config.toml")

ALGORITHMS = ("OLS", "OLS+LM", "CEP")


class SensorModel:
    """Simulates the HC-SR04 receivers, producing ticks like the base station.

    Args:
        config_path: config.toml with receiver positions and per-receiver
                     calibration (``distance = tick * slope + intercept``).
        seed:        Random seed, for repeatable runs.
    """

    def __init__(self, config_path: str = DEFAULT_CONFIG, seed: int | None = None):
        receivers = sorted(load_config(config_path)["receivers"], key=lambda r: r["id"])
        self.positions = np.array([r["position"] for r in receivers], dtype=float)
        self.slopes = np.array([r.get("offset", {}).get("slope", 1.0) for r in receivers])
        self.intercepts = np.array([r.get("offset", {}).get("intercept", 0.0) for r in receivers])
        self.count = len(receivers)

        self.enabled = np.ones(self.count, dtype=bool)
        self.noise_cm = 0.0          # std-dev of Gaussian range noise
        self.dropout_rate = 0.0      # chance a reading has no echo (tick 0)
        self.spurious_rate = 0.0     # chance a reading is a random false echo

        span = self.positions.max(axis=0) - self.positions.min(axis=0)
        self.max_range = 1.5 * float(np.hypot(*span))   # upper bound for false echoes
        self.rng = np.random.default_rng(seed)

    def measure(self, true_pos) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(ticks, measured_cm)`` for a transmitter at *true_pos*.

        ``measured_cm`` is the range each receiver actually "heard" (``nan``
        where no echo came back); ``ticks`` is what goes on the wire.
        """
        n = self.count
        ranges = np.linalg.norm(self.positions - np.asarray(true_pos, dtype=float), axis=1)
        ranges = ranges + self.rng.normal(0.0, self.noise_cm, n)

        spurious = self.rng.random(n) < self.spurious_rate
        ranges[spurious] = self.rng.uniform(0.0, self.max_range, spurious.sum())

        silent = (self.rng.random(n) < self.dropout_rate) | ~self.enabled
        ranges[silent] = np.nan

        # Invert each receiver's calibration so the library's own conversion
        # gives back the simulated range.  Ticks are integers on the wire.
        ticks = np.rint((ranges - self.intercepts) / self.slopes)
        ticks = np.where(silent, 0, np.clip(ticks, 0, None)).astype(int)
        return ticks, ranges


def solve_all(lib: UltraGPSPositionLib, ticks) -> dict:
    """Run every library algorithm on one tick reading.

    Uses ``get_position_full`` — exactly what ultragps-control calls — so the
    sanity filter runs once and its history advances the same way.  The OLS
    estimate is the library's own linear seed, recomputed on the same filtered
    receivers.

    Returns a dict with ``positions`` ({algorithm: xy or None}), the library's
    ``distances``, ``sane_indices`` and CEP ``best_indices``.
    """
    full = lib.get_position_full(ticks)
    sane = np.array(full["sane_indices"], dtype=int)
    ols = lib._ols(np.array(full["distances"], dtype=float), sane) if len(sane) >= 3 else None
    return {
        "positions": {"OLS": ols, "OLS+LM": full["lm_position"], "CEP": full["cep_position"]},
        "distances": full["distances"],
        "sane_indices": [int(i) for i in sane],
        "best_indices": [int(i) for i in full["best_indices"] or []],
    }


def benchmark(steps: int = 300, seed: int = 0) -> None:
    """Walk the transmitter round a circle under a few fault scenarios and
    print each algorithm's RMS position error."""
    scenarios = [
        ("clean",                      dict()),
        ("noise 3 cm",                 dict(noise_cm=3.0)),
        ("noise 3 cm + 10% dropouts",  dict(noise_cm=3.0, dropout_rate=0.10)),
        ("noise 3 cm + 5% spurious",   dict(noise_cm=3.0, spurious_rate=0.05)),
        ("noise 3 cm + tower 4 off",   dict(noise_cm=3.0, off=[4])),
    ]
    print(f"RMS position error (cm) over {steps} readings on a 60 cm circle")
    print(f"{'scenario':30s}" + "".join(f"{a:>10s}" for a in ALGORITHMS))
    for name, opts in scenarios:
        sensor = SensorModel(seed=seed)
        sensor.noise_cm = opts.get("noise_cm", 0.0)
        sensor.dropout_rate = opts.get("dropout_rate", 0.0)
        sensor.spurious_rate = opts.get("spurious_rate", 0.0)
        for i in opts.get("off", []):
            sensor.enabled[i] = False
        lib = UltraGPSPositionLib(DEFAULT_CONFIG)

        sq_err = {a: [] for a in ALGORITHMS}
        for k in range(steps):
            angle = 2 * np.pi * k / steps
            truth = np.array([60 * np.cos(angle), 60 * np.sin(angle)])
            ticks, _ = sensor.measure(truth)
            for algo, pos in solve_all(lib, ticks)["positions"].items():
                if pos is not None:
                    sq_err[algo].append(float(np.sum((np.asarray(pos) - truth) ** 2)))
        row = "".join(
            f"{np.sqrt(np.mean(e)) if e else float('nan'):10.1f}" for e in sq_err.values()
        )
        print(f"{name:30s}{row}")


if __name__ == "__main__":
    benchmark()
