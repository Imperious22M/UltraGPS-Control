"""Calibration panel.

Owns the two-run histogram calibration UI.  All calibration logic
(dialogs, histogram updates, offset display) lives here.
"""

from collections import deque

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QDialog,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal  # QTimer used for canvas refresh
from PyQt6.QtGui import QFont

from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

from ultragps_calibration import UltraGPSCalibration
from SettingsModule import SettingsModule


class CalibrationPanel(QWidget):
    """Full-page calibration panel.

    Contains the arena mini-plot, 6+6 histogram subplots (Run 1 / Run 2),
    an offset display, and the calibration control buttons.
    """

    # Signals used to forward calibration library callbacks to the main thread.
    # The calibration library calls on_reading/on_complete from a background
    # threading.Thread; emitting a pyqtSignal from any thread is safe and
    # automatically queued to the receiver's thread (the GUI thread here).
    _sig_histogram_update = pyqtSignal(int)        # run_num
    _sig_run_complete     = pyqtSignal(int, int)   # run_num, min_reads
    _sig_run_failed       = pyqtSignal(int, str)   # run_num, error_message
    _sig_tick_update      = pyqtSignal(int, object)  # run_num, list[float]

    def __init__(
        self,
        cal: UltraGPSCalibration,
        settings_module: SettingsModule,
        main_window,
    ):
        super().__init__()
        self._cal = cal
        self._settings = settings_module
        self._main_window = main_window
        self.setStyleSheet("background-color: black;")

        self.grid_padding = 20
        self.position_history = deque(maxlen=50)

        # Compass rose handles
        self._compass_x_arrow = None
        self._compass_x_text  = None
        self._compass_y_arrow = None
        self._compass_y_text  = None

        # Calibration point markers
        self.cal_point_scatter = None
        self.cal_point_label   = None

        # Latest raw tick values per run/receiver for the "Current Reading" display
        self._current_ticks: dict[int, dict[int, float]] = {1: {}, 2: {}}

        receiver_positions = self._settings.get_tower_coordinates()
        self._build_figure(receiver_positions)
        self._build_ui()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(33)
        self._refresh_timer.timeout.connect(self._canvas.draw_idle)

        # Wire cross-thread signals to GUI slots
        self._sig_histogram_update.connect(self._update_all_histograms)
        self._sig_run_complete.connect(self._on_run_complete)
        self._sig_run_failed.connect(self._on_run_failed)
        self._sig_tick_update.connect(self._on_tick_update)

    # ------------------------------------------------------------------
    # Figure
    # ------------------------------------------------------------------

    def _build_figure(self, receiver_positions: list) -> None:
        self.receiver_positions = receiver_positions
        rx = [p[1][0] for p in receiver_positions]
        ry = [p[1][1] for p in receiver_positions]

        self.fig = Figure(figsize=(18, 10), facecolor='black')
        gs = self.fig.add_gridspec(
            4, 7,
            width_ratios=[3, 1, 1, 0.2, 1, 1, 0.2],
            height_ratios=[0.02, 1, 1, 1],
            hspace=0.5, wspace=0.3,
            top=0.98, bottom=0.08,
        )

        # Arena
        self.ax = self.fig.add_subplot(gs[1:4, 0])
        self.ax.set_facecolor('black')
        self.ax.set_xlim(min(rx) - self.grid_padding, max(rx) + self.grid_padding)
        self.ax.set_ylim(min(ry) - self.grid_padding, max(ry) + self.grid_padding)
        self.ax.set_aspect('equal')
        self.ax.grid(True, alpha=0.3, color='gray')
        self.ax.set_xlabel('X Position', color='white')
        self.ax.set_ylabel('Y Position', color='white')
        self.ax.set_title('Calibration Arena', color='white')
        self.ax.tick_params(colors='white')
        for spine in self.ax.spines.values():
            spine.set_color('white')

        self._draw_compass_rose()

        self.ax.scatter(rx, ry, c='#FF00FF', s=100, zorder=5, label='Receivers')
        for rid, (x, y) in receiver_positions:
            self.ax.text(x + 5, y + 5, str(rid + 1), color='white', fontsize=10,
                         fontweight='bold', zorder=6, ha='left', va='bottom')

        conn = [0, 1, 2, 5, 4, 3, 0]
        self.ax.plot([rx[i] for i in conn], [ry[i] for i in conn],
                     color='#00FFFF', linewidth=2, alpha=0.7, label='Receiver Connections')

        self.vehicle_point, = self.ax.plot([], [], 'o', color='#39FF14', markersize=10, zorder=6)
        self.vehicle_trail, = self.ax.plot([], [], '-', color='#39FF14', linewidth=1, alpha=0.5)
        self.ax.legend(loc='upper right', facecolor='#222222',
                       edgecolor='white', labelcolor='white')

        # Run 1 histograms — left column (receivers 3, 2, 1) and right column (6, 5, 4)
        self.run1_axes: dict[int, object] = {}
        for recv_id, row in [(2, 1), (1, 2), (0, 3)]:
            ax = self.fig.add_subplot(gs[row, 1])
            self._style_hist_ax(ax, f"Receiver {recv_id + 1}")
            self.run1_axes[recv_id] = ax
        for recv_id, row in [(5, 1), (4, 2), (3, 3)]:
            ax = self.fig.add_subplot(gs[row, 2])
            self._style_hist_ax(ax, f"Receiver {recv_id + 1}")
            self.run1_axes[recv_id] = ax

        run1_title_ax = self.fig.add_subplot(gs[0, 1:3])
        run1_title_ax.set_facecolor('black')
        run1_title_ax.axis('off')
        run1_title_ax.text(0.5, 0.5, 'Run 1', color='#00FFFF', fontsize=14,
                           fontweight='bold', ha='center', va='center',
                           transform=run1_title_ax.transAxes)

        # Run 2 histograms
        self.run2_axes: dict[int, object] = {}
        for recv_id, row in [(2, 1), (1, 2), (0, 3)]:
            ax = self.fig.add_subplot(gs[row, 4])
            self._style_hist_ax(ax, f"Receiver {recv_id + 1}")
            self.run2_axes[recv_id] = ax
        for recv_id, row in [(5, 1), (4, 2), (3, 3)]:
            ax = self.fig.add_subplot(gs[row, 5])
            self._style_hist_ax(ax, f"Receiver {recv_id + 1}")
            self.run2_axes[recv_id] = ax

        run2_title_ax = self.fig.add_subplot(gs[0, 4:6])
        run2_title_ax.set_facecolor('black')
        run2_title_ax.axis('off')
        run2_title_ax.text(0.5, 0.5, 'Run 2', color='#00FFFF', fontsize=14,
                           fontweight='bold', ha='center', va='center',
                           transform=run2_title_ax.transAxes)

    def _style_hist_ax(self, ax, title: str) -> None:
        ax.set_facecolor('black')
        ax.set_title(title, color='white', fontsize=8)
        ax.tick_params(colors='white', labelsize=6)
        for spine in ax.spines.values():
            spine.set_color('white')
        ax.set_xlabel('Serial Value', color='white', fontsize=6)
        ax.set_ylabel('Count', color='white', fontsize=6)

    # ------------------------------------------------------------------
    # Qt UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Top bar
        top_bar = QWidget()
        top_bar.setStyleSheet("background-color: black;")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 5, 10, 5)

        title = QLabel("Calibration Mode")
        title.setFont(QFont('Arial', 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #FF00FF;")
        top_layout.addWidget(title)

        start_btn = QPushButton("Start Calibration")
        start_btn.setStyleSheet("""
            QPushButton { background-color:#FFD700; color:black; font:bold 12px Arial;
                          padding:5px 15px; border-radius:4px; }
            QPushButton:hover { background-color:#FFC000; }
        """)
        start_btn.clicked.connect(self._start_calibration)
        top_layout.addWidget(start_btn)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #FFFF00; font: bold 11px Arial;")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_layout.addWidget(self._status_label)

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

        # Canvas
        self._canvas = FigureCanvasQTAgg(self.fig)
        layout.addWidget(self._canvas, stretch=1)

        # Offset panel
        offset_panel = QWidget()
        offset_panel.setStyleSheet(
            "background-color:#222222; border-top:2px solid #444444;")
        offset_vbox = QVBoxLayout(offset_panel)

        offset_title = QLabel("Ax + b Receiver Offsets")
        offset_title.setFont(QFont('Arial', 12, QFont.Weight.Bold))
        offset_title.setStyleSheet("color: #FFD700;")
        offset_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        offset_vbox.addWidget(offset_title)

        offset_row = QWidget()
        offset_row_layout = QHBoxLayout(offset_row)

        self._offset_labels: dict[int, QLabel] = {}

        left_col = QWidget()
        left_col_layout = QVBoxLayout(left_col)
        for recv_id in [2, 1, 0]:
            lbl = QLabel(f"Receiver {recv_id + 1} Offset: 0.0x + 0.0")
            lbl.setStyleSheet("color:white; font:10px Arial;")
            left_col_layout.addWidget(lbl)
            self._offset_labels[recv_id] = lbl
        offset_row_layout.addWidget(left_col)

        right_col = QWidget()
        right_col_layout = QVBoxLayout(right_col)
        for recv_id in [5, 4, 3]:
            lbl = QLabel(f"Receiver {recv_id + 1} Offset: 0.0x + 0.0")
            lbl.setStyleSheet("color:white; font:10px Arial;")
            right_col_layout.addWidget(lbl)
            self._offset_labels[recv_id] = lbl
        offset_row_layout.addWidget(right_col)

        offset_vbox.addWidget(offset_row)
        layout.addWidget(offset_panel)

    # ------------------------------------------------------------------
    # Compass rose / arena updates
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

    def show_calibration_point(self, coords, num: int) -> None:
        self.hide_calibration_point()
        x, y = coords
        self.cal_point_scatter = self.ax.scatter(
            [x], [y], c='#39FF14', s=200, marker='D', zorder=8)
        self.cal_point_label = self.ax.text(
            x + 10, y + 10, f'Cal {num}', color='#39FF14',
            fontsize=10, fontweight='bold', zorder=8)
        self._canvas.draw_idle()

    def hide_calibration_point(self) -> None:
        if self.cal_point_scatter is not None:
            self.cal_point_scatter.remove()
            self.cal_point_scatter = None
        if self.cal_point_label is not None:
            self.cal_point_label.remove()
            self.cal_point_label = None

    # ------------------------------------------------------------------
    # Histogram display
    # ------------------------------------------------------------------

    def update_histogram(self, receiver_id: int, run_num: int) -> None:
        ax = self.run1_axes[receiver_id] if run_num == 1 else self.run2_axes[receiver_id]
        data = self._cal.get_histogram_data(receiver_id, run_num)
        ax.clear()
        self._style_hist_ax(ax, f"Receiver {receiver_id + 1}")

        current = self._current_ticks[run_num].get(receiver_id)
        if current is not None:
            ax.text(
                0.5, 0.89, f"Current: {int(current)}",
                ha='center', va='top', fontsize=7, color='#FFD700',
                transform=ax.transAxes,
                bbox=dict(boxstyle='round,pad=0.15', facecolor='black', alpha=0.6),
            )

        if not data:
            return

        sorted_data = sorted(data.items(), key=lambda kv: kv[1], reverse=True)
        values = [str(int(kv[0])) for kv in sorted_data[:10]]
        counts = [kv[1] for kv in sorted_data[:10]]

        if values:
            ax.bar(range(len(values)), counts, color='#00FFFF', alpha=0.7)
            n = len(values)
            if n <= 4:
                ax.set_xticks(range(n))
                ax.set_xticklabels(values, rotation=45, ha='right', fontsize=5)
            else:
                ticks = [0, n // 3, 2 * n // 3, n - 1]
                ax.set_xticks(ticks)
                ax.set_xticklabels([values[i] for i in ticks],
                                   rotation=45, ha='right', fontsize=5)
            most_val = sorted_data[0][0]
            most_cnt = sorted_data[0][1]
            ax.set_xlabel(f'{int(most_val)} (n={most_cnt})',
                          color='#39FF14', fontsize=8, fontweight='bold')

    def _on_tick_update(self, run_num: int, ticks: list) -> None:
        for recv_id in range(min(6, len(ticks))):
            self._current_ticks[run_num][recv_id] = ticks[recv_id]

    def _update_all_histograms(self, run_num: int) -> None:
        for recv_id in range(6):
            self.update_histogram(recv_id, run_num)

    # ------------------------------------------------------------------
    # Offset display
    # ------------------------------------------------------------------

    def _update_offset_display(self) -> None:
        for recv_id in range(6):
            if recv_id in self._offset_labels:
                offset = self._settings.get_receiver_offset(recv_id)
                if offset:
                    s = offset.get('slope', 0.0)
                    i = offset.get('intercept', 0.0)
                    self._offset_labels[recv_id].setText(
                        f"Receiver {recv_id + 1} Offset: {s:.4f}x + {i:.4f}")

    # ------------------------------------------------------------------
    # Calibration flow
    # ------------------------------------------------------------------

    def _start_calibration(self) -> None:
        cal_p1    = self._settings.cal_point_1
        min_reads = self._settings.calibration_reads

        self.show_calibration_point(cal_p1, 1)

        if not self._placement_dialog(
                "Calibration - Run 1",
                f"Place the transmitter at Calibration Point 1\n\n"
                f"Coordinates: ({cal_p1[0]:.1f}, {cal_p1[1]:.1f})\n\n"
                f"Minimum reads required: {min_reads}"):
            self.hide_calibration_point()
            return

        try:
            self._cal.clear_run_data(1)
            self._cal.start_run(
                1, min_reads,
                on_reading=lambda run, n: self._sig_histogram_update.emit(run),
                on_complete=lambda run: self._sig_run_complete.emit(run, min_reads),
                on_error=lambda run, msg: self._sig_run_failed.emit(run, msg),
                on_tick=lambda run, ticks: self._sig_tick_update.emit(run, ticks),
            )
            self._status_label.setText("● Collecting data for Run 1...")
            print("Calibration run 1 started")
        except Exception as exc:
            self._status_label.setText("")
            self.hide_calibration_point()
            self._on_run_failed(1, str(exc))

    def _on_run_complete(self, run_num: int, min_reads: int) -> None:
        if run_num == 1:
            self._status_label.setText("")
            cal_p2 = self._settings.cal_point_2
            self.show_calibration_point(cal_p2, 2)

            if not self._placement_dialog(
                    "Calibration - Run 2",
                    f"Run 1 Complete!\n\n"
                    f"Place the transmitter at Calibration Point 2\n\n"
                    f"Coordinates: ({cal_p2[0]:.1f}, {cal_p2[1]:.1f})"):
                self.hide_calibration_point()
                return

            self._cal.clear_run_data(2)
            self._cal.start_run(
                2, min_reads,
                on_reading=lambda run, n: self._sig_histogram_update.emit(run),
                on_complete=lambda run: self._sig_run_complete.emit(run, min_reads),
                on_error=lambda run, msg: self._sig_run_failed.emit(run, msg),
                on_tick=lambda run, ticks: self._sig_tick_update.emit(run, ticks),
            )
            self._status_label.setText("● Collecting data for Run 2...")
        else:
            self._status_label.setText("")
            self.hide_calibration_point()
            self._cal.calculate_and_save_offsets()
            self._settings.reload_config()
            self._update_offset_display()
            self._completion_dialog()

    def _on_run_failed(self, run_num: int, error_msg: str) -> None:
        self._status_label.setText("")
        self.hide_calibration_point()
        dlg = QDialog(self)
        dlg.setWindowTitle("Calibration Failed")
        dlg.setStyleSheet("background-color: black; color: white;")
        dlg.setFixedSize(420, 220)

        layout = QVBoxLayout(dlg)
        msg_lbl = QLabel(
            f"Run {run_num} failed:\n\n{error_msg}\n\n"
            "Check that the UltraGPS server is running\n"
            "and the hardware is connected."
        )
        msg_lbl.setStyleSheet("color: #FF4444; font: 12px Arial;")
        msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg_lbl.setWordWrap(True)
        layout.addWidget(msg_lbl)

        ok_btn = QPushButton("OK")
        ok_btn.setStyleSheet("""
            QPushButton { background-color: #FF4444; color: white;
                          font: bold 12px Arial; padding: 8px 30px; }
        """)
        ok_btn.clicked.connect(dlg.accept)
        layout.addWidget(ok_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        dlg.exec()

    # ------------------------------------------------------------------
    # Dialogs
    # ------------------------------------------------------------------

    def _placement_dialog(self, title: str, message: str) -> bool:
        """Show ready/cancel dialog.  Returns True if user clicked Ready."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setStyleSheet("background-color: black; color: white;")
        dlg.setFixedSize(420, 230)

        layout = QVBoxLayout(dlg)

        msg_lbl = QLabel(message)
        msg_lbl.setStyleSheet("color:#00FFFF; font:12px Arial;")
        msg_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg_lbl.setWordWrap(True)
        layout.addWidget(msg_lbl)

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)
        btn_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        ready_btn = QPushButton("Ready")
        ready_btn.setStyleSheet("""
            QPushButton { background-color:#39FF14; color:black;
                          font:bold 12px Arial; padding:8px 20px; }
        """)
        ready_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(ready_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet("""
            QPushButton { background-color:#FF4444; color:white;
                          font:bold 12px Arial; padding:8px 20px; }
        """)
        cancel_btn.clicked.connect(dlg.reject)
        btn_layout.addWidget(cancel_btn)

        layout.addWidget(btn_row)

        return dlg.exec() == QDialog.DialogCode.Accepted

    def _completion_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Calibration Complete")
        dlg.setStyleSheet("background-color: black;")
        dlg.setFixedSize(320, 160)

        layout = QVBoxLayout(dlg)

        msg = QLabel(
            "Calibration Complete!\n\n"
            "Offset values have been calculated\n"
            "and saved to the configuration file.")
        msg.setStyleSheet("color:#39FF14; font:12px Arial;")
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(msg)

        ok_btn = QPushButton("OK")
        ok_btn.setStyleSheet("""
            QPushButton { background-color:#39FF14; color:black;
                          font:bold 12px Arial; padding:8px 30px; }
        """)
        ok_btn.clicked.connect(dlg.accept)
        layout.addWidget(ok_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        dlg.exec()

    # ------------------------------------------------------------------
    # Panel lifecycle
    # ------------------------------------------------------------------

    def on_panel_show(self) -> None:
        self.update_arena(self._settings.get_tower_coordinates())
        self._settings.reload_config()
        self._update_offset_display()
        self._refresh_timer.start()

    def on_panel_hide(self) -> None:
        self._refresh_timer.stop()
        if self._cal.is_running:
            self._cal.stop_run()
