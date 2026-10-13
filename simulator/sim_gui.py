"""
UltraGPS simulator — interactive window
=======================================
Click anywhere in the arena to move the transmitter.  Every tick of the timer
the simulated receivers take a reading (``sim_core.SensorModel``) and the real
positioning library solves it with each algorithm (``sim_core.solve_all``).

    python simulator/sim_gui.py [--config path/to/config.toml] [--seed N]

Plot legend
-----------
  ▲ green   receiver used by the solver
  ▲ orange  receiver rejected by the library's sanity filter this reading
  ▲ grey    receiver switched off
  dashed    range circle each receiver measured (none = no echo)
  ✕ black   true transmitter position
  ● colour  estimate from each algorithm, with its recent trail
"""

from __future__ import annotations

import argparse
from collections import deque

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Circle
from matplotlib.widgets import Button, CheckButtons, RadioButtons, Slider

from sim_core import ALGORITHMS, DEFAULT_CONFIG, SensorModel, UltraGPSPositionLib, solve_all

COLORS = {"OLS": "tab:blue", "OLS+LM": "tab:red", "CEP": "tab:purple"}
TRAIL = 40          # readings kept in each estimate's trail
STATS_WINDOW = 100  # readings in the rolling RMS error
PERIOD_MS = 100     # one reading every PERIOD_MS


class Simulator:
    def __init__(self, config_path: str, seed: int | None):
        self.config_path = config_path
        self.sensor = SensorModel(config_path, seed=seed)
        self.lib = UltraGPSPositionLib(config_path)
        self.truth = np.array([0.0, 0.0])
        self.hold = self.truth.copy()
        self.motion = "Hold"
        self.step = 0
        self.running = True
        self.visible = {a: True for a in ALGORITHMS}
        self._reset_history()
        self._build_window()

    def _reset_history(self):
        self.trails = {a: deque(maxlen=TRAIL) for a in ALGORITHMS}
        self.errors = {a: deque(maxlen=STATS_WINDOW) for a in ALGORITHMS}

    # ── Window ──────────────────────────────────────────────────────────────

    def _build_window(self):
        fig = self.fig = plt.figure("UltraGPS positioning simulator", figsize=(13, 8))

        # Arena
        ax = self.ax = fig.add_axes([0.05, 0.07, 0.52, 0.88])
        rx, ry = self.sensor.positions.T
        pad = 60
        ax.set_xlim(rx.min() - pad, rx.max() + pad)
        ax.set_ylim(ry.min() - pad, ry.max() + pad)
        ax.set_aspect("equal")
        ax.grid(alpha=0.3)
        ax.set_xlabel(f"x ({self.lib.units})")
        ax.set_ylabel(f"y ({self.lib.units})")

        self.tower_dots = ax.scatter(rx, ry, marker="^", s=160, zorder=4, edgecolors="black")
        for i, (x, y) in enumerate(self.sensor.positions):
            ax.annotate(str(i), (x, y), xytext=(8, 8), textcoords="offset points", weight="bold")
        self.rings = [
            ax.add_patch(Circle((x, y), 0, fill=False, ls="--", lw=0.8, alpha=0.5))
            for x, y in self.sensor.positions
        ]
        self.truth_dot, = ax.plot([], [], "kx", ms=14, mew=3, zorder=6, label="true position")
        self.est_dots, self.trail_lines = {}, {}
        for a in ALGORITHMS:
            self.trail_lines[a], = ax.plot([], [], "-", color=COLORS[a], alpha=0.35, lw=1)
            self.est_dots[a], = ax.plot([], [], "o", color=COLORS[a], ms=9, zorder=5, label=a)
        ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=4, fontsize=9, frameon=False)
        fig.text(0.05, 0.015, "Click in the arena to move the transmitter", fontsize=9, style="italic")

        # Controls (right column)
        def panel(rect, title):
            a = fig.add_axes(rect)
            a.set_title(title, fontsize=9, loc="left")
            return a

        n = self.sensor.count
        self.tower_check = CheckButtons(
            panel([0.62, 0.66, 0.15, 0.27], "Receivers on"),
            [f"Receiver {i}" for i in range(n)], [True] * n,
        )
        self.tower_check.on_clicked(self._on_tower)

        self.algo_check = CheckButtons(
            panel([0.80, 0.78, 0.17, 0.15], "Show algorithm"), list(ALGORITHMS), [True] * len(ALGORITHMS),
        )
        self.algo_check.on_clicked(self._on_algo)

        self.motion_radio = RadioButtons(
            panel([0.80, 0.62, 0.17, 0.13], "Transmitter motion"), ["Hold", "Circle", "Wander"],
        )
        self.motion_radio.on_clicked(self._on_motion)

        def slider(y, label, vmax, init, step):
            return Slider(fig.add_axes([0.70, y, 0.22, 0.025]), label, 0, vmax, valinit=init, valstep=step)

        self.noise_slider = slider(0.56, "Range noise σ (cm)", 20, 0, 0.5)
        self.dropout_slider = slider(0.52, "Dropouts (%)", 50, 0, 1)
        self.spurious_slider = slider(0.48, "False echoes (%)", 50, 0, 1)
        self.jump_slider = slider(0.44, "Jump filter (cm)", 300, self.lib.max_differential, 5)

        self.pause_btn = Button(fig.add_axes([0.62, 0.37, 0.10, 0.045]), "Pause")
        self.pause_btn.on_clicked(self._on_pause)
        self.reset_btn = Button(fig.add_axes([0.74, 0.37, 0.10, 0.045]), "Reset")
        self.reset_btn.on_clicked(self._on_reset)

        # Readout
        self.readout = fig.text(0.62, 0.33, "", va="top", family="monospace", fontsize=9)

        fig.canvas.mpl_connect("button_press_event", self._on_click)
        self.timer = fig.canvas.new_timer(interval=PERIOD_MS)
        self.timer.add_callback(self.tick)
        self.timer.start()

    # ── Callbacks ───────────────────────────────────────────────────────────

    def _on_tower(self, label):
        i = int(label.split()[-1])
        self.sensor.enabled[i] = not self.sensor.enabled[i]

    def _on_algo(self, label):
        self.visible[label] = not self.visible[label]

    def _on_motion(self, label):
        self.motion = label
        self.hold = self.truth.copy()

    def _on_pause(self, _):
        self.running = not self.running
        self.pause_btn.label.set_text("Resume" if not self.running else "Pause")
        self.fig.canvas.draw_idle()

    def _on_reset(self, _):
        self.lib.reset_state()
        self._reset_history()

    def _on_click(self, event):
        if event.inaxes is self.ax and event.button == 1:
            self.truth = self.hold = np.array([event.xdata, event.ydata], dtype=float)
            self.motion_radio.set_active(0)   # back to Hold

    # ── Simulation step ─────────────────────────────────────────────────────

    def _move(self):
        self.step += 1
        t = self.step * PERIOD_MS / 1000.0
        if self.motion == "Circle":
            self.truth = self.hold + 60 * np.array([np.cos(0.5 * t) - 1, np.sin(0.5 * t)])
        elif self.motion == "Wander":
            # Bounded random walk inside the receiver box
            lo, hi = self.sensor.positions.min(axis=0), self.sensor.positions.max(axis=0)
            self.truth = np.clip(self.truth + self.sensor.rng.normal(0, 3, 2), lo + 10, hi - 10)

    def tick(self):
        if not self.running:
            return
        self._move()

        s = self.sensor
        s.noise_cm = self.noise_slider.val
        s.dropout_rate = self.dropout_slider.val / 100
        s.spurious_rate = self.spurious_slider.val / 100
        self.lib.max_differential = self.jump_slider.val

        ticks, heard = s.measure(self.truth)
        result = solve_all(self.lib, ticks)

        # Receivers: colour by state, ring by measured range
        sane = set(result["sane_indices"])
        colors = [
            "lightgrey" if not s.enabled[i] else ("tab:green" if i in sane else "tab:orange")
            for i in range(s.count)
        ]
        self.tower_dots.set_facecolors(colors)
        for ring, r in zip(self.rings, heard):
            ring.set_visible(not np.isnan(r))
            ring.set_radius(0 if np.isnan(r) else r)

        self.truth_dot.set_data([self.truth[0]], [self.truth[1]])

        lines = [f"{'algorithm':8s} {'error':>8s} {'RMS':>8s}"]
        for a, pos in result["positions"].items():
            if pos is not None:
                self.trails[a].append(np.asarray(pos, dtype=float))
                self.errors[a].append(float(np.linalg.norm(np.asarray(pos) - self.truth)))
            show = self.visible[a] and len(self.trails[a]) > 0
            trail = np.array(self.trails[a]) if show else np.empty((0, 2))
            self.trail_lines[a].set_data(trail[:, 0], trail[:, 1])
            self.est_dots[a].set_data(trail[-1:, 0], trail[-1:, 1])
            if self.errors[a]:
                rms = np.sqrt(np.mean(np.square(self.errors[a])))
                lines.append(f"{a:8s} {self.errors[a][-1]:7.1f}  {rms:7.1f}")
            else:
                lines.append(f"{a:8s} {'—':>7s}  {'—':>7s}")

        lines += [
            "",
            "ticks        " + " ".join(f"{t:5d}" for t in ticks),
            "library cm   " + " ".join(f"{d:5.0f}" for d in result["distances"]),
            f"used         {sorted(sane)}",
            f"CEP subset   {sorted(result['best_indices'])}",
        ]
        self.readout.set_text("\n".join(lines))
        self.fig.canvas.draw_idle()


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="config.toml with receivers")
    parser.add_argument("--seed", type=int, default=None, help="random seed")
    args = parser.parse_args()
    sim = Simulator(args.config, args.seed)  # noqa: F841 — keeps the timer alive
    plt.show()


if __name__ == "__main__":
    main()
