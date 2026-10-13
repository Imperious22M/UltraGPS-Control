"""
Sensor model — turns a true transmitter position into the tick counts the
base station would print.

Physics, in order:

  1. True range from the transmitter to each receiver (straight line, 2D).
  2. Speed-of-sound error: the calibration was taken at ``cal_temp_c``; at any
     other air temperature sound travels faster or slower, so every range is
     scaled by ``c_cal / c_actual``.
  3. Gaussian timing noise, expressed in cm.
  4. Faults, each of which matches something the real hardware does:

     * receiver switched off           -> 0 ticks (unused pins report 0)
     * echo missed / beyond max range  -> 0 ticks (no transition recorded)
     * erroneous echo (multipath, EMI) -> a random range inside max range

  5. Inverse calibration: ``tick = (distance - intercept) / slope``, rounded to
     an integer, so the library's own ``tick * slope + intercept`` recovers the
     (noisy) distance.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def speed_of_sound(temp_c: float) -> float:
    """Speed of sound in dry air (m/s)."""
    return 331.3 + 0.606 * temp_c


@dataclass
class Faults:
    noise_cm: float = 1.0        # std-dev of range noise
    dropout_rate: float = 0.0    # probability a reading is missed (0 ticks)
    spike_rate: float = 0.0      # probability a reading is a random wrong value
    temperature_c: float = 20.0  # air temperature during the run


class SensorModel:
    def __init__(
        self,
        receiver_coords: np.ndarray,
        slopes: np.ndarray,
        intercepts: np.ndarray,
        max_range_cm: float = 400.0,   # HC-SR04 rated range
        cal_temp_c: float = 20.0,
        seed: int | None = None,
    ):
        self.coords     = np.asarray(receiver_coords, dtype=float)
        self.slopes     = np.asarray(slopes, dtype=float)
        self.intercepts = np.asarray(intercepts, dtype=float)
        self.max_range  = max_range_cm
        self.cal_temp_c = cal_temp_c
        self.rng        = np.random.default_rng(seed)

    def true_distances(self, pos) -> np.ndarray:
        return np.linalg.norm(self.coords - np.asarray(pos, dtype=float), axis=1)

    def measure(self, pos, enabled, faults: Faults) -> tuple[list[int], list[str]]:
        """Return ``(ticks, status)`` — one tick count and one status label
        (``ok``, ``off``, ``dropout``, ``range``, ``spike``) per receiver."""
        n     = len(self.coords)
        scale = speed_of_sound(self.cal_temp_c) / speed_of_sound(faults.temperature_c)
        true  = self.true_distances(pos)
        dist  = true * scale + self.rng.normal(0.0, faults.noise_cm, n)

        status = ["ok"] * n
        for i in range(n):
            if not enabled[i]:
                status[i] = "off"
            elif true[i] > self.max_range:
                status[i] = "range"
            elif self.rng.random() < faults.dropout_rate:
                status[i] = "dropout"
            elif self.rng.random() < faults.spike_rate:
                dist[i], status[i] = self.rng.uniform(0.0, self.max_range), "spike"

        ticks = []
        for i in range(n):
            if status[i] in ("off", "range", "dropout"):
                ticks.append(0)
            else:
                ticks.append(max(0, round((dist[i] - self.intercepts[i]) / self.slopes[i])))
        return ticks, status
