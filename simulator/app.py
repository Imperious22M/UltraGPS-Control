"""
UltraGPS positioning simulator — interactive window.

    python3 simulator/app.py [--config config/config.toml] [--max-differential 50]

Click the arena to send the transmitter somewhere, toggle receivers on and off,
and drag the sliders to add noise and faults.  Every algorithm in the real
positioning library solves each frame side by side, with its error against the
true position shown live.
"""

from __future__ import annotations

import argparse

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.animation import FuncAnimation
from matplotlib.widgets import Button, CheckButtons, Slider

from simulation import Simulation
from solvers import ALGORITHMS, DEFAULT_CONFIG

DT          = 0.1   # s per frame — the base station's default 10 Hz
COLORS      = {"OLS": "tab:orange", "LM": "tab:blue", "CEP": "tab:green"}
LABELS      = {"OLS": "Linear LS (OLS)", "LM": "OLS + Levenberg-Marquardt", "CEP": "CEP best subset"}
STATUS_FILL = {"ok": "tab:green", "off": "lightgray", "dropout": "tab:orange",
               "range": "tab:orange", "spike": "tab:red"}
PATH_RADIUS = 100.0


class App:
    def __init__(self, sim: Simulation):
        self.sim     = sim
        self.paused  = False
        self.on_path = False
        self.angle   = 0.0
        self.shown   = {a: True for a in ALGORITHMS}

        self.fig = plt.figure("UltraGPS positioning simulator", figsize=(13, 8))
        self._build_arena()
        self._build_controls()
        self.fig.canvas.mpl_connect("button_press_event", self._on_click)
        self.anim = FuncAnimation(self.fig, self.update, interval=int(DT * 1000),
                                  cache_frame_data=False)

    # ─── Layout ──────────────────────────────────────────────────────────────

    def _build_arena(self) -> None:
        c  = self.sim.coords
        ax = self.ax = self.fig.add_axes([0.05, 0.13, 0.55, 0.82])
        ax.set_aspect("equal")
        ax.set_xlim(c[:, 0].min() - 60, c[:, 0].max() + 60)
        ax.set_ylim(c[:, 1].min() - 40, c[:, 1].max() + 40)
        ax.set_xlabel("x (cm)")
        ax.set_ylabel("y (cm)")
        ax.set_title("Click to move the transmitter")
        ax.grid(alpha=0.3)

        self.rings = [plt.Circle(xy, 0, fill=False, lw=0.8, alpha=0.5) for xy in c]
        for r in self.rings:
            ax.add_patch(r)
        self.towers = ax.scatter(c[:, 0], c[:, 1], s=180, marker="^",
                                 c=["tab:green"] * len(c), edgecolors="k", zorder=5)
        for i, (x, y) in enumerate(c):
            ax.annotate(f"R{i}", (x, y), xytext=(0, 12), textcoords="offset points",
                        ha="center", fontsize=9)

        self.trail = {a: ax.plot([], [], "-", color=COLORS[a], alpha=0.4, lw=1)[0]
                      for a in ALGORITHMS}
        self.est   = {a: ax.plot([], [], "o", color=COLORS[a], ms=9, label=LABELS[a])[0]
                      for a in ALGORITHMS}
        self.target_m, = ax.plot([], [], "o", mfc="none", mec="gray", ms=14, label="Target")
        self.true_m,   = ax.plot([], [], "kx", ms=12, mew=2.5, label="True position", zorder=6)
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.07), ncol=3, fontsize=8)

    def _build_controls(self) -> None:
        f, n = self.fig, len(self.sim.coords)

        f.text(0.64, 0.95, "Receivers", weight="bold")
        self.tower_cb = CheckButtons(f.add_axes([0.64, 0.70, 0.14, 0.24]),
                                     [f"R{i}" for i in range(n)], [True] * n)
        self.tower_cb.on_clicked(self._on_tower)

        f.text(0.80, 0.95, "Algorithms", weight="bold")
        self.algo_cb = CheckButtons(f.add_axes([0.80, 0.80, 0.18, 0.14]),
                                    list(ALGORITHMS), [True] * len(ALGORITHMS))
        self.algo_cb.on_clicked(self._on_algo)

        fl = self.sim.faults
        def slider(y, label, lo, hi, init, fmt):
            s = Slider(f.add_axes([0.72, y, 0.20, 0.025]), label, lo, hi, valinit=init, valfmt=fmt)
            s.on_changed(lambda _: self._on_slider())
            return s
        self.s_noise = slider(0.62, "Noise σ (cm)",    0, 20,   fl.noise_cm,           "%.1f")
        self.s_drop  = slider(0.58, "Dropouts (%)",    0, 50,   fl.dropout_rate * 100, "%.0f")
        self.s_spike = slider(0.54, "Bad values (%)",  0, 50,   fl.spike_rate * 100,   "%.0f")
        self.s_temp  = slider(0.50, "Air temp (°C)",   0, 40,   fl.temperature_c,      "%.0f")
        self.s_speed = slider(0.46, "Speed (cm/s)",    5, 200,  self.sim.speed,        "%.0f")

        self.b_pause = Button(f.add_axes([0.64, 0.38, 0.10, 0.045]), "Pause")
        self.b_path  = Button(f.add_axes([0.76, 0.38, 0.10, 0.045]), "Circle path")
        self.b_reset = Button(f.add_axes([0.88, 0.38, 0.10, 0.045]), "Reset stats")
        self.b_pause.on_clicked(self._on_pause)
        self.b_path.on_clicked(self._on_path)
        self.b_reset.on_clicked(lambda _: self.sim.reset())

        self.info = f.text(0.64, 0.34, "", family="monospace", fontsize=9, va="top")

    # ─── Callbacks ───────────────────────────────────────────────────────────

    def _on_click(self, event) -> None:
        if event.inaxes is self.ax and event.button == 1:
            self.on_path = False
            self.sim.target = np.array([event.xdata, event.ydata])

    def _on_tower(self, label) -> None:
        self.sim.enabled[int(label[1:])] ^= True

    def _on_algo(self, label) -> None:
        self.shown[label] = not self.shown[label]

    def _on_slider(self) -> None:
        fl = self.sim.faults
        fl.noise_cm      = self.s_noise.val
        fl.dropout_rate  = self.s_drop.val / 100
        fl.spike_rate    = self.s_spike.val / 100
        fl.temperature_c = self.s_temp.val
        self.sim.speed   = self.s_speed.val

    def _on_pause(self, _) -> None:
        self.paused = not self.paused
        self.b_pause.label.set_text("Resume" if self.paused else "Pause")

    def _on_path(self, _) -> None:
        self.on_path = not self.on_path

    # ─── Frame ───────────────────────────────────────────────────────────────

    def update(self, _=None) -> None:
        if self.paused:
            return
        if self.on_path:
            self.angle += self.sim.speed * DT / PATH_RADIUS
            self.sim.target = PATH_RADIUS * np.array([np.cos(self.angle), np.sin(self.angle)])

        f = self.sim.step(DT)

        self.true_m.set_data([f["true"][0]], [f["true"][1]])
        self.target_m.set_data([self.sim.target[0]], [self.sim.target[1]])

        # Receivers: colour = what the sensor did, thick edge = used by CEP
        self.towers.set_facecolors([STATUS_FILL[s] for s in f["status"]])
        self.towers.set_linewidths([3 if i in f["cep_subset"] else 1
                                    for i in range(len(f["status"]))])

        # Measured-range circles: solid if the library kept the reading
        for i, ring in enumerate(self.rings):
            ring.set_radius(max(f["distances"][i], 0))
            kept = i in f["sane"]
            ring.set_linestyle("-" if kept else ":")
            ring.set_edgecolor("tab:gray" if kept else "tab:red")
            ring.set_visible(f["status"][i] != "off")

        for a in ALGORITHMS:
            pos   = f["positions"][a]
            trail = np.array(self.sim.trails[a]) if self.sim.trails[a] else np.empty((0, 2))
            self.est[a].set_visible(self.shown[a] and pos is not None)
            self.trail[a].set_visible(self.shown[a])
            if pos is not None:
                self.est[a].set_data([pos[0]], [pos[1]])
            self.trail[a].set_data(trail[:, 0], trail[:, 1])

        self.info.set_text(self._info_text(f))

    def _info_text(self, f: dict) -> str:
        lines = [f"{'error (cm)':<10} {'now':>7} {'RMS':>7} {'fails':>6}"]
        for a in ALGORITHMS:
            now, rms, fails = self.sim.stats(a)
            lines.append(f"{a:<10} {now:7.1f} {rms:7.1f} {fails:6d}")
        lines += [
            "",
            f"frames     {self.sim.frames}",
            f"kept       {', '.join(f'R{i}' for i in f['sane']) or 'none'}",
            f"CEP used   {', '.join(f'R{i}' for i in f['cep_subset']) or 'none'}",
            "",
            "rx  status   ticks   dist(cm)",
        ]
        for i, (s, t, d) in enumerate(zip(f["status"], f["ticks"], f["distances"])):
            lines.append(f"R{i}  {s:<8} {t:6d}  {d:8.1f}")
        return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--config", default=DEFAULT_CONFIG, help="config.toml with receivers + calibration")
    p.add_argument("--max-differential", type=float, default=50.0,
                   help="library jump filter, cm per frame (default 50)")
    p.add_argument("--seed", type=int, default=None, help="random seed for repeatable runs")
    args = p.parse_args()

    app = App(Simulation(args.config, args.max_differential, seed=args.seed))  # noqa: F841 (keeps the animation alive)
    plt.show()


if __name__ == "__main__":
    main()
