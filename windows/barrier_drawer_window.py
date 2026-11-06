"""Barrier Drawer panel.

Interactive click-to-draw editor for creating, editing, and saving virtual
barriers on the arena canvas.  Supports polygon, circle, and line barriers
with configurable trigger modes, colours, and callbacks, and image overlays
(a court or field diagram, say) that can be moved, scaled, rotated and traced
into a polygon barrier.

The panel follows the window size: its controls through ``windows.ui_scale``
and the arena plot through a ``ScalingCanvas``.  The arena itself (receivers
joined in any layout, the origin and the compass rose) is the shared
``ArenaView``.  Barriers and images are read from barriers.toml whenever the
panel is shown and whenever the configuration changes (an origin shift
translates that file), so an old frame is never saved over a translated one.
"""

import math
import os
import re
import shutil
import tomllib
from functools import partial

import numpy as np
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QComboBox, QDoubleSpinBox, QFrame, QDialog, QFileDialog, QGridLayout,
    QRadioButton, QSizePolicy,
)
from PyQt6.QtCore import Qt, QEvent

import matplotlib.colors as mcolors
import matplotlib.image as mpl_image
from matplotlib.figure import Figure
from matplotlib.patches import Polygon as MplPolygon, Circle as MplCircle
from matplotlib.transforms import Affine2D

from SettingsModule import SettingsModule
from ultragps_barrier import (
    BarrierData, BarrierType, TriggerMode, TriggerWhen,
    ImageOverlay,
    load_barriers, validate_barrier,
    check_point_in_barrier,
    load_images, save_all, polygon_from_image,
)
from windows import ui_scale, widgets
from windows.arena_view import ArenaView

#: The panel's accent colour (its main-menu button is orange).
ACCENT = widgets.ORANGE

#: Background of the ruler and image-import buttons.
TOOL_BLUE = "#4444CC"

#: Grey of the "Clear All" button.
NEUTRAL_GREY = "#888888"

#: Distance (logical pixels at the canvas's reference size) within which a
#: click grabs a handle, a ruler end or a line barrier.
PICK_PX = 12.0

#: Canvas size (logical pixels) in a 1200 x 900 window: the plot's text,
#: markers and lines have their design size there and scale with the canvas.
CANVAS_REF_SIZE = (780, 780)

#: Widths (design pixels) of the tools sidebar and the barrier side panel.
TOOLS_WIDTH = 170
SIDE_PANEL_WIDTH = 250

#: Preset barrier colours; the combo's last entry, "Custom", asks for a hex value.
PRESET_COLORS = [
    ("Red",     "#FF0000"),
    ("Orange",  "#FF8800"),
    ("Yellow",  "#FFFF00"),
    ("Green",   "#00FF00"),
    ("Cyan",    "#00FFFF"),
    ("Magenta", "#FF00FF"),
]

IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.gif)"

_HEX_COLOR = re.compile(r'^#[0-9A-Fa-f]{6}$')

_TYPE_NAMES = {BarrierType.POLYGON: "Polygon",
               BarrierType.CIRCLE:  "Circle",
               BarrierType.LINE:    "Line"}
_TRIGGER_MODES = {"Event": TriggerMode.EVENT, "Continuous": TriggerMode.CONTINUOUS}
_TRIGGER_WHEN = {"Inside": TriggerWhen.INSIDE, "Outside": TriggerWhen.OUTSIDE}

# Style sheets (lengths in design pixels; ui_scale scales them)
_LIST_CSS = """
    QListWidget { background:#111111; color:white; font:12px Arial;
                  border:1px solid #FF8800; }
    QListWidget::item { background:#111111; color:white; }
    QListWidget::item:focus { outline:0; }
    QListWidget::item:selected { background:#FF8800; color:black; }
    QListWidget::item:selected:!active { background:#FF8800; color:black; }
    QListWidget::item:selected:focus { background:#FF8800; color:black; outline:0; }
"""

_FORM_CSS = """
    QLabel { color:#AAAAAA; font:bold 12px Arial; }
    QComboBox, QDoubleSpinBox, QLineEdit { background:#222222; color:white;
                                           font:12px Arial; padding:2px 6px; }
    QComboBox:disabled, QDoubleSpinBox:disabled, QLineEdit:disabled {
        background:#1a1a1a; color:#555555; }
    QComboBox QAbstractItemView { background:#222222; color:white;
                                  selection-background-color:#FF8800;
                                  selection-color:black; }
"""

_RADIO_CSS = """
    QRadioButton { color:#CCCCCC; font:12px Arial; spacing:6px; }
    QRadioButton::indicator { width:13px; height:13px; }
    QRadioButton::indicator:checked { background-color:#FF8800;
        border:2px solid #FF8800; border-radius:7px; }
    QRadioButton::indicator:unchecked { background-color:#222222;
        border:2px solid #666666; border-radius:7px; }
"""

_DIALOG_CSS = """
    QDialog { background-color:#1a1a1a; }
    QLabel { color:white; font:12px Arial; }
    QLabel#error { color:#FF4444; font:italic 11px Arial; }
    QLineEdit, QDoubleSpinBox { background:#222222; color:white; font:12px Arial;
                                padding:4px; }
"""


# ---------------------------------------------------------------------------
# Small widget helpers
# ---------------------------------------------------------------------------

def _style_inputs(widget, css: str) -> None:
    """Give a box of input fields the style sheet *css*, scaled now and on
    every change of the UI scale.

    On a scaled-up UI the spin-box buttons get a width that grows with it (the
    style's own stay 16 px wide, with tiny arrows); below that the style's own
    are kept, since it draws no arrows in narrower ones.  A combo box's arrow
    keeps the style's size (styling it would need an image), as in
    ``windows.widgets``.
    """
    ui_scale.bind(widget, partial(_apply_input_css, css), key="style")


def _apply_input_css(css: str, widget, factor: float) -> None:
    sheet = ui_scale.scale_css(css, factor)
    if factor > 1.0:
        width = ui_scale.scale_length(16, factor)
        sheet += f"QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width:{width}px; }}"
    widget.setStyleSheet(sheet)


def _title(text: str, size_pt: float) -> QLabel:
    """A bold, centred title in the panel's accent colour."""
    lbl = QLabel(text)
    ui_scale.font(lbl, size_pt, bold=True)
    lbl.setStyleSheet(f"color: {ACCENT};")
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return lbl


def _status_label() -> QLabel:
    """A word-wrapped status line (its colour is set with each message)."""
    lbl = QLabel()
    lbl.setTextFormat(Qt.TextFormat.PlainText)      # messages quote names as typed
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
    lbl.setWordWrap(True)
    # A word longer than the column (a file name full of underscores, say)
    # must not widen the column past its scroll area, which would cut off
    # everything on its right
    lbl.setMinimumWidth(1)
    _set_label(lbl, "Ready", "white")
    return lbl


def _set_label(lbl: QLabel, text: str, color: str) -> None:
    """Show *text* in *color* on a status line."""
    lbl.setText(text)
    ui_scale.style(lbl, f"color: {color}; font: bold 13px Arial;")


def _separator(shape: QFrame.Shape, css: str = "color: #333333;") -> QFrame:
    """A thin horizontal or vertical rule."""
    line = QFrame()
    line.setFrameShape(shape)
    ui_scale.style(line, css)
    return line


def _spacer(width: float) -> QWidget:
    """A fixed horizontal gap (design pixels) that scales with the UI."""
    gap = QWidget()
    ui_scale.fixed_width(gap, width)
    return gap


def _form_grid(rows, spacing: float = 6) -> QGridLayout:
    """Label / field rows; each label right-aligned and centred on its field
    (which may be taller than the label, e.g. a spin box)."""
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    ui_scale.spacing(grid, spacing)
    for row, (label, field) in enumerate(rows):
        grid.addWidget(QLabel(label), row, 0,
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(field, row, 1)
    grid.setColumnStretch(1, 1)
    return grid


def _section_problem(path: str) -> str | None:
    """Why barriers.toml cannot be shown as it is because its ``barrier`` or
    ``image`` value is not a list of ``[[...]]`` tables (``barrier = 5``, a
    hand edit), or None.  The loaders skip such a value; the drawer treats the
    file as unreadable instead, so that saving asks before replacing it."""
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as fh:
        data = tomllib.load(fh)
    for key in ('barrier', 'image'):
        if key in data and not isinstance(data[key], list):
            return f"'{key}' is {data[key]!r}, not a list of [[{key}]] sections"
    return None


def _dialog_buttons(dialog: QDialog) -> QHBoxLayout:
    """OK (answers Enter) and Cancel for one of the panel's dialogs."""
    row = QHBoxLayout()
    row.addStretch(1)
    ok = widgets.neon_button("OK", widgets.GREEN, padding=(5, 18))
    ok.setDefault(True)
    ok.clicked.connect(dialog.accept)
    cancel = widgets.neon_button("Cancel", "#555555", "white", padding=(5, 18))
    cancel.clicked.connect(dialog.reject)
    row.addWidget(ok)
    row.addWidget(cancel)
    return row


class _CustomColorDialog(QDialog):
    """Small popup that lets the user enter a custom hex colour."""

    def __init__(self, parent=None, initial_hex: str = "#FF0000") -> None:
        super().__init__(parent)
        self.setWindowTitle("Custom Colour")
        self.setModal(True)
        _style_inputs(self, _DIALOG_CSS)

        layout = QVBoxLayout(self)
        ui_scale.margins(layout, 14, 12, 14, 12)
        ui_scale.spacing(layout, 8)
        layout.addWidget(QLabel("Enter hex colour (#RRGGBB):"))

        self._hex_edit = QLineEdit(initial_hex)
        ui_scale.min_width(self._hex_edit, 260)
        layout.addWidget(self._hex_edit)

        # Error label — hidden until validation fails
        self._error_lbl = QLabel("")
        self._error_lbl.setObjectName("error")
        self._error_lbl.setWordWrap(True)
        self._error_lbl.setVisible(False)
        layout.addWidget(self._error_lbl)

        layout.addLayout(_dialog_buttons(self))

    def accept(self) -> None:
        """Validate before closing — show an inline error and stay open if invalid."""
        hex_val = self.hex_value()
        if _HEX_COLOR.match(hex_val):
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
        self.setModal(True)
        _style_inputs(self, _DIALOG_CSS)

        self._width_spin = self._size_spin()
        self._height_spin = self._size_spin()

        layout = QVBoxLayout(self)
        ui_scale.margins(layout, 14, 12, 14, 12)
        ui_scale.spacing(layout, 10)
        layout.addLayout(_form_grid((("Width (cm):", self._width_spin),
                                     ("Height (cm):", self._height_spin)), spacing=8))
        layout.addLayout(_dialog_buttons(self))

    @staticmethod
    def _size_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(1.0, 10000.0)
        spin.setValue(100.0)
        spin.setSuffix(" cm")
        ui_scale.min_width(spin, 140)
        return spin

    def values(self) -> tuple[float, float]:
        """The entered (width, height) in cm."""
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
        # Paint the panel's own black background (dialogs opened from it keep theirs)
        self.setObjectName("barrierDrawerPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("QWidget#barrierDrawerPanel { background-color: black; }")

        # Barrier storage; artists are keyed by id() of their barrier/overlay,
        # so that two entries with the same name never share artists
        self._barriers: list[BarrierData] = []
        self._barrier_artists: dict[int, list] = {}   # id -> [shape, name label]
        self._load_error: str | None = None          # why barriers.toml could not be read

        # Drawing state machine
        self._drawing_mode = None  # None, "polygon", "circle", "line"
        self._polygon_vertices: list[tuple] = []
        self._circle_center = None
        self._line_point1 = None
        self._preview_artists: list = []

        # UI interaction state
        self._active: bool = False      # between on_panel_show and on_panel_hide
        self._current_mode: str = 'normal'
        self._has_unsaved_changes: bool = False
        self._prev_color_idx: int = 0  # for reverting if Custom dialog is cancelled

        # Drag-edit state
        self._drag_state = None
        self._handle_artists: list = []
        self._handle_meta: list = []  # parallel: ('vertex', idx) | ('centroid',) | ('center',) | ('radius',)

        # Ruler tool state
        self._ruler_mode: bool = False
        self._ruler_p1: tuple | None = None    # pending first point (before ruler drawn)
        self._ruler_p1_dot = None              # dot artist for pending first point
        self._ruler_endpoints: list = []       # [(x1,y1),(x2,y2)] for complete ruler
        self._ruler_artists: list = []         # [line, dot1, dot2, ann, dot_mid] for complete ruler

        # Image overlay state
        self._images: list[ImageOverlay] = []
        self._image_artists: dict[int, object] = {}   # id(overlay) -> AxesImage
        self._selected_image_idx: int = -1
        self._img_handle_artists: list = []
        self._img_handle_meta: list = []  # ('img_center',)|('img_corner',i)|('img_rotate',)

        # Canvas event connection IDs (connected while the panel is shown)
        self._cids: list[int] = []

        self._build_figure()
        self._build_ui()
        self._refresh_arena()

        main_window.config_changed.connect(self._on_config_changed)

    # ------------------------------------------------------------------
    # Figure
    # ------------------------------------------------------------------

    def _build_figure(self) -> None:
        """The arena figure on a canvas whose DPI follows its size."""
        self.fig = Figure(facecolor='black')
        self.fig.subplots_adjust(left=0.10, right=0.98, bottom=0.07, top=0.94)
        self.ax = self.fig.add_subplot(111)
        self._canvas = ui_scale.ScalingCanvas(self.fig, ref_size=CANVAS_REF_SIZE)
        # Take the keyboard focus on a click, so that Escape reaches the canvas
        # (an embedded matplotlib canvas takes no focus by itself)
        self._canvas.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        # Receivers, their outline, the origin and the compass rose.  The view
        # also turns autoscaling off, so barriers and images never move the
        # limits.  Its legend sits above the plot, right of the title, where it
        # covers neither a receiver nor a barrier.
        self._view = ArenaView(self.ax, title='Barrier Drawer', legend_loc='lower right',
                               legend_anchor=(1.0, 1.0), legend_ncol=2)

    def _refresh_arena(self) -> bool:
        """Draw the receivers from the settings and fit the view to them.

        Returns False (status set) if config.toml holds a receiver position
        that cannot be drawn.
        """
        ok = True
        try:
            # Only the active receivers (R1..RN, number_of_receivers) are drawn
            active = self._settings.number_of_receivers
            self._view.set_receivers([(rid, xy) for rid, xy in self._settings.get_tower_coordinates()
                                      if 0 <= int(rid) < active])
        except (TypeError, ValueError, IndexError) as exc:   # a malformed position in config.toml
            self._view.set_receivers([])
            self._set_status(f"Could not draw the receivers: {exc}", widgets.RED)
            ok = False
        self._view.set_limits()
        return ok

    # ------------------------------------------------------------------
    # Qt UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_tools())
        root.addWidget(_separator(QFrame.Shape.VLine))
        root.addLayout(self._build_centre(), stretch=1)
        root.addWidget(self._build_side_panel())

        # Controls that are editable when a barrier is selected (Type is always read-only)
        self._edit_controls = [
            self._trigger_combo, self._when_combo, self._color_combo,
            self._alpha_spin, self._name_edit, self._callback_edit,
        ]
        for w in self._edit_controls:
            w.setEnabled(False)
        self._type_combo.setEnabled(False)
        self._update_button_states('normal')

        self._barrier_list.currentRowChanged.connect(self._on_barrier_selected)
        self._image_list.currentRowChanged.connect(self._on_image_selected)

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

    def _build_tools(self) -> QWidget:
        """The tools sidebar (far left): ruler and name display settings."""
        tools = QWidget()
        ui_scale.fixed_width(tools, TOOLS_WIDTH)
        layout = QVBoxLayout(tools)
        ui_scale.margins(layout, 6, 8, 6, 8)
        ui_scale.spacing(layout, 8)

        layout.addWidget(_title("Tools", 12))

        self._tools_status_lbl = _status_label()
        ui_scale.min_height(self._tools_status_lbl, 75)
        layout.addWidget(self._tools_status_lbl)

        self._ruler_btn = widgets.neon_button(
            "Virtual Ruler", TOOL_BLUE, "white", font_px=13, padding=(5, 14))
        self._ruler_btn.clicked.connect(self._toggle_ruler)
        layout.addWidget(self._ruler_btn)

        # --- Settings sub-section ---
        layout.addWidget(_separator(
            QFrame.Shape.HLine, "color: #333333; margin-top: 4px; margin-bottom: 2px;"))
        layout.addWidget(_title("Settings", 10))

        # Two radios that are never both on: both off shows the selected
        # barrier's name only
        self._show_all_names_rb = QRadioButton("Show all names")
        self._show_all_names_rb.setChecked(True)
        self._show_all_names_rb.clicked.connect(self._on_show_all_names_clicked)
        self._no_names_rb = QRadioButton("No barrier names")
        self._no_names_rb.clicked.connect(self._on_no_names_clicked)
        for rb in (self._show_all_names_rb, self._no_names_rb):
            rb.setAutoExclusive(False)
            ui_scale.style(rb, _RADIO_CSS)
            layout.addWidget(rb)

        layout.addStretch()
        return tools

    def _build_centre(self) -> QVBoxLayout:
        """Title bar, the arena canvas and the drawing / saving buttons."""
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        # --- Top bar ---
        top_layout = QHBoxLayout()
        ui_scale.margins(top_layout, 10, 5, 10, 5)
        title_lbl = QLabel("Barrier Drawer")
        ui_scale.font(title_lbl, 16, bold=True)
        title_lbl.setStyleSheet(f"color: {ACCENT};")
        top_layout.addWidget(title_lbl)
        top_layout.addStretch()
        back_btn = widgets.neon_button("← Back", widgets.GREEN, font_px=14, padding=(6, 12))
        back_btn.clicked.connect(lambda: self._main_window.show_panel('main'))
        top_layout.addWidget(back_btn)
        col.addLayout(top_layout)

        # --- Canvas ---
        col.addWidget(self._canvas, stretch=1)

        # --- Action buttons ---
        row = QHBoxLayout()
        ui_scale.margins(row, 10, 5, 10, 5)
        ui_scale.spacing(row, 6)
        row.addStretch(1)

        def button(text, bg, fg, slot):
            btn = widgets.neon_button(text, bg, fg, font_px=13, padding=(5, 14))
            btn.clicked.connect(slot)
            row.addWidget(btn)
            return btn

        self._draw_btn = button("New Barrier", ACCENT, "black", self._start_drawing)
        self._finish_btn = button("Finish", widgets.GREEN, "black", self._finish_drawing)
        self._cancel_btn = button("Cancel", widgets.RED, "white", self._cancel_drawing)
        row.addWidget(_spacer(20))
        self._clear_changes_btn = button(
            "Clear All Changes", widgets.RED, "white", self._clear_all_changes)
        row.addWidget(_spacer(20))
        self._save_btn = button("Save All", widgets.GOLD, "black", self._save_barriers)
        row.addStretch(1)
        col.addLayout(row)
        return col

    def _build_side_panel(self) -> QWidget:
        """Barrier list, barrier properties, status line and image overlays,
        scrolling when the window is too small to show them all."""
        side = QWidget()
        # A scroll area fills its widget's background: make that black too
        side.setObjectName("barrierSidePanel")
        side.setStyleSheet("QWidget#barrierSidePanel { background-color: black; }")
        col = QVBoxLayout(side)
        ui_scale.margins(col, 5, 5, 5, 5)
        ui_scale.spacing(col, 6)

        # --- Barriers ---
        col.addWidget(_title("Barriers", 12))
        self._barrier_list = QListWidget()
        ui_scale.style(self._barrier_list, _LIST_CSS)
        ui_scale.min_height(self._barrier_list, 90)
        # The list takes whatever height is left; its default (unscaled) size
        # hint would make the panel scroll on a small window for no reason
        self._barrier_list.setSizePolicy(QSizePolicy.Policy.Expanding,
                                         QSizePolicy.Policy.Ignored)
        self._barrier_list.viewport().installEventFilter(self)
        col.addWidget(self._barrier_list, stretch=1)

        list_row = QHBoxLayout()
        ui_scale.spacing(list_row, 6)
        self._del_btn = widgets.neon_button(
            "Delete Barrier", widgets.RED, "white", font_px=12, padding=(5, 10))
        self._del_btn.clicked.connect(self._delete_selected)
        self._clear_btn = widgets.neon_button(
            "Clear All", NEUTRAL_GREY, "white", font_px=12, padding=(5, 10))
        self._clear_btn.clicked.connect(self._clear_all)
        list_row.addWidget(self._del_btn)
        list_row.addWidget(self._clear_btn)
        col.addLayout(list_row)

        # --- Properties of the barrier being drawn or selected ---
        col.addWidget(_title("Properties", 10))
        col.addWidget(self._build_properties())

        self._status_lbl = _status_label()
        ui_scale.min_height(self._status_lbl, 60)
        col.addWidget(self._status_lbl)

        # --- Images ---
        col.addWidget(_title("Images", 12))
        self._image_list = QListWidget()
        ui_scale.style(self._image_list, _LIST_CSS)
        ui_scale.max_height(self._image_list, 120)
        self._image_list.viewport().installEventFilter(self)
        col.addWidget(self._image_list)

        img_row = QHBoxLayout()
        ui_scale.spacing(img_row, 6)
        self._import_img_btn = widgets.neon_button(
            "Import", TOOL_BLUE, "white", font_px=12, padding=(5, 10))
        self._import_img_btn.clicked.connect(self._import_image)
        self._delete_img_btn = widgets.neon_button(
            "Delete", widgets.RED, "white", font_px=12, padding=(5, 10))
        self._delete_img_btn.setEnabled(False)
        self._delete_img_btn.clicked.connect(self._delete_image_selected)
        self._extract_img_btn = widgets.neon_button(
            "→ Barrier", widgets.GREEN, font_px=12, padding=(5, 10))
        self._extract_img_btn.setEnabled(False)
        self._extract_img_btn.clicked.connect(self._extract_image_as_barrier)
        for btn in (self._import_img_btn, self._delete_img_btn, self._extract_img_btn):
            img_row.addWidget(btn)
        col.addLayout(img_row)

        scroll = widgets.scroll_area(side)
        ui_scale.fixed_width(scroll, SIDE_PANEL_WIDTH)
        return scroll

    def _build_properties(self) -> QWidget:
        """Type, trigger, colour, opacity, name and callback fields."""
        self._type_combo = QComboBox()
        self._type_combo.addItems(list(_TYPE_NAMES.values()))
        self._trigger_combo = QComboBox()
        self._trigger_combo.addItems(list(_TRIGGER_MODES))
        self._when_combo = QComboBox()
        self._when_combo.addItems(list(_TRIGGER_WHEN))
        self._color_combo = QComboBox()
        for name, hex_value in PRESET_COLORS:
            self._color_combo.addItem(name, hex_value)
        self._color_combo.addItem("Custom", None)
        self._alpha_spin = QDoubleSpinBox()
        self._alpha_spin.setRange(0.0, 1.0)
        self._alpha_spin.setSingleStep(0.1)
        self._alpha_spin.setValue(0.3)
        self._name_edit = QLineEdit()
        self._callback_edit = QLineEdit()

        box = QWidget()
        _style_inputs(box, _FORM_CSS)
        box.setLayout(_form_grid((
            ("Type:", self._type_combo), ("Trigger:", self._trigger_combo),
            ("When:", self._when_combo), ("Color:", self._color_combo),
            ("Alpha:", self._alpha_spin), ("Name:", self._name_edit),
            ("Callback:", self._callback_edit))))
        return box

    def _set_status(self, text: str, color: str = "white") -> None:
        """Show a message under the barrier properties."""
        _set_label(self._status_lbl, text, color)

    def _set_tools_status(self, text: str, color: str = "white") -> None:
        """Show a message in the tools sidebar (ruler state and distance)."""
        _set_label(self._tools_status_lbl, text, color)

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

        # Selecting a barrier or an image (importing one selects it) would load
        # other values into, or blank, the fields of the barrier being drawn
        self._barrier_list.setEnabled(not drawing)
        self._image_list.setEnabled(not drawing)
        self._import_img_btn.setEnabled(not drawing)
        image_selected = 0 <= self._selected_image_idx < len(self._images)
        self._delete_img_btn.setEnabled(not drawing and image_selected)
        self._extract_img_btn.setEnabled(not drawing and image_selected)

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

    def _mark_unsaved(self) -> None:
        """Note that memory now differs from barriers.toml."""
        if not self._has_unsaved_changes:
            self._has_unsaved_changes = True
            self._update_unsaved_state()

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
        self._canvas.draw_idle()

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

    def _clear_all_changes(self) -> None:
        """Revert all in-memory barrier and image edits to the last saved file state."""
        if self._reload_from_disk(keep_row=self._barrier_list.currentRow()):
            self._set_status("All changes cleared — reverted to saved file", widgets.RED)

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
                    self._canvas.draw_idle()
                    return True
        return super().eventFilter(obj, event)

    def _deselect_barrier(self) -> None:
        self._drag_state = None
        self._clear_handles()
        self._barrier_list.clearSelection()
        self._barrier_list.setCurrentRow(-1)
        self._barrier_list.clearFocus()
        self._canvas.draw_idle()

    # ------------------------------------------------------------------
    # Handle rendering and drag editing
    # ------------------------------------------------------------------

    def _pick_radius(self) -> float:
        """``PICK_PX`` at the canvas's current scale, in display pixels (the
        pixels of matplotlib's mouse events: physical ones on a high-DPI
        screen), so that picking keeps pace with the markers."""
        ratio = float(getattr(self._canvas, 'device_pixel_ratio', 1) or 1)
        return PICK_PX * self._canvas.scale_factor * ratio

    def _hit_point(self, event, artists: list) -> 'int | None':
        """Index of the first one-point marker in *artists* within the pick
        radius of the mouse *event*, or None."""
        if event.x is None or event.y is None:
            return None
        radius = self._pick_radius()
        for i, artist in enumerate(artists):
            px, py = self.ax.transData.transform(
                (artist.get_xdata()[0], artist.get_ydata()[0]))
            if math.hypot(px - event.x, py - event.y) <= radius:
                return i
        return None

    def _draw_selected_handles(self, barrier: BarrierData) -> None:
        """Render draggable vertex/control handles for the selected barrier."""
        self._clear_handles()
        kw = dict(markersize=8, markeredgecolor='black', markeredgewidth=1.5, zorder=20)

        if barrier.barrier_type == BarrierType.POLYGON and barrier.vertices:
            for i, (vx, vy) in enumerate(barrier.vertices):
                h, = self.ax.plot(vx, vy, 'o', color='white', **kw)
                self._handle_artists.append(h)
                self._handle_meta.append(('vertex', i))
            cx = sum(v[0] for v in barrier.vertices) / len(barrier.vertices)
            cy = sum(v[1] for v in barrier.vertices) / len(barrier.vertices)
            h, = self.ax.plot(cx, cy, 'D', color=ACCENT, **kw)
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
                h, = self.ax.plot(mx, my, 'D', color=ACCENT, **kw)
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
        """Return index of the handle under the cursor, or None."""
        return self._hit_point(event, self._handle_artists)

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
            dist = math.hypot(px - x1 - t * dx, py - y1 - t * dy)
            return dist <= self._pick_radius()
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
                    h.set_xdata([cx])
                    h.set_ydata([cy])
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
            barrier.radius = max(1.0, math.hypot(abs_x - cx, abs_y - cy))

    def _apply_drag(self, event) -> None:
        """Apply the current drag delta and refresh."""
        state = self._drag_state
        if state is None or event.xdata is None or event.ydata is None:
            return

        dx = event.xdata - state['last_x']
        dy = event.ydata - state['last_y']
        state['last_x'] = event.xdata
        state['last_y'] = event.ydata
        if dx == 0 and dy == 0:
            return
        state['moved'] = True

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
                self._update_image_artist(img)
                self._update_image_handle_positions(img)
                self._canvas.draw_idle()
            return

        if state['type'] == 'img_handle':
            idx = self._selected_image_idx
            if 0 <= idx < len(self._images):
                img = self._images[idx]
                self._apply_image_handle_move(
                    img, state['handle_idx'], event.xdata, event.ydata, dx, dy)
                self._update_image_artist(img)
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

        self._remove_barrier_artists(barrier)
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
        self._set_status(f"Drawing {type_text} — click on canvas", ACCENT)
        self._update_button_states('drawing')
        self._canvas.draw_idle()

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
        else:
            self._color_combo.blockSignals(True)
            self._color_combo.setCurrentIndex(self._prev_color_idx)
            self._color_combo.blockSignals(False)
        dlg.deleteLater()

    def _on_type_combo_changed(self, text: str) -> None:
        if self._current_mode != 'drawing' or not text:
            return
        self._drawing_mode = text.lower()
        self._polygon_vertices = []
        self._circle_center = None
        self._line_point1 = None
        self._clear_preview()
        self._canvas.draw_idle()
        self._set_status(f"Drawing {self._drawing_mode} — click on canvas", ACCENT)

    def _finish_drawing(self) -> None:
        if self._drawing_mode == "polygon":
            self._finish_polygon()
        elif self._drawing_mode == "circle":
            if self._circle_center is not None:
                self._set_status("Click canvas to set radius", ACCENT)
        elif self._drawing_mode == "line":
            if self._line_point1 is not None:
                self._set_status("Click canvas to set endpoint", ACCENT)

    def _cancel_drawing(self) -> None:
        self._drawing_mode = None
        self._polygon_vertices = []
        self._circle_center = None
        self._line_point1 = None
        self._clear_preview()
        self._barrier_list.clearSelection()
        self._barrier_list.setCurrentRow(-1)
        self._set_status("Drawing cancelled", widgets.RED)
        self._update_button_states('normal')
        self._canvas.draw_idle()

    # ------------------------------------------------------------------
    # Canvas event handlers
    # ------------------------------------------------------------------

    def _on_canvas_click(self, event) -> None:
        if event.inaxes != self.ax or event.xdata is None:
            return
        if self._view.legend_contains(event):
            return      # the user is dragging the legend out of the way

        x, y = event.xdata, event.ydata

        # ---- drawing mode ------------------------------------------------
        if self._drawing_mode is not None:
            if self._drawing_mode == "polygon":
                if event.dblclick:
                    self._finish_polygon()
                    return
                self._polygon_vertices.append((x, y))
                self._update_polygon_preview()
                self._set_status(
                    f"Polygon: {len(self._polygon_vertices)} vertices (dbl-click to close)",
                    ACCENT)

            elif self._drawing_mode == "circle":
                if self._circle_center is None:
                    self._circle_center = (x, y)
                    dot = self.ax.plot(x, y, 'o', color=self._current_color(),
                                       markersize=6, zorder=10)
                    self._preview_artists.extend(dot)
                    self._canvas.draw_idle()
                    self._set_status("Circle: click to set radius", ACCENT)
                else:
                    self._finish_circle(x, y)

            elif self._drawing_mode == "line":
                if self._line_point1 is None:
                    self._line_point1 = (x, y)
                    dot = self.ax.plot(x, y, 'o', color=self._current_color(),
                                       markersize=6, zorder=10)
                    self._preview_artists.extend(dot)
                    self._canvas.draw_idle()
                    self._set_status("Line: click to set second endpoint", ACCENT)
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
            if self._hit_test_barrier_body(event, self._barriers[row]):
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
            if self._ruler_artists or self._ruler_p1 is None:
                # First point (a ruler already shown is replaced)
                self._clear_ruler()
                self._ruler_p1 = (x, y)
                self._ruler_p1_dot, = self.ax.plot(
                    x, y, 'o', color='#DDDDDD', markersize=7, zorder=31)
                self._canvas.draw_idle()
                self._set_tools_status("Ruler: click second point", ACCENT)
            else:
                # Second point — draw the ruler
                self._draw_ruler(self._ruler_p1, (x, y))
                self._ruler_p1 = None

    def _on_canvas_release(self, event) -> None:
        state, self._drag_state = self._drag_state, None
        # The ruler is not saved, so moving it changes nothing on disk
        if (state is not None and state.get('moved')
                and state['type'] not in ('ruler_endpoint', 'ruler_midpoint')):
            self._mark_unsaved()

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
                r = math.hypot(x - cx, y - cy)
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
            self._set_status("Polygon needs at least 3 vertices", widgets.RED)
            return

        barrier = self._make_barrier_from_controls(BarrierType.POLYGON)
        barrier.vertices = list(self._polygon_vertices)
        self._add_drawn_barrier(barrier)

    def _finish_circle(self, x: float, y: float) -> None:
        cx, cy = self._circle_center
        radius = math.hypot(x - cx, y - cy)
        if radius <= 0:
            self._set_status("Circle radius must be > 0", widgets.RED)
            return

        barrier = self._make_barrier_from_controls(BarrierType.CIRCLE)
        barrier.center = (cx, cy)
        barrier.radius = radius
        self._add_drawn_barrier(barrier)

    def _finish_line(self, x: float, y: float) -> None:
        p1 = self._line_point1
        p2 = (x, y)
        if p1 == p2:
            self._set_status("Line endpoints must differ", widgets.RED)
            return

        barrier = self._make_barrier_from_controls(BarrierType.LINE)
        barrier.point1 = p1
        barrier.point2 = p2
        barrier.thickness = 5.0  # default thickness in cm
        self._add_drawn_barrier(barrier)

    def _add_drawn_barrier(self, barrier: BarrierData) -> None:
        """Validate a finished drawing, then add, draw and save it."""
        ok, msg = validate_barrier(barrier)
        if not ok:
            self._set_status(f"Invalid: {msg}", widgets.RED)
            return

        self._barriers.append(barrier)
        self._clear_preview()
        self._drawing_mode = None
        self._polygon_vertices = []
        self._circle_center = None
        self._line_point1 = None
        self._draw_barrier(barrier)
        self._refresh_barrier_list()
        if self._write_barriers_file():
            self._set_status(
                f"Added and saved {barrier.barrier_type.value} '{barrier.name}'", widgets.GREEN)
        self._update_button_states('normal')
        self._canvas.draw_idle()

    def _make_barrier_from_controls(self, btype: BarrierType) -> BarrierData:
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
            trigger_mode=_TRIGGER_MODES[self._trigger_combo.currentText()],
            trigger_when=_TRIGGER_WHEN[self._when_combo.currentText()],
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
        try:
            mcolors.to_rgba(barrier.color)
            color = barrier.color
        except (ValueError, TypeError):
            color = '#FFFFFF'
        alpha = barrier.alpha
        # Names are shown as typed: '$' starts no maths ('a_$b_$' would not
        # parse, and the whole figure would fail to draw)
        label_kw = dict(color='white', fontsize=8, fontweight='bold', parse_math=False,
                        ha='center', va='center', zorder=9,
                        bbox=dict(boxstyle='round,pad=0.2', facecolor='black',
                                  alpha=0.7, edgecolor=color))

        if barrier.barrier_type == BarrierType.POLYGON and barrier.vertices:
            verts = [(v[0], v[1]) for v in barrier.vertices]
            patch = MplPolygon(verts, closed=True, facecolor=color,
                               alpha=alpha, edgecolor=color, linewidth=2, zorder=8)
            self.ax.add_patch(patch)
            artists.append(patch)
            cx = sum(v[0] for v in verts) / len(verts)
            cy = sum(v[1] for v in verts) / len(verts)
            artists.append(self.ax.text(cx, cy, barrier.name, **label_kw))

        elif barrier.barrier_type == BarrierType.CIRCLE and barrier.center:
            patch = MplCircle(barrier.center, barrier.radius, facecolor=color,
                              alpha=alpha, edgecolor=color, linewidth=2, zorder=8)
            self.ax.add_patch(patch)
            artists.append(patch)
            artists.append(self.ax.text(barrier.center[0], barrier.center[1],
                                        barrier.name, **label_kw))

        elif barrier.barrier_type == BarrierType.LINE and barrier.point1 and barrier.point2:
            lw = max(2, (barrier.thickness or 5.0) * 0.5)
            ln, = self.ax.plot(
                [barrier.point1[0], barrier.point2[0]],
                [barrier.point1[1], barrier.point2[1]],
                color=color, linewidth=lw, alpha=max(alpha, 0.6), zorder=8)
            artists.append(ln)
            mx = (barrier.point1[0] + barrier.point2[0]) / 2
            my = (barrier.point1[1] + barrier.point2[1]) / 2
            artists.append(self.ax.text(mx, my, barrier.name, **label_kw))

        if len(artists) == 2:
            artists[1].set_visible(self._name_visible(barrier))
        self._barrier_artists[id(barrier)] = artists

    def _remove_barrier_artists(self, barrier: BarrierData) -> None:
        for artist in self._barrier_artists.pop(id(barrier), []):
            try:
                artist.remove()
            except (ValueError, AttributeError):
                pass

    def _remove_all_barrier_artists(self) -> None:
        for artists in self._barrier_artists.values():
            for artist in artists:
                try:
                    artist.remove()
                except (ValueError, AttributeError):
                    pass
        self._barrier_artists.clear()

    # ------------------------------------------------------------------
    # Barrier list widget
    # ------------------------------------------------------------------

    @staticmethod
    def _list_text(barrier: BarrierData) -> str:
        return f"{barrier.name} ({barrier.barrier_type.value}, {barrier.trigger_mode.value})"

    def _refresh_barrier_list(self) -> None:
        self._barrier_list.clear()
        for barrier in self._barriers:
            self._barrier_list.addItem(self._list_text(barrier))

    def _delete_selected(self) -> None:
        row = self._barrier_list.currentRow()
        if row < 0 or row >= len(self._barriers):
            return
        barrier = self._barriers[row]
        if not widgets.confirm(self, "Delete Barrier", f"Delete barrier '{barrier.name}'?",
                               ok_text="Delete", danger=True):
            return
        self._remove_barrier_artists(barrier)
        self._barriers.pop(row)
        self._has_unsaved_changes = True
        self._refresh_barrier_list()
        self._update_button_states('normal')
        self._canvas.draw_idle()
        self._set_status(f"Deleted '{barrier.name}'", widgets.RED)

    def _load_barrier_into_controls(self, barrier: BarrierData) -> None:
        _edit_widgets = (
            self._type_combo, self._trigger_combo, self._when_combo,
            self._color_combo, self._alpha_spin,
            self._name_edit, self._callback_edit,
        )
        for w in _edit_widgets:
            w.blockSignals(True)

        trigger_names = {mode: name for name, mode in _TRIGGER_MODES.items()}
        when_names = {when: name for name, when in _TRIGGER_WHEN.items()}
        self._type_combo.setCurrentText(_TYPE_NAMES.get(barrier.barrier_type, "Polygon"))
        self._trigger_combo.setCurrentText(trigger_names.get(barrier.trigger_mode, "Event"))
        self._when_combo.setCurrentText(when_names.get(barrier.trigger_when, "Inside"))

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
        t = self._trigger_combo.currentText()
        barrier.trigger_mode  = _TRIGGER_MODES.get(t, barrier.trigger_mode)
        w = self._when_combo.currentText()
        barrier.trigger_when  = _TRIGGER_WHEN.get(w, barrier.trigger_when)
        barrier.color         = self._current_color()
        barrier.alpha         = self._alpha_spin.value()
        barrier.callback_name = self._callback_edit.text().strip() or "on_barrier"

        # Names identify barriers (events, the position panel's state), so a
        # name another barrier already has is not taken over
        new_name = self._name_edit.text().strip() or barrier.name
        if new_name != barrier.name and any(
                other is not barrier and other.name == new_name for other in self._barriers):
            self._set_status(f"Another barrier is already called '{new_name}'", widgets.RED)
        else:
            barrier.name = new_name

        # Update list item text directly to avoid clearing the selection
        item = self._barrier_list.item(row)
        if item:
            item.setText(self._list_text(barrier))

        self._remove_barrier_artists(barrier)
        self._draw_barrier(barrier)
        self._canvas.draw_idle()
        self._mark_unsaved()

    # ------------------------------------------------------------------
    # Save / Load / Clear
    # ------------------------------------------------------------------

    def _get_barriers_path(self) -> str:
        return os.path.join(self._settings.config_dir, 'barriers.toml')

    def _write_barriers_file(self) -> bool:
        """Write the barriers and images in memory to barriers.toml.

        Returns whether it was written; on failure the status line says why
        and the changes stay marked as unsaved.  A file that could not be read
        is only replaced after the user agrees, since whatever it holds is lost.
        """
        path = self._get_barriers_path()
        if self._load_error is not None and os.path.exists(path):
            if not widgets.confirm(
                    self, "Replace barriers.toml?",
                    f"{path} could not be read ({self._load_error}).\n\n"
                    "Saving replaces it with the barriers and images shown here; "
                    "anything else it holds is lost.",
                    ok_text="Replace", danger=True):
                self._set_status("Not saved: barriers.toml was left as it is", widgets.RED)
                self._mark_unsaved()
                return False
        try:
            save_all(path, self._barriers, self._images)
        except (OSError, ValueError) as exc:     # ValueError: text UTF-8 cannot encode
            self._set_status(f"Could not save {path}: {exc}", widgets.RED)
            self._mark_unsaved()
            return False
        self._load_error = None
        self._has_unsaved_changes = False
        self._update_unsaved_state()
        return True

    def _save_barriers(self) -> None:
        if self._write_barriers_file():
            self._set_status(
                f"Saved {len(self._barriers)} barriers, {len(self._images)} images", widgets.GOLD)

    def _reload_from_disk(self, keep_row: int = -1) -> bool:
        """Replace the barriers and images in memory (and on the canvas) with
        the ones in barriers.toml, selecting barrier *keep_row* again if it
        still exists.

        Returns False (status set, nothing shown) if the file could not be
        read; saving then asks before replacing it.
        """
        path = self._get_barriers_path()
        try:
            barriers = load_barriers(path)
            images = load_images(path)
            problem = _section_problem(path)
            if problem is not None:
                raise ValueError(problem)
        except (OSError, ValueError, TypeError) as exc:
            # tomllib.TOMLDecodeError is a ValueError
            self._load_error = str(exc)
            barriers, images = [], []
        else:
            self._load_error = None

        self._drag_state = None
        self._clear_handles()
        self._deselect_image()
        self._remove_all_barrier_artists()
        self._remove_all_image_artists()
        self._barriers = barriers
        self._images = images
        self._draw_all_barriers()
        self._draw_all_images()
        self._refresh_barrier_list()
        self._refresh_image_list()
        self._has_unsaved_changes = False
        # Restore selection so controls reload
        if 0 <= keep_row < len(self._barriers):
            self._barrier_list.setCurrentRow(keep_row)
        else:
            self._blank_edit_controls()
        self._update_button_states('normal')
        self._canvas.draw_idle()

        if self._load_error is not None:
            self._set_status(
                f"Could not read barriers.toml: {self._load_error}", widgets.RED)
            return False
        return True

    def _clear_all(self) -> None:
        if not self._barriers:
            return
        if not widgets.confirm(self, "Clear All Barriers",
                               f"Remove all {len(self._barriers)} barriers?",
                               ok_text="Remove All", danger=True):
            return

        self._remove_all_barrier_artists()
        self._barriers.clear()
        self._has_unsaved_changes = True
        self._refresh_barrier_list()
        self._update_button_states('normal')
        self._canvas.draw_idle()
        self._set_status("All barriers cleared", "white")

    # ------------------------------------------------------------------
    # Image overlay rendering & editing
    # ------------------------------------------------------------------

    def _get_resources_dir(self) -> str:
        return os.path.join(self._settings.config_dir, 'resources')

    def _draw_image(self, overlay: ImageOverlay) -> None:
        """Render (or re-render) a single image overlay on the canvas."""
        self._remove_image_artist(overlay)
        geometry = (overlay.center_x, overlay.center_y, overlay.width_cm,
                    overlay.height_cm, overlay.rotation_deg)
        if not all(math.isfinite(value) for value in geometry):
            return      # a nan or inf in barriers.toml: there is nowhere to put it
        filepath = os.path.join(self._get_resources_dir(), overlay.filename)
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

        # A greyscale image is shown as grey, over its type's full range (not
        # through a colour map stretched to its own darkest and lightest pixel)
        grey = {}
        if img_data.ndim == 2:
            top = 1.0 if img_data.dtype.kind == 'f' else np.iinfo(img_data.dtype).max
            grey = dict(cmap='gray', vmin=0.0, vmax=top)

        # Do NOT pass aspect= — imshow(aspect='auto') calls ax.set_aspect('auto')
        # internally which overrides the arena's equal aspect ratio.
        im = self.ax.imshow(
            img_data,
            origin='upper',
            zorder=4,
            interpolation='bilinear',
            **grey,
        )
        self._place_image(im, overlay)
        self._image_artists[id(overlay)] = im

        # Hard-restore view limits — never let image drawing affect the arena
        self.ax.set_xlim(xlim)
        self.ax.set_ylim(ylim)

    def _place_image(self, im, overlay: ImageOverlay) -> None:
        """Put image artist *im* where *overlay* says: its extent at the
        overlay's arena coordinates, rotated about its centre."""
        half_w = overlay.width_cm / 2.0
        half_h = overlay.height_cm / 2.0
        cx, cy = overlay.center_x, overlay.center_y
        im.set_extent((cx - half_w, cx + half_w, cy - half_h, cy + half_h))
        # Rotation only — translation is already encoded in the extent
        im.set_transform(
            Affine2D().rotate_around(cx, cy, math.radians(overlay.rotation_deg))
            + self.ax.transData)

    def _update_image_artist(self, overlay: ImageOverlay) -> None:
        """Move an overlay's drawn image after a drag (without re-reading it)."""
        im = self._image_artists.get(id(overlay))
        if im is None:
            self._draw_image(overlay)
        else:
            self._place_image(im, overlay)

    def _remove_image_artist(self, overlay: ImageOverlay) -> None:
        artist = self._image_artists.pop(id(overlay), None)
        if artist is not None:
            try:
                artist.remove()
            except (ValueError, AttributeError):
                pass

    def _remove_all_image_artists(self) -> None:
        for artist in self._image_artists.values():
            try:
                artist.remove()
            except (ValueError, AttributeError):
                pass
        self._image_artists.clear()

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
            self._canvas.draw_idle()
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
        self._update_button_states('normal')

        self._selected_image_idx = idx
        self._image_list.blockSignals(True)
        self._image_list.setCurrentRow(idx)
        self._image_list.blockSignals(False)
        self._delete_img_btn.setEnabled(True)
        self._extract_img_btn.setEnabled(True)
        if 0 <= idx < len(self._images):
            self._draw_image_handles(self._images[idx])
        self._canvas.draw_idle()

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
        kw = dict(markersize=9, markeredgecolor='black', markeredgewidth=1.5, zorder=25)

        # Center handle
        h, = self.ax.plot(overlay.center_x, overlay.center_y, 'D', color=ACCENT, **kw)
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
        for h, meta in zip(self._img_handle_artists, self._img_handle_meta):
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
        if self._selected_image_idx < 0:
            return None
        return self._hit_point(event, self._img_handle_artists)

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
        """Ask for an image file and its real-world size, then pin it to the arena."""
        path, _ = QFileDialog.getOpenFileName(self, "Import Image", "", IMAGE_FILTER)
        if not path:
            return
        dlg = _ImportImageDialog(self)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        width_cm, height_cm = dlg.values()
        dlg.deleteLater()
        if accepted:
            self._add_image(path, width_cm, height_cm)

    def _add_image(self, path: str, width_cm: float, height_cm: float) -> None:
        """Copy the image at *path* into resources/ and pin it, *width_cm* x
        *height_cm*, to the centre of the visible arena; then save."""
        try:
            mpl_image.imread(path)      # refuse a file that cannot be shown
        except Exception as exc:
            self._set_status(f"Could not read image '{os.path.basename(path)}': {exc}",
                             widgets.RED)
            return

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
        base_name = os.path.basename(path)
        dest = os.path.join(resources_dir, base_name)
        try:
            os.makedirs(resources_dir, exist_ok=True)
            if os.path.abspath(path) != os.path.abspath(dest):
                # Avoid name collision
                stem, ext = os.path.splitext(base_name)
                counter = 1
                while os.path.exists(dest):
                    dest = os.path.join(resources_dir, f"{stem}_{counter}{ext}")
                    counter += 1
                shutil.copy2(path, dest)
        except OSError as exc:
            self._set_status(f"Could not copy the image into {resources_dir}: {exc}",
                             widgets.RED)
            return

        # Unique name
        filename = os.path.basename(dest)
        stem = os.path.splitext(filename)[0]
        existing_names = {img.name for img in self._images}
        name = stem
        counter = 2
        while name in existing_names:
            name = f"{stem}_{counter}"
            counter += 1

        # Place image at centre of visible arena
        overlay = ImageOverlay(
            name=name,
            filename=filename,
            center_x=(x_range[0] + x_range[1]) / 2.0,
            center_y=(y_range[0] + y_range[1]) / 2.0,
            width_cm=width_cm,
            height_cm=height_cm,
            rotation_deg=0.0,
        )
        self._images.append(overlay)
        self._draw_image(overlay)
        self._refresh_image_list()
        self._select_image(len(self._images) - 1)
        if self._write_barriers_file():
            if clamped:
                self._set_status(
                    f"'{name}' imported — dimensions too large, "
                    f"scaled to {width_cm:.0f}×{height_cm:.0f} cm", ACCENT)
            else:
                self._set_status(
                    f"Imported '{name}' ({width_cm:.0f}×{height_cm:.0f} cm)", widgets.GREEN)
        self._canvas.draw_idle()

    def _delete_image_selected(self) -> None:
        idx = self._selected_image_idx
        if idx < 0 or idx >= len(self._images):
            return
        overlay = self._images[idx]
        if not widgets.confirm(self, "Delete Image",
                               f"Remove image '{overlay.name}' from canvas?\n"
                               f"(The file in resources/ will not be deleted.)",
                               ok_text="Remove", danger=True):
            return
        self._remove_image_artist(overlay)
        self._images.pop(idx)
        self._deselect_image()
        self._refresh_image_list()
        if self._write_barriers_file():
            self._set_status(f"Removed image '{overlay.name}'", widgets.RED)
        self._canvas.draw_idle()

    def _extract_image_as_barrier(self) -> None:
        idx = self._selected_image_idx
        if idx < 0 or idx >= len(self._images):
            return
        overlay = self._images[idx]
        self._set_status("Extracting polygon…", ACCENT)

        try:
            verts = polygon_from_image(overlay, self._get_resources_dir())
        except Exception as exc:
            self._set_status(f"Extraction failed: {exc}", widgets.RED)
            return

        if len(verts) < 3:
            self._set_status("Could not extract enough vertices", widgets.RED)
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
            color=ACCENT,
            alpha=0.3,
            vertices=verts,
        )
        self._barriers.append(barrier)
        self._draw_barrier(barrier)
        self._refresh_barrier_list()
        if self._write_barriers_file():
            self._set_status(f"Extracted '{name}' ({len(verts)} vertices)", widgets.GREEN)
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

    def _name_visible(self, barrier: BarrierData) -> bool:
        """Whether *barrier*'s name label is shown in the current mode."""
        mode = self._name_display_mode()
        if mode == 'selected':
            row = self._barrier_list.currentRow()
            return 0 <= row < len(self._barriers) and self._barriers[row] is barrier
        return mode == 'all'

    def _update_name_visibility(self) -> None:
        """Apply current name-display mode to all drawn barrier label artists."""
        for barrier in self._barriers:
            artists = self._barrier_artists.get(id(barrier), [])
            if len(artists) >= 2:
                artists[1].set_visible(self._name_visible(barrier))
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
            self._set_tools_status("Ready")
        else:
            self._ruler_mode = True
            self._ruler_btn.setText("Stop Ruler")
            if not self._ruler_artists:
                self._set_tools_status("Virtual ruler active", ACCENT)

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
        dist = math.hypot(x2 - x1, y2 - y1)
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
        self._set_tools_status(f"Distance:\n{dist:.1f} cm", widgets.GREEN)

    def _redraw_ruler(self) -> None:
        """Update ruler artists in-place after an endpoint or midpoint is dragged."""
        if len(self._ruler_artists) < 5 or len(self._ruler_endpoints) < 2:
            return
        (x1, y1), (x2, y2) = self._ruler_endpoints
        dist = math.hypot(x2 - x1, y2 - y1)
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2

        line, dot1, dot2, ann, dot_mid = self._ruler_artists
        line.set_xdata([x1, x2])
        line.set_ydata([y1, y2])
        dot1.set_xdata([x1])
        dot1.set_ydata([y1])
        dot2.set_xdata([x2])
        dot2.set_ydata([y2])
        dot_mid.set_xdata([mx])
        dot_mid.set_ydata([my])
        ann.xy = (mx, my)
        ann.set_text(f"{dist:.1f} cm")
        self._canvas.draw_idle()
        self._set_tools_status(f"Distance:\n{dist:.1f} cm", widgets.GREEN)

    def _hit_test_ruler_endpoints(self, event) -> 'int | None':
        """Return 0 or 1 if the cursor is on a ruler endpoint, else None."""
        if len(self._ruler_artists) < 3:
            return None
        return self._hit_point(event, self._ruler_artists[1:3])   # dot1, dot2

    def _hit_test_ruler_midpoint(self, event) -> bool:
        """Return True if the cursor is on the ruler midpoint dot."""
        if len(self._ruler_artists) < 5:
            return False
        return self._hit_point(event, self._ruler_artists[4:5]) is not None

    def _on_canvas_key(self, event) -> None:
        """Handle Escape: cancel a pending ruler first-point."""
        if event.key == 'escape' and self._ruler_p1 is not None and not self._ruler_artists:
            self._clear_ruler_p1()
            self._canvas.draw_idle()
            if self._ruler_mode:
                self._set_tools_status("Virtual ruler active", ACCENT)

    # ------------------------------------------------------------------
    # Panel lifecycle
    # ------------------------------------------------------------------

    def _reset_interaction(self) -> None:
        """Drop whatever is in progress: a drawing, a drag, the ruler, the
        handles and the image selection."""
        self._drawing_mode = None
        self._polygon_vertices = []
        self._circle_center = None
        self._line_point1 = None
        self._drag_state = None
        self._clear_preview()
        if self._ruler_mode:
            self._toggle_ruler()        # leaves ruler mode and removes the ruler
        else:
            self._clear_ruler_p1()
            self._clear_ruler()
        self._clear_handles()
        self._deselect_image()
        self._update_button_states('normal')

    def _on_config_changed(self) -> None:
        """Follow a saved configuration change (receivers moved, origin shifted).

        An origin shift has translated barriers.toml on disk, so the barriers
        and images in memory are replaced by the file's: saving the old ones
        would put the old frame back.  Unsaved edits are dropped.
        """
        dropped = self._has_unsaved_changes
        if self._active:
            self._reset_interaction()
        arena_ok = self._refresh_arena()
        if self._reload_from_disk() and arena_ok and self._active:
            note = " (unsaved edits were discarded)" if dropped else ""
            self._set_status(f"Arena settings changed — barriers reloaded{note}", ACCENT)

    def on_panel_show(self) -> None:
        """Redraw the receivers, reload barriers.toml and listen to the canvas."""
        self._active = True
        arena_ok = self._refresh_arena()
        if self._reload_from_disk() and arena_ok:
            self._set_status("Ready")
        if not self._cids:
            connect = self._canvas.mpl_connect
            self._cids = [
                connect('button_press_event', self._on_canvas_click),
                connect('button_release_event', self._on_canvas_release),
                connect('motion_notify_event', self._on_canvas_motion),
                connect('key_press_event', self._on_canvas_key),
            ]

    def on_panel_hide(self) -> None:
        """Stop listening to the canvas and drop whatever is in progress
        (unsaved edits stay in memory until the next show reloads the file)."""
        self._active = False
        for cid in self._cids:
            self._canvas.mpl_disconnect(cid)
        self._cids = []
        self._reset_interaction()
