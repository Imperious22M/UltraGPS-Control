"""Barrier Drawer panel.

Interactive click-to-draw editor for creating, editing, and saving virtual
barriers on the arena canvas.  Supports polygon, circle, and line barriers
with configurable trigger modes, colours, and callbacks.
"""

import os
import math

import numpy as np

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QListWidget, QListWidgetItem, QComboBox, QDoubleSpinBox, QMessageBox,
    QFrame,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor

from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.patches import Polygon as MplPolygon, Circle as MplCircle
import matplotlib.lines as mlines

from SettingsModule import SettingsModule
from ultragps_barrier import (
    BarrierData, BarrierType, TriggerMode, TriggerWhen,
    load_barriers, save_barriers, validate_barrier,
)


class BarrierDrawerPanel(QWidget):
    """Full-page barrier drawing panel.

    Lets the user draw polygon, circle, and line barriers directly on the
    arena canvas, edit their properties, and persist them to barriers.toml.
    """

    def __init__(self, settings_module: SettingsModule, main_window) -> None:
        super().__init__()
        self._settings = settings_module
        self._main_window = main_window
        self.setStyleSheet("background-color: black;")

        self.grid_padding = 20

        # Compass rose handles
        self._compass_x_arrow = None
        self._compass_x_text = None
        self._compass_y_arrow = None
        self._compass_y_text = None

        # Receiver plot elements
        self.receiver_scatter = None
        self.receiver_labels: list = []
        self.connection_line = None

        # Barrier storage
        self._barriers: list[BarrierData] = []
        self._barrier_patches: dict[str, list] = {}

        # Drawing state machine
        self._drawing_mode = None  # None, "polygon", "circle", "line"
        self._polygon_vertices: list[tuple] = []
        self._circle_center = None
        self._line_point1 = None
        self._preview_artists: list = []

        # Canvas event connection IDs
        self._cid_press = None
        self._cid_release = None
        self._cid_motion = None

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
        self.ax.set_title('Barrier Drawer', color='white')
        self.ax.tick_params(colors='white')
        for spine in self.ax.spines.values():
            spine.set_color('white')

        self._draw_compass_rose()
        self._init_receiver_plot(receiver_positions)

    def _init_receiver_plot(self, receiver_positions: list) -> None:
        rx = [p[1][0] for p in receiver_positions]
        ry = [p[1][1] for p in receiver_positions]

        self.receiver_scatter = self.ax.scatter(
            rx, ry, c='#FF00FF', s=100, zorder=5, label='Receivers')

        self.receiver_labels = []
        for rid, (x, y) in receiver_positions:
            lbl = self.ax.text(
                x + 5, y + 5, f"{rid + 1}\n({x:.1f}, {y:.1f})",
                color='white', fontsize=9, fontweight='bold',
                zorder=6, ha='left', va='bottom')
            self.receiver_labels.append(lbl)

        conn = [0, 1, 2, 5, 4, 3, 0]
        self.connection_line, = self.ax.plot(
            [rx[i] for i in conn], [ry[i] for i in conn],
            color='#00FFFF', linewidth=2, alpha=0.7)

        self.ax.legend(loc='upper right', facecolor='#222222',
                       edgecolor='white', labelcolor='white')

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

    def _update_arena(self, receiver_positions: list) -> None:
        """Refresh receiver scatter and connection lines from new positions."""
        self.receiver_positions = receiver_positions
        rx = [p[1][0] for p in receiver_positions]
        ry = [p[1][1] for p in receiver_positions]

        self.ax.set_xlim(min(rx) - self.grid_padding, max(rx) + self.grid_padding)
        self.ax.set_ylim(min(ry) - self.grid_padding, max(ry) + self.grid_padding)
        self._draw_compass_rose()

        if self.receiver_scatter:
            self.receiver_scatter.set_offsets(np.column_stack([rx, ry]))

        for i, (rid, (x, y)) in enumerate(receiver_positions):
            txt = f"{rid + 1}\n({x:.1f}, {y:.1f})"
            if i < len(self.receiver_labels):
                self.receiver_labels[i].set_text(txt)
                self.receiver_labels[i].set_position((x + 5, y + 5))

        conn = [0, 1, 2, 5, 4, 3, 0]
        if self.connection_line:
            self.connection_line.set_data(
                [rx[i] for i in conn], [ry[i] for i in conn])

    # ------------------------------------------------------------------
    # Qt UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Left column: top bar + canvas + controls
        left_col = QVBoxLayout()
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(0)

        # --- Top bar ---
        top_bar = QWidget()
        top_bar.setStyleSheet("background-color: black;")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 5, 10, 5)

        title_lbl = QLabel("Barrier Drawer")
        title_lbl.setFont(QFont('Arial', 16, QFont.Weight.Bold))
        title_lbl.setStyleSheet("color: #FF8800;")
        top_layout.addWidget(title_lbl)
        top_layout.addStretch()

        back_btn = QPushButton("\u2190 Back")
        back_btn.setStyleSheet("""
            QPushButton { background-color:#39FF14; color:black; font:bold 12px Arial;
                          padding:5px 10px; border-radius:4px; }
            QPushButton:hover { background-color:#2BCC10; }
        """)
        back_btn.clicked.connect(lambda: self._main_window.show_panel('main'))
        top_layout.addWidget(back_btn)
        left_col.addWidget(top_bar)

        # --- Canvas ---
        self._canvas = FigureCanvasQTAgg(self.fig)
        left_col.addWidget(self._canvas, stretch=1)

        # --- Row 1: Drawing controls ---
        row1 = QWidget()
        row1.setStyleSheet("background-color: black;")
        r1 = QHBoxLayout(row1)
        r1.setContentsMargins(10, 5, 10, 5)

        r1.addWidget(self._styled_label("Type:"))
        self._type_combo = QComboBox()
        self._type_combo.addItems(["Polygon", "Circle", "Line"])
        self._type_combo.setStyleSheet(
            "QComboBox { background:#222222; color:white; font:11px Arial; padding:2px 6px; }")
        r1.addWidget(self._type_combo)

        r1.addWidget(self._styled_label("Trigger:"))
        self._trigger_combo = QComboBox()
        self._trigger_combo.addItems(["Event", "Continuous"])
        self._trigger_combo.setStyleSheet(
            "QComboBox { background:#222222; color:white; font:11px Arial; padding:2px 6px; }")
        r1.addWidget(self._trigger_combo)

        r1.addWidget(self._styled_label("When:"))
        self._when_combo = QComboBox()
        self._when_combo.addItems(["Inside", "Outside"])
        self._when_combo.setStyleSheet(
            "QComboBox { background:#222222; color:white; font:11px Arial; padding:2px 6px; }")
        r1.addWidget(self._when_combo)

        r1.addWidget(self._styled_label("Color:"))
        self._color_combo = QComboBox()
        self._color_combo.addItems([
            "#FF0000", "#FF8800", "#FFFF00", "#00FF00", "#00FFFF", "#FF00FF"])
        self._color_combo.setStyleSheet(
            "QComboBox { background:#222222; color:white; font:11px Arial; padding:2px 6px; }")
        r1.addWidget(self._color_combo)

        r1.addWidget(self._styled_label("Alpha:"))
        self._alpha_spin = QDoubleSpinBox()
        self._alpha_spin.setRange(0.0, 1.0)
        self._alpha_spin.setSingleStep(0.1)
        self._alpha_spin.setValue(0.3)
        self._alpha_spin.setStyleSheet(
            "QDoubleSpinBox { background:#222222; color:white; font:11px Arial; padding:2px 6px; }")
        r1.addWidget(self._alpha_spin)

        r1.addWidget(self._styled_label("Name:"))
        self._name_edit = QLineEdit("barrier_1")
        self._name_edit.setStyleSheet(
            "QLineEdit { background:#222222; color:white; font:11px Arial; padding:2px 6px; }")
        self._name_edit.setFixedWidth(120)
        r1.addWidget(self._name_edit)

        r1.addWidget(self._styled_label("Callback:"))
        self._callback_edit = QLineEdit("on_barrier")
        self._callback_edit.setStyleSheet(
            "QLineEdit { background:#222222; color:white; font:11px Arial; padding:2px 6px; }")
        self._callback_edit.setFixedWidth(120)
        r1.addWidget(self._callback_edit)

        left_col.addWidget(row1)

        # --- Row 2: Action buttons ---
        row2 = QWidget()
        row2.setStyleSheet("background-color: black;")
        r2 = QHBoxLayout(row2)
        r2.setContentsMargins(10, 5, 10, 5)

        draw_btn = QPushButton("Draw")
        draw_btn.setStyleSheet("""
            QPushButton { background-color:#FF8800; color:black; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#CC6D00; }
        """)
        draw_btn.clicked.connect(self._start_drawing)
        r2.addWidget(draw_btn)

        finish_btn = QPushButton("Finish")
        finish_btn.setStyleSheet("""
            QPushButton { background-color:#39FF14; color:black; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#2BCC10; }
        """)
        finish_btn.clicked.connect(self._finish_drawing)
        r2.addWidget(finish_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet("""
            QPushButton { background-color:#FF4444; color:white; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#CC3333; }
        """)
        cancel_btn.clicked.connect(self._cancel_drawing)
        r2.addWidget(cancel_btn)

        r2.addSpacing(20)

        edit_btn = QPushButton("Edit")
        edit_btn.setStyleSheet("""
            QPushButton { background-color:#00FFFF; color:black; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#00CCCC; }
        """)
        edit_btn.clicked.connect(self._edit_selected)
        r2.addWidget(edit_btn)

        del_btn = QPushButton("Delete")
        del_btn.setStyleSheet("""
            QPushButton { background-color:#FF4444; color:white; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#CC3333; }
        """)
        del_btn.clicked.connect(self._delete_selected)
        r2.addWidget(del_btn)

        r2.addSpacing(20)

        save_btn = QPushButton("Save All")
        save_btn.setStyleSheet("""
            QPushButton { background-color:#FFD700; color:black; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#FFC000; }
        """)
        save_btn.clicked.connect(self._save_barriers)
        r2.addWidget(save_btn)

        clear_btn = QPushButton("Clear All")
        clear_btn.setStyleSheet("""
            QPushButton { background-color:#888888; color:white; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#666666; }
        """)
        clear_btn.clicked.connect(self._clear_all)
        r2.addWidget(clear_btn)

        r2.addStretch()
        left_col.addWidget(row2)
        root.addLayout(left_col, stretch=1)

        # --- Right column: Barrier list ---
        right_col = QVBoxLayout()
        right_col.setContentsMargins(5, 5, 5, 5)

        list_title = QLabel("Barriers")
        list_title.setFont(QFont('Arial', 12, QFont.Weight.Bold))
        list_title.setStyleSheet("color: #FF8800;")
        list_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_col.addWidget(list_title)

        self._barrier_list = QListWidget()
        self._barrier_list.setStyleSheet("""
            QListWidget { background:#111111; color:white; font:11px Arial;
                          border:1px solid #FF8800; }
            QListWidget::item:selected { background:#FF8800; color:black; }
        """)
        self._barrier_list.setFixedWidth(220)
        right_col.addWidget(self._barrier_list, stretch=1)

        # Status label at bottom of list
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet("color: #888888; font: 10px Arial;")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_col.addWidget(self._status_lbl)

        root.addLayout(right_col)

    @staticmethod
    def _styled_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#AAAAAA; font:bold 10px Arial;")
        return lbl

    # ------------------------------------------------------------------
    # Drawing state machine
    # ------------------------------------------------------------------

    def _start_drawing(self) -> None:
        type_text = self._type_combo.currentText().lower()
        self._drawing_mode = type_text
        self._polygon_vertices = []
        self._circle_center = None
        self._line_point1 = None
        self._clear_preview()
        self._status_lbl.setText(f"Drawing {type_text} \u2014 click on canvas")
        self._status_lbl.setStyleSheet("color: #FF8800; font: 10px Arial;")

    def _finish_drawing(self) -> None:
        if self._drawing_mode == "polygon":
            self._finish_polygon()
        elif self._drawing_mode == "circle":
            if self._circle_center is not None:
                self._status_lbl.setText("Click canvas to set radius")
        elif self._drawing_mode == "line":
            if self._line_point1 is not None:
                self._status_lbl.setText("Click canvas to set endpoint")

    def _cancel_drawing(self) -> None:
        self._drawing_mode = None
        self._polygon_vertices = []
        self._circle_center = None
        self._line_point1 = None
        self._clear_preview()
        self._status_lbl.setText("Drawing cancelled")
        self._status_lbl.setStyleSheet("color: #FF4444; font: 10px Arial;")
        self._canvas.draw_idle()

    # ------------------------------------------------------------------
    # Canvas event handlers
    # ------------------------------------------------------------------

    def _on_canvas_click(self, event) -> None:
        if event.inaxes != self.ax or event.xdata is None:
            return
        if self._drawing_mode is None:
            return

        x, y = event.xdata, event.ydata

        if self._drawing_mode == "polygon":
            if event.dblclick:
                self._finish_polygon()
                return
            self._polygon_vertices.append((x, y))
            self._update_polygon_preview()
            self._status_lbl.setText(
                f"Polygon: {len(self._polygon_vertices)} vertices (dbl-click to close)")

        elif self._drawing_mode == "circle":
            if self._circle_center is None:
                self._circle_center = (x, y)
                dot = self.ax.plot(x, y, 'o', color=self._current_color(),
                                   markersize=6, zorder=10)
                self._preview_artists.extend(dot)
                self._canvas.draw_idle()
                self._status_lbl.setText("Circle: click to set radius")
            else:
                self._finish_circle(x, y)

        elif self._drawing_mode == "line":
            if self._line_point1 is None:
                self._line_point1 = (x, y)
                dot = self.ax.plot(x, y, 'o', color=self._current_color(),
                                   markersize=6, zorder=10)
                self._preview_artists.extend(dot)
                self._canvas.draw_idle()
                self._status_lbl.setText("Line: click to set second endpoint")
            else:
                self._finish_line(x, y)

    def _on_canvas_release(self, event) -> None:
        pass  # reserved for future drag interactions

    def _on_canvas_motion(self, event) -> None:
        if event.inaxes != self.ax or event.xdata is None:
            return
        if self._drawing_mode is None:
            return

        x, y = event.xdata, event.ydata

        self._clear_motion_preview()

        if self._drawing_mode == "polygon" and self._polygon_vertices:
            lx, ly = self._polygon_vertices[-1]
            ln, = self.ax.plot([lx, x], [ly, y], '--',
                               color=self._current_color(), linewidth=1,
                               alpha=0.6, zorder=9)
            ln._motion_preview = True
            self._preview_artists.append(ln)

        elif self._drawing_mode == "circle" and self._circle_center is not None:
            cx, cy = self._circle_center
            r = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
            circ = MplCircle((cx, cy), r, fill=False,
                             edgecolor=self._current_color(), linewidth=1,
                             linestyle='--', alpha=0.6, zorder=9)
            circ._motion_preview = True
            self.ax.add_patch(circ)
            self._preview_artists.append(circ)

        elif self._drawing_mode == "line" and self._line_point1 is not None:
            lx, ly = self._line_point1
            ln, = self.ax.plot([lx, x], [ly, y], '--',
                               color=self._current_color(), linewidth=1,
                               alpha=0.6, zorder=9)
            ln._motion_preview = True
            self._preview_artists.append(ln)

        self._canvas.draw_idle()

    # ------------------------------------------------------------------
    # Drawing finishers
    # ------------------------------------------------------------------

    def _finish_polygon(self) -> None:
        if len(self._polygon_vertices) < 3:
            self._status_lbl.setText("Polygon needs at least 3 vertices")
            self._status_lbl.setStyleSheet("color: #FF4444; font: 10px Arial;")
            return

        barrier = self._make_barrier_from_controls(BarrierType.POLYGON)
        barrier.vertices = list(self._polygon_vertices)

        ok, msg = validate_barrier(barrier)
        if not ok:
            self._status_lbl.setText(f"Invalid: {msg}")
            self._status_lbl.setStyleSheet("color: #FF4444; font: 10px Arial;")
            return

        self._barriers.append(barrier)
        self._clear_preview()
        self._drawing_mode = None
        self._polygon_vertices = []
        self._draw_barrier(barrier)
        self._refresh_barrier_list()
        self._status_lbl.setText(f"Added polygon '{barrier.name}'")
        self._status_lbl.setStyleSheet("color: #39FF14; font: 10px Arial;")
        self._canvas.draw_idle()

    def _finish_circle(self, x: float, y: float) -> None:
        cx, cy = self._circle_center
        radius = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        if radius <= 0:
            self._status_lbl.setText("Circle radius must be > 0")
            self._status_lbl.setStyleSheet("color: #FF4444; font: 10px Arial;")
            return

        barrier = self._make_barrier_from_controls(BarrierType.CIRCLE)
        barrier.center = (cx, cy)
        barrier.radius = radius

        ok, msg = validate_barrier(barrier)
        if not ok:
            self._status_lbl.setText(f"Invalid: {msg}")
            self._status_lbl.setStyleSheet("color: #FF4444; font: 10px Arial;")
            return

        self._barriers.append(barrier)
        self._clear_preview()
        self._drawing_mode = None
        self._circle_center = None
        self._draw_barrier(barrier)
        self._refresh_barrier_list()
        self._status_lbl.setText(f"Added circle '{barrier.name}'")
        self._status_lbl.setStyleSheet("color: #39FF14; font: 10px Arial;")
        self._canvas.draw_idle()

    def _finish_line(self, x: float, y: float) -> None:
        p1 = self._line_point1
        p2 = (x, y)
        if p1 == p2:
            self._status_lbl.setText("Line endpoints must differ")
            self._status_lbl.setStyleSheet("color: #FF4444; font: 10px Arial;")
            return

        barrier = self._make_barrier_from_controls(BarrierType.LINE)
        barrier.point1 = p1
        barrier.point2 = p2
        barrier.thickness = 5.0  # default thickness in cm

        ok, msg = validate_barrier(barrier)
        if not ok:
            self._status_lbl.setText(f"Invalid: {msg}")
            self._status_lbl.setStyleSheet("color: #FF4444; font: 10px Arial;")
            return

        self._barriers.append(barrier)
        self._clear_preview()
        self._drawing_mode = None
        self._line_point1 = None
        self._draw_barrier(barrier)
        self._refresh_barrier_list()
        self._status_lbl.setText(f"Added line '{barrier.name}'")
        self._status_lbl.setStyleSheet("color: #39FF14; font: 10px Arial;")
        self._canvas.draw_idle()

    def _make_barrier_from_controls(self, btype: BarrierType) -> BarrierData:
        trigger_map = {"Event": TriggerMode.EVENT, "Continuous": TriggerMode.CONTINUOUS}
        when_map = {"Inside": TriggerWhen.INSIDE, "Outside": TriggerWhen.OUTSIDE}

        name = self._name_edit.text().strip() or "unnamed"
        # Ensure unique name
        existing = {b.name for b in self._barriers}
        base_name = name
        counter = 2
        while name in existing:
            name = f"{base_name}_{counter}"
            counter += 1

        return BarrierData(
            name=name,
            barrier_type=btype,
            trigger_mode=trigger_map[self._trigger_combo.currentText()],
            trigger_when=when_map[self._when_combo.currentText()],
            callback_name=self._callback_edit.text().strip() or "on_barrier",
            color=self._color_combo.currentText(),
            alpha=self._alpha_spin.value(),
        )

    # ------------------------------------------------------------------
    # Preview helpers
    # ------------------------------------------------------------------

    def _clear_preview(self) -> None:
        for artist in self._preview_artists:
            try:
                artist.remove()
            except (ValueError, AttributeError):
                pass
        self._preview_artists.clear()

    def _clear_motion_preview(self) -> None:
        kept = []
        for artist in self._preview_artists:
            if getattr(artist, '_motion_preview', False):
                try:
                    artist.remove()
                except (ValueError, AttributeError):
                    pass
            else:
                kept.append(artist)
        self._preview_artists = kept

    def _update_polygon_preview(self) -> None:
        self._clear_preview()
        color = self._current_color()
        for vx, vy in self._polygon_vertices:
            dot = self.ax.plot(vx, vy, 'o', color=color, markersize=5, zorder=10)
            self._preview_artists.extend(dot)
        for i in range(1, len(self._polygon_vertices)):
            x1, y1 = self._polygon_vertices[i - 1]
            x2, y2 = self._polygon_vertices[i]
            ln, = self.ax.plot([x1, x2], [y1, y2], '-', color=color,
                               linewidth=1.5, alpha=0.8, zorder=9)
            self._preview_artists.append(ln)
        self._canvas.draw_idle()

    def _current_color(self) -> str:
        return self._color_combo.currentText()

    # ------------------------------------------------------------------
    # Barrier rendering
    # ------------------------------------------------------------------

    def _draw_all_barriers(self) -> None:
        for barrier in self._barriers:
            self._draw_barrier(barrier)

    def _draw_barrier(self, barrier: BarrierData) -> None:
        artists = []
        color = barrier.color
        alpha = barrier.alpha

        if barrier.barrier_type == BarrierType.POLYGON and barrier.vertices:
            verts = [(v[0], v[1]) for v in barrier.vertices]
            patch = MplPolygon(verts, closed=True, facecolor=color,
                               alpha=alpha, edgecolor=color, linewidth=2, zorder=8)
            self.ax.add_patch(patch)
            artists.append(patch)
            cx = sum(v[0] for v in verts) / len(verts)
            cy = sum(v[1] for v in verts) / len(verts)
            lbl = self.ax.text(cx, cy, barrier.name, color='white', fontsize=8,
                               fontweight='bold', ha='center', va='center', zorder=9,
                               bbox=dict(boxstyle='round,pad=0.2', facecolor='black',
                                         alpha=0.7, edgecolor=color))
            artists.append(lbl)

        elif barrier.barrier_type == BarrierType.CIRCLE and barrier.center:
            patch = MplCircle(barrier.center, barrier.radius, facecolor=color,
                              alpha=alpha, edgecolor=color, linewidth=2, zorder=8)
            self.ax.add_patch(patch)
            artists.append(patch)
            lbl = self.ax.text(barrier.center[0], barrier.center[1], barrier.name,
                               color='white', fontsize=8, fontweight='bold',
                               ha='center', va='center', zorder=9,
                               bbox=dict(boxstyle='round,pad=0.2', facecolor='black',
                                         alpha=0.7, edgecolor=color))
            artists.append(lbl)

        elif barrier.barrier_type == BarrierType.LINE and barrier.point1 and barrier.point2:
            lw = max(2, (barrier.thickness or 5.0) * 0.5)
            ln, = self.ax.plot(
                [barrier.point1[0], barrier.point2[0]],
                [barrier.point1[1], barrier.point2[1]],
                color=color, linewidth=lw, alpha=max(alpha, 0.6), zorder=8)
            artists.append(ln)
            mx = (barrier.point1[0] + barrier.point2[0]) / 2
            my = (barrier.point1[1] + barrier.point2[1]) / 2
            lbl = self.ax.text(mx, my, barrier.name, color='white', fontsize=8,
                               fontweight='bold', ha='center', va='center', zorder=9,
                               bbox=dict(boxstyle='round,pad=0.2', facecolor='black',
                                         alpha=0.7, edgecolor=color))
            artists.append(lbl)

        self._barrier_patches[barrier.name] = artists

    def _remove_barrier_patches(self, name: str) -> None:
        for artist in self._barrier_patches.pop(name, []):
            try:
                artist.remove()
            except (ValueError, AttributeError):
                pass

    def _redraw_all_barriers(self) -> None:
        for name in list(self._barrier_patches.keys()):
            self._remove_barrier_patches(name)
        self._draw_all_barriers()
        self._canvas.draw_idle()

    # ------------------------------------------------------------------
    # Barrier list widget
    # ------------------------------------------------------------------

    def _refresh_barrier_list(self) -> None:
        self._barrier_list.clear()
        for barrier in self._barriers:
            text = f"{barrier.name} ({barrier.barrier_type.value}, {barrier.trigger_mode.value})"
            item = QListWidgetItem(text)
            self._barrier_list.addItem(item)

    def _delete_selected(self) -> None:
        row = self._barrier_list.currentRow()
        if row < 0 or row >= len(self._barriers):
            return
        barrier = self._barriers[row]
        self._remove_barrier_patches(barrier.name)
        self._barriers.pop(row)
        self._refresh_barrier_list()
        self._canvas.draw_idle()
        self._status_lbl.setText(f"Deleted '{barrier.name}'")
        self._status_lbl.setStyleSheet("color: #FF4444; font: 10px Arial;")

    def _edit_selected(self) -> None:
        row = self._barrier_list.currentRow()
        if row < 0 or row >= len(self._barriers):
            return
        barrier = self._barriers[row]

        # Load barrier fields into controls
        type_map = {BarrierType.POLYGON: "Polygon",
                    BarrierType.CIRCLE: "Circle",
                    BarrierType.LINE: "Line"}
        trigger_map = {TriggerMode.EVENT: "Event",
                       TriggerMode.CONTINUOUS: "Continuous"}
        when_map = {TriggerWhen.INSIDE: "Inside",
                    TriggerWhen.OUTSIDE: "Outside"}

        self._type_combo.setCurrentText(type_map.get(barrier.barrier_type, "Polygon"))
        self._trigger_combo.setCurrentText(trigger_map.get(barrier.trigger_mode, "Event"))
        self._when_combo.setCurrentText(when_map.get(barrier.trigger_when, "Inside"))

        idx = self._color_combo.findText(barrier.color)
        if idx >= 0:
            self._color_combo.setCurrentIndex(idx)

        self._alpha_spin.setValue(barrier.alpha)
        self._name_edit.setText(barrier.name)
        self._callback_edit.setText(barrier.callback_name)

        self._remove_barrier_patches(barrier.name)
        self._barriers.pop(row)
        self._refresh_barrier_list()
        self._canvas.draw_idle()
        self._status_lbl.setText(f"Editing '{barrier.name}' \u2014 click Draw to re-draw")
        self._status_lbl.setStyleSheet("color: #00FFFF; font: 10px Arial;")

    # ------------------------------------------------------------------
    # Save / Load / Clear
    # ------------------------------------------------------------------

    def _get_barriers_path(self) -> str:
        config_dir = os.path.dirname(self._settings._config_path)
        return os.path.join(config_dir, 'barriers.toml')

    def _save_barriers(self) -> None:
        path = self._get_barriers_path()
        save_barriers(path, self._barriers)
        self._status_lbl.setText(f"Saved {len(self._barriers)} barriers")
        self._status_lbl.setStyleSheet("color: #FFD700; font: 10px Arial;")

    def _load_barriers(self) -> None:
        path = self._get_barriers_path()
        self._barriers = load_barriers(path)
        self._redraw_all_barriers()
        self._refresh_barrier_list()

    def _clear_all(self) -> None:
        if not self._barriers:
            return
        reply = QMessageBox.question(
            self, "Clear All Barriers",
            f"Remove all {len(self._barriers)} barriers?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for name in list(self._barrier_patches.keys()):
            self._remove_barrier_patches(name)
        self._barriers.clear()
        self._refresh_barrier_list()
        self._canvas.draw_idle()
        self._status_lbl.setText("All barriers cleared")
        self._status_lbl.setStyleSheet("color: #888888; font: 10px Arial;")

    # ------------------------------------------------------------------
    # Panel lifecycle
    # ------------------------------------------------------------------

    def on_panel_show(self) -> None:
        receiver_positions = self._settings.get_tower_coordinates()
        self._update_arena(receiver_positions)

        self._load_barriers()

        self._cid_press = self._canvas.mpl_connect(
            'button_press_event', self._on_canvas_click)
        self._cid_release = self._canvas.mpl_connect(
            'button_release_event', self._on_canvas_release)
        self._cid_motion = self._canvas.mpl_connect(
            'motion_notify_event', self._on_canvas_motion)

        self._refresh_timer.start()

    def on_panel_hide(self) -> None:
        self._refresh_timer.stop()
        for cid_attr in ('_cid_press', '_cid_release', '_cid_motion'):
            cid = getattr(self, cid_attr, None)
            if cid is not None:
                self._canvas.mpl_disconnect(cid)
                setattr(self, cid_attr, None)
        self._drawing_mode = None
        self._clear_preview()
