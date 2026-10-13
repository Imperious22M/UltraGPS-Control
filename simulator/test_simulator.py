"""Headless checks for the simulator:  python3 -m unittest simulator/test_simulator.py"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sensor_model import Faults  # noqa: E402
from simulation import Simulation  # noqa: E402


def run(sim: Simulation, pos, frames: int = 30) -> dict:
    sim.true = sim.target = np.array(pos, dtype=float)
    for _ in range(frames):
        sim.step()
    return sim.last


class SimulatorTest(unittest.TestCase):
    def test_ticks_round_trip_through_library_calibration(self):
        sim = Simulation(seed=0)
        sim.faults = Faults(noise_cm=0.0)
        frame = run(sim, [20, -40], frames=1)
        true = sim.sensor.true_distances(frame["true"])
        # Integer ticks quantise distance by at most half a slope
        np.testing.assert_allclose(frame["distances"], true, atol=0.6)

    def test_clean_signal_every_algorithm_is_accurate(self):
        sim = Simulation(seed=0)
        sim.faults = Faults(noise_cm=0.0)
        run(sim, [30, 50])
        for a in ("OLS", "LM", "CEP"):
            self.assertLess(sim.stats(a)[0], 2.0, a)

    def test_receiver_off_reports_zero_ticks(self):
        sim = Simulation(seed=0)
        sim.enabled[2] = False
        frame = run(sim, [0, 0], frames=1)
        self.assertEqual(frame["ticks"][2], 0)
        self.assertEqual(frame["status"][2], "off")

    def test_cep_drops_a_dead_receiver(self):
        sim = Simulation(seed=0)
        sim.enabled[0] = False
        frame = run(sim, [30, 50])
        self.assertNotIn(0, frame["cep_subset"])
        self.assertLess(sim.stats("CEP")[0], 5.0)


if __name__ == "__main__":
    unittest.main()
