"""Position tracking panel.

Contains NetworkThread (QThread) for continuous position calculation and
PositionPanel (QWidget) for display.  All positioning logic lives here.
"""

import time
import numpy as np
from collections import deque

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox, QFrame,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont

from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.gridspec import GridSpec
from matplotlib.patches import Circle

from ultragps_client import UltraGPSClient
from ultragps_position import UltraGPSPositionLib
from SettingsModule import SettingsModule


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------

class NetworkThread(QThread):
    """Runs the continuous position loop in a background thread.

    Emits signals for each piece of data so the GUI can update safely on the
    main thread via Qt's queued connection mechanism.
    """

    lm_updated           = pyqtSignal(float, float)  # LM x, y
    cep_updated          = pyqtSignal(float, float)  # CEP x, y
    distances_updated    = pyqtSignal(object)         # list[float] raw distances
    sane_updated         = pyqtSignal(object)         # list[int] sane receiver indices
    cep_validity_changed = pyqtSignal(bool)           # True = invalid position
    insuff_changed       = pyqtSignal(bool)           # True = < 3 sane receivers
    pos_per_sec_updated  = pyqtSignal(float)          # positions calculated per second

    def __init__(self, client: UltraGPSClient, position_lib: UltraGPSPositionLib):
        super().__init__()
        self._client = client
        self._position_lib = position_lib
        self._active = False
        self._use_continuous = True  # True=UDP/continuous, False=TCP/pulse

    def set_mode(self, use_continuous: bool) -> None:
        self._use_continuous = use_continuous

    def run(self) -> None:
        self._active = True
        self._position_lib.reset_state()

        prev_continuous = None
        rate_count = 0
        rate_start = time.monotonic()

        while self._active:
            current_continuous = self._use_continuous

            # Handle mode transitions
            if current_continuous != prev_continuous:
                # Send the continouous command only if needed to switch modes
                # This is because the pulse command will automatically change the 
                # mode on the Arduino to continouous.
                if current_continuous:
                    self._client.continuous()
                    self._client.continuous()
                prev_continuous = current_continuous

            try:
                if current_continuous:
                    ticks = self._client.get_latest_reading()
                else:
                    ticks = self._client.pulse()

                result = self._position_lib.get_position_full(ticks)

                if result is None:
                    if current_continuous:
                        time.sleep(0.1)
                    continue

                raw_distances = result.get("distances")
                sane_indices  = result.get("sane_indices", [])
                lm_pos        = result.get("lm_position")
                cep_pos       = result.get("cep_position")
                cep_success   = result.get("cep_success", False)

                if lm_pos is not None:
                    self.lm_updated.emit(float(lm_pos[0]), float(lm_pos[1]))
                if cep_pos is not None:
                    self.cep_updated.emit(float(cep_pos[0]), float(cep_pos[1]))
                if raw_distances is not None:
                    self.distances_updated.emit(list(raw_distances))

                self.sane_updated.emit(list(sane_indices))
                self.cep_validity_changed.emit(not cep_success)
                self.insuff_changed.emit(len(sane_indices) < 3)

                rate_count += 1
                elapsed = time.monotonic() - rate_start
                if elapsed >= 1.0:
                    self.pos_per_sec_updated.emit(rate_count / elapsed)
                    rate_count = 0
                    rate_start = time.monotonic()
                
                # If we are in continuous mode, we need to add a delay to
                # prevent the server from spamming
                if current_continuous:
                    time.sleep(0.1)

            except Exception as exc:
                print(f"NetworkThread error: {exc}")
                if current_continuous:
                    time.sleep(0.1)

        print("NetworkThread stopped")

    def stop(self) -> None:
        self._active = False
        self.wait(2000)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class PositionPanel(QWidget):
    """Full-page position tracking panel.

    Owns the matplotlib figure (arena + 6 distance mini-plots) and the
    NetworkThread.  Canvas is refreshed by a 30 Hz QTimer that is started/
    stopped when the panel becomes visible/hidden.
    """

    def __init__(
        self,
        client: UltraGPSClient,
        position_lib: UltraGPSPositionLib,
        settings_module: SettingsModule,
        main_window,
    ):
        super().__init__()
        self._client = client
        self._position_lib = position_lib
        self._settings = settings_module
        self._main_window = main_window
        self.setStyleSheet("background-color: black;")

        self.grid_padding = 20
        self.position_history    = deque(maxlen=50)
        self.cep_history         = deque(maxlen=50)
        self.distance_histories  = [deque(maxlen=50) for _ in range(6)]
        self.distance_filter_buffers = [deque(maxlen=5) for _ in range(6)]
        self._last_draw_time = time.monotonic()

        # Compass rose artist handles
        self._compass_x_arrow = None
        self._compass_x_text  = None
        self._compass_y_arrow = None
        self._compass_y_text  = None

        # Display toggles (mirrored by QCheckBox widgets)
        self.show_multilateration = True
        self.show_cep             = True

        receiver_positions = self._settings.get_tower_coordinates()
        self._build_figure(receiver_positions)
        self._build_ui()

        # NetworkThread
        self._net_thread = NetworkThread(self._client, self._position_lib)
        self._net_thread.lm_updated.connect(self._on_lm_updated)
        self._net_thread.cep_updated.connect(self._on_cep_updated)
        self._net_thread.distances_updated.connect(self._on_distances_updated)
        self._net_thread.sane_updated.connect(self._on_sane_updated)
        self._net_thread.cep_validity_changed.connect(self._on_cep_validity_changed)
        self._net_thread.insuff_changed.connect(self._on_insuff_changed)
        self._net_thread.pos_per_sec_updated.connect(self._on_pos_sec_updated)
        self._mode_cb.toggled.connect(self._net_thread.set_mode)

        # Canvas refresh at ~30 Hz (started/stopped with panel visibility)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(33)
        self._refresh_timer.timeout.connect(self._on_refresh_timer)

    # ------------------------------------------------------------------
    # Figure construction
    # ------------------------------------------------------------------

    def _build_figure(self, receiver_positions: list) -> None:
        self.receiver_positions = receiver_positions

        rx = [p[1][0] for p in receiver_positions]
        ry = [p[1][1] for p in receiver_positions]

        self.fig = Figure(figsize=(16, 10), facecolor='black')
        gs = GridSpec(3, 3, figure=self.fig,
                      width_ratios=[1, 5, 1], height_ratios=[1, 1, 1],
                      hspace=0.3, wspace=0.4)

        # Main arena subplot
        self.ax = self.fig.add_subplot(gs[0:3, 1])
        self.ax.set_facecolor('black')
        self.ax.set_xlim(min(rx) - self.grid_padding, max(rx) + self.grid_padding)
        self.ax.set_ylim(min(ry) - self.grid_padding, max(ry) + self.grid_padding)
        self.ax.set_aspect('equal')
        self.ax.grid(True, alpha=0.3, color='gray')
        self.ax.set_xlabel('X Position', color='white')
        self.ax.set_ylabel('Y Position', color='white')
        self.ax.set_title('Vehicle Position Tracking', color='white')
        self.ax.tick_params(colors='white')
        for spine in self.ax.spines.values():
            spine.set_color('white')

        # Distance insets — left column: receivers 1-3 (indices 0-2)
        #                    right column: receivers 4-6 (indices 3-5)
        self.distance_insets  = []
        self.distance_lines   = []
        self.distance_windows = []
        self.sane_leds        = []

        for idx, row in enumerate([2, 1, 0]):   # top = receiver 3, bottom = receiver 1
            ax_d = self._make_distance_inset(gs, row, 0, idx + 1,
                                              label_side='right', led_x=0.08)
            self.distance_insets.append(ax_d[0])
            self.distance_lines.append(ax_d[1])
            self.distance_windows.append(ax_d[2])
            self.sane_leds.append(ax_d[3])

        for idx, row in enumerate([2, 1, 0]):   # top = receiver 6, bottom = receiver 4
            ax_d = self._make_distance_inset(gs, row, 2, idx + 4,
                                              label_side='left', led_x=0.92)
            self.distance_insets.append(ax_d[0])
            self.distance_lines.append(ax_d[1])
            self.distance_windows.append(ax_d[2])
            self.sane_leds.append(ax_d[3])

        self._draw_compass_rose()

        # Receivers
        self.ax.scatter(rx, ry, c='#FF00FF', s=100, zorder=5, label='Receivers')
        for rid, (x, y) in receiver_positions:
            self.ax.text(x + 5, y + 5, str(rid + 1), color='white', fontsize=10,
                         fontweight='bold', zorder=6, ha='left', va='bottom')

        conn = [0, 1, 2, 5, 4, 3, 0]
        self.ax.plot([rx[i] for i in conn], [ry[i] for i in conn],
                     color='#00FFFF', linewidth=2, alpha=0.7, label='Receiver Connections')

        # Vehicle plot artists
        self.vehicle_point, = self.ax.plot(
            [], [], 'o', color='#39FF14', markersize=10, zorder=6,
            label='Multilateration Position')
        self.vehicle_trail, = self.ax.plot(
            [], [], '-', color='#39FF14', linewidth=1, alpha=0.5, label='Multilat Trail')
        self.cep_point, = self.ax.plot(
            [], [], 'o', color='#FFFF00', markersize=10, zorder=6, label='CEP Position')
        self.cep_trail, = self.ax.plot(
            [], [], '-', color='#FFFF00', linewidth=1, alpha=0.5, label='CEP Trail')

        self.ax.legend(loc='upper right', facecolor='#222222',
                       edgecolor='white', labelcolor='white')

    def _make_distance_inset(self, gs, row, col, receiver_num, label_side, led_x):
        ax = self.fig.add_subplot(gs[row, col])
        ax.set_facecolor('black')
        ax.set_title(f'Receiver {receiver_num}', fontsize=10, color='white')
        ax.tick_params(axis='both', labelsize=8, colors='white')
        ax.set_xlim(0, 50)
        ax.set_ylim(0, 500)
        ax.set_xlabel('Sample', fontsize=8, color='white')
        ax.set_ylabel('Distance (cm)', fontsize=8, color='white')
        if label_side == 'right':
            ax.yaxis.set_label_position('right')
            ax.yaxis.tick_right()
            dw_ha, dw_x = 'right', 0.98
        else:
            ax.yaxis.set_label_position('left')
            ax.yaxis.tick_left()
            dw_ha, dw_x = 'left', 0.02
        ax.grid(True, alpha=0.3, color='gray')
        for spine in ax.spines.values():
            spine.set_color('white')

        line, = ax.plot([], [], color='#39FF14', linewidth=1)
        dw = ax.text(dw_x, 0.95, '---', ha=dw_ha, va='top', fontsize=12,
                     fontweight='bold', transform=ax.transAxes, color='white',
                     bbox=dict(boxstyle='round', facecolor='#222222', alpha=0.8))
        led = Circle((led_x, 0.88), 0.06, transform=ax.transAxes,
                     facecolor='#39FF14', edgecolor='white', linewidth=1.5, zorder=10)
        ax.add_patch(led)

        return ax, line, dw, led

    # ------------------------------------------------------------------
    # Qt widget layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # --- Top bar ---
        top_bar = QWidget()
        top_bar.setStyleSheet("background-color: black;")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 5, 10, 5)

        title = QLabel("Position Tracking")
        title.setFont(QFont('Arial', 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #39FF14;")
        top_layout.addWidget(title)
        top_layout.addStretch()

        back_btn = QPushButton("← Back")
        back_btn.setStyleSheet("""
            QPushButton { background-color:#39FF14; color:black; font:bold 12px Arial;
                          padding:5px 10px; border-radius:4px; }
            QPushButton:hover { background-color:#2BCC10; }
        """)
        back_btn.clicked.connect(lambda: self._main_window.show_panel('main'))
        top_layout.addWidget(back_btn)
        layout.addWidget(top_bar)

        # --- Canvas ---
        self._canvas = FigureCanvasQTAgg(self.fig)
        layout.addWidget(self._canvas, stretch=1)

        # --- Control bar ---
        ctrl = QWidget()
        ctrl.setStyleSheet("background-color: black;")
        ctrl_layout = QHBoxLayout(ctrl)
        ctrl_layout.setContentsMargins(20, 5, 20, 10)

        # Multilateration group
        ml_group = QWidget()
        ml_layout = QVBoxLayout(ml_group)
        ml_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.multilat_label = QLabel("Multilat: (---, ---)")
        self.multilat_label.setFont(QFont('Arial', 14, QFont.Weight.Bold))
        self.multilat_label.setStyleSheet("color: #39FF14;")
        self.multilat_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ml_layout.addWidget(self.multilat_label)

        ml_cb = QCheckBox("Show Multilateration")
        ml_cb.setChecked(True)
        ml_cb.setStyleSheet("color:#39FF14; font:bold 10px Arial;")
        ml_cb.toggled.connect(lambda checked: setattr(self, 'show_multilateration', checked))
        ml_layout.addWidget(ml_cb, alignment=Qt.AlignmentFlag.AlignCenter)
        ctrl_layout.addWidget(ml_group)

        # Insufficient receivers group
        insuff_group = QWidget()
        insuff_layout = QVBoxLayout(insuff_group)
        insuff_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        insuff_lbl = QLabel("Insufficient Receivers:")
        insuff_lbl.setStyleSheet("color:white; font:bold 12px Arial;")
        insuff_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        insuff_layout.addWidget(insuff_lbl)

        self.insuff_led = QFrame()
        self.insuff_led.setFixedSize(20, 20)
        self.insuff_led.setStyleSheet(
            "background-color:#39FF14; border-radius:10px; border:2px solid white;")
        insuff_layout.addWidget(self.insuff_led, alignment=Qt.AlignmentFlag.AlignCenter)

        self._mode_cb = QCheckBox("Continuous:UDP")
        self._mode_cb.setChecked(True)
        self._mode_cb.setStyleSheet("color:#00FFFF; font:bold 10px Arial;")
        self._mode_cb.toggled.connect(
            lambda checked: self._mode_cb.setText("Continuous:UDP" if checked else "Normal:TCP"))
        insuff_layout.addWidget(self._mode_cb, alignment=Qt.AlignmentFlag.AlignCenter)
        ctrl_layout.addWidget(insuff_group)

        # CEP group
        cep_group = QWidget()
        cep_layout = QVBoxLayout(cep_group)
        cep_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        cep_row = QWidget()
        cep_row_layout = QHBoxLayout(cep_row)
        cep_row_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.cep_label = QLabel("CEP: (---, ---)")
        self.cep_label.setFont(QFont('Arial', 14, QFont.Weight.Bold))
        self.cep_label.setStyleSheet("color:#FFFF00;")
        cep_row_layout.addWidget(self.cep_label)

        self.cep_validity_led = QFrame()
        self.cep_validity_led.setFixedSize(20, 20)
        self.cep_validity_led.setStyleSheet(
            "background-color:#39FF14; border-radius:10px; border:2px solid white;")
        cep_row_layout.addWidget(self.cep_validity_led)
        cep_layout.addWidget(cep_row)

        cep_cb = QCheckBox("Show CEP")
        cep_cb.setChecked(True)
        cep_cb.setStyleSheet("color:#FFFF00; font:bold 10px Arial;")
        cep_cb.toggled.connect(lambda checked: setattr(self, 'show_cep', checked))
        cep_layout.addWidget(cep_cb, alignment=Qt.AlignmentFlag.AlignCenter)
        ctrl_layout.addWidget(cep_group)

        # Performance counters
        perf_group = QWidget()
        perf_layout = QVBoxLayout(perf_group)
        perf_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._fps_label = QLabel("FPS: --")
        self._fps_label.setFont(QFont('Courier', 11, QFont.Weight.Bold))
        self._fps_label.setStyleSheet("color:#AAAAAA;")
        self._fps_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        perf_layout.addWidget(self._fps_label)

        self._pos_sec_label = QLabel("Pos/s: --")
        self._pos_sec_label.setFont(QFont('Courier', 11, QFont.Weight.Bold))
        self._pos_sec_label.setStyleSheet("color:#AAAAAA;")
        self._pos_sec_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        perf_layout.addWidget(self._pos_sec_label)
        ctrl_layout.addWidget(perf_group)

        # Navigation buttons
        nav_group = QWidget()
        nav_layout = QVBoxLayout(nav_group)

        cal_btn = QPushButton("Calibration")
        cal_btn.setStyleSheet("""
            QPushButton { background-color:#FF00FF; color:white; font:bold 12px Arial;
                          padding:8px 15px; border-radius:4px; }
            QPushButton:hover { background-color:#CC00CC; }
        """)
        cal_btn.clicked.connect(lambda: self._main_window.show_panel('calibration'))
        nav_layout.addWidget(cal_btn)

        arena_btn = QPushButton("Arena Maker")
        arena_btn.setStyleSheet("""
            QPushButton { background-color:#00FFFF; color:black; font:bold 12px Arial;
                          padding:8px 15px; border-radius:4px; }
            QPushButton:hover { background-color:#00CCCC; }
        """)
        arena_btn.clicked.connect(lambda: self._main_window.show_panel('arena_maker'))
        nav_layout.addWidget(arena_btn)
        ctrl_layout.addWidget(nav_group)

        layout.addWidget(ctrl)

    # ------------------------------------------------------------------
    # Matplotlib helpers
    # ------------------------------------------------------------------

    def _draw_compass_rose(self) -> None:
        for attr in ('_compass_x_arrow', '_compass_x_text',
                     '_compass_y_arrow', '_compass_y_text'):
            el = getattr(self, attr, None)
            if el is not None:
                el.remove()

        x_min, x_max = self.ax.get_xlim()
        y_min, y_max = self.ax.get_ylim()
        al = min(x_max - x_min, y_max - y_min) * 0.08

        self._compass_x_arrow = self.ax.annotate(
            '', xy=(al, 0), xytext=(0, 0),
            arrowprops=dict(arrowstyle='->', color='white', lw=2, zorder=7))
        self._compass_x_text = self.ax.text(
            al * 0.5, -al * 0.3, 'X', color='white', fontsize=12,
            fontweight='bold', ha='center', va='top', zorder=7)
        self._compass_y_arrow = self.ax.annotate(
            '', xy=(0, al), xytext=(0, 0),
            arrowprops=dict(arrowstyle='->', color='white', lw=2, zorder=7))
        self._compass_y_text = self.ax.text(
            -al * 0.3, al * 0.5, 'Y', color='white', fontsize=12,
            fontweight='bold', ha='right', va='center', zorder=7)

    def update_arena(self, receiver_positions: list) -> None:
        """Refresh receiver layout on the arena and reload the position library."""
        self.receiver_positions = receiver_positions
        rx = [p[1][0] for p in receiver_positions]
        ry = [p[1][1] for p in receiver_positions]

        self.ax.set_xlim(min(rx) - self.grid_padding, max(rx) + self.grid_padding)
        self.ax.set_ylim(min(ry) - self.grid_padding, max(ry) + self.grid_padding)
        self._draw_compass_rose()

        for artist in self.ax.collections[:]:
            if artist.get_label() == 'Receivers':
                artist.remove()
        for text in self.ax.texts[:]:
            t = text.get_text()
            if t.isdigit() and 1 <= int(t) <= 6:
                text.remove()
        for line in self.ax.lines[:]:
            if line.get_label() == 'Receiver Connections':
                line.remove()

        self.ax.scatter(rx, ry, c='#FF00FF', s=100, zorder=5, label='Receivers')
        for rid, (x, y) in receiver_positions:
            self.ax.text(x + 5, y + 5, str(rid + 1), color='white', fontsize=10,
                         fontweight='bold', zorder=6, ha='left', va='bottom')

        conn = [0, 1, 2, 5, 4, 3, 0]
        self.ax.plot([rx[i] for i in conn], [ry[i] for i in conn],
                     color='#00FFFF', linewidth=2, alpha=0.7, label='Receiver Connections')

        self.position_history.clear()
        self.cep_history.clear()
        self._position_lib.reload()

    def apply_median_filter(self, distances: list) -> np.ndarray:
        filtered = np.zeros(6)
        for i in range(6):
            self.distance_filter_buffers[i].append(distances[i])
            filtered[i] = np.median(list(self.distance_filter_buffers[i]))
        return filtered

    # ------------------------------------------------------------------
    # NetworkThread signal handlers  (run on main thread via queued conn)
    # ------------------------------------------------------------------

    def _on_lm_updated(self, x: float, y: float) -> None:
        self.position_history.append((x, y))
        self.multilat_label.setText(f'Multilat: ({x:.1f}, {y:.1f})')

        if self.show_multilateration:
            self.vehicle_point.set_data([x], [y])
            if len(self.position_history) > 1:
                tx = [p[0] for p in self.position_history]
                ty = [p[1] for p in self.position_history]
                self.vehicle_trail.set_data(tx, ty)
        else:
            self.vehicle_point.set_data([], [])
            self.vehicle_trail.set_data([], [])

    def _on_cep_updated(self, x: float, y: float) -> None:
        self.cep_history.append((x, y))
        self.cep_label.setText(f'CEP: ({x:.1f}, {y:.1f})')

        if self.show_cep:
            self.cep_point.set_data([x], [y])
            if len(self.cep_history) > 1:
                tx = [p[0] for p in self.cep_history]
                ty = [p[1] for p in self.cep_history]
                self.cep_trail.set_data(tx, ty)
        else:
            self.cep_point.set_data([], [])
            self.cep_trail.set_data([], [])

    def _on_distances_updated(self, distances: list) -> None:
        for i in range(6):
            d = distances[i]
            self.distance_histories[i].append(d)
            hist = list(self.distance_histories[i])
            self.distance_lines[i].set_data(list(range(len(hist))), hist)
            if hist:
                yc = hist[-1]
                yr = yc * 3
                self.distance_insets[i].set_ylim(max(0, yc - yr), yc + yr)
                self.distance_insets[i].set_xlim(0, max(50, len(hist)))
            self.distance_windows[i].set_text(f'{d:.1f} cm')

    def _on_sane_updated(self, sane_indices: list) -> None:
        for i in range(6):
            color = '#39FF14' if i in sane_indices else '#FF0000'
            self.sane_leds[i].set_facecolor(color)

    def _on_cep_validity_changed(self, invalid: bool) -> None:
        color = '#FF0000' if invalid else '#39FF14'
        self.cep_validity_led.setStyleSheet(
            f"background-color:{color}; border-radius:10px; border:2px solid white;")

    def _on_insuff_changed(self, insufficient: bool) -> None:
        color = '#FF0000' if insufficient else '#39FF14'
        self.insuff_led.setStyleSheet(
            f"background-color:{color}; border-radius:10px; border:2px solid white;")

    def _on_pos_sec_updated(self, rate: float) -> None:
        self._pos_sec_label.setText(f"Pos/s: {rate:.1f}")

    def _on_refresh_timer(self) -> None:
        now = time.monotonic()
        dt = now - self._last_draw_time
        if dt > 0:
            self._fps_label.setText(f"FPS: {1.0 / dt:.1f}")
        self._last_draw_time = now
        self._canvas.draw_idle()

    # ------------------------------------------------------------------
    # Panel lifecycle
    # ------------------------------------------------------------------

    def on_panel_show(self) -> None:
        self.update_arena(self._settings.get_tower_coordinates())
        if not self._net_thread.isRunning():
            self._net_thread.start()
        self._refresh_timer.start()

    def on_panel_hide(self) -> None:
        self._refresh_timer.stop()
        if self._net_thread.isRunning():
            self._net_thread.stop()
