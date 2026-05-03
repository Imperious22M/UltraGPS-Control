"""Barrier Drawer panel.

Interactive click-to-draw editor for creating, editing, and saving virtual
barriers on the arena canvas.  Supports polygon, circle, and line barriers
with configurable trigger modes, colours, and callbacks.
"""

import os
import math
import shutil

import numpy as np

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QListWidget, QListWidgetItem, QComboBox, QDoubleSpinBox, QMessageBox,
    QFrame, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QRadioButton,
    QButtonGroup,
)
from PyQt6.QtCore import Qt, QTimer, QEvent
from PyQt6.QtGui import QFont, QColor

from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.patches import Polygon as MplPolygon, Circle as MplCircle
from matplotlib.transforms import Affine2D
import matplotlib.image as mpl_image
import matplotlib.lines as mlines

from SettingsModule import SettingsModule
from ultragps_barrier import (
    BarrierData, BarrierType, TriggerMode, TriggerWhen,
    ImageOverlay,
    load_barriers, save_barriers, validate_barrier,
    check_point_in_barrier,
    load_images, save_all, polygon_from_image,
)


class _CustomColorDialog(QDialog):
    """Small popup that lets the user enter a custom hex colour."""

    import re as _re  # class-level so accept() can use it without a module import

    def __init__(self, parent=None, initial_hex: str = "#FF0000") -> None:
        super().__init__(parent)
        self.setWindowTitle("Custom Colour")
        self.setStyleSheet("background-color: #1a1a1a; color: white;")
        self.setMinimumWidth(280)

        layout = QVBoxLayout(self)

        lbl = QLabel("Enter hex colour (#RRGGBB):")
        lbl.setStyleSheet("color: white; font: 11px Arial;")
        layout.addWidget(lbl)

        self._hex_edit = QLineEdit(initial_hex)
        self._hex_edit.setStyleSheet(
            "background:#222; color:white; font:12px Arial; padding:4px;")
        layout.addWidget(self._hex_edit)

        # Error label — hidden until validation fails
        self._error_lbl = QLabel("")
        self._error_lbl.setStyleSheet("color: #FF4444; font: italic 10px Arial;")
        self._error_lbl.setVisible(False)
        layout.addWidget(self._error_lbl)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.setStyleSheet("color: white; font: 11px Arial;")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        """Validate before closing — show an inline error and stay open if invalid."""
        import re
        hex_val = self.hex_value()
        if re.match(r'^#[0-9A-Fa-f]{6}$', hex_val):
            self._error_lbl.setVisible(False)
            super().accept()
        else:
            self._error_lbl.setText(
                f"Invalid colour '{hex_val}' — must be exactly 6 hex digits, e.g. #FF8800")
            self._error_lbl.setVisible(True)
            self.adjustSize()

    def hex_value(self) -> str:
        """Return the sanitised hex colour entered by the user (truncated to #RRGGBB)."""
        raw = self._hex_edit.text().strip()
        if not raw.startswith('#'):
            raw = '#' + raw
        return raw[:7]


class _ImportImageDialog(QDialog):
    """Ask the user for the real-world width and height of an imported image."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import Image")
        self.setStyleSheet("background-color: #1a1a1a; color: white;")

        form = QFormLayout()
        lbl_style = "color: white; font: 11px Arial;"

        self._width_spin = QDoubleSpinBox()
        self._width_spin.setRange(1.0, 10000.0)
        self._width_spin.setValue(100.0)
        self._width_spin.setSuffix(" cm")
        self._width_spin.setStyleSheet("background:#222; color:white; font:11px Arial;")

        self._height_spin = QDoubleSpinBox()
        self._height_spin.setRange(1.0, 10000.0)
        self._height_spin.setValue(100.0)
        self._height_spin.setSuffix(" cm")
        self._height_spin.setStyleSheet("background:#222; color:white; font:11px Arial;")

        w_lbl = QLabel("Width (cm):")
        w_lbl.setStyleSheet(lbl_style)
        h_lbl = QLabel("Height (cm):")
        h_lbl.setStyleSheet(lbl_style)

        form.addRow(w_lbl, self._width_spin)
        form.addRow(h_lbl, self._height_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.setStyleSheet("color: white; font: 11px Arial;")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> tuple[float, float]:
        return self._width_spin.value(), self._height_spin.value()


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
        self._prev_color_idx: int = 0  # for reverting if Custom dialog is cancelled

        # Drag-edit state
        self._drag_state = None
        self._handle_artists: list = []
        self._handle_meta: list = []  # parallel: ('vertex', idx) | ('center',) | ('radius',)

        # Ruler tool state
        self._ruler_mode: bool = False
        self._ruler_p1: tuple | None = None    # pending first point (before ruler drawn)
        self._ruler_p1_dot = None              # dot artist for pending first point
        self._ruler_endpoints: list = []       # [(x1,y1),(x2,y2)] for complete ruler
        self._ruler_artists: list = []         # [line, dot1, dot2, ann] for complete ruler
        self._cid_key = None

        # Image overlay state
        self._images: list[ImageOverlay] = []
        self._image_artists: dict[str, object] = {}   # name -> AxesImage
        self._selected_image_idx: int = -1
        self._img_handle_artists: list = []
        self._img_handle_meta: list = []  # ('img_center',)|('img_corner',i)|('img_rotate',)

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
        # Lock limits so imshow / add_patch never auto-rescale the arena
        self.ax.set_autoscale_on(False)

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

        # --- Tools sidebar (far left) ---
        tools_widget = QWidget()
        tools_widget.setFixedWidth(170)
        tools_widget.setStyleSheet("background-color: black;")
        tools_layout = QVBoxLayout(tools_widget)
        tools_layout.setContentsMargins(6, 8, 6, 8)
        tools_layout.setSpacing(8)

        tools_title = QLabel("Tools")
        tools_title.setFont(QFont('Arial', 12, QFont.Weight.Bold))
        tools_title.setStyleSheet("color: #FF8800;")
        tools_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tools_layout.addWidget(tools_title)

        self._tools_status_lbl = QLabel("Ready")
        self._tools_status_lbl.setStyleSheet("color: white; font: bold 13px Arial;")
        self._tools_status_lbl.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        self._tools_status_lbl.setWordWrap(True)
        self._tools_status_lbl.setMinimumHeight(75)
        tools_layout.addWidget(self._tools_status_lbl)

        self._ruler_btn = QPushButton("Virtual Ruler")
        self._ruler_btn.setStyleSheet("""
            QPushButton { background-color:#4444CC; color:white; font:bold 13px Arial;
                          padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background-color:#3333AA; }
            QPushButton:disabled { background-color:#222244; color:#666666; }
        """)
        self._ruler_btn.clicked.connect(self._toggle_ruler)
        tools_layout.addWidget(self._ruler_btn)

        # --- Settings sub-section (inside Tools sidebar) ---
        tools_sep = QFrame()
        tools_sep.setFrameShape(QFrame.Shape.HLine)
        tools_sep.setStyleSheet("color: #333333; margin-top: 4px; margin-bottom: 2px;")
        tools_layout.addWidget(tools_sep)

        settings_title = QLabel("Settings")
        settings_title.setFont(QFont('Arial', 10, QFont.Weight.Bold))
        settings_title.setStyleSheet("color: #FF8800;")
        settings_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tools_layout.addWidget(settings_title)

        _rb_ss = ("QRadioButton { color: #CCCCCC; font: 11px Arial; spacing: 6px; }"
                  " QRadioButton::indicator { width: 13px; height: 13px; }"
                  " QRadioButton::indicator:checked { background-color: #FF8800;"
                  "   border: 2px solid #FF8800; border-radius: 7px; }"
                  " QRadioButton::indicator:unchecked { background-color: #222222;"
                  "   border: 2px solid #666666; border-radius: 7px; }")

        self._show_all_names_rb = QRadioButton("Show all names")
        self._show_all_names_rb.setStyleSheet(_rb_ss)
        self._show_all_names_rb.setChecked(True)
        self._show_all_names_rb.setAutoExclusive(False)
        self._show_all_names_rb.clicked.connect(self._on_show_all_names_clicked)
        tools_layout.addWidget(self._show_all_names_rb)

        self._no_names_rb = QRadioButton("No barrier names")
        self._no_names_rb.setStyleSheet(_rb_ss)
        self._no_names_rb.setChecked(False)
        self._no_names_rb.setAutoExclusive(False)
        self._no_names_rb.clicked.connect(self._on_no_names_clicked)
        tools_layout.addWidget(self._no_names_rb)

        tools_layout.addStretch()
        root.addWidget(tools_widget)

        # Thin vertical separator
        vsep = QFrame()
        vsep.setFrameShape(QFrame.Shape.VLine)
        vsep.setStyleSheet("color: #333333;")
        root.addWidget(vsep)

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
            QPushButton { background-color:#39FF14; color:black; font:bold 14px Arial;
                          padding:6px 12px; border-radius:4px; }
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
        r1.setSpacing(4)

        _combo_ss = ("QComboBox { background:#222222; color:white; font:11px Arial;"
                     " padding:2px 6px; }"
                     " QComboBox:disabled { background:#1a1a1a; color:#555555; }")
        _spin_ss  = ("QDoubleSpinBox { background:#222222; color:white; font:11px Arial;"
                     " padding:2px 6px; }"
                     " QDoubleSpinBox:disabled { background:#1a1a1a; color:#555555; }")
        _edit_ss  = ("QLineEdit { background:#222222; color:white; font:11px Arial;"
                     " padding:2px 6px; }"
                     " QLineEdit:disabled { background:#1a1a1a; color:#555555; }")

        r1.addStretch(1)

        self._type_combo = QComboBox()
        self._type_combo.addItems(["Polygon", "Circle", "Line"])
        self._type_combo.setStyleSheet(_combo_ss)
        r1.addWidget(self._styled_label("Type:"))
        r1.addWidget(self._type_combo)
        r1.addSpacing(10)

        self._trigger_combo = QComboBox()
        self._trigger_combo.addItems(["Event", "Continuous"])
        self._trigger_combo.setStyleSheet(_combo_ss)
        r1.addWidget(self._styled_label("Trigger:"))
        r1.addWidget(self._trigger_combo)
        r1.addSpacing(10)

        self._when_combo = QComboBox()
        self._when_combo.addItems(["Inside", "Outside"])
        self._when_combo.setStyleSheet(_combo_ss)
        r1.addWidget(self._styled_label("When:"))
        r1.addWidget(self._when_combo)
        r1.addSpacing(10)

        self._color_combo = QComboBox()
        for _name, _hex in [
            ("Red",     "#FF0000"),
            ("Orange",  "#FF8800"),
            ("Yellow",  "#FFFF00"),
            ("Green",   "#00FF00"),
            ("Cyan",    "#00FFFF"),
            ("Magenta", "#FF00FF"),
            ("Custom",  None),
        ]:
            self._color_combo.addItem(_name, _hex)
        self._color_combo.setStyleSheet(_combo_ss)
        r1.addWidget(self._styled_label("Color:"))
        r1.addWidget(self._color_combo)
        r1.addSpacing(10)

        self._alpha_spin = QDoubleSpinBox()
        self._alpha_spin.setRange(0.0, 1.0)
        self._alpha_spin.setSingleStep(0.1)
        self._alpha_spin.setValue(0.3)
        self._alpha_spin.setStyleSheet(_spin_ss)
        r1.addWidget(self._styled_label("Alpha:"))
        r1.addWidget(self._alpha_spin)
        r1.addSpacing(10)

        self._name_edit = QLineEdit()
        self._name_edit.setStyleSheet(_edit_ss)
        self._name_edit.setFixedWidth(120)
        r1.addWidget(self._styled_label("Name:"))
        r1.addWidget(self._name_edit)
        r1.addSpacing(10)

        self._callback_edit = QLineEdit()
        self._callback_edit.setStyleSheet(_edit_ss)
        self._callback_edit.setFixedWidth(120)
        r1.addWidget(self._styled_label("Callback:"))
        r1.addWidget(self._callback_edit)

        r1.addStretch(1)

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
        r2.addStretch(1)

        self._draw_btn = QPushButton("New Barrier")
        self._draw_btn.setStyleSheet("""
            QPushButton { background-color:#FF8800; color:black; font:bold 13px Arial;
                          padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background-color:#CC6D00; }
            QPushButton:disabled { background-color:#5C3100; color:#666666; }
        """)
        self._draw_btn.clicked.connect(self._start_drawing)
        r2.addWidget(self._draw_btn)

        self._finish_btn = QPushButton("Finish")
        self._finish_btn.setStyleSheet("""
            QPushButton { background-color:#39FF14; color:black; font:bold 13px Arial;
                          padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background-color:#2BCC10; }
            QPushButton:disabled { background-color:#1A5508; color:#666666; }
        """)
        self._finish_btn.clicked.connect(self._finish_drawing)
        r2.addWidget(self._finish_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setStyleSheet("""
            QPushButton { background-color:#FF4444; color:white; font:bold 13px Arial;
                          padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background-color:#CC3333; }
            QPushButton:disabled { background-color:#552222; color:#666666; }
        """)
        self._cancel_btn.clicked.connect(self._cancel_drawing)
        r2.addWidget(self._cancel_btn)

        r2.addSpacing(20)

        self._clear_changes_btn = QPushButton("Clear All Changes")
        self._clear_changes_btn.setStyleSheet("""
            QPushButton { background-color:#FF4444; color:white; font:bold 13px Arial;
                          padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background-color:#CC3333; }
            QPushButton:disabled { background-color:#552222; color:#666666; }
        """)
        self._clear_changes_btn.clicked.connect(self._clear_all_changes)
        r2.addWidget(self._clear_changes_btn)

        r2.addSpacing(20)

        self._save_btn = QPushButton("Save All")
        self._save_btn.setStyleSheet("""
            QPushButton { background-color:#FFD700; color:black; font:bold 13px Arial;
                          padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background-color:#FFC000; }
            QPushButton:disabled { background-color:#554700; color:#666666; }
        """)
        self._save_btn.clicked.connect(self._save_barriers)
        r2.addWidget(self._save_btn)

        self._clear_btn = QPushButton("Clear All")
        self._clear_btn.setStyleSheet("""
            QPushButton { background-color:#888888; color:white; font:bold 13px Arial;
                          padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background-color:#666666; }
            QPushButton:disabled { background-color:#444444; color:#666666; }
        """)
        self._clear_btn.clicked.connect(self._clear_all)
        r2.addWidget(self._clear_btn)

        self._del_btn = QPushButton("Delete Barrier")
        self._del_btn.setStyleSheet("""
            QPushButton { background-color:#FF4444; color:white; font:bold 13px Arial;
                          padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background-color:#CC3333; }
            QPushButton:disabled { background-color:#552222; color:#666666; }
        """)
        self._del_btn.clicked.connect(self._delete_selected)
        r2.addWidget(self._del_btn)

        r2.addStretch(1)
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
        self._status_lbl.setStyleSheet("color: white; font: bold 13px Arial;")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setMinimumHeight(75)
        right_col.addWidget(self._status_lbl)

        self._barrier_list.currentRowChanged.connect(self._on_barrier_selected)

        # --- Images section ---
        img_title = QLabel("Images")
        img_title.setFont(QFont('Arial', 12, QFont.Weight.Bold))
        img_title.setStyleSheet("color: #FF8800;")
        img_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_col.addWidget(img_title)

        self._image_list = QListWidget()
        self._image_list.setStyleSheet("""
            QListWidget { background:#111111; color:white; font:11px Arial;
                          border:1px solid #FF8800; }
            QListWidget::item:selected { background:#FF8800; color:black; }
            QListWidget::item:selected:!active { background:#FF8800; color:black; }
        """)
        self._image_list.setFixedWidth(220)
        self._image_list.setMaximumHeight(120)
        self._image_list.viewport().installEventFilter(self)
        right_col.addWidget(self._image_list)

        img_btn_row = QHBoxLayout()
        self._import_img_btn = QPushButton("Import")
        self._import_img_btn.setStyleSheet("""
            QPushButton { background-color:#4444CC; color:white; font:bold 13px Arial;
                          padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background-color:#3333AA; }
        """)
        self._import_img_btn.clicked.connect(self._import_image)
        img_btn_row.addWidget(self._import_img_btn)

        self._delete_img_btn = QPushButton("Delete")
        self._delete_img_btn.setStyleSheet("""
            QPushButton { background-color:#FF4444; color:white; font:bold 13px Arial;
                          padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background-color:#CC3333; }
            QPushButton:disabled { background-color:#552222; color:#666666; }
        """)
        self._delete_img_btn.setEnabled(False)
        self._delete_img_btn.clicked.connect(self._delete_image_selected)
        img_btn_row.addWidget(self._delete_img_btn)

        self._extract_img_btn = QPushButton("→ Barrier")
        self._extract_img_btn.setStyleSheet("""
            QPushButton { background-color:#39FF14; color:black; font:bold 13px Arial;
                          padding:5px 14px; border-radius:4px; }
            QPushButton:hover { background-color:#2BCC10; }
            QPushButton:disabled { background-color:#1A5508; color:#666666; }
        """)
        self._extract_img_btn.setEnabled(False)
        self._extract_img_btn.clicked.connect(self._extract_image_as_barrier)
        img_btn_row.addWidget(self._extract_img_btn)

        right_col.addLayout(img_btn_row)

        self._image_list.currentRowChanged.connect(self._on_image_selected)

        root.addLayout(right_col)
        self._update_button_states('normal')

        # Live-edit signals — fire whenever a barrier is selected
        for sig in (
            self._trigger_combo.currentTextChanged,
            self._when_combo.currentTextChanged,
            self._name_edit.textChanged,
            self._callback_edit.textChanged,
        ):
            sig.connect(lambda _: self._live_update_barrier())
        self._alpha_spin.valueChanged.connect(lambda _: self._live_update_barrier())
        # Color uses activated so Custom re-opens the dialog even when already selected
        self._color_combo.activated.connect(self._on_color_activated)
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
        self._ruler_btn.setEnabled(not drawing)

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
        # Deselect any image when a barrier is selected
        if row >= 0:
            self._deselect_image()
        if 0 <= row < len(self._barriers):
            self._load_barrier_into_controls(self._barriers[row])
            self._draw_selected_handles(self._barriers[row])
        else:
            self._blank_edit_controls()
            self._clear_handles()
        self._update_button_states('normal')
        if self._name_display_mode() == 'selected':
            self._update_name_visibility()

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
        """Persist barriers and images to disk without updating the status label."""
        save_all(self._get_barriers_path(), self._barriers, self._images)
        self._has_unsaved_changes = False
        self._update_unsaved_state()

    def _clear_all_changes(self) -> None:
        """Revert all in-memory barrier and image edits to the last saved file state."""
        saved_row = self._barrier_list.currentRow()
        path = self._get_barriers_path()
        self._barriers = load_barriers(path)
        self._images = load_images(path)
        for name in list(self._barrier_patches.keys()):
            self._remove_barrier_patches(name)
        self._remove_all_image_artists()
        self._draw_all_barriers()
        self._draw_all_images()
        self._refresh_barrier_list()
        self._refresh_image_list()
        self._has_unsaved_changes = False
        # Restore selection so controls reload
        if 0 <= saved_row < len(self._barriers):
            self._barrier_list.setCurrentRow(saved_row)
        else:
            self._blank_edit_controls()
        self._update_button_states('normal')
        self._canvas.draw_idle()
        self._status_lbl.setText("All changes cleared — reverted to saved file")
        self._status_lbl.setStyleSheet("color: #FF4444; font: bold 13px Arial;")

    def eventFilter(self, obj, event) -> bool:
        """Deselect a list item when it is clicked while already selected."""
        click_types = (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
        if (event.type() in click_types
                and event.button() == Qt.MouseButton.LeftButton):
            if obj is self._barrier_list.viewport():
                item = self._barrier_list.itemAt(event.pos())
                if item is not None and self._barrier_list.currentItem() is item:
                    self._deselect_barrier()
                    return True
            elif obj is self._image_list.viewport():
                item = self._image_list.itemAt(event.pos())
                if item is not None and self._image_list.currentItem() is item:
                    self._deselect_image()
                    self._clear_image_handles()
                    self._canvas.draw_idle()
                    return True
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
        """Apply the current drag delta and refresh."""
        state = self._drag_state
        if state is None or event.xdata is None or event.ydata is None:
            return

        dx = event.xdata - state['last_x']
        dy = event.ydata - state['last_y']
        state['last_x'] = event.xdata
        state['last_y'] = event.ydata

        if state['type'] == 'ruler_endpoint':
            idx = state['idx']
            if len(self._ruler_endpoints) == 2:
                x, y = self._ruler_endpoints[idx]
                self._ruler_endpoints[idx] = (x + dx, y + dy)
                self._redraw_ruler()
            return

        if state['type'] == 'ruler_midpoint':
            if len(self._ruler_endpoints) == 2:
                (x1, y1), (x2, y2) = self._ruler_endpoints
                self._ruler_endpoints = [(x1 + dx, y1 + dy), (x2 + dx, y2 + dy)]
                self._redraw_ruler()
            return

        if state['type'] == 'img_move':
            idx = self._selected_image_idx
            if 0 <= idx < len(self._images):
                img = self._images[idx]
                img.center_x += dx
                img.center_y += dy
                self._redraw_image(img)
                self._update_image_handle_positions(img)
                self._canvas.draw_idle()
            return

        if state['type'] == 'img_handle':
            idx = self._selected_image_idx
            if 0 <= idx < len(self._images):
                img = self._images[idx]
                h_idx = state['handle_idx']
                self._apply_image_handle_move(
                    img, h_idx, event.xdata, event.ydata, dx, dy)
                self._redraw_image(img)
                self._update_image_handle_positions(img)
                self._canvas.draw_idle()
            return

        row = state['row']
        if row < 0 or row >= len(self._barriers):
            return
        barrier = self._barriers[row]

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
        self._status_lbl.setStyleSheet("color: #FF8800; font: bold 13px Arial;")
        self._update_button_states('drawing')

    def _on_color_activated(self, index: int) -> None:
        """Handle colour combo selection; open Custom dialog when needed."""
        if self._color_combo.itemText(index) == "Custom":
            self._prompt_custom_color()
        else:
            self._prev_color_idx = index
            self._live_update_barrier()

    def _prompt_custom_color(self) -> None:
        """Open the hex colour dialog; revert selection if cancelled."""
        current_hex = self._color_combo.currentData() or "#FF0000"
        dlg = _CustomColorDialog(self, current_hex)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            hex_val = dlg.hex_value()
            idx = self._color_combo.currentIndex()
            self._color_combo.blockSignals(True)
            self._color_combo.setItemData(idx, hex_val)
            self._color_combo.blockSignals(False)
            self._prev_color_idx = idx
            self._live_update_barrier()
            self._canvas.draw()   # force immediate repaint, not just idle schedule
        else:
            self._color_combo.blockSignals(True)
            self._color_combo.setCurrentIndex(self._prev_color_idx)
            self._color_combo.blockSignals(False)

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
        self._status_lbl.setStyleSheet("color: #FF8800; font: bold 13px Arial;")

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
        self._status_lbl.setStyleSheet("color: #FF4444; font: bold 13px Arial;")
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

        # ---- ruler endpoint drag (always, if a ruler exists) ---------------
        ep_idx = self._hit_test_ruler_endpoints(event)
        if ep_idx is not None:
            self._drag_state = {
                'type': 'ruler_endpoint', 'idx': ep_idx,
                'last_x': x, 'last_y': y,
            }
            return

        # ---- ruler midpoint drag (translate entire ruler) -------------------
        if self._hit_test_ruler_midpoint(event):
            self._drag_state = {
                'type': 'ruler_midpoint',
                'last_x': x, 'last_y': y,
            }
            return

        # ---- barrier handles (highest priority — never blocked by images) --
        row = self._barrier_list.currentRow()
        if 0 <= row < len(self._barriers):
            barrier = self._barriers[row]
            handle_idx = self._hit_test_handles(event)
            if handle_idx is not None:
                self._drag_state = {
                    'type': 'handle', 'row': row, 'handle_idx': handle_idx,
                    'last_x': x, 'last_y': y,
                }
                return

        # ---- image handle drag (selected image) ---------------------------
        img_h_idx = self._hit_test_image_handles(event)
        if img_h_idx is not None:
            self._drag_state = {
                'type': 'img_handle', 'handle_idx': img_h_idx,
                'last_x': x, 'last_y': y,
            }
            return

        # ---- barrier body drag (already-selected barrier) -----------------
        if 0 <= row < len(self._barriers):
            barrier = self._barriers[row]
            if self._hit_test_barrier_body(event, barrier):
                self._drag_state = {
                    'type': 'body', 'row': row,
                    'last_x': x, 'last_y': y,
                }
                return

        # ---- image body click/drag ----------------------------------------
        img_body_idx = self._hit_test_image_body(event)
        if img_body_idx is not None:
            if img_body_idx != self._selected_image_idx:
                self._select_image(img_body_idx)
            self._drag_state = {
                'type': 'img_move', 'last_x': x, 'last_y': y,
            }
            return

        # ---- ruler point placement (only in ruler mode, empty canvas) ------
        if self._ruler_mode:
            if self._ruler_artists:
                # Ruler already shown: clear it, this click becomes new first point
                self._clear_ruler()
                self._ruler_p1 = (x, y)
                self._ruler_p1_dot, = self.ax.plot(
                    x, y, 'o', color='#DDDDDD', markersize=7, zorder=31)
                self._canvas.draw_idle()
                self._tools_status_lbl.setText("Ruler: click second point")
                self._tools_status_lbl.setStyleSheet("color: #FF8800; font: bold 13px Arial;")
            elif self._ruler_p1 is None:
                # First point
                self._ruler_p1 = (x, y)
                self._ruler_p1_dot, = self.ax.plot(
                    x, y, 'o', color='#DDDDDD', markersize=7, zorder=31)
                self._canvas.draw_idle()
                self._tools_status_lbl.setText("Ruler: click second point")
                self._tools_status_lbl.setStyleSheet("color: #FF8800; font: bold 13px Arial;")
            else:
                # Second point — draw the ruler
                self._draw_ruler(self._ruler_p1, (x, y))
                self._ruler_p1 = None

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
            self._status_lbl.setStyleSheet("color: #FF4444; font: bold 13px Arial;")
            return

        barrier = self._make_barrier_from_controls(BarrierType.POLYGON)
        barrier.vertices = list(self._polygon_vertices)

        ok, msg = validate_barrier(barrier)
        if not ok:
            self._status_lbl.setText(f"Invalid: {msg}")
            self._status_lbl.setStyleSheet("color: #FF4444; font: bold 13px Arial;")
            return

        self._barriers.append(barrier)
        self._clear_preview()
        self._drawing_mode = None
        self._polygon_vertices = []
        self._draw_barrier(barrier)
        self._refresh_barrier_list()
        self._auto_save()
        self._status_lbl.setText(f"Added and saved polygon '{barrier.name}'")
        self._status_lbl.setStyleSheet("color: #39FF14; font: bold 13px Arial;")
        self._update_button_states('normal')
        self._canvas.draw_idle()

    def _finish_circle(self, x: float, y: float) -> None:
        cx, cy = self._circle_center
        radius = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        if radius <= 0:
            self._status_lbl.setText("Circle radius must be > 0")
            self._status_lbl.setStyleSheet("color: #FF4444; font: bold 13px Arial;")
            return

        barrier = self._make_barrier_from_controls(BarrierType.CIRCLE)
        barrier.center = (cx, cy)
        barrier.radius = radius

        ok, msg = validate_barrier(barrier)
        if not ok:
            self._status_lbl.setText(f"Invalid: {msg}")
            self._status_lbl.setStyleSheet("color: #FF4444; font: bold 13px Arial;")
            return

        self._barriers.append(barrier)
        self._clear_preview()
        self._drawing_mode = None
        self._circle_center = None
        self._draw_barrier(barrier)
        self._refresh_barrier_list()
        self._auto_save()
        self._status_lbl.setText(f"Added and saved circle '{barrier.name}'")
        self._status_lbl.setStyleSheet("color: #39FF14; font: bold 13px Arial;")
        self._update_button_states('normal')
        self._canvas.draw_idle()

    def _finish_line(self, x: float, y: float) -> None:
        p1 = self._line_point1
        p2 = (x, y)
        if p1 == p2:
            self._status_lbl.setText("Line endpoints must differ")
            self._status_lbl.setStyleSheet("color: #FF4444; font: bold 13px Arial;")
            return

        barrier = self._make_barrier_from_controls(BarrierType.LINE)
        barrier.point1 = p1
        barrier.point2 = p2
        barrier.thickness = 5.0  # default thickness in cm

        ok, msg = validate_barrier(barrier)
        if not ok:
            self._status_lbl.setText(f"Invalid: {msg}")
            self._status_lbl.setStyleSheet("color: #FF4444; font: bold 13px Arial;")
            return

        self._barriers.append(barrier)
        self._clear_preview()
        self._drawing_mode = None
        self._line_point1 = None
        self._draw_barrier(barrier)
        self._refresh_barrier_list()
        self._auto_save()
        self._status_lbl.setText(f"Added and saved line '{barrier.name}'")
        self._status_lbl.setStyleSheet("color: #39FF14; font: bold 13px Arial;")
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
            color=self._current_color(),
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
        data = self._color_combo.currentData()
        return data if data else "#FF0000"

    # ------------------------------------------------------------------
    # Barrier rendering
    # ------------------------------------------------------------------

    def _draw_all_barriers(self) -> None:
        for barrier in self._barriers:
            self._draw_barrier(barrier)

    def _draw_barrier(self, barrier: BarrierData) -> None:
        artists = []
        import matplotlib.colors as _mcolors
        try:
            _mcolors.to_rgba(barrier.color)
            color = barrier.color
        except (ValueError, TypeError):
            color = '#FFFFFF'
        alpha = barrier.alpha

        mode = self._name_display_mode()
        row = self._barrier_list.currentRow()
        selected_name = (self._barriers[row].name
                         if 0 <= row < len(self._barriers) else None)

        def _lbl_visible() -> bool:
            return mode == 'all' or (mode == 'selected' and barrier.name == selected_name)

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
            lbl.set_visible(_lbl_visible())
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
            lbl.set_visible(_lbl_visible())
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
            lbl.set_visible(_lbl_visible())
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
        self._status_lbl.setStyleSheet("color: #FF4444; font: bold 13px Arial;")


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

        # Find predefined colour by stored data value; fall back to Custom
        color_idx = next(
            (i for i in range(self._color_combo.count())
             if self._color_combo.itemData(i) == barrier.color),
            None,
        )
        if color_idx is not None:
            self._color_combo.setCurrentIndex(color_idx)
            self._prev_color_idx = color_idx
        else:
            custom_idx = self._color_combo.count() - 1
            self._color_combo.setItemData(custom_idx, barrier.color)
            self._color_combo.setCurrentIndex(custom_idx)
            self._prev_color_idx = custom_idx

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
        barrier.color         = self._current_color()
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
        save_all(path, self._barriers, self._images)
        self._has_unsaved_changes = False
        self._update_unsaved_state()
        self._status_lbl.setText(
            f"Saved {len(self._barriers)} barriers, {len(self._images)} images")
        self._status_lbl.setStyleSheet("color: #FFD700; font: bold 13px Arial;")

    def _load_barriers(self) -> None:
        path = self._get_barriers_path()
        self._barriers = load_barriers(path)
        self._images = load_images(path)
        self._redraw_all_barriers()
        self._draw_all_images()
        self._refresh_barrier_list()
        self._refresh_image_list()
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
        self._status_lbl.setStyleSheet("color: white; font: bold 13px Arial;")

    # ------------------------------------------------------------------
    # Image overlay rendering & editing
    # ------------------------------------------------------------------

    def _get_resources_dir(self) -> str:
        config_dir = os.path.dirname(self._settings._config_path)
        return os.path.join(config_dir, 'resources')

    def _draw_image(self, overlay: ImageOverlay) -> None:
        """Render (or re-render) a single image overlay on the canvas."""
        self._remove_image_artist(overlay.name)
        resources_dir = self._get_resources_dir()
        filepath = os.path.join(resources_dir, overlay.filename)
        if not os.path.exists(filepath):
            return

        try:
            img_data = mpl_image.imread(filepath)
        except Exception:
            return

        # Snapshot limits before imshow — imshow updates internal data limits
        # even when autoscale_on=False, so we restore them explicitly.
        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()

        half_w = overlay.width_cm / 2.0
        half_h = overlay.height_cm / 2.0
        cx, cy = overlay.center_x, overlay.center_y

        # Place extent at actual arena coordinates so internal data-limit updates
        # stay inside the arena rather than near the origin.
        # Do NOT pass aspect= — imshow(aspect='auto') calls ax.set_aspect('auto')
        # internally which overrides the equal-aspect-ratio set in _build_figure.
        extent = [cx - half_w, cx + half_w, cy - half_h, cy + half_h]

        im = self.ax.imshow(
            img_data,
            extent=extent,
            origin='upper',
            zorder=4,
            interpolation='bilinear',
        )
        # Rotation only — translation is already encoded in the extent
        tr = (
            Affine2D()
            .rotate_around(cx, cy, math.radians(overlay.rotation_deg))
            + self.ax.transData
        )
        im.set_transform(tr)
        self._image_artists[overlay.name] = im

        # Hard-restore view limits — never let image drawing affect the arena
        self.ax.set_xlim(xlim)
        self.ax.set_ylim(ylim)

    def _remove_image_artist(self, name: str) -> None:
        artist = self._image_artists.pop(name, None)
        if artist is not None:
            try:
                artist.remove()
            except (ValueError, AttributeError):
                pass

    def _remove_all_image_artists(self) -> None:
        for name in list(self._image_artists.keys()):
            self._remove_image_artist(name)

    def _redraw_image(self, overlay: ImageOverlay) -> None:
        """Remove and re-draw a single overlay (called after geometry changes)."""
        self._draw_image(overlay)

    def _draw_all_images(self) -> None:
        self._remove_all_image_artists()
        for overlay in self._images:
            self._draw_image(overlay)
        self._canvas.draw_idle()

    def _refresh_image_list(self) -> None:
        self._image_list.clear()
        for img in self._images:
            self._image_list.addItem(
                f"{img.name} ({img.width_cm:.0f}×{img.height_cm:.0f} cm)")

    def _on_image_selected(self, row: int) -> None:
        if row < 0:
            self._deselect_image()
            return
        # Deselect barrier list when image is selected
        self._barrier_list.blockSignals(True)
        self._barrier_list.clearSelection()
        self._barrier_list.setCurrentRow(-1)
        self._barrier_list.blockSignals(False)
        self._clear_handles()
        self._blank_edit_controls()
        self._update_button_states('normal')
        self._select_image(row)

    def _select_image(self, idx: int) -> None:
        # Clear any barrier selection first
        self._barrier_list.blockSignals(True)
        self._barrier_list.clearSelection()
        self._barrier_list.setCurrentRow(-1)
        self._barrier_list.blockSignals(False)
        self._clear_handles()
        self._blank_edit_controls()

        self._selected_image_idx = idx
        self._image_list.blockSignals(True)
        self._image_list.setCurrentRow(idx)
        self._image_list.blockSignals(False)
        self._delete_img_btn.setEnabled(True)
        self._extract_img_btn.setEnabled(True)
        if 0 <= idx < len(self._images):
            self._draw_image_handles(self._images[idx])

    def _deselect_image(self) -> None:
        self._selected_image_idx = -1
        self._clear_image_handles()
        self._image_list.blockSignals(True)
        self._image_list.clearSelection()
        self._image_list.setCurrentRow(-1)
        self._image_list.blockSignals(False)
        self._delete_img_btn.setEnabled(False)
        self._extract_img_btn.setEnabled(False)

    # ---- image handles -------------------------------------------------------

    def _get_image_corner_positions(self, overlay: ImageOverlay) -> list[tuple]:
        """Return the 4 screen corners (arena cm) of the overlay, in order TL TR BR BL."""
        hw = overlay.width_cm / 2.0
        hh = overlay.height_cm / 2.0
        local_corners = [(-hw, hh), (hw, hh), (hw, -hh), (-hw, -hh)]
        rad = math.radians(overlay.rotation_deg)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        result = []
        for lx, ly in local_corners:
            rx = lx * cos_r - ly * sin_r + overlay.center_x
            ry = lx * sin_r + ly * cos_r + overlay.center_y
            result.append((rx, ry))
        return result

    def _get_rotation_handle_pos(self, overlay: ImageOverlay) -> tuple:
        """Return the position of the rotation handle (above the image centre)."""
        offset = overlay.height_cm / 2.0 + 10.0
        rad = math.radians(overlay.rotation_deg)
        rx = -math.sin(rad) * offset + overlay.center_x
        ry = math.cos(rad) * offset + overlay.center_y
        return (rx, ry)

    def _draw_image_handles(self, overlay: ImageOverlay) -> None:
        self._clear_image_handles()
        kw = dict(markersize=9, markeredgecolor='black', markeredgewidth=1.5,
                  zorder=25, picker=False)

        # Center handle
        h, = self.ax.plot(overlay.center_x, overlay.center_y,
                          'D', color='#FF8800', **kw)
        self._img_handle_artists.append(h)
        self._img_handle_meta.append(('img_center',))

        # Corner handles
        for i, (cx, cy) in enumerate(self._get_image_corner_positions(overlay)):
            h, = self.ax.plot(cx, cy, 's', color='#FFFFFF', **kw)
            self._img_handle_artists.append(h)
            self._img_handle_meta.append(('img_corner', i))

        # Rotation handle
        rx, ry = self._get_rotation_handle_pos(overlay)
        h, = self.ax.plot(rx, ry, '^', color='#00FFFF', **kw)
        self._img_handle_artists.append(h)
        self._img_handle_meta.append(('img_rotate',))

        self._canvas.draw_idle()

    def _clear_image_handles(self) -> None:
        for h in self._img_handle_artists:
            try:
                h.remove()
            except (ValueError, AttributeError):
                pass
        self._img_handle_artists.clear()
        self._img_handle_meta.clear()

    def _update_image_handle_positions(self, overlay: ImageOverlay) -> None:
        corners = self._get_image_corner_positions(overlay)
        rx, ry = self._get_rotation_handle_pos(overlay)
        for i, (h, meta) in enumerate(
                zip(self._img_handle_artists, self._img_handle_meta)):
            tag = meta[0]
            if tag == 'img_center':
                h.set_xdata([overlay.center_x])
                h.set_ydata([overlay.center_y])
            elif tag == 'img_corner':
                ci = meta[1]
                h.set_xdata([corners[ci][0]])
                h.set_ydata([corners[ci][1]])
            elif tag == 'img_rotate':
                h.set_xdata([rx])
                h.set_ydata([ry])

    def _hit_test_image_handles(self, event) -> 'int | None':
        if self._selected_image_idx < 0 or event.x is None:
            return None
        for i, h in enumerate(self._img_handle_artists):
            hx, hy = h.get_xdata()[0], h.get_ydata()[0]
            disp = self.ax.transData.transform((hx, hy))
            if math.sqrt((disp[0] - event.x) ** 2 + (disp[1] - event.y) ** 2) <= 12:
                return i
        return None

    def _hit_test_image_body(self, event) -> 'int | None':
        """Return the index of the image overlay under the cursor, or None."""
        if event.xdata is None or event.ydata is None:
            return None
        for i, overlay in enumerate(self._images):
            if self._point_in_image(event.xdata, event.ydata, overlay):
                return i
        return None

    def _point_in_image(self, px: float, py: float, overlay: ImageOverlay) -> bool:
        """Return True if (px,py) falls inside the (possibly rotated) image rectangle."""
        rad = math.radians(-overlay.rotation_deg)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        dx = px - overlay.center_x
        dy = py - overlay.center_y
        lx = dx * cos_r - dy * sin_r
        ly = dx * sin_r + dy * cos_r
        return abs(lx) <= overlay.width_cm / 2.0 and abs(ly) <= overlay.height_cm / 2.0

    def _apply_image_handle_move(
        self,
        overlay: ImageOverlay,
        h_idx: int,
        abs_x: float, abs_y: float,
        dx: float, dy: float,
    ) -> None:
        if h_idx >= len(self._img_handle_meta):
            return
        meta = self._img_handle_meta[h_idx]
        tag = meta[0]

        if tag == 'img_center':
            overlay.center_x += dx
            overlay.center_y += dy

        elif tag == 'img_corner':
            # Scale: adjust width/height by how far the corner moved
            # Project delta onto local axes
            rad = math.radians(overlay.rotation_deg)
            cos_r, sin_r = math.cos(rad), math.sin(rad)
            ldx = dx * cos_r + dy * sin_r
            ldy = -dx * sin_r + dy * cos_r
            ci = meta[1]
            # TL=0 TR=1 BR=2 BL=3
            x_sign = 1 if ci in (1, 2) else -1
            y_sign = 1 if ci in (0, 1) else -1
            delta_w = ldx * x_sign * 2
            delta_h = ldy * y_sign * 2
            overlay.width_cm = max(5.0, overlay.width_cm + delta_w)
            overlay.height_cm = max(5.0, overlay.height_cm + delta_h)

        elif tag == 'img_rotate':
            # Angle of cursor relative to image centre
            angle = math.degrees(
                math.atan2(abs_y - overlay.center_y, abs_x - overlay.center_x))
            # Rotation handle sits at 90° in local space; adjust
            overlay.rotation_deg = angle - 90.0

    # ---- image management buttons -------------------------------------------

    def _import_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif)")
        if not path:
            return

        dlg = _ImportImageDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        width_cm, height_cm = dlg.values()

        x_range = self.ax.get_xlim()
        y_range = self.ax.get_ylim()
        arena_w = x_range[1] - x_range[0]
        arena_h = y_range[1] - y_range[0]

        # Only clamp when the image is larger than the entire arena
        clamped = False
        if width_cm > arena_w or height_cm > arena_h:
            scale = min(arena_w / width_cm, arena_h / height_cm)
            width_cm *= scale
            height_cm *= scale
            clamped = True

        resources_dir = self._get_resources_dir()
        os.makedirs(resources_dir, exist_ok=True)

        # Build unique filename
        base_name = os.path.basename(path)
        dest = os.path.join(resources_dir, base_name)
        if os.path.abspath(path) != os.path.abspath(dest):
            # Avoid name collision
            stem, ext = os.path.splitext(base_name)
            counter = 1
            while os.path.exists(dest):
                dest = os.path.join(resources_dir, f"{stem}_{counter}{ext}")
                counter += 1
            shutil.copy2(path, dest)

        # Place image at centre of visible arena
        cx = (x_range[0] + x_range[1]) / 2.0
        cy = (y_range[0] + y_range[1]) / 2.0

        # Unique name
        filename = os.path.basename(dest)
        stem = os.path.splitext(filename)[0]
        existing_names = {img.name for img in self._images}
        name = stem
        counter = 2
        while name in existing_names:
            name = f"{stem}_{counter}"
            counter += 1

        overlay = ImageOverlay(
            name=name,
            filename=filename,
            center_x=cx,
            center_y=cy,
            width_cm=width_cm,
            height_cm=height_cm,
            rotation_deg=0.0,
        )
        self._images.append(overlay)
        self._draw_image(overlay)
        self._refresh_image_list()
        self._select_image(len(self._images) - 1)
        self._auto_save()
        if clamped:
            self._status_lbl.setText(
                f"'{name}' imported — dimensions too large, "
                f"scaled to {width_cm:.0f}×{height_cm:.0f} cm")
            self._status_lbl.setStyleSheet("color: #FF8800; font: bold 13px Arial;")
        else:
            self._status_lbl.setText(
                f"Imported '{name}' ({width_cm:.0f}×{height_cm:.0f} cm)")
            self._status_lbl.setStyleSheet("color: #39FF14; font: bold 13px Arial;")
        self._canvas.draw_idle()

    def _delete_image_selected(self) -> None:
        idx = self._selected_image_idx
        if idx < 0 or idx >= len(self._images):
            return
        overlay = self._images[idx]
        reply = QMessageBox.question(
            self, "Delete Image",
            f"Remove image '{overlay.name}' from canvas?\n"
            f"(The file in resources/ will not be deleted.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._remove_image_artist(overlay.name)
        self._images.pop(idx)
        self._deselect_image()
        self._refresh_image_list()
        self._auto_save()
        self._status_lbl.setText(f"Removed image '{overlay.name}'")
        self._status_lbl.setStyleSheet("color: #FF4444; font: bold 13px Arial;")
        self._canvas.draw_idle()

    def _extract_image_as_barrier(self) -> None:
        idx = self._selected_image_idx
        if idx < 0 or idx >= len(self._images):
            return
        overlay = self._images[idx]
        resources_dir = self._get_resources_dir()
        self._status_lbl.setText("Extracting polygon…")
        self._status_lbl.setStyleSheet("color: #FF8800; font: bold 13px Arial;")

        try:
            verts = polygon_from_image(overlay, resources_dir)
        except Exception as exc:
            self._status_lbl.setText(f"Extraction failed: {exc}")
            self._status_lbl.setStyleSheet("color: #FF4444; font: bold 13px Arial;")
            return

        if len(verts) < 3:
            self._status_lbl.setText("Could not extract enough vertices")
            self._status_lbl.setStyleSheet("color: #FF4444; font: bold 13px Arial;")
            return

        existing = {b.name for b in self._barriers}
        name = overlay.name + "_barrier"
        counter = 2
        while name in existing:
            name = f"{overlay.name}_barrier_{counter}"
            counter += 1

        barrier = BarrierData(
            name=name,
            barrier_type=BarrierType.POLYGON,
            trigger_mode=TriggerMode.EVENT,
            trigger_when=TriggerWhen.INSIDE,
            callback_name="on_barrier",
            color="#FF8800",
            alpha=0.3,
            vertices=verts,
        )
        self._barriers.append(barrier)
        self._draw_barrier(barrier)
        self._refresh_barrier_list()
        self._auto_save()
        self._status_lbl.setText(
            f"Extracted '{name}' ({len(verts)} vertices)")
        self._status_lbl.setStyleSheet("color: #39FF14; font: bold 13px Arial;")
        self._canvas.draw_idle()

    # ------------------------------------------------------------------
    # Name display settings
    # ------------------------------------------------------------------

    def _name_display_mode(self) -> str:
        """Return 'all', 'selected', or 'none' based on the radio button states."""
        if self._no_names_rb.isChecked():
            return 'none'
        if self._show_all_names_rb.isChecked():
            return 'all'
        return 'selected'

    def _update_name_visibility(self) -> None:
        """Apply current name-display mode to all drawn barrier label artists."""
        mode = self._name_display_mode()
        row = self._barrier_list.currentRow()
        selected_name = (self._barriers[row].name
                         if 0 <= row < len(self._barriers) else None)
        for name, artists in self._barrier_patches.items():
            if len(artists) >= 2:
                artists[1].set_visible(
                    mode == 'all' or (mode == 'selected' and name == selected_name)
                )
        self._canvas.draw_idle()

    def _on_show_all_names_clicked(self) -> None:
        if self._show_all_names_rb.isChecked():
            self._no_names_rb.setChecked(False)
        self._update_name_visibility()

    def _on_no_names_clicked(self) -> None:
        if self._no_names_rb.isChecked():
            self._show_all_names_rb.setChecked(False)
        self._update_name_visibility()

    # ------------------------------------------------------------------
    # Ruler tool
    # ------------------------------------------------------------------

    def _toggle_ruler(self) -> None:
        if self._ruler_mode:
            self._ruler_mode = False
            self._ruler_btn.setText("Virtual Ruler")
            self._clear_ruler_p1()
            self._clear_ruler()
            self._tools_status_lbl.setText("Ready")
            self._tools_status_lbl.setStyleSheet("color: white; font: bold 13px Arial;")
        else:
            self._ruler_mode = True
            self._ruler_btn.setText("Stop Ruler")
            if not self._ruler_artists:
                self._tools_status_lbl.setText("Virtual ruler active")
                self._tools_status_lbl.setStyleSheet("color: #FF8800; font: bold 13px Arial;")

    def _clear_ruler_p1(self) -> None:
        """Remove the pending first-point dot and reset first-point state."""
        if self._ruler_p1_dot is not None:
            try:
                self._ruler_p1_dot.remove()
            except (ValueError, AttributeError):
                pass
            self._ruler_p1_dot = None
        self._ruler_p1 = None

    def _clear_ruler(self) -> None:
        """Remove the complete ruler (line + dots + annotation)."""
        for artist in self._ruler_artists:
            try:
                artist.remove()
            except (ValueError, AttributeError):
                pass
        self._ruler_artists.clear()
        self._ruler_endpoints.clear()
        self._canvas.draw_idle()

    def _draw_ruler(self, p1: tuple, p2: tuple) -> None:
        """Create the complete ruler between p1 and p2, removing any p1 dot first."""
        self._clear_ruler_p1()
        self._clear_ruler()
        x1, y1 = p1
        x2, y2 = p2
        self._ruler_endpoints = [(x1, y1), (x2, y2)]

        line, = self.ax.plot([x1, x2], [y1, y2], '--',
                              color='#DDDDDD', linewidth=1.5, alpha=0.9, zorder=30)
        dot1, = self.ax.plot(x1, y1, 'o', color='#DDDDDD', markersize=7, zorder=31)
        dot2, = self.ax.plot(x2, y2, 'o', color='#DDDDDD', markersize=7, zorder=31)
        dist = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        ann = self.ax.annotate(
            f"{dist:.1f} cm",
            xy=(mx, my), xytext=(0, 12),
            textcoords='offset points',
            ha='center', va='bottom',
            fontsize=10, fontweight='bold', color='black',
            bbox=dict(boxstyle='round,pad=0.3', facecolor='white',
                      alpha=0.9, edgecolor='#888888'),
            zorder=32,
        )
        dot_mid, = self.ax.plot(mx, my, 's', color='#DDDDDD', markersize=7, zorder=31)
        self._ruler_artists = [line, dot1, dot2, ann, dot_mid]
        self._canvas.draw_idle()
        self._tools_status_lbl.setText(f"Distance:\n{dist:.1f} cm")
        self._tools_status_lbl.setStyleSheet("color: #39FF14; font: bold 13px Arial;")

    def _redraw_ruler(self) -> None:
        """Update ruler artists in-place after an endpoint or midpoint is dragged."""
        if len(self._ruler_artists) < 5 or len(self._ruler_endpoints) < 2:
            return
        (x1, y1), (x2, y2) = self._ruler_endpoints
        dist = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2

        line, dot1, dot2, ann, dot_mid = self._ruler_artists
        line.set_xdata([x1, x2]); line.set_ydata([y1, y2])
        dot1.set_xdata([x1]);     dot1.set_ydata([y1])
        dot2.set_xdata([x2]);     dot2.set_ydata([y2])
        dot_mid.set_xdata([mx]);  dot_mid.set_ydata([my])
        ann.xy = (mx, my)
        ann.set_text(f"{dist:.1f} cm")
        self._canvas.draw_idle()
        self._tools_status_lbl.setText(f"Distance:\n{dist:.1f} cm")

    def _hit_test_ruler_endpoints(self, event) -> 'int | None':
        """Return 0 or 1 if the cursor is within 12 px of a ruler endpoint, else None."""
        if len(self._ruler_artists) < 3 or event.x is None or event.y is None:
            return None
        for idx in (0, 1):
            h = self._ruler_artists[idx + 1]   # dot1 at [1], dot2 at [2]
            hx, hy = h.get_xdata()[0], h.get_ydata()[0]
            disp = self.ax.transData.transform((hx, hy))
            if math.sqrt((disp[0] - event.x) ** 2 + (disp[1] - event.y) ** 2) <= 12:
                return idx
        return None

    def _hit_test_ruler_midpoint(self, event) -> bool:
        """Return True if the cursor is within 12 px of the ruler midpoint dot."""
        if len(self._ruler_artists) < 5 or event.x is None or event.y is None:
            return False
        dot_mid = self._ruler_artists[4]
        mx, my = dot_mid.get_xdata()[0], dot_mid.get_ydata()[0]
        disp = self.ax.transData.transform((mx, my))
        return math.sqrt((disp[0] - event.x) ** 2 + (disp[1] - event.y) ** 2) <= 12

    def _on_canvas_key(self, event) -> None:
        """Handle Escape: cancel a pending ruler first-point."""
        if event.key == 'escape' and self._ruler_p1 is not None and not self._ruler_artists:
            self._clear_ruler_p1()
            self._canvas.draw_idle()
            if self._ruler_mode:
                self._tools_status_lbl.setText("Virtual ruler active")
                self._tools_status_lbl.setStyleSheet("color: #FF8800; font: bold 13px Arial;")

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
        self._cid_key = self._canvas.mpl_connect(
            'key_press_event', self._on_canvas_key)

        self._refresh_timer.start()

    def on_panel_hide(self) -> None:
        self._refresh_timer.stop()
        for cid_attr in ('_cid_press', '_cid_release', '_cid_motion', '_cid_key'):
            cid = getattr(self, cid_attr, None)
            if cid is not None:
                self._canvas.mpl_disconnect(cid)
                setattr(self, cid_attr, None)
        self._drawing_mode = None
        self._drag_state = None
        self._ruler_mode = False
        self._clear_ruler_p1()
        self._clear_preview()
        self._clear_handles()
        self._clear_ruler()
        self._deselect_image()
