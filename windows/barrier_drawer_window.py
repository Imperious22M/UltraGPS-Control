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
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import QFont, QColor

from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.patches import Polygon as MplPolygon, Circle as MplCircle
import matplotlib.lines as mlines

from SettingsModule import SettingsModule
from ultragps_barrier import (
    BarrierData, BarrierType, TriggerMode, TriggerWhen,
    load_barriers, save_barriers, validate_barrier,
    check_point_in_barrier,
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

        # UI interaction state
        self._current_mode: str = 'normal'
        self._has_unsaved_changes: bool = False

        # Drag-edit state
        self._drag_state = None
        self._handle_artists: list = []
        self._handle_meta: list = []  # parallel: ('vertex', idx) | ('center',) | ('radius',)

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

        _combo_ss = ("QComboBox { background:#222222; color:white; font:11px Arial;"
                     " padding:2px 6px; }"
                     " QComboBox:disabled { background:#1a1a1a; color:#555555; }")
        _spin_ss  = ("QDoubleSpinBox { background:#222222; color:white; font:11px Arial;"
                     " padding:2px 6px; }"
                     " QDoubleSpinBox:disabled { background:#1a1a1a; color:#555555; }")
        _edit_ss  = ("QLineEdit { background:#222222; color:white; font:11px Arial;"
                     " padding:2px 6px; }"
                     " QLineEdit:disabled { background:#1a1a1a; color:#555555; }")

        r1.addWidget(self._styled_label("Type:"))
        self._type_combo = QComboBox()
        self._type_combo.addItems(["Polygon", "Circle", "Line"])
        self._type_combo.setStyleSheet(_combo_ss)
        r1.addWidget(self._type_combo)

        r1.addWidget(self._styled_label("Trigger:"))
        self._trigger_combo = QComboBox()
        self._trigger_combo.addItems(["Event", "Continuous"])
        self._trigger_combo.setStyleSheet(_combo_ss)
        r1.addWidget(self._trigger_combo)

        r1.addWidget(self._styled_label("When:"))
        self._when_combo = QComboBox()
        self._when_combo.addItems(["Inside", "Outside"])
        self._when_combo.setStyleSheet(_combo_ss)
        r1.addWidget(self._when_combo)

        r1.addWidget(self._styled_label("Color:"))
        self._color_combo = QComboBox()
        self._color_combo.addItems([
            "#FF0000", "#FF8800", "#FFFF00", "#00FF00", "#00FFFF", "#FF00FF"])
        self._color_combo.setStyleSheet(_combo_ss)
        r1.addWidget(self._color_combo)

        r1.addWidget(self._styled_label("Alpha:"))
        self._alpha_spin = QDoubleSpinBox()
        self._alpha_spin.setRange(0.0, 1.0)
        self._alpha_spin.setSingleStep(0.1)
        self._alpha_spin.setValue(0.3)
        self._alpha_spin.setStyleSheet(_spin_ss)
        r1.addWidget(self._alpha_spin)

        r1.addWidget(self._styled_label("Name:"))
        self._name_edit = QLineEdit()
        self._name_edit.setStyleSheet(_edit_ss)
        self._name_edit.setFixedWidth(120)
        r1.addWidget(self._name_edit)

        r1.addWidget(self._styled_label("Callback:"))
        self._callback_edit = QLineEdit()
        self._callback_edit.setStyleSheet(_edit_ss)
        self._callback_edit.setFixedWidth(120)
        r1.addWidget(self._callback_edit)

        # Controls that are editable when a barrier is selected (Type is always read-only)
        self._edit_controls = [
            self._trigger_combo, self._when_combo, self._color_combo,
            self._alpha_spin, self._name_edit, self._callback_edit,
        ]
        for w in self._edit_controls:
            w.setEnabled(False)
        self._type_combo.setEnabled(False)

        left_col.addWidget(row1)

        # --- Row 2: Action buttons ---
        row2 = QWidget()
        row2.setStyleSheet("background-color: black;")
        r2 = QHBoxLayout(row2)
        r2.setContentsMargins(10, 5, 10, 5)

        self._draw_btn = QPushButton("New Barrier")
        self._draw_btn.setStyleSheet("""
            QPushButton { background-color:#FF8800; color:black; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#CC6D00; }
            QPushButton:disabled { background-color:#5C3100; color:#666666; }
        """)
        self._draw_btn.clicked.connect(self._start_drawing)
        r2.addWidget(self._draw_btn)

        self._finish_btn = QPushButton("Finish")
        self._finish_btn.setStyleSheet("""
            QPushButton { background-color:#39FF14; color:black; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#2BCC10; }
            QPushButton:disabled { background-color:#1A5508; color:#666666; }
        """)
        self._finish_btn.clicked.connect(self._finish_drawing)
        r2.addWidget(self._finish_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setStyleSheet("""
            QPushButton { background-color:#FF4444; color:white; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#CC3333; }
            QPushButton:disabled { background-color:#552222; color:#666666; }
        """)
        self._cancel_btn.clicked.connect(self._cancel_drawing)
        r2.addWidget(self._cancel_btn)

        r2.addSpacing(20)

        self._clear_changes_btn = QPushButton("Clear All Changes")
        self._clear_changes_btn.setStyleSheet("""
            QPushButton { background-color:#FF4444; color:white; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#CC3333; }
            QPushButton:disabled { background-color:#552222; color:#666666; }
        """)
        self._clear_changes_btn.clicked.connect(self._clear_all_changes)
        r2.addWidget(self._clear_changes_btn)

        r2.addSpacing(20)

        self._save_btn = QPushButton("Save All")
        self._save_btn.setStyleSheet("""
            QPushButton { background-color:#FFD700; color:black; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#FFC000; }
            QPushButton:disabled { background-color:#554700; color:#666666; }
        """)
        self._save_btn.clicked.connect(self._save_barriers)
        r2.addWidget(self._save_btn)

        self._clear_btn = QPushButton("Clear All")
        self._clear_btn.setStyleSheet("""
            QPushButton { background-color:#888888; color:white; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#666666; }
            QPushButton:disabled { background-color:#444444; color:#666666; }
        """)
        self._clear_btn.clicked.connect(self._clear_all)
        r2.addWidget(self._clear_btn)

        self._del_btn = QPushButton("Delete Barrier")
        self._del_btn.setStyleSheet("""
            QPushButton { background-color:#FF4444; color:white; font:bold 11px Arial;
                          padding:4px 12px; border-radius:4px; }
            QPushButton:hover { background-color:#CC3333; }
            QPushButton:disabled { background-color:#552222; color:#666666; }
        """)
        self._del_btn.clicked.connect(self._delete_selected)
        r2.addWidget(self._del_btn)

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
            QListWidget::item { background:#111111; color:white; }
            QListWidget::item:focus { outline:0; }
            QListWidget::item:selected { background:#FF8800; color:black; }
            QListWidget::item:selected:!active { background:#FF8800; color:black; }
            QListWidget::item:selected:focus { background:#FF8800; color:black; outline:0; }
        """)
        self._barrier_list.setFixedWidth(220)
        self._barrier_list.viewport().installEventFilter(self)
        right_col.addWidget(self._barrier_list, stretch=1)

        # Status label at bottom of list
        self._status_lbl = QLabel("Ready")
        self._status_lbl.setStyleSheet("color: #888888; font: 10px Arial;")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_col.addWidget(self._status_lbl)

        self._barrier_list.currentRowChanged.connect(self._on_barrier_selected)

        root.addLayout(right_col)
        self._update_button_states('normal')

        # Live-edit signals — fire whenever a barrier is selected
        for sig in (
            self._trigger_combo.currentTextChanged,
            self._when_combo.currentTextChanged,
            self._color_combo.currentTextChanged,
            self._name_edit.textChanged,
            self._callback_edit.textChanged,
        ):
            sig.connect(lambda _: self._live_update_barrier())
        self._alpha_spin.valueChanged.connect(lambda _: self._live_update_barrier())
        self._type_combo.currentTextChanged.connect(self._on_type_combo_changed)

    @staticmethod
    def _styled_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#AAAAAA; font:bold 10px Arial;")
        return lbl

    def _update_button_states(self, mode: str = 'normal') -> None:
        """Enable/disable buttons and controls based on the current interaction mode."""
        self._current_mode = mode
        drawing = mode == 'drawing'
        selected = self._barrier_list.currentRow() >= 0

        self._draw_btn.setEnabled(not drawing)
        self._finish_btn.setEnabled(drawing)
        self._cancel_btn.setEnabled(drawing)

        self._del_btn.setEnabled(not drawing and selected)
        self._save_btn.setEnabled(not drawing)
        self._clear_btn.setEnabled(not drawing)

        if drawing:
            # All fields editable when configuring a new barrier
            for w in self._edit_controls:
                w.setEnabled(True)
            self._type_combo.setEnabled(True)
        else:
            # For existing barriers: editable when selected, Type always read-only
            for w in self._edit_controls:
                w.setEnabled(selected)
            self._type_combo.setEnabled(False)

        self._update_unsaved_state()

    def _update_unsaved_state(self) -> None:
        """Sync Save button label and Clear All Changes button with unsaved state."""
        self._save_btn.setText(
            "Save All Changes" if self._has_unsaved_changes else "Save All")
        self._clear_changes_btn.setEnabled(
            self._current_mode != 'drawing' and self._has_unsaved_changes)

    def _on_barrier_selected(self, row: int) -> None:
        """Called when the barrier list selection changes."""
        self._drag_state = None
        if self._current_mode == 'drawing':
            return
        if 0 <= row < len(self._barriers):
            self._load_barrier_into_controls(self._barriers[row])
            self._draw_selected_handles(self._barriers[row])
        else:
            self._blank_edit_controls()
            self._clear_handles()
        self._update_button_states('normal')

    def _blank_edit_controls(self) -> None:
        """Clear all edit controls to an empty/default state."""
        for w in (self._type_combo, self._trigger_combo,
                  self._when_combo, self._color_combo):
            w.blockSignals(True)
            w.setCurrentIndex(-1)
            w.blockSignals(False)
        self._alpha_spin.blockSignals(True)
        self._alpha_spin.setValue(0.0)
        self._alpha_spin.blockSignals(False)
        self._name_edit.blockSignals(True)
        self._name_edit.clear()
        self._name_edit.blockSignals(False)
        self._callback_edit.blockSignals(True)
        self._callback_edit.clear()
        self._callback_edit.blockSignals(False)

    def _auto_save(self) -> None:
        """Persist barriers to disk without updating the status label."""
        save_barriers(self._get_barriers_path(), self._barriers)
        self._has_unsaved_changes = False
        self._update_unsaved_state()

    def _clear_all_changes(self) -> None:
        """Revert all in-memory barrier edits to the last saved file state."""
        saved_row = self._barrier_list.currentRow()
        path = self._get_barriers_path()
        self._barriers = load_barriers(path)
        for name in list(self._barrier_patches.keys()):
            self._remove_barrier_patches(name)
        self._draw_all_barriers()
        self._refresh_barrier_list()
        self._has_unsaved_changes = False
        # Restore selection so controls reload
        if 0 <= saved_row < len(self._barriers):
            self._barrier_list.setCurrentRow(saved_row)
        else:
            self._blank_edit_controls()
        self._update_button_states('normal')
        self._canvas.draw_idle()
        self._status_lbl.setText("All changes cleared — reverted to saved file")
        self._status_lbl.setStyleSheet("color: #FF4444; font: 10px Arial;")

    def eventFilter(self, obj, event) -> bool:
        """Deselect a list item when it is clicked while already selected."""
        if (obj is self._barrier_list.viewport()
                and event.type() in (QEvent.Type.MouseButtonPress,
                                     QEvent.Type.MouseButtonDblClick)
                and event.button() == Qt.MouseButton.LeftButton):
            item = self._barrier_list.itemAt(event.pos())
            if item is not None and self._barrier_list.currentItem() is item:
                self._deselect_barrier()
                return True  # consume event so the list cannot re-select on release
        return super().eventFilter(obj, event)

    def _deselect_barrier(self) -> None:
        self._drag_state = None
        self._clear_handles()
        self._barrier_list.clearSelection()
        self._barrier_list.setCurrentRow(-1)
        self._barrier_list.clearFocus()

    # ------------------------------------------------------------------
    # Handle rendering and drag editing
    # ------------------------------------------------------------------

    def _draw_selected_handles(self, barrier: BarrierData) -> None:
        """Render draggable vertex/control handles for the selected barrier."""
        self._clear_handles()
        kw = dict(markersize=8, markeredgecolor='black', markeredgewidth=1.5,
                  zorder=20, picker=False)

        if barrier.barrier_type == BarrierType.POLYGON and barrier.vertices:
            for i, (vx, vy) in enumerate(barrier.vertices):
                h, = self.ax.plot(vx, vy, 'o', color='white', **kw)
                self._handle_artists.append(h)
                self._handle_meta.append(('vertex', i))
            cx = sum(v[0] for v in barrier.vertices) / len(barrier.vertices)
            cy = sum(v[1] for v in barrier.vertices) / len(barrier.vertices)
            h, = self.ax.plot(cx, cy, 'D', color='#FF8800', **kw)
            self._handle_artists.append(h)
            self._handle_meta.append(('centroid',))

        elif barrier.barrier_type == BarrierType.LINE:
            for idx, pt in enumerate((barrier.point1, barrier.point2)):
                if pt is None:
                    continue
                h, = self.ax.plot(pt[0], pt[1], 'o', color='white', **kw)
                self._handle_artists.append(h)
                self._handle_meta.append(('vertex', idx))
            if barrier.point1 and barrier.point2:
                mx = (barrier.point1[0] + barrier.point2[0]) / 2
                my = (barrier.point1[1] + barrier.point2[1]) / 2
                h, = self.ax.plot(mx, my, 'D', color='#FF8800', **kw)
                self._handle_artists.append(h)
                self._handle_meta.append(('centroid',))

        elif barrier.barrier_type == BarrierType.CIRCLE and barrier.center:
            cx, cy = barrier.center
            h, = self.ax.plot(cx, cy, 'o', color='white', **kw)
            self._handle_artists.append(h)
            self._handle_meta.append(('center',))
            h, = self.ax.plot(cx, cy + (barrier.radius or 0), 's',
                               color='#FFFF00', **kw)
            self._handle_artists.append(h)
            self._handle_meta.append(('radius',))

        self._canvas.draw_idle()

    def _clear_handles(self) -> None:
        for h in self._handle_artists:
            try:
                h.remove()
            except (ValueError, AttributeError):
                pass
        self._handle_artists.clear()
        self._handle_meta.clear()

    def _hit_test_handles(self, event) -> 'int | None':
        """Return index of the handle under the cursor (12 px radius), or None."""
        if event.x is None or event.y is None:
            return None
        for i, h in enumerate(self._handle_artists):
            hx, hy = h.get_xdata()[0], h.get_ydata()[0]
            disp = self.ax.transData.transform((hx, hy))
            if math.sqrt((disp[0] - event.x) ** 2 + (disp[1] - event.y) ** 2) <= 12:
                return i
        return None

    def _hit_test_barrier_body(self, event, barrier: BarrierData) -> bool:
        """Return True if the click lands on the barrier body."""
        if event.xdata is None or event.ydata is None:
            return False
        if barrier.barrier_type == BarrierType.LINE and barrier.point1 and barrier.point2:
            px, py = event.x, event.y
            x1, y1 = self.ax.transData.transform(barrier.point1)
            x2, y2 = self.ax.transData.transform(barrier.point2)
            dx, dy = x2 - x1, y2 - y1
            seg_sq = dx * dx + dy * dy
            if seg_sq == 0:
                return False
            t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg_sq))
            dist = math.sqrt((px - x1 - t * dx) ** 2 + (py - y1 - t * dy) ** 2)
            return dist <= 12
        return check_point_in_barrier((event.xdata, event.ydata), barrier)

    def _update_handle_positions(self, barrier: BarrierData) -> None:
        """Reposition handle artists in-place after barrier geometry changes."""
        for i, meta in enumerate(self._handle_meta):
            if i >= len(self._handle_artists):
                break
            h = self._handle_artists[i]
            tag = meta[0]
            if tag == 'vertex':
                idx = meta[1]
                if barrier.barrier_type == BarrierType.POLYGON and barrier.vertices:
                    if idx < len(barrier.vertices):
                        vx, vy = barrier.vertices[idx]
                        h.set_xdata([vx])
                        h.set_ydata([vy])
                elif barrier.barrier_type == BarrierType.LINE:
                    pt = barrier.point1 if idx == 0 else barrier.point2
                    if pt:
                        h.set_xdata([pt[0]])
                        h.set_ydata([pt[1]])
            elif tag == 'centroid':
                if barrier.barrier_type == BarrierType.POLYGON and barrier.vertices:
                    cx = sum(v[0] for v in barrier.vertices) / len(barrier.vertices)
                    cy = sum(v[1] for v in barrier.vertices) / len(barrier.vertices)
                    h.set_xdata([cx]); h.set_ydata([cy])
                elif (barrier.barrier_type == BarrierType.LINE
                        and barrier.point1 and barrier.point2):
                    h.set_xdata([(barrier.point1[0] + barrier.point2[0]) / 2])
                    h.set_ydata([(barrier.point1[1] + barrier.point2[1]) / 2])
            elif tag == 'center' and barrier.center:
                h.set_xdata([barrier.center[0]])
                h.set_ydata([barrier.center[1]])
            elif tag == 'radius' and barrier.center and barrier.radius is not None:
                h.set_xdata([barrier.center[0]])
                h.set_ydata([barrier.center[1] + barrier.radius])

    def _move_barrier(self, barrier: BarrierData, dx: float, dy: float) -> None:
        """Translate the entire barrier by (dx, dy) in data coordinates."""
        if barrier.barrier_type == BarrierType.POLYGON and barrier.vertices:
            barrier.vertices = [(vx + dx, vy + dy) for vx, vy in barrier.vertices]
        elif barrier.barrier_type == BarrierType.CIRCLE and barrier.center:
            cx, cy = barrier.center
            barrier.center = (cx + dx, cy + dy)
        elif barrier.barrier_type == BarrierType.LINE:
            if barrier.point1:
                barrier.point1 = (barrier.point1[0] + dx, barrier.point1[1] + dy)
            if barrier.point2:
                barrier.point2 = (barrier.point2[0] + dx, barrier.point2[1] + dy)

    def _apply_handle_move(self, barrier: BarrierData, meta: tuple,
                            abs_x: float, abs_y: float,
                            dx: float, dy: float) -> None:
        """Apply one drag increment to a single handle."""
        tag = meta[0]
        if tag == 'vertex':
            idx = meta[1]
            if barrier.barrier_type == BarrierType.POLYGON and barrier.vertices:
                verts = list(barrier.vertices)
                if idx < len(verts):
                    vx, vy = verts[idx]
                    verts[idx] = (vx + dx, vy + dy)
                    barrier.vertices = verts
            elif barrier.barrier_type == BarrierType.LINE:
                if idx == 0 and barrier.point1:
                    barrier.point1 = (barrier.point1[0] + dx, barrier.point1[1] + dy)
                elif idx == 1 and barrier.point2:
                    barrier.point2 = (barrier.point2[0] + dx, barrier.point2[1] + dy)
        elif tag == 'centroid':
            self._move_barrier(barrier, dx, dy)
        elif tag == 'center' and barrier.center:
            cx, cy = barrier.center
            barrier.center = (cx + dx, cy + dy)
        elif tag == 'radius' and barrier.center:
            cx, cy = barrier.center
            barrier.radius = max(1.0, math.sqrt((abs_x - cx) ** 2 + (abs_y - cy) ** 2))

    def _apply_drag(self, event) -> None:
        """Apply the current drag delta to the selected barrier and refresh."""
        state = self._drag_state
        if state is None or event.xdata is None or event.ydata is None:
            return
        row = state['row']
        if row < 0 or row >= len(self._barriers):
            return
        barrier = self._barriers[row]
        dx = event.xdata - state['last_x']
        dy = event.ydata - state['last_y']
        state['last_x'] = event.xdata
        state['last_y'] = event.ydata

        if state['type'] == 'body':
            self._move_barrier(barrier, dx, dy)
        else:
            handle_idx = state['handle_idx']
            if handle_idx < len(self._handle_meta):
                self._apply_handle_move(
                    barrier, self._handle_meta[handle_idx],
                    event.xdata, event.ydata, dx, dy)

        self._remove_barrier_patches(barrier.name)
        self._draw_barrier(barrier)
        self._update_handle_positions(barrier)
        self._canvas.draw_idle()

    # ------------------------------------------------------------------
    # Drawing state machine
    # ------------------------------------------------------------------

    def _start_drawing(self) -> None:
        # Deselect any barrier so live-edit signals don't accidentally modify it
        self._barrier_list.clearSelection()
        self._barrier_list.setCurrentRow(-1)

        # Fill in defaults for any controls that are still blank
        for combo, idx in (
            (self._type_combo,    0),   # Polygon
            (self._trigger_combo, 0),   # Event
            (self._when_combo,    0),   # Inside
            (self._color_combo,   0),   # #FF0000
        ):
            if combo.currentIndex() < 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)
        if self._alpha_spin.value() == 0.0:
            self._alpha_spin.blockSignals(True)
            self._alpha_spin.setValue(0.3)
            self._alpha_spin.blockSignals(False)
        if not self._name_edit.text().strip():
            self._name_edit.blockSignals(True)
            self._name_edit.setText("barrier_1")
            self._name_edit.blockSignals(False)
        if not self._callback_edit.text().strip():
            self._callback_edit.blockSignals(True)
            self._callback_edit.setText("on_barrier")
            self._callback_edit.blockSignals(False)

        type_text = self._type_combo.currentText().lower()
        self._drawing_mode = type_text
        self._polygon_vertices = []
        self._circle_center = None
        self._line_point1 = None
        self._clear_preview()
        self._status_lbl.setText(f"Drawing {type_text} \u2014 click on canvas")
        self._status_lbl.setStyleSheet("color: #FF8800; font: 10px Arial;")
        self._update_button_states('drawing')

    def _on_type_combo_changed(self, text: str) -> None:
        if self._current_mode != 'drawing' or not text:
            return
        self._drawing_mode = text.lower()
        self._polygon_vertices = []
        self._circle_center = None
        self._line_point1 = None
        self._clear_preview()
        self._canvas.draw_idle()
        self._status_lbl.setText(f"Drawing {self._drawing_mode} — click on canvas")
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
        self._barrier_list.clearSelection()
        self._barrier_list.setCurrentRow(-1)
        self._status_lbl.setText("Drawing cancelled")
        self._status_lbl.setStyleSheet("color: #FF4444; font: 10px Arial;")
        self._update_button_states('normal')
        self._canvas.draw_idle()

    # ------------------------------------------------------------------
    # Canvas event handlers
    # ------------------------------------------------------------------

    def _on_canvas_click(self, event) -> None:
        if event.inaxes != self.ax or event.xdata is None:
            return

        x, y = event.xdata, event.ydata

        # ---- drawing mode ------------------------------------------------
        if self._drawing_mode is not None:
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
            return

        # ---- drag start (barrier selected) --------------------------------
        row = self._barrier_list.currentRow()
        if row < 0 or row >= len(self._barriers):
            return
        barrier = self._barriers[row]

        handle_idx = self._hit_test_handles(event)
        if handle_idx is not None:
            self._drag_state = {
                'type': 'handle', 'row': row, 'handle_idx': handle_idx,
                'last_x': x, 'last_y': y,
            }
            return

        if self._hit_test_barrier_body(event, barrier):
            self._drag_state = {
                'type': 'body', 'row': row,
                'last_x': x, 'last_y': y,
            }

    def _on_canvas_release(self, event) -> None:
        if self._drag_state is not None:
            if not self._has_unsaved_changes:
                self._has_unsaved_changes = True
                self._update_unsaved_state()
            self._drag_state = None

    def _on_canvas_motion(self, event) -> None:
        if event.inaxes != self.ax or event.xdata is None:
            return

        if self._drawing_mode is not None:
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
            return

        if self._drag_state is not None:
            self._apply_drag(event)

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
        self._auto_save()
        self._status_lbl.setText(f"Added and saved polygon '{barrier.name}'")
        self._status_lbl.setStyleSheet("color: #39FF14; font: 10px Arial;")
        self._update_button_states('normal')
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
        self._auto_save()
        self._status_lbl.setText(f"Added and saved circle '{barrier.name}'")
        self._status_lbl.setStyleSheet("color: #39FF14; font: 10px Arial;")
        self._update_button_states('normal')
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
        self._auto_save()
        self._status_lbl.setText(f"Added and saved line '{barrier.name}'")
        self._status_lbl.setStyleSheet("color: #39FF14; font: 10px Arial;")
        self._update_button_states('normal')
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
        reply = QMessageBox.question(
            self, "Delete Barrier",
            f"Delete barrier '{barrier.name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._remove_barrier_patches(barrier.name)
        self._barriers.pop(row)
        self._has_unsaved_changes = True
        self._refresh_barrier_list()
        self._canvas.draw_idle()
        self._status_lbl.setText(f"Deleted '{barrier.name}'")
        self._status_lbl.setStyleSheet("color: #FF4444; font: 10px Arial;")


    def _load_barrier_into_controls(self, barrier: BarrierData) -> None:
        _edit_widgets = (
            self._type_combo, self._trigger_combo, self._when_combo,
            self._color_combo, self._alpha_spin,
            self._name_edit, self._callback_edit,
        )
        for w in _edit_widgets:
            w.blockSignals(True)

        type_map = {BarrierType.POLYGON: "Polygon",
                    BarrierType.CIRCLE:  "Circle",
                    BarrierType.LINE:    "Line"}
        trigger_map = {TriggerMode.EVENT:       "Event",
                       TriggerMode.CONTINUOUS:  "Continuous"}
        when_map = {TriggerWhen.INSIDE:  "Inside",
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

        for w in _edit_widgets:
            w.blockSignals(False)

    def _live_update_barrier(self) -> None:
        """Apply current control values to the selected barrier and redraw."""
        row = self._barrier_list.currentRow()
        if row < 0 or row >= len(self._barriers):
            return

        barrier = self._barriers[row]
        old_name = barrier.name

        trigger_map = {"Event": TriggerMode.EVENT, "Continuous": TriggerMode.CONTINUOUS}
        when_map = {"Inside": TriggerWhen.INSIDE, "Outside": TriggerWhen.OUTSIDE}

        t = self._trigger_combo.currentText()
        barrier.trigger_mode  = trigger_map[t] if t in trigger_map else barrier.trigger_mode
        w = self._when_combo.currentText()
        barrier.trigger_when  = when_map[w] if w in when_map else barrier.trigger_when
        barrier.color         = self._color_combo.currentText() or barrier.color
        barrier.alpha         = self._alpha_spin.value()
        barrier.callback_name = self._callback_edit.text().strip() or "on_barrier"

        new_name = self._name_edit.text().strip() or old_name
        if new_name != old_name:
            patches = self._barrier_patches.pop(old_name, [])
            self._barrier_patches[new_name] = patches
        barrier.name = new_name

        # Update list item text directly to avoid clearing the selection
        item = self._barrier_list.item(row)
        if item:
            item.setText(
                f"{barrier.name} ({barrier.barrier_type.value},"
                f" {barrier.trigger_mode.value})")

        self._remove_barrier_patches(barrier.name)
        self._draw_barrier(barrier)
        self._canvas.draw_idle()

        if not self._has_unsaved_changes:
            self._has_unsaved_changes = True
            self._update_unsaved_state()


    # ------------------------------------------------------------------
    # Save / Load / Clear
    # ------------------------------------------------------------------

    def _get_barriers_path(self) -> str:
        config_dir = os.path.dirname(self._settings._config_path)
        return os.path.join(config_dir, 'barriers.toml')

    def _save_barriers(self) -> None:
        path = self._get_barriers_path()
        save_barriers(path, self._barriers)
        self._has_unsaved_changes = False
        self._update_unsaved_state()
        self._status_lbl.setText(f"Saved {len(self._barriers)} barriers")
        self._status_lbl.setStyleSheet("color: #FFD700; font: 10px Arial;")

    def _load_barriers(self) -> None:
        path = self._get_barriers_path()
        self._barriers = load_barriers(path)
        self._redraw_all_barriers()
        self._refresh_barrier_list()
        self._has_unsaved_changes = False
        self._update_unsaved_state()

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
        self._drag_state = None
        self._clear_preview()
        self._clear_handles()
