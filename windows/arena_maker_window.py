"""Arena Maker panel.

Owns all arena geometry calculation logic, distance entry fields,
calibration point entries, and the settings verification flow.
"""

import math

import numpy as np

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QDialog, QScrollArea, QTextEdit, QFrame,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

from SettingsModule import SettingsModule


class ArenaMakerPanel(QWidget):
    """Full-page arena configuration panel.

    Allows the user to set receiver positions via inter-receiver distances,
    place calibration points, and verify/save the configuration.
    """

    def __init__(self, settings_module: SettingsModule, main_window):
        super().__init__()
        self._settings = settings_module
        self._main_window = main_window
        self.setStyleSheet("background-color: black;")

        self.grid_padding = 20

        # Compass rose handles
        self._compass_x_arrow = None
        self._compass_x_text  = None
        self._compass_y_arrow = None
        self._compass_y_text  = None

        # Stored plot elements for incremental updates
        self.receiver_scatter  = None
        self.receiver_labels   = []
        self.connection_line   = None
        self.distance_labels   = []
        self.origin_lines      = []
        self.origin_labels     = []
        self.r9_line           = None
        self.r9_label          = None
        self.r10_line          = None
        self.r10_label         = None
        self.cal_point_1_scatter = None
        self.cal_point_1_label   = None
        self.cal_point_2_scatter = None
        self.cal_point_2_label   = None
        self.rc1_line  = None
        self.rc1_label = None
        self.rc2_line  = None
        self.rc2_label = None

        # Entry widget references
        self._distance_entries: dict[str, QLineEdit] = {}
        self._cal_point_entries: dict = {}

        # R9 / R10 previous-value tracking for adjustment logic
        self._prev_r9  = 0.0
        self._prev_r10 = 0.0

        receiver_positions = self._settings.get_tower_coordinates()
        self._build_figure(receiver_positions)
        self._build_ui()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(33)
        self._refresh_timer.timeout.connect(self._canvas.draw_idle)

    # ------------------------------------------------------------------
    # Figure
    # ------------------------------------------------------------------

    def _build_figure(self, receiver_positions: list) -> None:
        self.receiver_positions = receiver_positions
        rx = [p[1][0] for p in receiver_positions]
        ry = [p[1][1] for p in receiver_positions]

        self.fig = Figure(figsize=(10, 10), facecolor='black')
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor('black')
        self.ax.set_xlim(min(rx) - self.grid_padding, max(rx) + self.grid_padding)
        self.ax.set_ylim(min(ry) - self.grid_padding, max(ry) + self.grid_padding)
        self.ax.set_aspect('equal')
        self.ax.grid(True, alpha=0.3, color='gray')
        self.ax.set_xlabel('X Position', color='white')
        self.ax.set_ylabel('Y Position', color='white')
        self.ax.set_title('Arena Maker', color='white')
        self.ax.tick_params(colors='white')
        for spine in self.ax.spines.values():
            spine.set_color('white')

        self._draw_compass_rose()

        # Origin marker
        self.ax.scatter([0], [0], c='#0080FF', s=120, zorder=5)
        self.ax.text(5, 5, '(0, 0)', color='#0080FF', fontsize=9,
                     fontweight='bold', zorder=6, ha='left', va='bottom')

        self._init_plot_elements(receiver_positions)
        self.ax.legend(loc='upper right', facecolor='#222222',
                       edgecolor='white', labelcolor='white')

    def _init_plot_elements(self, receiver_positions: list) -> None:
        rx = [p[1][0] for p in receiver_positions]
        ry = [p[1][1] for p in receiver_positions]
        pos_dict = {p[0]: p[1] for p in receiver_positions}

        self.receiver_scatter = self.ax.scatter(
            rx, ry, c='#FF00FF', s=100, zorder=5, label='Receivers')

        self.receiver_labels = []
        for rid, (x, y) in receiver_positions:
            lbl = self.ax.text(x + 5, y + 5, f"{rid + 1}\n({x:.1f}, {y:.1f})",
                               color='white', fontsize=9, fontweight='bold',
                               zorder=6, ha='left', va='bottom')
            self.receiver_labels.append(lbl)

        conn = [0, 1, 2, 5, 4, 3, 0]
        line, = self.ax.plot([rx[i] for i in conn], [ry[i] for i in conn],
                             color='#00FFFF', linewidth=2, alpha=0.7)
        self.connection_line = line

        # R-distance labels
        self.distance_labels = []
        distance_pairs = [
            ('R1', 2, 1), ('R2', 1, 0), ('R3', 0, 3),
            ('R4', 3, 4), ('R5', 4, 5), ('R6', 5, 2),
        ]
        for name, from_id, to_id in distance_pairs:
            x1, y1 = pos_dict[from_id]
            x2, y2 = pos_dict[to_id]
            mid_x = (x1 + x2) / 2
            mid_y = (y1 + y2) / 2
            dist = abs(y2 - y1) if name in ('R1', 'R2', 'R4', 'R5') else abs(x2 - x1)
            if name in ('R1', 'R2'):
                ox, oy, ha = 15, 0, 'left'
            elif name in ('R4', 'R5'):
                ox, oy, ha = -15, 0, 'right'
            elif name == 'R3':
                ox, oy, ha = 0, 10, 'center'
            else:
                ox, oy, ha = 0, -10, 'center'
            lbl = self.ax.text(
                mid_x + ox, mid_y + oy, f"{name}\n{dist:.1f}",
                color='#FFFF00', fontsize=9, fontweight='bold', zorder=8,
                ha=ha, va='center',
                bbox=dict(boxstyle='round,pad=0.2', facecolor='black',
                          alpha=0.7, edgecolor='#FFFF00'))
            self.distance_labels.append(lbl)

        # R7 / R8 origin lines
        self.origin_lines  = []
        self.origin_labels = []
        x2_r, y2_r = pos_dict[1]
        x5_r, y5_r = pos_dict[4]

        for rx_val, label_name in [(x2_r, 'R7'), (x5_r, 'R8')]:
            ln, = self.ax.plot([0, rx_val], [0, 0], color='#FF8800',
                               linewidth=2, linestyle='--', zorder=4)
            self.origin_lines.append(ln)
            lbl = self.ax.text(
                rx_val / 2, 8, f"{label_name}\n{abs(rx_val):.1f}",
                color='#FF8800', fontsize=9, fontweight='bold', zorder=8,
                ha='center', va='bottom',
                bbox=dict(boxstyle='round,pad=0.2', facecolor='black',
                          alpha=0.7, edgecolor='#FF8800'))
            self.origin_labels.append(lbl)

        # R9 / R10 — shown only when non-zero
        self.r9_line = self.r9_label = None
        self.r10_line = self.r10_label = None

        if abs(y2_r) > 0.1:
            ln, = self.ax.plot([x2_r, x2_r], [0, y2_r], color='#00FF88',
                               linewidth=2, linestyle='--', zorder=4)
            self.r9_line = ln
            self.r9_label = self.ax.text(
                x2_r - 8, y2_r / 2, f"R9\n{y2_r:.1f}",
                color='#00FF88', fontsize=9, fontweight='bold', zorder=8,
                ha='right', va='center',
                bbox=dict(boxstyle='round,pad=0.2', facecolor='black',
                          alpha=0.7, edgecolor='#00FF88'))

        if abs(y5_r) > 0.1:
            ln, = self.ax.plot([x5_r, x5_r], [0, y5_r], color='#00FF88',
                               linewidth=2, linestyle='--', zorder=4)
            self.r10_line = ln
            self.r10_label = self.ax.text(
                x5_r + 8, y5_r / 2, f"R10\n{y5_r:.1f}",
                color='#00FF88', fontsize=9, fontweight='bold', zorder=8,
                ha='left', va='center',
                bbox=dict(boxstyle='round,pad=0.2', facecolor='black',
                          alpha=0.7, edgecolor='#00FF88'))

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

        title_lbl = QLabel("Arena Maker")
        title_lbl.setFont(QFont('Arial', 16, QFont.Weight.Bold))
        title_lbl.setStyleSheet("color: #00FFFF;")
        top_layout.addWidget(title_lbl)
        top_layout.addStretch()

        # Re-dimension button (always visible in arena maker)
        redim_btn = QPushButton("Re-dimension")
        redim_btn.setStyleSheet("""
            QPushButton { background-color:#00FFFF; color:black; font:bold 10px Arial;
                          padding:3px 8px; border-radius:4px; }
            QPushButton:hover { background-color:#00CCCC; }
        """)
        redim_btn.clicked.connect(self._redimension_arena)
        top_layout.addWidget(redim_btn)

        # Back / verify button — changes label after first successful verify
        self._top_right_btn = QPushButton("Verify & Save")
        self._top_right_btn.setStyleSheet("""
            QPushButton { background-color:#FFD700; color:black; font:bold 12px Arial;
                          padding:5px 10px; border-radius:4px; }
            QPushButton:hover { background-color:#FFC000; }
        """)
        self._top_right_btn.clicked.connect(self._verify_and_save_settings)
        top_layout.addWidget(self._top_right_btn)

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

        # Distance entries
        dist_widget = QWidget()
        dist_widget.setStyleSheet("background-color: black;")
        dist_layout = QHBoxLayout(dist_widget)
        dist_layout.setContentsMargins(10, 5, 10, 5)

        current_distances = self._calculate_distances_from_positions(
            self.receiver_positions)
        distance_info = [
            ('R1',  'Rcvr 3→2',    current_distances[0]),
            ('R2',  'Rcvr 2→1',    current_distances[1]),
            ('R3',  'Rcvr 1→4',    current_distances[2]),
            ('R4',  'Rcvr 4→5',    current_distances[3]),
            ('R5',  'Rcvr 5→6',    current_distances[4]),
            ('R6',  'Rcvr 6→3',    current_distances[5]),
            ('R7',  'Origin→2 X',  current_distances[6]),
            ('R8',  'Origin→5 X',  current_distances[7]),
            ('R9',  'Rcvr2 Y-ofs', current_distances[8]),
            ('R10', 'Rcvr5 Y-ofs', current_distances[9]),
        ]

        self._distance_entries = {}
        for name, desc, value in distance_info:
            col = QWidget()
            col_layout = QVBoxLayout(col)
            col_layout.setContentsMargins(5, 0, 5, 0)
            col_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

            lbl = QLabel(f"{name}\n({desc})")
            lbl.setStyleSheet("color:#00FFFF; font:bold 10px Arial;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col_layout.addWidget(lbl)

            entry = QLineEdit(f"{value:.1f}")
            entry.setStyleSheet("font:12px Arial; text-align:center;")
            entry.setFixedWidth(80)
            entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
            entry.returnPressed.connect(self._update_arena_from_distances)
            entry.editingFinished.connect(self._update_arena_from_distances)
            col_layout.addWidget(entry)
            dist_layout.addWidget(col)
            self._distance_entries[name] = entry

        layout.addWidget(dist_widget)

        # Calibration point entries
        cal_widget = QWidget()
        cal_widget.setStyleSheet("background-color: black;")
        cal_outer = QVBoxLayout(cal_widget)

        cal_title = QLabel("Calibration Points")
        cal_title.setFont(QFont('Arial', 12, QFont.Weight.Bold))
        cal_title.setStyleSheet("color: #00FF00;")
        cal_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cal_outer.addWidget(cal_title)

        cal_row = QWidget()
        cal_row_layout = QHBoxLayout(cal_row)
        cal_row_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        cal_p1 = self._settings.cal_point_1
        cal_p2 = self._settings.cal_point_2
        rc1    = math.sqrt(cal_p1[0]**2 + cal_p1[1]**2)
        rc2    = math.sqrt(cal_p2[0]**2 + cal_p2[1]**2)

        self._cal_point_entries = {}
        for name, xv, yv, rcv in [('Cal 1', cal_p1[0], cal_p1[1], rc1),
                                    ('Cal 2', cal_p2[0], cal_p2[1], rc2)]:
            grp = QWidget()
            grp_layout = QVBoxLayout(grp)
            grp_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

            grp_title = QLabel(name)
            grp_title.setStyleSheet("color:#00FF00; font:bold 10px Arial;")
            grp_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grp_layout.addWidget(grp_title)

            x_entry = self._labelled_entry(grp_layout, "X:", f"{xv:.1f}", '#00FF00')
            y_entry = self._labelled_entry(grp_layout, "Y:", f"{yv:.1f}", '#00FF00')
            rc_entry = self._labelled_entry(grp_layout, "RC:", f"{rcv:.1f}", '#00FF88')

            x_entry.editingFinished.connect(self._update_calibration_points_from_entries)
            y_entry.editingFinished.connect(self._update_calibration_points_from_entries)
            rc_entry.editingFinished.connect(self._update_rc_from_entries)

            self._cal_point_entries[name] = {'x': x_entry, 'y': y_entry}
            rc_key = 'RC_1' if name == 'Cal 1' else 'RC_2'
            self._cal_point_entries[rc_key] = rc_entry

            cal_row_layout.addWidget(grp)
            cal_row_layout.addSpacing(30)

        cal_outer.addWidget(cal_row)
        layout.addWidget(cal_widget)

    @staticmethod
    def _labelled_entry(parent_layout, label_text: str,
                        value: str, color: str) -> QLineEdit:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel(label_text)
        lbl.setStyleSheet(f"color:{color}; font:10px Arial;")
        row_layout.addWidget(lbl)

        entry = QLineEdit(value)
        entry.setStyleSheet("font:12px Arial;")
        entry.setFixedWidth(80)
        entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row_layout.addWidget(entry)

        parent_layout.addWidget(row)
        return entry

    # ------------------------------------------------------------------
    # Compass rose
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

    # ------------------------------------------------------------------
    # Arena position calculation
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_receiver_positions_from_dimensions(
            width: float, height: float) -> list:
        hw, hh = width / 2.0, height / 2.0
        return [
            (0, (-hw, -hh)), (1, (-hw, 0.0)), (2, (-hw, hh)),
            (3, ( hw, -hh)), (4, ( hw, 0.0)), (5, ( hw, hh)),
        ]

    @staticmethod
    def _calculate_positions_from_distances(
            r1, r2, r3, r4, r5, r6, r7=None, r8=None, r9=0, r10=0) -> list:
        if r7 is None:
            r7 = r6 / 2.0
        if r8 is None:
            r8 = r6 / 2.0

        x3 = -r6 / 2.0;  x2 = -r7;       x1 = -r3 / 2.0
        x4 =  r3 / 2.0;  x5 =  r8;        x6 =  r6 / 2.0
        y2 = r9;          y5 = r10
        y3 = y2 + r1;    y1 = y2 - r2
        y6 = y5 + r5;    y4 = y5 - r4

        return [
            (0, (x1, y1)), (1, (x2, y2)), (2, (x3, y3)),
            (3, (x4, y4)), (4, (x5, y5)), (5, (x6, y6)),
        ]

    @staticmethod
    def _calculate_distances_from_positions(positions: list) -> tuple:
        pd = {p[0]: p[1] for p in positions}

        r1 = abs(pd[2][1] - pd[1][1])
        r2 = abs(pd[1][1] - pd[0][1])
        r4 = abs(pd[4][1] - pd[3][1])
        r5 = abs(pd[5][1] - pd[4][1])
        r3 = abs(pd[3][0] - pd[0][0])
        r6 = abs(pd[5][0] - pd[2][0])
        r7 = abs(pd[1][0])
        r8 = abs(pd[4][0])
        r9  = pd[1][1]
        r10 = pd[4][1]

        return r1, r2, r3, r4, r5, r6, r7, r8, r9, r10

    # ------------------------------------------------------------------
    # Receiver position plot update
    # ------------------------------------------------------------------

    def update_arena(self, receiver_positions: list) -> None:
        self.update_receiver_positions(receiver_positions)

    def update_receiver_positions(self, new_positions: list) -> None:
        self.receiver_positions = new_positions
        rx = [p[1][0] for p in new_positions]
        ry = [p[1][1] for p in new_positions]
        pos_dict = {p[0]: p[1] for p in new_positions}

        self.ax.set_xlim(min(rx) - self.grid_padding, max(rx) + self.grid_padding)
        self.ax.set_ylim(min(ry) - self.grid_padding, max(ry) + self.grid_padding)
        self._draw_compass_rose()

        # Scatter
        if self.receiver_scatter:
            self.receiver_scatter.set_offsets(np.column_stack([rx, ry]))
        else:
            self.receiver_scatter = self.ax.scatter(
                rx, ry, c='#FF00FF', s=100, zorder=5, label='Receivers')

        # Labels
        for i, (rid, (x, y)) in enumerate(new_positions):
            txt = f"{rid + 1}\n({x:.1f}, {y:.1f})"
            if i < len(self.receiver_labels):
                self.receiver_labels[i].set_text(txt)
                self.receiver_labels[i].set_position((x + 5, y + 5))
            else:
                lbl = self.ax.text(x + 5, y + 5, txt, color='white', fontsize=9,
                                   fontweight='bold', zorder=6, ha='left', va='bottom')
                self.receiver_labels.append(lbl)

        # Connection line
        conn = [0, 1, 2, 5, 4, 3, 0]
        cx = [rx[i] for i in conn]
        cy = [ry[i] for i in conn]
        if self.connection_line:
            self.connection_line.set_data(cx, cy)
        else:
            self.connection_line, = self.ax.plot(cx, cy, color='#00FFFF', linewidth=2, alpha=0.7)

        # Distance labels
        distance_pairs = [
            ('R1', 2, 1), ('R2', 1, 0), ('R3', 0, 3),
            ('R4', 3, 4), ('R5', 4, 5), ('R6', 5, 2),
        ]
        for i, (name, from_id, to_id) in enumerate(distance_pairs):
            x1, y1 = pos_dict[from_id]
            x2, y2 = pos_dict[to_id]
            mid_x = (x1 + x2) / 2
            mid_y = (y1 + y2) / 2
            dist = abs(y2 - y1) if name in ('R1', 'R2', 'R4', 'R5') else abs(x2 - x1)
            txt = f"{name}\n{dist:.1f}"
            if name in ('R1', 'R2'):
                ox, oy = 15, 0
            elif name in ('R4', 'R5'):
                ox, oy = -15, 0
            elif name == 'R3':
                ox, oy = 0, 10
            else:
                ox, oy = 0, -10
            if i < len(self.distance_labels):
                self.distance_labels[i].set_text(txt)
                self.distance_labels[i].set_position((mid_x + ox, mid_y + oy))

        # R7 / R8
        x2_r, _ = pos_dict[1]
        x5_r, _ = pos_dict[4]
        for idx, (rx_val, name) in enumerate([(x2_r, 'R7'), (x5_r, 'R8')]):
            if idx < len(self.origin_lines):
                self.origin_lines[idx].set_data([0, rx_val], [0, 0])
                self.origin_labels[idx].set_text(f"{name}\n{abs(rx_val):.1f}")
                self.origin_labels[idx].set_position((rx_val / 2, 8))

        # R9 / R10
        _, y2_r = pos_dict[1]
        _, y5_r = pos_dict[4]
        self._update_offset_line(
            'r9', x2_r, 0, x2_r, y2_r, y2_r, 'R9', x2_r - 8, y2_r / 2, 'right')
        self._update_offset_line(
            'r10', x5_r, 0, x5_r, y5_r, y5_r, 'R10', x5_r + 8, y5_r / 2, 'left')

        handles, labels = self.ax.get_legend_handles_labels()
        if handles:
            self.ax.legend(handles, labels, loc='upper right',
                           facecolor='#222222', edgecolor='white', labelcolor='white')
        self._canvas.draw_idle()

    def _update_offset_line(self, attr_prefix, lx1, ly1, lx2, ly2,
                             val, label_name, label_x, label_y, ha):
        line_attr  = f'{attr_prefix}_line'
        label_attr = f'{attr_prefix}_label'
        if abs(val) > 0.1:
            if getattr(self, line_attr) is None:
                ln, = self.ax.plot([lx1, lx2], [ly1, ly2],
                                   color='#00FF88', linewidth=2, linestyle='--', zorder=4)
                setattr(self, line_attr, ln)
                lbl = self.ax.text(
                    label_x, label_y, f"{label_name}\n{val:.1f}",
                    color='#00FF88', fontsize=9, fontweight='bold', zorder=8,
                    ha=ha, va='center',
                    bbox=dict(boxstyle='round,pad=0.2', facecolor='black',
                              alpha=0.7, edgecolor='#00FF88'))
                setattr(self, label_attr, lbl)
            else:
                getattr(self, line_attr).set_data([lx1, lx2], [ly1, ly2])
                getattr(self, line_attr).set_visible(True)
                getattr(self, label_attr).set_text(f"{label_name}\n{val:.1f}")
                getattr(self, label_attr).set_position((label_x, label_y))
                getattr(self, label_attr).set_visible(True)
        else:
            ln = getattr(self, line_attr)
            if ln is not None:
                ln.set_visible(False)
            lbl = getattr(self, label_attr)
            if lbl is not None:
                lbl.set_visible(False)

    # ------------------------------------------------------------------
    # Calibration point plot update
    # ------------------------------------------------------------------

    def update_calibration_points(self, cal_p1, cal_p2) -> None:
        x1, y1 = cal_p1[0], cal_p1[1]
        x2, y2 = cal_p2[0], cal_p2[1]

        # Cal point 1
        if self.cal_point_1_scatter is None:
            self.cal_point_1_scatter = self.ax.scatter(
                [x1], [y1], c='#00FF00', s=150, marker='D', zorder=6, label='Cal Points')
        else:
            self.cal_point_1_scatter.set_offsets(np.array([[x1, y1]]))
            self.cal_point_1_scatter.set_visible(True)

        if self.cal_point_1_label is None:
            self.cal_point_1_label = self.ax.text(
                x1 + 8, y1, f"Cal 1\n({x1:.1f}, {y1:.1f})",
                color='#00FF00', fontsize=9, fontweight='bold', zorder=6, ha='left', va='center')
        else:
            self.cal_point_1_label.set_text(f"Cal 1\n({x1:.1f}, {y1:.1f})")
            self.cal_point_1_label.set_position((x1 + 8, y1))
            self.cal_point_1_label.set_visible(True)

        # Cal point 2
        if self.cal_point_2_scatter is None:
            self.cal_point_2_scatter = self.ax.scatter(
                [x2], [y2], c='#00FF00', s=150, marker='D', zorder=6)
        else:
            self.cal_point_2_scatter.set_offsets(np.array([[x2, y2]]))
            self.cal_point_2_scatter.set_visible(True)

        if self.cal_point_2_label is None:
            self.cal_point_2_label = self.ax.text(
                x2 + 8, y2, f"Cal 2\n({x2:.1f}, {y2:.1f})",
                color='#00FF00', fontsize=9, fontweight='bold', zorder=6, ha='left', va='center')
        else:
            self.cal_point_2_label.set_text(f"Cal 2\n({x2:.1f}, {y2:.1f})")
            self.cal_point_2_label.set_position((x2 + 8, y2))
            self.cal_point_2_label.set_visible(True)

        # RC lines
        rc1 = math.sqrt(x1**2 + y1**2)
        if self.rc1_line is None:
            self.rc1_line, = self.ax.plot([0, x1], [0, y1], color='#00FF00',
                                          linewidth=2, linestyle='--', zorder=4)
            self.rc1_label = self.ax.text(
                x1 / 2 + 8, y1 / 2, f"RC_1\n{rc1:.1f}",
                color='#00FF00', fontsize=9, fontweight='bold', zorder=8,
                ha='left', va='center',
                bbox=dict(boxstyle='round,pad=0.2', facecolor='black',
                          alpha=0.7, edgecolor='#00FF00'))
        else:
            self.rc1_line.set_data([0, x1], [0, y1])
            self.rc1_line.set_visible(True)
            self.rc1_label.set_text(f"RC_1\n{rc1:.1f}")
            self.rc1_label.set_position((x1 / 2 + 8, y1 / 2))
            self.rc1_label.set_visible(True)

        rc2 = math.sqrt(x2**2 + y2**2)
        if self.rc2_line is None:
            self.rc2_line, = self.ax.plot([0, x2], [0, y2], color='#00FF00',
                                          linewidth=2, linestyle='--', zorder=4)
            self.rc2_label = self.ax.text(
                x2 / 2 + 8, y2 / 2, f"RC_2\n{rc2:.1f}",
                color='#00FF00', fontsize=9, fontweight='bold', zorder=8,
                ha='left', va='center',
                bbox=dict(boxstyle='round,pad=0.2', facecolor='black',
                          alpha=0.7, edgecolor='#00FF00'))
        else:
            self.rc2_line.set_data([0, x2], [0, y2])
            self.rc2_line.set_visible(True)
            self.rc2_label.set_text(f"RC_2\n{rc2:.1f}")
            self.rc2_label.set_position((x2 / 2 + 8, y2 / 2))
            self.rc2_label.set_visible(True)

        handles, labels = self.ax.get_legend_handles_labels()
        if handles:
            self.ax.legend(handles, labels, loc='upper right',
                           facecolor='#222222', edgecolor='white', labelcolor='white')
        self._canvas.draw_idle()

    # ------------------------------------------------------------------
    # Entry-field driven updates
    # ------------------------------------------------------------------

    def _update_arena_from_distances(self) -> None:
        try:
            r1  = float(self._distance_entries['R1'].text())
            r2  = float(self._distance_entries['R2'].text())
            r3  = float(self._distance_entries['R3'].text())
            r4  = float(self._distance_entries['R4'].text())
            r5  = float(self._distance_entries['R5'].text())
            r6  = float(self._distance_entries['R6'].text())
            r7  = float(self._distance_entries['R7'].text())
            r8  = float(self._distance_entries['R8'].text())
            r9  = float(self._distance_entries['R9'].text())
            r10 = float(self._distance_entries['R10'].text())
        except ValueError:
            return

        prev_r9  = self._prev_r9
        prev_r10 = self._prev_r10

        # R9 adjustment: keep receivers 1 and 3 fixed in absolute Y
        if r9 != prev_r9:
            r2 -= prev_r9;  r1 += prev_r9  # undo previous
            r2 += r9;       r1 -= r9        # apply new
            self._distance_entries['R1'].setText(f"{r1:.1f}")
            self._distance_entries['R2'].setText(f"{r2:.1f}")
            self._prev_r9 = r9

        # R10 adjustment: keep receivers 4 and 6 fixed in absolute Y
        if r10 != prev_r10:
            r4 -= prev_r10; r5 += prev_r10  # undo previous
            r4 += r10;      r5 -= r10        # apply new
            self._distance_entries['R4'].setText(f"{r4:.1f}")
            self._distance_entries['R5'].setText(f"{r5:.1f}")
            self._prev_r10 = r10

        if any(v <= 0 for v in [r1, r2, r3, r4, r5, r6, r7, r8]):
            return

        new_positions = self._calculate_positions_from_distances(
            r1, r2, r3, r4, r5, r6, r7, r8, r9, r10)
        self.update_receiver_positions(new_positions)

        for rid, (x, y) in new_positions:
            self._settings.set_receiver_position(rid, x, y)

        cal_p1 = self._settings.cal_point_1
        cal_p2 = self._settings.cal_point_2
        for rid, (rx, ry) in new_positions:
            d1 = math.sqrt((rx - cal_p1[0])**2 + (ry - cal_p1[1])**2)
            d2 = math.sqrt((rx - cal_p2[0])**2 + (ry - cal_p2[1])**2)
            self._settings.set_receiver_cal_distances(rid, [d1, d2])

    def _update_calibration_points_from_entries(self) -> None:
        try:
            c1x = float(self._cal_point_entries['Cal 1']['x'].text())
            c1y = float(self._cal_point_entries['Cal 1']['y'].text())
            c2x = float(self._cal_point_entries['Cal 2']['x'].text())
            c2y = float(self._cal_point_entries['Cal 2']['y'].text())
        except ValueError:
            return

        self._settings.cal_point_1 = [c1x, c1y]
        self._settings.cal_point_2 = [c2x, c2y]

        self.update_calibration_points([c1x, c1y], [c2x, c2y])

        rc1 = math.sqrt(c1x**2 + c1y**2)
        rc2 = math.sqrt(c2x**2 + c2y**2)
        self._cal_point_entries['RC_1'].setText(f"{rc1:.1f}")
        self._cal_point_entries['RC_2'].setText(f"{rc2:.1f}")

        for rid in range(6):
            pos = self._settings.get_receiver_position(rid)
            if pos:
                rx, ry = pos
                d1 = math.sqrt((rx - c1x)**2 + (ry - c1y)**2)
                d2 = math.sqrt((rx - c2x)**2 + (ry - c2y)**2)
                self._settings.set_receiver_cal_distances(rid, [d1, d2])

    def _update_rc_from_entries(self) -> None:
        try:
            rc1 = float(self._cal_point_entries['RC_1'].text())
            rc2 = float(self._cal_point_entries['RC_2'].text())
        except ValueError:
            return

        cal_p1 = list(self._settings.cal_point_1)
        cal_p2 = list(self._settings.cal_point_2)

        cur1 = math.sqrt(cal_p1[0]**2 + cal_p1[1]**2)
        if cur1 > 0.001:
            s = rc1 / cur1
            cal_p1 = [cal_p1[0] * s, cal_p1[1] * s]
        else:
            cal_p1 = [0.0, rc1]

        cur2 = math.sqrt(cal_p2[0]**2 + cal_p2[1]**2)
        if cur2 > 0.001:
            s = rc2 / cur2
            cal_p2 = [cal_p2[0] * s, cal_p2[1] * s]
        else:
            cal_p2 = [0.0, -rc2]

        self._settings.cal_point_1 = cal_p1
        self._settings.cal_point_2 = cal_p2

        self._cal_point_entries['Cal 1']['x'].setText(f"{cal_p1[0]:.1f}")
        self._cal_point_entries['Cal 1']['y'].setText(f"{cal_p1[1]:.1f}")
        self._cal_point_entries['Cal 2']['x'].setText(f"{cal_p2[0]:.1f}")
        self._cal_point_entries['Cal 2']['y'].setText(f"{cal_p2[1]:.1f}")

        self.update_calibration_points(cal_p1, cal_p2)

        for rid in range(6):
            pos = self._settings.get_receiver_position(rid)
            if pos:
                rx, ry = pos
                d1 = math.sqrt((rx - cal_p1[0])**2 + (ry - cal_p1[1])**2)
                d2 = math.sqrt((rx - cal_p2[0])**2 + (ry - cal_p2[1])**2)
                self._settings.set_receiver_cal_distances(rid, [d1, d2])

    # ------------------------------------------------------------------
    # Redimension dialog
    # ------------------------------------------------------------------

    def _ask_arena_dimensions(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Arena Dimensions")
        dlg.setStyleSheet("background-color: black; color: white;")
        dlg.setFixedSize(400, 220)

        layout = QVBoxLayout(dlg)

        title = QLabel("Enter Arena Dimensions")
        title.setFont(QFont('Arial', 14, QFont.Weight.Bold))
        title.setStyleSheet("color:#00FFFF;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        w_row = QWidget()
        w_layout = QHBoxLayout(w_row)
        w_layout.addWidget(QLabel("Width (cm):"))
        w_entry = QLineEdit()
        w_entry.setFixedWidth(100)
        w_layout.addWidget(w_entry)
        layout.addWidget(w_row)

        h_row = QWidget()
        h_layout = QHBoxLayout(h_row)
        h_layout.addWidget(QLabel("Height (cm):"))
        h_entry = QLineEdit()
        h_entry.setFixedWidth(100)
        h_layout.addWidget(h_entry)
        layout.addWidget(h_row)

        for widget in (title, w_row, h_row):
            widget.setStyleSheet("color: white;")

        err_lbl = QLabel("")
        err_lbl.setStyleSheet("color:red;")
        err_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(err_lbl)

        btn_row = QWidget()
        btn_layout = QHBoxLayout(btn_row)

        result = [None, None]

        def on_ok():
            try:
                w = float(w_entry.text())
                h = float(h_entry.text())
                if w > 0 and h > 0:
                    result[0], result[1] = w, h
                    dlg.accept()
                else:
                    err_lbl.setText("Dimensions must be positive numbers!")
            except ValueError:
                err_lbl.setText("Please enter valid numbers!")

        ok_btn = QPushButton("OK")
        ok_btn.setStyleSheet("""
            QPushButton { background-color:#39FF14; color:black; font:bold 12px Arial;
                          padding:5px 10px; }
        """)
        ok_btn.clicked.connect(on_ok)
        btn_layout.addWidget(ok_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet("""
            QPushButton { background-color:#FF0000; color:white; font:bold 12px Arial;
                          padding:5px 10px; }
        """)
        cancel_btn.clicked.connect(dlg.reject)
        btn_layout.addWidget(cancel_btn)
        layout.addWidget(btn_row)

        w_entry.returnPressed.connect(h_entry.setFocus)
        h_entry.returnPressed.connect(on_ok)
        w_entry.setFocus()

        dlg.exec()
        return result[0], result[1]

    def _redimension_arena(self) -> None:
        width, height = self._ask_arena_dimensions()
        if width is None:
            return

        new_positions = self._calculate_receiver_positions_from_dimensions(width, height)
        self.update_receiver_positions(new_positions)
        for rid, (x, y) in new_positions:
            self._settings.set_receiver_position(rid, x, y)

        cal_p1 = [0.0, height / 4.0]
        cal_p2 = [0.0, -height / 4.0]
        self._settings.cal_point_1 = cal_p1
        self._settings.cal_point_2 = cal_p2
        self.update_calibration_points(cal_p1, cal_p2)

        for rid, (rx, ry) in new_positions:
            d1 = math.sqrt((rx - cal_p1[0])**2 + (ry - cal_p1[1])**2)
            d2 = math.sqrt((rx - cal_p2[0])**2 + (ry - cal_p2[1])**2)
            self._settings.set_receiver_cal_distances(rid, [d1, d2])

        self._refresh_entries_from_state(new_positions, cal_p1, cal_p2)

    def _refresh_entries_from_state(self, positions, cal_p1, cal_p2) -> None:
        dists = self._calculate_distances_from_positions(positions)
        for i, name in enumerate(['R1','R2','R3','R4','R5','R6','R7','R8','R9','R10']):
            if name in self._distance_entries:
                self._distance_entries[name].setText(f"{dists[i]:.1f}")

        self._cal_point_entries['Cal 1']['x'].setText(f"{cal_p1[0]:.1f}")
        self._cal_point_entries['Cal 1']['y'].setText(f"{cal_p1[1]:.1f}")
        self._cal_point_entries['Cal 2']['x'].setText(f"{cal_p2[0]:.1f}")
        self._cal_point_entries['Cal 2']['y'].setText(f"{cal_p2[1]:.1f}")
        rc1 = math.sqrt(cal_p1[0]**2 + cal_p1[1]**2)
        rc2 = math.sqrt(cal_p2[0]**2 + cal_p2[1]**2)
        self._cal_point_entries['RC_1'].setText(f"{rc1:.1f}")
        self._cal_point_entries['RC_2'].setText(f"{rc2:.1f}")

    # ------------------------------------------------------------------
    # Verify and save
    # ------------------------------------------------------------------

    def _verify_and_save_settings(self) -> None:
        try:
            c1x = float(self._cal_point_entries['Cal 1']['x'].text())
            c1y = float(self._cal_point_entries['Cal 1']['y'].text())
            c2x = float(self._cal_point_entries['Cal 2']['x'].text())
            c2y = float(self._cal_point_entries['Cal 2']['y'].text())
            self._settings.cal_point_1 = [c1x, c1y]
            self._settings.cal_point_2 = [c2x, c2y]
        except ValueError as e:
            self._verify_result_dialog(False, [f"Invalid value: {e}"])
            return

        for rid, (x, y) in self.receiver_positions:
            self._settings.set_receiver_position(rid, x, y)

        cal_p1 = self._settings.cal_point_1
        cal_p2 = self._settings.cal_point_2
        for rid in range(6):
            pos = self._settings.get_receiver_position(rid)
            if pos:
                rx, ry = pos
                d1 = math.sqrt((rx - cal_p1[0])**2 + (ry - cal_p1[1])**2)
                d2 = math.sqrt((rx - cal_p2[0])**2 + (ry - cal_p2[1])**2)
                self._settings.set_receiver_cal_distances(rid, [d1, d2])

        is_valid, errors = self._settings.verify_settings()
        if is_valid:
            self._settings.valid_settings = True
        self._verify_result_dialog(is_valid, errors if not is_valid else ["All settings verified successfully!"])

    def _verify_result_dialog(self, success: bool, messages: list) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Verification Result")
        dlg.setStyleSheet("background-color: black;")
        dlg.setFixedSize(500, 300)

        layout = QVBoxLayout(dlg)

        title_color = '#00FF00' if success else '#FF4444'
        title_text  = "Settings Verified!" if success else "Verification Failed"
        title = QLabel(title_text)
        title.setFont(QFont('Arial', 16, QFont.Weight.Bold))
        title.setStyleSheet(f"color:{title_color};")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        msg_area = QTextEdit("\n".join(messages))
        msg_area.setReadOnly(True)
        msg_area.setStyleSheet("background-color:#111111; color:white; font:11px Arial;")
        layout.addWidget(msg_area, stretch=1)

        ok_btn = QPushButton("OK")
        ok_btn.setStyleSheet("""
            QPushButton { background-color:#00FFFF; color:black; font:bold 12px Arial;
                          padding:8px 20px; }
        """)
        ok_btn.clicked.connect(dlg.accept)
        layout.addWidget(ok_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        dlg.exec()

    # ------------------------------------------------------------------
    # Panel lifecycle
    # ------------------------------------------------------------------

    def on_panel_show(self) -> None:
        # If settings are invalid, ask for dimensions first
        if not self._settings.valid_settings:
            width, height = self._ask_arena_dimensions()
            if width is None:
                self._main_window.show_panel('main')
                return

            new_positions = self._calculate_receiver_positions_from_dimensions(width, height)
            self.update_receiver_positions(new_positions)
            for rid, (x, y) in new_positions:
                self._settings.set_receiver_position(rid, x, y)

            cal_p1 = [0.0, height / 4.0]
            cal_p2 = [0.0, -height / 4.0]
            self._settings.cal_point_1 = cal_p1
            self._settings.cal_point_2 = cal_p2
            self.update_calibration_points(cal_p1, cal_p2)

            for rid, (rx, ry) in new_positions:
                d1 = math.sqrt((rx - cal_p1[0])**2 + (ry - cal_p1[1])**2)
                d2 = math.sqrt((rx - cal_p2[0])**2 + (ry - cal_p2[1])**2)
                self._settings.set_receiver_cal_distances(rid, [d1, d2])

            self._refresh_entries_from_state(new_positions, cal_p1, cal_p2)
        else:
            positions = self._settings.get_tower_coordinates()
            self.update_receiver_positions(positions)
            self.update_calibration_points(
                self._settings.cal_point_1, self._settings.cal_point_2)
            self._refresh_entries_from_state(
                positions,
                self._settings.cal_point_1,
                self._settings.cal_point_2)

        self._refresh_timer.start()

    def on_panel_hide(self) -> None:
        self._refresh_timer.stop()
