"""
Solvers — runs every positioning algorithm in the real ``ultragps_position``
library on one frame of ticks.

The library is imported from this checkout's ``libs/`` folder, so the
simulator always exercises the same code the control program runs.

Algorithms shown:

  * ``OLS``  — the linear least-squares estimate the library uses as its seed
               (``_ols``; private, but it is the first stage of both solvers).
  * ``LM``   — OLS seed refined by Levenberg-Marquardt (``lm_position``).
  * ``CEP``  — best receiver subset by Circular Error Probable
               (``cep_position``).

All three see the same receivers: the ones that pass the library's bounds and
jump (differential) filter, run once per frame by ``get_position_full`` —
exactly as ``windows/position_window.py`` calls it.
"""

from __future__ import annotations

import os
import sys

import numpy as np

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "libs", "ultragps_position"))

from ultragps_position import UltraGPSPositionLib, load_config  # noqa: E402

DEFAULT_CONFIG = os.path.join(_REPO, "config", "config.toml")
ALGORITHMS     = ("OLS", "LM", "CEP")


def load_calibration(config_path: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(coords, slopes, intercepts)`` in receiver-id order."""
    receivers = sorted(load_config(config_path)["receivers"], key=lambda r: r["id"])
    coords     = np.array([r["position"] for r in receivers], dtype=float)
    slopes     = np.array([r["offset"]["slope"] for r in receivers], dtype=float)
    intercepts = np.array([r["offset"]["intercept"] for r in receivers], dtype=float)
    return coords, slopes, intercepts


class Solvers:
    def __init__(self, config_path: str = DEFAULT_CONFIG, max_differential: float = 50.0):
        self.lib = UltraGPSPositionLib(config_path, max_differential=max_differential)

    def reset(self) -> None:
        self.lib.reset_state()

    def solve(self, ticks: list[int]) -> dict:
        """Return ``{"positions": {name: xy or None}, "success": {name: bool},
        "distances": [...], "sane": [...], "cep_subset": [...]}``."""
        r    = self.lib.get_position_full(ticks)
        sane = r["sane_indices"]

        ols = None
        if len(sane) >= 3:
            ols = self.lib._ols(np.array(r["distances"], dtype=float), np.array(sane))

        return {
            "positions":  {"OLS": ols, "LM": r["lm_position"], "CEP": r["cep_position"]},
            "success":    {"OLS": ols is not None, "LM": r["lm_success"], "CEP": r["cep_success"]},
            "distances":  list(r["distances"]),
            "sane":       list(sane),
            "cep_subset": list(r["best_indices"] or []),
        }
