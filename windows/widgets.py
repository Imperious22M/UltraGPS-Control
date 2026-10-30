"""Shared widgets and dialogs for the panels.

* ``neon_button``          the application's flat neon push button.
* ``Section`` / ``section``  a titled, framed group of controls, optionally collapsible.
* ``progress_bar``         a slim progress bar / busy indicator.
* ``scroll_area``          a dark scroll area for side panels on small windows.
* ``info``, ``warning``, ``error``, ``confirm``
                           dark message dialogs that size to their text.
* ``ask_point`` / ``PointDialog``
                           ask for a point as X/Y in the current frame, as an
                           offset from a reference point, or as a distance and
                           angle from it.
* ``PointTable``           an editable list of points (P1.., R1..) with
                           validation, "Offset..." and "Pick on arena".
* ``OriginTool``           choose a new (0, 0): pick it on the arena, type it,
                           offset it from a reference, or measure it with the
                           transmitter.
* ``Worker`` / ``run_in_thread``
                           run a callable off the GUI thread and get its
                           result back on the GUI thread.

Every size goes through ``windows.ui_scale`` (style sheets, fonts, minimum
widths), so all of it follows the window size; no dialog has a fixed size.
Coordinates are arena centimetres.
"""

from __future__ import annotations

import math
import threading
import time

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QAbstractScrollArea, QButtonGroup, QComboBox, QDialog, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit, QProgressBar,
    QPushButton, QRadioButton, QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from windows import ui_scale

GREEN = "#39FF14"
MAGENTA = "#FF00FF"
CYAN = "#00FFFF"
ORANGE = "#FF8800"
GOLD = "#FFD700"
RED = "#FF4444"
BLUE = "#0080FF"
AMBER = "#FFB347"
DIM = "#AAAAAA"

#: A typed coordinate farther out than this (1 km) is refused as a typo.
MAX_COORD_CM = 1e5

#: Name of the arena origin in reference lists.
ORIGIN_REF = "Origin (0, 0)"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _shade(color: str, k: float) -> str:
    """``color`` (#RRGGBB) with each channel multiplied by ``k``."""
    c = QColor(color)
    return QColor(min(255, int(c.red() * k)), min(255, int(c.green() * k)),
                  min(255, int(c.blue() * k))).name().upper()


def parse_number(text: str, limit: float = MAX_COORD_CM) -> float | None:
    """``text`` as a finite number within +/- ``limit``, or None."""
    try:
        value = float(str(text).strip())
    except ValueError:
        return None
    if not math.isfinite(value) or abs(value) > limit:
        return None
    return value


def format_number(value: float, decimals: int = 3) -> str:
    """``value`` with up to ``decimals`` decimals and no trailing zeros."""
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def point_from_offset(ref, dx: float, dy: float) -> tuple[float, float]:
    """The point ``(dx, dy)`` away from ``ref``."""
    return _clean(ref[0] + dx), _clean(ref[1] + dy)


def point_from_polar(ref, distance: float, angle_deg: float) -> tuple[float, float]:
    """The point ``distance`` cm from ``ref`` at ``angle_deg`` (0 = +X,
    counter-clockwise)."""
    t = math.radians(angle_deg)
    return _clean(ref[0] + distance * math.cos(t)), _clean(ref[1] + distance * math.sin(t))


def polar_from_points(ref, point) -> tuple[float, float]:
    """``(distance, angle_deg)`` of ``point`` seen from ``ref``; the angle is in
    [0, 360), 0 = +X, counter-clockwise (0 when the points coincide)."""
    dx, dy = point[0] - ref[0], point[1] - ref[1]
    distance = math.hypot(dx, dy)
    angle = math.degrees(math.atan2(dy, dx)) % 360.0 if distance > 0 else 0.0
    return _clean(distance), _clean(angle % 360.0)


def _clean(value: float) -> float:
    """Round away floating-point dust (1e-6 cm) and negative zero."""
    value = round(float(value), 6)
    return 0.0 if value == 0 else value


def _input_css(accent: str) -> str:
    """Style sheet for a container of labels, line edits, combo boxes and
    radio buttons (cascades to its children)."""
    return f"""
        QLabel {{ color: white; font: 12px Arial; background: transparent; }}
        QLineEdit {{ background-color: #222222; color: white; border: 1px solid #444444;
                     border-radius: 3px; padding: 3px 6px; font: 12px Arial; }}
        QLineEdit:focus {{ border-color: {accent}; }}
        QLineEdit:disabled {{ background-color: #151515; color: #555555; border-color: #2A2A2A; }}
        QComboBox {{ background-color: #222222; color: white; border: 1px solid #444444;
                     border-radius: 3px; padding: 3px 6px; font: 12px Arial; }}
        QComboBox:disabled {{ background-color: #151515; color: #555555; }}
        QComboBox QAbstractItemView {{ background-color: #222222; color: white;
                     selection-background-color: {accent}; selection-color: black; }}
        QRadioButton {{ color: white; font: 12px Arial; spacing: 6px; background: transparent; }}
        QRadioButton::indicator {{ width: 13px; height: 13px; border-radius: 7px; }}
        QRadioButton::indicator:checked {{ background-color: {accent}; border: 2px solid {accent}; }}
        QRadioButton::indicator:unchecked {{ background-color: #222222; border: 2px solid #666666; }}
        QPlainTextEdit {{ background-color: #111111; color: white; border: 1px solid #333333;
                          font: 11px monospace; }}
    """


_INVALID_EDIT_CSS = "QLineEdit { border: 1px solid #FF4444; }"


def _mark_invalid(edit: QLineEdit, invalid: bool) -> None:
    """Red border on an input that does not hold a valid value."""
    ui_scale.style(edit, _INVALID_EDIT_CSS if invalid else "")


def _label(text: str = "", color: str = "white", px: int = 12, bold: bool = False,
           wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    ui_scale.style(lbl, f"color: {color}; font: {'bold ' if bold else ''}{px}px Arial; "
                        f"background: transparent;")
    if wrap:
        lbl.setWordWrap(True)
    return lbl


# ---------------------------------------------------------------------------
# Buttons and sections
# ---------------------------------------------------------------------------

def neon_button(text: str, bg: str = GREEN, fg: str = "black", hover: str | None = None,
                font_px: int = 12, padding=(5, 10), radius: int = 4, checkable: bool = False,
                tooltip: str | None = None, parent=None) -> QPushButton:
    """A flat push button in one of the neon colours (today's look), scaled
    with the UI.  ``padding`` is (vertical, horizontal) pixels or a CSS string.

    A checkable button shows its checked state inverted (black face, coloured
    text and border).
    """
    btn = QPushButton(text, parent)
    hover = hover or _shade(bg, 0.8)
    disabled = _shade(bg, 0.35)
    pad = padding if isinstance(padding, str) else f"{padding[0]}px {padding[1]}px"
    border = f"border: 2px solid {bg};" if checkable else "border: none;"
    css = f"""
        QPushButton {{ background-color: {bg}; color: {fg}; font: bold {font_px}px Arial;
                      padding: {pad}; border-radius: {radius}px; {border} }}
        QPushButton:hover {{ background-color: {hover}; }}
        QPushButton:disabled {{ background-color: {disabled}; color: #666666; }}
    """
    if checkable:
        btn.setCheckable(True)
        # Disabled, checked or not, the border dims too (a bright one looks active)
        css += f"""
            QPushButton:checked {{ background-color: black; color: {bg}; border: 2px solid {bg}; }}
            QPushButton:checked:hover {{ background-color: #1A1A1A; }}
            QPushButton:disabled {{ border-color: {disabled}; }}
            QPushButton:checked:disabled {{ background-color: black; color: {disabled};
                                           border-color: {disabled}; }}
        """
    ui_scale.style(btn, css)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


class Section(QFrame):
    """A framed group of controls under a bold coloured title.

    Add controls to ``body`` (a QVBoxLayout) or with ``add`` / ``add_layout``.
    A collapsible section's title is a button that shows and hides the body;
    ``toggled(expanded)`` is emitted when it does.
    """

    toggled = pyqtSignal(bool)

    def __init__(self, title: str, color: str = CYAN, collapsible: bool = False,
                 expanded: bool = True, parent=None):
        super().__init__(parent)
        self._title = title
        self._collapsible = collapsible
        self.setObjectName("uiSection")
        ui_scale.style(self, "QFrame#uiSection { background-color: #0D0D0D; "
                             "border: 1px solid #333333; border-radius: 6px; }")

        outer = QVBoxLayout(self)
        ui_scale.margins(outer, 8, 6, 8, 8)
        ui_scale.spacing(outer, 6)

        if collapsible:
            self._header = QPushButton()
            self._header.setCheckable(True)
            self._header.setChecked(expanded)
            self._header.setCursor(Qt.CursorShape.PointingHandCursor)
            self._header.toggled.connect(self.set_expanded)
            ui_scale.style(self._header, f"""
                QPushButton {{ color: {color}; font: bold 13px Arial; background: transparent;
                              border: none; text-align: left; padding: 0px; }}
                QPushButton:hover {{ color: white; }}
            """)
        else:
            self._header = QLabel()
            ui_scale.style(self._header, f"color: {color}; font: bold 13px Arial; "
                                         f"background: transparent; border: none;")
        outer.addWidget(self._header)

        self._content = QWidget()
        self._content.setObjectName("uiSectionBody")
        ui_scale.style(self._content, "QWidget#uiSectionBody { background: transparent; }")
        self.body = QVBoxLayout(self._content)
        self.body.setContentsMargins(0, 0, 0, 0)
        ui_scale.spacing(self.body, 6)
        outer.addWidget(self._content)

        self._expanded = bool(expanded)
        self._content.setVisible(self._expanded)
        self._update_title()

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        """Add ``widget`` to the body; returns it."""
        self.body.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self.body.addLayout(layout)

    def set_title(self, title: str) -> None:
        self._title = title
        self._update_title()

    def title(self) -> str:
        return self._title

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._content.setVisible(expanded)
        if self._collapsible and self._header.isChecked() != expanded:
            self._header.setChecked(expanded)
        self._update_title()
        self.toggled.emit(expanded)

    def _update_title(self) -> None:
        if self._collapsible:
            arrow = "▾" if self._expanded else "▸"
            self._header.setText(f"{arrow} {self._title}")
        else:
            self._header.setText(self._title)


def section(title: str, color: str = CYAN, collapsible: bool = False,
            expanded: bool = True, parent=None) -> Section:
    """A ``Section`` (see there)."""
    return Section(title, color, collapsible, expanded, parent)


def progress_bar(accent: str = CYAN, busy: bool = False, text: bool = False,
                 parent=None) -> QProgressBar:
    """A slim dark progress bar in ``accent``, scaled with the UI.

    ``busy`` starts it as a busy indicator (no known total, ``setRange(0, 0)``);
    give it a range later to show progress.  ``text`` shows the percentage.
    """
    bar = QProgressBar(parent)
    bar.setTextVisible(bool(text))
    if busy:
        bar.setRange(0, 0)
    ui_scale.style(bar, f"""
        QProgressBar {{ background-color: #1A1A1A; border: 1px solid #333333; border-radius: 3px;
                       color: white; font: bold 10px Arial; text-align: center; }}
        QProgressBar::chunk {{ background-color: {accent}; border-radius: 2px; }}
    """)
    ui_scale.fixed_height(bar, 16 if text else 8)
    return bar


_SCROLL_CSS = """
    QScrollArea#uiScroll, QScrollArea#uiScroll > QWidget#qt_scrollarea_viewport {
        background-color: black; border: none; }
    QScrollBar:vertical { background: #111111; width: 10px; margin: 0px; border: none; }
    QScrollBar:horizontal { background: #111111; height: 10px; margin: 0px; border: none; }
    QScrollBar::handle:vertical { background: #444444; min-height: 24px; border-radius: 4px; }
    QScrollBar::handle:horizontal { background: #444444; min-width: 24px; border-radius: 4px; }
    QScrollBar::handle:hover { background: #666666; }
    QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; border: none; }
    QScrollBar::add-page, QScrollBar::sub-page { background: none; }
"""


def scroll_area(widget: QWidget, horizontal: bool = False, parent=None) -> QScrollArea:
    """``widget`` in a frameless dark scroll area with slim scaled scroll bars,
    for a side panel that must stay usable when the window is small: it
    scrolls vertically (and horizontally with ``horizontal``) instead of
    forcing the window to be taller.  Scroll bars inside ``widget`` get the
    same dark look."""
    area = QScrollArea(parent)
    area.setObjectName("uiScroll")
    area.setWidget(widget)
    # setWidget makes the widget fill itself with the palette's (light) window
    # colour: let the black viewport show through instead
    widget.setAutoFillBackground(False)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    if not horizontal:
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    ui_scale.style(area, _SCROLL_CSS)
    return area


# ---------------------------------------------------------------------------
# Message dialogs
# ---------------------------------------------------------------------------

def _exec(dialog: QDialog) -> int:
    """Run ``dialog`` modally and return its result code (tests replace this)."""
    return dialog.exec()


def _accepted(code) -> bool:
    return int(code) == int(QDialog.DialogCode.Accepted.value)


def _dialog(parent, title: str, accent: str) -> QDialog:
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setModal(True)
    ui_scale.style(dlg, "QDialog { background-color: black; }" + _input_css(accent))
    return dlg


def _button_row(dlg: QDialog, buttons, default: int | None = None
                ) -> tuple[QHBoxLayout, list[QPushButton]]:
    """``buttons``: [(text, bg, fg, accept?)].  The ``default`` one (an index;
    None: the first accepting button) answers Enter."""
    if default is None:
        default = next((k for k, b in enumerate(buttons) if b[3]), None)
    row = QHBoxLayout()
    row.addStretch(1)
    made = []
    for k, (text, bg, fg, accept) in enumerate(buttons):
        btn = neon_button(text, bg, fg, padding=(7, 22))
        btn.clicked.connect(dlg.accept if accept else dlg.reject)
        btn.setDefault(k == default)
        row.addWidget(btn)
        made.append(btn)
    row.addStretch(1)
    return row, made


def message_dialog(parent, title: str, text: str, color: str = CYAN, buttons=None,
                   details: str | None = None, default: int | None = None) -> QDialog:
    """Build (not run) a dark message dialog: a coloured heading, the text,
    optional scrollable ``details`` and a row of ``buttons``
    (``[(text, bg, fg, accept?)]``, default a single OK; button ``default``
    answers Enter, the first accepting one when None)."""
    dlg = _dialog(parent, title, color)
    layout = QVBoxLayout(dlg)
    ui_scale.margins(layout, 18, 16, 18, 14)
    ui_scale.spacing(layout, 12)

    heading = _label(title, color, 15, bold=True)
    heading.setTextFormat(Qt.TextFormat.PlainText)
    layout.addWidget(heading)

    # Plain text: messages quote names the user typed (a barrier called "<b>x")
    message = _label(text, color if color != CYAN else "white", 12, wrap=True)
    message.setTextFormat(Qt.TextFormat.PlainText)
    message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    ui_scale.min_width(message, 360)
    layout.addWidget(message)

    if details:
        box = QPlainTextEdit(details)
        box.setReadOnly(True)
        ui_scale.min_height(box, 120)
        layout.addWidget(box, 1)

    row, made = _button_row(dlg, buttons or [("OK", color, "black", True)], default)
    layout.addLayout(row)
    # A focused button takes Enter in a dialog: start on the default one
    for btn in made:
        if btn.isDefault():
            btn.setFocus()
    dlg.adjustSize()
    return dlg


def _run(dlg: QDialog) -> bool:
    try:
        return _accepted(_exec(dlg))
    finally:
        dlg.deleteLater()


def info(parent, title: str, text: str, details: str | None = None) -> None:
    """Tell the user something (cyan)."""
    _run(message_dialog(parent, title, text, CYAN, details=details))


def warning(parent, title: str, text: str, details: str | None = None) -> None:
    """Warn the user (gold)."""
    _run(message_dialog(parent, title, text, GOLD, details=details))


def error(parent, title: str, text: str, details: str | None = None) -> None:
    """Report an error (red)."""
    _run(message_dialog(parent, title, text, RED, [("OK", RED, "white", True)], details))


def confirm(parent, title: str, text: str, ok_text: str = "OK", cancel_text: str = "Cancel",
            danger: bool = False, details: str | None = None) -> bool:
    """Ask a yes/no question; True if the user chose ``ok_text``.  With
    ``danger`` (destructive actions) the confirming button is red and Enter
    answers ``cancel_text``."""
    ok = (ok_text, RED, "white", True) if danger else (ok_text, GREEN, "black", True)
    cancel = (cancel_text, "#555555", "white", False)
    color = GOLD if danger else CYAN
    return _run(message_dialog(parent, title, text, color, [ok, cancel], details,
                               default=1 if danger else 0))


# ---------------------------------------------------------------------------
# Point entry
# ---------------------------------------------------------------------------

class PointDialog(QDialog):
    """Ask for a point in one of three ways:

    * ``"absolute"`` -- X and Y in the current frame;
    * ``"offset"``   -- dX and dY from a reference point;
    * ``"polar"``    -- a distance and an angle from a reference point
      (degrees, 0 = +X, counter-clockwise).

    ``references`` maps names to points (default: just the origin); the origin
    is always offered first.  Switching modes keeps the point and refills the
    fields; changing the reference keeps the typed offset and moves the point.
    ``value()`` is the point, or None while the input is invalid (OK is then
    disabled).
    """

    ABSOLUTE = "absolute"
    OFFSET = "offset"
    POLAR = "polar"
    MODES = (ABSOLUTE, OFFSET, POLAR)

    def __init__(self, parent=None, title: str = "Point", initial=(0.0, 0.0),
                 references: dict | None = None, text: str | None = None,
                 mode: str = ABSOLUTE, accent: str = CYAN):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        ui_scale.style(self, "QDialog { background-color: black; }" + _input_css(accent))

        refs = {ORIGIN_REF: (0.0, 0.0)}
        for name, p in (references or {}).items():
            if name != ORIGIN_REF:
                refs[str(name)] = (float(p[0]), float(p[1]))
        self._refs = refs
        self._point: tuple[float, float] | None = (float(initial[0]), float(initial[1]))
        self._mode = mode if mode in self.MODES else self.ABSOLUTE

        layout = QVBoxLayout(self)
        ui_scale.margins(layout, 18, 16, 18, 14)
        ui_scale.spacing(layout, 10)
        layout.addWidget(_label(title, accent, 15, bold=True))
        if text:
            note = _label(text, DIM, 12, wrap=True)
            ui_scale.min_width(note, 380)
            layout.addWidget(note)

        grid = QGridLayout()
        ui_scale.spacing(grid, 8)
        self._group = QButtonGroup(self)
        self._radios = {}
        for row, (key, caption) in enumerate(((self.ABSOLUTE, "X / Y"),
                                              (self.OFFSET, "Offset from reference"),
                                              (self.POLAR, "Distance + angle from reference"))):
            radio = QRadioButton(caption)
            self._group.addButton(radio)
            self._radios[key] = radio
            grid.addWidget(radio, row * 2, 0, 1, 4)

        def edit():
            e = QLineEdit()
            ui_scale.min_width(e, 90)
            e.textEdited.connect(self._on_edited)
            return e

        self._x, self._y = edit(), edit()
        self._dx, self._dy = edit(), edit()
        self._dist, self._angle = edit(), edit()
        for row, pairs in ((1, (("X", self._x), ("Y", self._y))),
                           (3, (("dX", self._dx), ("dY", self._dy))),
                           (5, (("Distance", self._dist), ("Angle °", self._angle)))):
            col = 0
            for caption, widget in pairs:
                grid.addWidget(_label(caption, DIM, 12), row, col, Qt.AlignmentFlag.AlignRight)
                grid.addWidget(widget, row, col + 1)
                col += 2
        layout.addLayout(grid)

        ref_row = QHBoxLayout()
        ref_row.addWidget(_label("Reference", DIM, 12))
        self._ref_combo = QComboBox()
        self._ref_combo.addItems(list(refs))
        self._ref_combo.currentIndexChanged.connect(self._on_reference)
        ref_row.addWidget(self._ref_combo, 1)
        layout.addLayout(ref_row)
        layout.addWidget(_label("Angles: 0° = +X, counter-clockwise (90° = +Y).", DIM, 11))

        self._result = _label("", accent, 13, bold=True)
        layout.addWidget(self._result)
        self._problem = _label("", RED, 11, wrap=True)
        layout.addWidget(self._problem)

        row, buttons = _button_row(self, [("OK", GREEN, "black", True),
                                          ("Cancel", "#555555", "white", False)])
        self._ok = buttons[0]
        layout.addLayout(row)

        self._radios[self._mode].setChecked(True)
        self._group.buttonToggled.connect(self._on_mode)
        self._fill_fields()
        self._update()
        self.adjustSize()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def value(self) -> tuple[float, float] | None:
        """The point the input describes, or None while it is invalid."""
        return self._point

    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode in self.MODES:
            self._radios[mode].setChecked(True)

    def reference(self) -> str:
        return self._ref_combo.currentText()

    def set_reference(self, name: str) -> None:
        index = self._ref_combo.findText(name)
        if index >= 0:
            self._ref_combo.setCurrentIndex(index)

    def set_fields(self, a: str, b: str) -> None:
        """Type ``a`` and ``b`` into the current mode's two fields (as the user
        would) and recompute the point."""
        first, second = self._fields(self._mode)
        first.setText(a)
        second.setText(b)
        self._on_edited()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fields(self, mode: str) -> tuple[QLineEdit, QLineEdit]:
        return {self.ABSOLUTE: (self._x, self._y), self.OFFSET: (self._dx, self._dy),
                self.POLAR: (self._dist, self._angle)}[mode]

    def _ref_point(self) -> tuple[float, float]:
        return self._refs.get(self._ref_combo.currentText(), (0.0, 0.0))

    def _fill_fields(self, include_active: bool = True) -> None:
        """Show the current point in every mode's fields (the inactive ones are
        disabled, so they show the same point the other two ways); with
        ``include_active`` False the fields being typed in are left alone."""
        ref = self._ref_point()
        for mode in self.MODES:
            if mode == self._mode and not include_active:
                continue
            first, second = self._fields(mode)
            if self._point is None:
                if mode != self._mode:
                    first.clear()
                    second.clear()
                continue
            if mode == self.ABSOLUTE:
                a, b = self._point
            elif mode == self.OFFSET:
                a, b = self._point[0] - ref[0], self._point[1] - ref[1]
            else:
                a, b = polar_from_points(ref, self._point)
            first.setText(format_number(a))
            second.setText(format_number(b))

    def _compute(self) -> tuple[tuple[float, float] | None, str]:
        first, second = self._fields(self._mode)
        a = parse_number(first.text())
        b = parse_number(second.text())
        if self._mode == self.POLAR and a is not None and a < 0:
            a = None
        _mark_invalid(first, a is None)
        _mark_invalid(second, b is None)
        if a is None or b is None:
            what = {self.ABSOLUTE: "X and Y", self.OFFSET: "dX and dY",
                    self.POLAR: "Distance (0 or more) and angle"}[self._mode]
            return None, f"{what} must be numbers within ±{MAX_COORD_CM:g} cm."
        ref = self._ref_point()
        if self._mode == self.ABSOLUTE:
            point = (_clean(a), _clean(b))
        elif self._mode == self.OFFSET:
            point = point_from_offset(ref, a, b)
        else:
            point = point_from_polar(ref, a, b)
        if abs(point[0]) > MAX_COORD_CM or abs(point[1]) > MAX_COORD_CM:
            return None, f"The point is more than {MAX_COORD_CM:g} cm from (0, 0)."
        return point, ""

    def _on_edited(self, *_args) -> None:
        self._point, problem = self._compute()
        self._fill_fields(include_active=False)
        self._update(problem)

    def _on_mode(self, button, checked: bool) -> None:
        if not checked:
            return
        for key, radio in self._radios.items():
            if radio is button:
                self._mode = key
        for key in self.MODES:
            for field in self._fields(key):
                field.setEnabled(key == self._mode)
                if key != self._mode:
                    _mark_invalid(field, False)
        self._fill_fields()
        self._update()

    def _on_reference(self, _index: int) -> None:
        if self._mode != self.ABSOLUTE:
            self._on_edited()                 # same offset from the new reference: the point moves
        else:
            self._fill_fields(include_active=False)
            self._update()

    def _update(self, problem: str = "") -> None:
        for key in self.MODES:
            for field in self._fields(key):
                field.setEnabled(key == self._mode)
        self._ref_combo.setEnabled(self._mode != self.ABSOLUTE)
        if self._point is None:
            self._result.setText("→ (---, ---)")
        else:
            self._result.setText(f"→ ({self._point[0]:.1f}, {self._point[1]:.1f}) cm")
        self._problem.setText(problem)
        self._problem.setVisible(bool(problem))
        self._ok.setEnabled(self._point is not None)


def ask_point(parent, title: str, initial=(0.0, 0.0), references: dict | None = None,
              text: str | None = None, mode: str = PointDialog.ABSOLUTE,
              accent: str = CYAN) -> tuple[float, float] | None:
    """Ask for a point (see ``PointDialog``); ``(x, y)`` or None if cancelled.

    ``references`` = ``{"R1": (x, y), ...}``; "Origin (0, 0)" is always offered.
    """
    dlg = PointDialog(parent, title, initial, references, text, mode, accent)
    try:
        return dlg.value() if _accepted(_exec(dlg)) else None
    finally:
        dlg.deleteLater()


# ---------------------------------------------------------------------------
# Point table
# ---------------------------------------------------------------------------

def _apply_row_height(header: QHeaderView, factor: float) -> None:
    """Scaled row height for every row, existing and new.

    The minimum goes first (Qt clamps a new default size to the current
    minimum), and the existing rows are sized explicitly: depending on the Qt
    version, a row that Qt itself resized (to meet a larger minimum) no
    longer follows the default size.
    """
    size = ui_scale.scale_length(26, factor)
    header.setMinimumSectionSize(ui_scale.scale_length(18, factor))
    header.setDefaultSectionSize(size)
    for section in range(header.count()):
        if header.sectionSize(section) != size:
            header.resizeSection(section, size)


class PointTable(QWidget):
    """An editable list of points: one row per point with its label (P1..,
    R1..), X, Y and optional extra columns.

    Extra columns hold a number or nothing (None, shown as their blank text,
    e.g. "auto" for a receiver facing that is aimed automatically).  Edits are
    validated (numbers only, coordinates within +/- 1e5 cm); an invalid edit
    is put back and explained under the table.

    Signals:
        pointsEdited(list)     the user changed the points (typing, Offset...,
                               a pick on the arena, adding or removing a row);
                               the list is ``points()``.
        selectionChanged(int)  the selected row (-1: none).
        pickModeChanged(bool)  "Pick on arena" was switched on or off.  While
                               it is on, the owner routes the next arena click
                               to ``apply_pick(x, y)`` (and turns the
                               ``ArenaInteractor``'s ``pick_enabled`` off, so a
                               click on a draggable point counts too).

    ``set_points`` and ``set_point`` change the points without emitting
    ``pointsEdited``.
    """

    pointsEdited = pyqtSignal(list)
    selectionChanged = pyqtSignal(int)
    pickModeChanged = pyqtSignal(bool)

    def __init__(self, prefix: str = "P", extra_columns=(), accent: str = GREEN,
                 min_rows: int = 0, max_rows: int | None = None, add_remove: bool = False,
                 new_point=None, offset_button: bool = True, pick_button: bool = True,
                 parent=None):
        super().__init__(parent)
        self._prefix = prefix
        self._accent = accent
        self._extras: list[tuple[str, str]] = []
        for col in extra_columns:
            if isinstance(col, str):
                self._extras.append((col, "auto"))
            else:
                self._extras.append((str(col[0]), str(col[1])))
        self._min_rows = max(0, int(min_rows))
        self._max_rows = None if max_rows is None else max(int(max_rows), self._min_rows)
        self._new_point = new_point
        self._rows: list[list] = []
        self._labels: list[str] = []
        self._auto_labels = True
        self._selected = -1
        self._filling = False
        self._row_status: dict[int, tuple[str | None, str]] = {}
        self._references: dict = {}

        ui_scale.style(self, _input_css(accent))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        ui_scale.spacing(layout, 6)

        self._table = QTableWidget(0, 3 + len(self._extras))
        self._table.setHorizontalHeaderLabels(["", "X (cm)", "Y (cm)"] + [h for h, _ in self._extras])
        self._table.verticalHeader().hide()
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                    | QAbstractItemView.EditTrigger.EditKeyPressed
                                    | QAbstractItemView.EditTrigger.AnyKeyPressed)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
        self._table.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        ui_scale.style(self._table, f"""
            QTableWidget {{ background-color: #111111; color: white; gridline-color: #333333;
                           border: 1px solid #333333; font: 12px Arial;
                           selection-background-color: {accent}; selection-color: black; }}
            QHeaderView::section {{ background-color: #1A1A1A; color: {DIM}; border: none;
                                   border-bottom: 1px solid #333333; padding: 3px 6px;
                                   font: bold 11px Arial; }}
            QTableWidget QLineEdit {{ background-color: #222222; color: white;
                                     border: 1px solid {accent}; padding: 0px 4px; }}
        """)
        ui_scale.bind(self._table.verticalHeader(), _apply_row_height, key="rows")
        self._table.itemChanged.connect(self._on_item_changed)
        self._table.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self._table)

        row = QHBoxLayout()
        ui_scale.spacing(row, 6)
        self._offset_btn = neon_button("Offset…", accent, "black", font_px=11, padding=(4, 8),
                                       tooltip="Set the selected point as X/Y, as an offset from a "
                                               "reference point, or as a distance and angle from it")
        self._offset_btn.clicked.connect(self._on_offset)
        self._pick_btn = neon_button("Pick on arena", accent, "black", font_px=11, padding=(4, 8),
                                     checkable=True,
                                     tooltip="Click the arena to place the selected point")
        self._pick_btn.toggled.connect(self._on_pick_toggled)
        self._add_btn = neon_button("+", accent, "black", font_px=12, padding=(4, 10),
                                    tooltip="Add a point")
        self._add_btn.clicked.connect(self._on_add)
        self._remove_btn = neon_button("−", accent, "black", font_px=12, padding=(4, 10),
                                       tooltip="Remove the selected point")
        self._remove_btn.clicked.connect(self._on_remove)
        for btn, shown in ((self._offset_btn, offset_button), (self._pick_btn, pick_button),
                           (self._add_btn, add_remove), (self._remove_btn, add_remove)):
            btn.setVisible(shown)
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)

        self._status = _label("", RED, 11, wrap=True)
        self._status.setVisible(False)
        layout.addWidget(self._status)
        self._update_buttons()

    # ------------------------------------------------------------------
    # Points
    # ------------------------------------------------------------------

    def set_points(self, points, labels=None) -> None:
        """Show ``points`` (``[(x, y, *extras)]``; missing extras are None) with
        ``labels`` (default prefix + 1-based index).  Keeps the selected row if
        it still exists.  Does not emit ``pointsEdited``."""
        rows = []
        for p in points:
            p = list(p)
            row = [float(p[0]), float(p[1])]
            for k in range(len(self._extras)):
                v = p[2 + k] if len(p) > 2 + k else None
                row.append(None if v is None else float(v))
            rows.append(row)
        self._rows = rows
        self._auto_labels = labels is None
        self._labels = self._make_labels(labels)
        self._row_status = {r: s for r, s in self._row_status.items() if r < len(rows)}
        self._fill(keep_selection=True)

    def points(self) -> list[tuple]:
        """``[(x, y, *extras)]`` in row order."""
        return [tuple(row) for row in self._rows]

    def labels(self) -> list[str]:
        return list(self._labels)

    def row_count(self) -> int:
        return len(self._rows)

    def set_point(self, row: int, x: float, y: float, extras=None) -> None:
        """Change one row's X/Y (and extras when given) without emitting
        ``pointsEdited`` (e.g. while the owner drags the point on the arena)."""
        if not 0 <= row < len(self._rows):
            return
        self._rows[row][0] = float(x)
        self._rows[row][1] = float(y)
        if extras is not None:
            for k, v in enumerate(list(extras)[:len(self._extras)]):
                self._rows[row][2 + k] = None if v is None else float(v)
        self._fill_row(row)

    def set_references(self, references: dict) -> None:
        """Extra reference points for "Offset..." (e.g. ``{"R1": (x, y)}``);
        the origin and the other rows are always offered."""
        self._references = {str(k): (float(v[0]), float(v[1])) for k, v in references.items()}

    def set_row_status(self, row: int, color: str | None = None, tooltip: str = "") -> None:
        """Colour a row's text (e.g. red for a refused point, amber for a
        warning) and give it a tooltip; None restores the normal look."""
        if color is None and not tooltip:
            self._row_status.pop(row, None)
        else:
            self._row_status[row] = (color, tooltip)
        if 0 <= row < len(self._rows):
            self._fill_row(row)

    def clear_row_status(self) -> None:
        rows = list(self._row_status)
        self._row_status.clear()
        for row in rows:
            if row < len(self._rows):
                self._fill_row(row)

    # ------------------------------------------------------------------
    # Selection and picking
    # ------------------------------------------------------------------

    def selected(self) -> int:
        """The selected row, or -1."""
        return self._selected

    def select(self, row: int) -> None:
        """Select ``row`` (-1 clears the selection)."""
        if 0 <= row < len(self._rows):
            self._table.selectRow(row)
        else:
            self._table.clearSelection()
            self._on_selection()

    def pick_mode(self) -> bool:
        return self._pick_btn.isChecked()

    def set_pick_mode(self, on: bool) -> None:
        self._pick_btn.setChecked(bool(on))

    def apply_pick(self, x: float, y: float) -> bool:
        """Place the selected row at the clicked (x, y) and leave pick mode.
        False (nothing changed) when pick mode is off or no row is selected."""
        if not self.pick_mode() or self._selected < 0:
            return False
        if parse_number(x) is None or parse_number(y) is None:
            return False
        self._rows[self._selected][0] = float(x)
        self._rows[self._selected][1] = float(y)
        self._fill_row(self._selected)
        self._set_status("")
        self.pointsEdited.emit(self.points())
        self.set_pick_mode(False)
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _make_labels(self, labels) -> list[str]:
        n = len(self._rows)
        if labels is None:
            return [f"{self._prefix}{k + 1}" for k in range(n)]
        labels = [str(s) for s in labels][:n]
        return labels + [f"{self._prefix}{k + 1}" for k in range(len(labels), n)]

    def _format(self, col: int, value) -> tuple[str, bool]:
        """Text for value column ``col`` (0 = X) and whether it is a blank extra."""
        if value is None:
            return self._extras[col - 2][1], True
        return f"{value:.1f}", False

    def _fill(self, keep_selection: bool = False) -> None:
        """Rebuild every row.  The table's own signals are blocked meanwhile and
        the selection is reconciled afterwards (one ``selectionChanged`` at most)."""
        previous = self._selected
        self._table.blockSignals(True)
        self._filling = True
        try:
            self._table.setRowCount(len(self._rows))
            for row in range(len(self._rows)):
                self._fill_row(row)
            if keep_selection:
                if 0 <= previous < len(self._rows):
                    self._table.selectRow(previous)
                else:
                    self._table.clearSelection()
        finally:
            self._filling = False
            self._table.blockSignals(False)
        self._on_selection()
        self._update_buttons()

    def _fill_row(self, row: int) -> None:
        """Show row ``row`` (updating its items in place, so an open editor survives)."""
        filling, self._filling = self._filling, True
        try:
            color, tip = self._row_status.get(row, (None, ""))
            cells = [(self._labels[row], False, True)]
            for col, value in enumerate(self._rows[row]):
                text, blank = self._format(col, value)
                cells.append((text, blank, False))
            for col, (text, blank, is_label) in enumerate(cells):
                item = self._table.item(row, col)
                if item is None:
                    item = QTableWidgetItem()
                    if is_label:
                        item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    else:
                        item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                              | Qt.AlignmentFlag.AlignVCenter)
                    self._table.setItem(row, col, item)
                item.setText(text)
                if is_label:
                    item.setForeground(QColor(color or self._accent))
                else:
                    item.setForeground(QColor(color or ("#777777" if blank else "white")))
                item.setToolTip(tip)
        finally:
            self._filling = filling

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling:
            return
        row, col = item.row(), item.column() - 1
        if col < 0 or not 0 <= row < len(self._rows):
            return
        old = self._rows[row][col]
        text = item.text().strip()
        if text == self._format(col, old)[0]:
            return                                   # committed unchanged: keep full precision
        name = self._labels[row]
        if col >= 2:
            header, blank = self._extras[col - 2]
            if text == "" or text.lower() == blank.lower():
                value = None
            else:
                value = parse_number(text)
                if value is None:
                    self._fill_row(row)
                    self._set_status(f"{name}: {header} must be a number (or blank for {blank}).")
                    return
        else:
            value = parse_number(text)
            if value is None:
                self._fill_row(row)
                self._set_status(f"{name}: {'X' if col == 0 else 'Y'} must be a number within "
                                 f"±{MAX_COORD_CM:g} cm.")
                return
        self._rows[row][col] = value
        self._fill_row(row)
        self._set_status("")
        self.pointsEdited.emit(self.points())

    def _on_selection(self) -> None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        row = rows[0].row() if rows else -1
        if row == self._selected:
            return
        self._selected = row
        self._update_buttons()
        self.selectionChanged.emit(row)

    def _on_pick_toggled(self, on: bool) -> None:
        if on and self._selected < 0 and self._rows:
            self.select(0)
        self.pickModeChanged.emit(on)

    def _reference_points(self, row: int) -> dict:
        refs = dict(self._references)
        for k, label in enumerate(self._labels):
            if k != row:
                refs[label] = (self._rows[k][0], self._rows[k][1])
        return refs

    def _on_offset(self) -> None:
        row = self._selected
        if not 0 <= row < len(self._rows):
            return
        point = ask_point(self, f"Point {self._labels[row]}",
                          initial=(self._rows[row][0], self._rows[row][1]),
                          references=self._reference_points(row), mode=PointDialog.OFFSET,
                          accent=self._accent)
        if point is None:
            return
        self._rows[row][0], self._rows[row][1] = point
        self._fill_row(row)
        self._set_status("")
        self.pointsEdited.emit(self.points())

    def _on_add(self) -> None:
        if self._max_rows is not None and len(self._rows) >= self._max_rows:
            return
        if callable(self._new_point):
            p = list(self._new_point(self.points()))
        elif self._rows:
            p = [self._rows[-1][0] + 20.0, self._rows[-1][1]]
        else:
            p = [0.0, 0.0]
        row = [float(p[0]), float(p[1])] + [None] * len(self._extras)
        self._rows.append(row)
        self._labels = self._make_labels(None if self._auto_labels else self._labels)
        self._fill()
        self.select(len(self._rows) - 1)
        self.pointsEdited.emit(self.points())

    def _on_remove(self) -> None:
        if len(self._rows) <= self._min_rows or not self._rows:
            return
        row = self._selected if 0 <= self._selected < len(self._rows) else len(self._rows) - 1
        del self._rows[row]
        if not self._auto_labels:
            del self._labels[row]
        self._labels = self._make_labels(None if self._auto_labels else self._labels)
        self._row_status = {}
        self._fill()
        self.select(min(row, len(self._rows) - 1))
        self.pointsEdited.emit(self.points())

    def _update_buttons(self) -> None:
        has_row = 0 <= self._selected < len(self._rows)
        self._offset_btn.setEnabled(has_row)
        self._pick_btn.setEnabled(bool(self._rows))
        self._add_btn.setEnabled(self._max_rows is None or len(self._rows) < self._max_rows)
        self._remove_btn.setEnabled(len(self._rows) > self._min_rows)

    def _set_status(self, text: str) -> None:
        self._status.setText(text)
        self._status.setVisible(bool(text))


# ---------------------------------------------------------------------------
# Origin tool
# ---------------------------------------------------------------------------

class OriginTool(QWidget):
    """"Set (0, 0)": choose a point of the current frame to become the new origin.

    The candidate can be (a) picked on the arena (the owner routes the click to
    ``apply_pick``), (b) typed as X/Y in the current frame, (c) given as an
    offset (dX/dY or distance + angle) from the current (0, 0) or a receiver
    ("Offset..."), or (d) measured: the transmitter is placed on the physical
    mark and ``measure`` is called on a worker thread.  "Apply" takes exactly
    the point the X / Y fields show (Enter not needed; refused while they are
    half typed or invalid), asks for confirmation (receivers, calibration
    points and barriers are all translated) and emits ``originRequested(x, y)``.

    Args:
        measure:    ``measure(cancel=callable, on_progress=callable)`` returning a
                    ``windows.measure.MeasureResult`` (or anything with
                    ``position``, or an ``(x, y)`` pair); None disables
                    measuring.  ``on_progress(done, total)`` may be called.
        references: ``{"R1": (x, y), ...}`` for "Offset..." (the origin is always offered).

    Signals:
        originRequested(float, float)  make this point of the current frame (0, 0).
                                       An owner that refuses or fails says so with
                                       ``set_status(message, RED)`` in its slot: the
                                       tool then keeps the point for another try.
                                       Otherwise the tool clears the point and shows
                                       the last status the slot set, or its own
                                       "moved" message.
        previewChanged(object)         the candidate ``(x, y)``, or None when cleared.
        pickModeChanged(bool)          the owner routes arena clicks to ``apply_pick``
                                       (with the interactor's ``pick_enabled`` off,
                                       so a click on a receiver counts).
        measuringChanged(bool)         a measurement started or ended.
    """

    originRequested = pyqtSignal(float, float)
    previewChanged = pyqtSignal(object)
    pickModeChanged = pyqtSignal(bool)
    measuringChanged = pyqtSignal(bool)

    def __init__(self, measure=None, references: dict | None = None, accent: str = BLUE,
                 parent=None):
        super().__init__(parent)
        self._measure = measure
        self._references: dict = {}
        self._candidate: tuple[float, float] | None = None
        self._worker: Worker | None = None
        self._accent = accent
        self._slot_reports: list | None = None    # statuses set during originRequested
        self.set_references(references or {})

        ui_scale.style(self, _input_css(accent))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        box = Section("Set (0, 0)", accent)
        outer.addWidget(box)

        box.add(_label("Choose the point that becomes the arena's new (0, 0). Every receiver, "
                       "calibration point and barrier moves with it.", DIM, 11, wrap=True))

        xy = QHBoxLayout()
        ui_scale.spacing(xy, 6)
        self._x_edit, self._y_edit = QLineEdit(), QLineEdit()
        for caption, edit in (("X", self._x_edit), ("Y", self._y_edit)):
            edit.setPlaceholderText("cm")
            ui_scale.min_width(edit, 60)
            edit.editingFinished.connect(self._on_typed)
            edit.textEdited.connect(self._on_text_edited)
            xy.addWidget(_label(caption, DIM, 12))
            xy.addWidget(edit, 1)
        box.add_layout(xy)

        buttons = QHBoxLayout()
        ui_scale.spacing(buttons, 6)
        self._pick_btn = neon_button("Pick on arena", accent, "white", font_px=11, padding=(4, 8),
                                     checkable=True, tooltip="Click the arena at the new (0, 0)")
        self._pick_btn.toggled.connect(self.pickModeChanged.emit)
        self._offset_btn = neon_button("Offset…", accent, "white", font_px=11, padding=(4, 8),
                                       tooltip="Give the new (0, 0) as an offset (dX/dY or distance "
                                               "+ angle) from the current (0, 0) or a receiver")
        self._offset_btn.clicked.connect(self._on_offset)
        buttons.addWidget(self._pick_btn)
        buttons.addWidget(self._offset_btn)
        box.add_layout(buttons)

        self._measure_btn = neon_button("Measure with transmitter", accent, "white", font_px=11,
                                        padding=(4, 8))
        self._measure_btn.clicked.connect(self._on_measure_clicked)
        box.add(self._measure_btn)

        self._progress = progress_bar(accent)
        self._progress.setVisible(False)
        box.add(self._progress)

        self._status = _label("", DIM, 11, wrap=True)
        box.add(self._status)

        actions = QHBoxLayout()
        ui_scale.spacing(actions, 6)
        self._apply_btn = neon_button("Apply", GREEN, "black", font_px=12, padding=(5, 14))
        self._apply_btn.clicked.connect(self._on_apply)
        self._clear_btn = neon_button("Clear", "#555555", "white", font_px=12, padding=(5, 14))
        self._clear_btn.clicked.connect(self.clear)
        actions.addWidget(self._apply_btn)
        actions.addWidget(self._clear_btn)
        actions.addStretch(1)
        box.add_layout(actions)
        self._set_status("")
        self._update()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_measure(self, measure) -> None:
        """Set (or with None remove) the measuring callable."""
        self._measure = measure
        self._update()

    def set_references(self, references: dict) -> None:
        """Reference points for "Offset..." besides the origin, e.g. receivers."""
        self._references = {str(k): (float(v[0]), float(v[1])) for k, v in references.items()}

    def candidate(self) -> tuple[float, float] | None:
        """The chosen point (current frame), or None."""
        return self._candidate

    def set_candidate(self, x: float, y: float) -> None:
        """Make (x, y) the candidate and emit ``previewChanged``."""
        self._candidate = (float(x), float(y))
        self._x_edit.setText(f"{x:.1f}")
        self._y_edit.setText(f"{y:.1f}")
        _mark_invalid(self._x_edit, False)
        _mark_invalid(self._y_edit, False)
        self._update()
        self.previewChanged.emit(self._candidate)

    def clear(self) -> None:
        """Forget the candidate (``previewChanged(None)``)."""
        had = self._candidate is not None
        self._candidate = None
        self._x_edit.clear()
        self._y_edit.clear()
        _mark_invalid(self._x_edit, False)
        _mark_invalid(self._y_edit, False)
        self.set_pick_mode(False)
        self._set_status("")
        self._update()
        if had:
            self.previewChanged.emit(None)

    def pick_mode(self) -> bool:
        return self._pick_btn.isChecked()

    def set_pick_mode(self, on: bool) -> None:
        self._pick_btn.setChecked(bool(on))

    def apply_pick(self, x: float, y: float) -> bool:
        """Use an arena click as the candidate (only in pick mode); leaves pick mode."""
        if not self.pick_mode():
            return False
        self.set_candidate(x, y)
        self._set_status("Picked on the arena.")
        self.set_pick_mode(False)
        return True

    def set_status(self, text: str, color: str = DIM) -> None:
        """Show ``text`` under the buttons ("" hides it).  Called in RED from an
        ``originRequested`` slot it refuses the move (the point is kept)."""
        if self._slot_reports is not None:
            self._slot_reports.append((text, color))
        self._set_status(text, color)

    def is_measuring(self) -> bool:
        return self._worker is not None

    def cancel_measurement(self) -> None:
        """Ask a running measurement to stop (its partial result is discarded)."""
        if self._worker is not None:
            self._worker.cancel()
            self._set_status("Cancelling…", AMBER)

    def wait(self, timeout_ms: int = 5000) -> bool:
        """Wait for a running measurement's thread to end (True if none is left)."""
        worker = self._worker
        return worker is None or worker.wait(timeout_ms)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _references_with_origin(self) -> dict:
        refs = {ORIGIN_REF: (0.0, 0.0)}
        refs.update(self._references)
        return refs

    def _on_typed(self) -> None:
        tx, ty = self._x_edit.text().strip(), self._y_edit.text().strip()
        if not tx and not ty:
            if self._candidate is not None:
                self.clear()
            return
        x, y = parse_number(tx), parse_number(ty)
        _mark_invalid(self._x_edit, bool(tx) and x is None)
        _mark_invalid(self._y_edit, bool(ty) and y is None)
        if (tx and x is None) or (ty and y is None):
            self._set_status(f"X and Y must be numbers within ±{MAX_COORD_CM:g} cm.", RED)
            return
        if x is None or y is None:            # the other field is still to be typed
            self._set_status("")
            return
        c = self._candidate
        if c is not None and tx == f"{c[0]:.1f}" and ty == f"{c[1]:.1f}":
            return                        # still the candidate's own text: keep its full precision
        self.set_candidate(x, y)
        self._set_status("")

    def _on_text_edited(self, _text: str) -> None:
        self._update()                        # Apply is usable as soon as X and Y are typed

    def _on_offset(self) -> None:
        point = ask_point(self, "New (0, 0)", initial=self._candidate or (0.0, 0.0),
                          references=self._references_with_origin(), mode=PointDialog.OFFSET,
                          text="Where the new (0, 0) is, in the current frame.",
                          accent=self._accent)
        if point is not None:
            self.set_candidate(*point)
            self._set_status("")

    def _on_measure_clicked(self) -> None:
        if self._worker is not None:
            self.cancel_measurement()
            return
        if self._measure is None:
            return
        self._set_status("Measuring…")
        self._progress.setRange(0, 0)                 # busy until the first progress report
        self._progress.setVisible(True)
        self._worker = run_in_thread(
            self._measure, pass_cancel=True, pass_progress=True,
            on_result=self._on_measured, on_error=self._on_measure_failed,
            on_progress=self._on_measure_progress, on_finished=self._on_measure_finished)
        self._update()
        self.measuringChanged.emit(True)

    def _on_measure_progress(self, value) -> None:
        if self._worker is None or self._worker.is_cancelled():
            return
        if isinstance(value, tuple) and len(value) == 2:
            done, total = int(value[0]), max(1, int(value[1]))
            self._progress.setRange(0, total)
            self._progress.setValue(min(done, total))
            self._set_status(f"Measuring… {done}/{total}")

    def _on_measured(self, result) -> None:
        worker = self._worker
        if worker is not None and worker.is_cancelled():
            self._set_status("Measurement cancelled.", AMBER)
            return
        if getattr(result, "cancelled", False):
            self._set_status("Measurement cancelled.", AMBER)
            return
        position, spread, detail = _describe_measurement(result)
        if position is None:
            self._set_status(f"No position fix{f' ({detail})' if detail else ''}.", RED)
            return
        self.set_candidate(*position)
        spread_text = f" ± {spread:.1f} cm" if spread is not None else ""
        self._set_status(f"Measured ({position[0]:.1f}, {position[1]:.1f}) cm{spread_text}"
                         f"{', ' + detail if detail else ''}.", GREEN)

    def _on_measure_failed(self, message: str) -> None:
        self._set_status(f"Measurement failed: {message}", RED)

    def _on_measure_finished(self) -> None:
        self._worker = None
        self._progress.setVisible(False)
        self._update()
        self.measuringChanged.emit(False)

    def _typed_point(self) -> tuple[float, float] | None:
        """The point the X / Y fields hold, or None (empty, partial or invalid)."""
        x, y = parse_number(self._x_edit.text()), parse_number(self._y_edit.text())
        return None if x is None or y is None else (x, y)

    def _fields_show_candidate(self) -> bool:
        """Whether the X / Y fields still describe the candidate."""
        c = self._candidate
        if c is None:
            return False
        tx, ty = self._x_edit.text().strip(), self._y_edit.text().strip()
        if tx == f"{c[0]:.1f}" and ty == f"{c[1]:.1f}":
            return True
        return self._typed_point() == c

    def _on_apply(self) -> None:
        # Take what the fields say now (Enter not pressed yet, say): never
        # move the origin to an older candidate than the one on show.
        self._on_typed()
        if self._candidate is None:
            self._set_status("Choose the new (0, 0) first.", AMBER)
            return
        if not self._fields_show_candidate():
            self._set_status("Type X and Y as numbers (or pick the point) first.", RED)
            return
        x, y = self._candidate
        if not (math.isfinite(x) and math.isfinite(y)):
            self._set_status("The new (0, 0) must be a finite point.", RED)
            return
        if math.hypot(x, y) < 1e-9:
            self._set_status("That point is already (0, 0).", AMBER)
            return
        if not confirm(self, "Move (0, 0)",
                       f"Make ({x:.1f}, {y:.1f}) the new (0, 0)?\n\n"
                       f"Every receiver position, calibration point and barrier (and image "
                       f"overlay) will be moved by ({-x:.1f}, {-y:.1f}) cm so this point becomes "
                       f"(0, 0). Slopes and intercepts are not changed.",
                       ok_text="Move (0, 0)"):
            return
        # The owner moves the frame in its slot (a direct call); one that
        # refuses or fails says so in RED, and the point stays for another try
        self._slot_reports = reports = []
        try:
            self.originRequested.emit(x, y)
        finally:
            self._slot_reports = None
        if any(str(color).upper() == RED for _, color in reports):
            return
        self.clear()
        text, color = next((r for r in reversed(reports) if r[0]),
                           (f"(0, 0) moved to the old ({x:.1f}, {y:.1f}).", GREEN))
        self._set_status(text, color)

    def _update(self) -> None:
        measuring = self._worker is not None
        self._measure_btn.setEnabled(self._measure is not None)
        self._measure_btn.setText("Cancel measuring" if measuring else "Measure with transmitter")
        self._measure_btn.setToolTip(
            "Needs the ground station connection" if self._measure is None else
            "Put the transmitter on the physical (0, 0) mark: its measured position (the mean "
            "of several fixes) becomes the candidate")
        chosen = self._candidate is not None or self._typed_point() is not None
        self._apply_btn.setEnabled(chosen and not measuring)
        self._clear_btn.setEnabled(self._candidate is not None)

    def _set_status(self, text: str, color: str = DIM) -> None:
        self._status.setText(text)
        ui_scale.style(self._status, f"color: {color}; font: 11px Arial; background: transparent;")
        self._status.setVisible(bool(text))

    def hideEvent(self, event) -> None:
        # Leaving the panel: stop routing clicks here and stop pulsing the
        # transmitter.  A minimised window (a spontaneous hide) keeps both.
        if not event.spontaneous():
            self.set_pick_mode(False)
            self.cancel_measurement()
        super().hideEvent(event)


def _describe_measurement(result) -> tuple[tuple[float, float] | None, float | None, str]:
    """``(position, spread_cm, detail)`` from a ``MeasureResult``, anything with
    ``position``, or an ``(x, y)`` pair."""
    if result is None:
        return None, None, ""
    if isinstance(result, (tuple, list)) and len(result) == 2:
        return (float(result[0]), float(result[1])), None, ""
    position = getattr(result, "position", None)
    spread = getattr(result, "spread", None)
    n_ok, n_total = getattr(result, "n_ok", None), getattr(result, "n_total", None)
    parts = [f"{n_ok}/{n_total} readings with a fix"] if n_ok is not None else []
    if getattr(result, "link_lost", False):
        parts.insert(0, "ground-station link down")
    elif position is None and getattr(result, "timed_out", False):
        parts.insert(0, "timed out")
    detail = ", ".join(parts)
    if position is None:
        return None, None, detail
    x, y = float(position[0]), float(position[1])
    if not (math.isfinite(x) and math.isfinite(y)):       # a broken solve is no fix
        return None, None, detail
    return (x, y), spread, detail


# ---------------------------------------------------------------------------
# Background work
# ---------------------------------------------------------------------------

class Worker(QThread):
    """Runs ``fn(*args, **kwargs)`` on its own thread.

    Signals are emitted from the worker thread; connections made on the GUI
    thread (to widget methods or plain callables) are therefore delivered on
    the GUI thread.

    ``result(object)``   the return value (also after ``cancel()``, if the
                         callable returns normally: it may return a partial
                         result; check ``is_cancelled()``).
    ``error(str)``       the exception's message, if it raised.
    ``progress(object)`` whatever ``report_progress`` was given (one value, or
                         a tuple of several).
    ``finished``         (QThread's) after either of the above.

    With ``pass_cancel`` the callable gets ``cancel=worker.is_cancelled`` (a
    callable returning True once ``cancel()`` was called); with
    ``pass_progress`` it gets ``on_progress=worker.report_progress``.
    """

    result = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(object)

    def __init__(self, fn, *args, pass_cancel: bool = False, pass_progress: bool = False,
                 parent: QObject | None = None, **kwargs):
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._pass_cancel = pass_cancel
        self._pass_progress = pass_progress
        self._cancel = threading.Event()
        self.value = None
        self.exception: BaseException | None = None

    def cancel(self) -> None:
        """Ask the callable to stop (it must check ``is_cancelled``)."""
        self._cancel.set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def report_progress(self, *values) -> None:
        """Thread-safe: emit ``progress`` with one value or a tuple."""
        self.progress.emit(values[0] if len(values) == 1 else values)

    def run(self) -> None:
        kwargs = dict(self._kwargs)
        if self._pass_cancel:
            kwargs["cancel"] = self.is_cancelled
        if self._pass_progress:
            kwargs["on_progress"] = self.report_progress
        try:
            value = self._fn(*self._args, **kwargs)
        except Exception as exc:          # an exception escaping run() would abort the app
            self.exception = exc
            self.error.emit(str(exc) or type(exc).__name__)
            return
        self.value = value
        self.result.emit(value)


_running: set = set()


def run_in_thread(fn, *args, on_result=None, on_error=None, on_progress=None,
                  on_finished=None, pass_cancel: bool = False, pass_progress: bool = False,
                  **kwargs) -> Worker:
    """Start a ``Worker`` for ``fn(*args, **kwargs)`` and wire the callbacks
    (all called on the GUI thread).  The worker is kept alive while it runs
    and deleted afterwards; keep the returned object to ``cancel()`` it."""
    worker = Worker(fn, *args, pass_cancel=pass_cancel, pass_progress=pass_progress, **kwargs)
    if on_result is not None:
        worker.result.connect(on_result)
    if on_error is not None:
        worker.error.connect(on_error)
    if on_progress is not None:
        worker.progress.connect(on_progress)

    def done():
        _running.discard(worker)
        if on_finished is not None:
            on_finished()
        worker.deleteLater()

    worker.finished.connect(done)
    _running.add(worker)
    worker.start()
    return worker


def running_workers() -> list[Worker]:
    """Workers started by ``run_in_thread`` that have not finished."""
    return list(_running)


def shutdown_workers(timeout_ms: int = 3000) -> bool:
    """Cancel every running worker and wait up to ``timeout_ms`` in all for
    them to end (for the main window's ``closeEvent``).  True if they all
    ended.

    A callable blocked in a ground-station command (the ~16 s D sweep, say)
    only sees the cancel flag once the command returns: disconnect the
    client first, which makes a waiting command return at once.
    """
    workers = list(_running)
    for worker in workers:
        worker.cancel()
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    ended = True
    for worker in workers:                    # wait for each, within the one deadline
        remaining = max(0, int((deadline - time.monotonic()) * 1000))
        ended = worker.wait(remaining) and ended
    return ended
