"""Arena Maker panel: lay out the receivers and see how well they cover the arena.

The hardware has six receiver channels; the layout uses the first N of them
(R1..RN, 3 <= N <= 6, config.toml's ``number_of_receivers``).  The others
stay dormant: config.toml keeps their entries (calibration, position,
facing), but they are not drawn, judged or saved here.

Receivers are placed by clicking the arena ("Pick on arena" for the selected
receiver; "Place all by clicking" asks how many receivers to use, then takes
R1..RN in turn: they disappear and each one reappears where it is clicked,
and the rest of the panel stays locked until all are placed, or Esc / Cancel
puts the previous layout back), by dragging them, by typing X / Y, or as an offset from the
origin or another receiver ("Offset...").  Each one faces the direction in
its "Facing (°)" column (0 = +X, 90 = +Y) or, left blank, the centroid of
the receivers.  The two older ways of building a six-receiver layout are kept
as templates, with exactly their old maths: a W x H rectangle
(``positions_from_dimensions``) and the tape-measure distances D1..D10
between receivers (the old R1..R10, ``positions_from_distances``), whose
markers are drawn on the arena while that template is open (or always, if
chosen).

Whenever the layout changes, ``ultragps_position.coverage`` estimates what
it gives: how many receivers hear the transmitter at each spot of the arena
(3 are needed for a fix), under one receiver model (by default the HC-SR04
datasheet's 2-400 cm within ±15° as hard limits; every figure, the reception
angle first, is edited in "Sensor settings..." and saved in sensor.toml of the
config folder) and the base station's sampling window.  The shading is off
until chosen; the summary numbers are always shown.  The maps are computed on
a worker thread; while a receiver is dragged a coarse preview follows it.

The calibration points are drawn read-only (the Calibration panel edits
them) and the origin tool moves (0, 0).  Nothing is written to config.toml
until "Verify & Save" (an origin move applies to the saved frame at once):
it refuses a layout the position library cannot use, saves the receiver
count and the active receivers' positions and facings, verifies the whole
configuration and tells the main window.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from functools import partial
from itertools import combinations

import numpy as np
from matplotlib.figure import Figure
from matplotlib.path import Path
from PyQt6.QtCore import QEvent, Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QSizePolicy,
    QSpinBox, QVBoxLayout, QWidget,
)

from SettingsModule import (
    COLLINEAR_TOLERANCE, DEFAULT_SENSOR, MIN_RECEIVERS, RECEIVER_COUNT, SAME_SPOT_CM,
    SENSOR_FILENAME, SettingsModule,
)
from ultragps_client import DEFAULT_SAMPLING_TIME_MS, SAMPLING_TIME_MAX_MS, SAMPLING_TIME_MIN_MS
from ultragps_position import coverage
from windows import measure, ui_scale, widgets
from windows.arena_view import RECEIVERS, ArenaInteractor, ArenaView, outline_xy, resolve_facing
from windows.ui_scale import ScalingCanvas
from windows.widgets import AMBER, BLUE, CYAN, DIM, GOLD, GREEN, MAGENTA, ORANGE, RED

log = logging.getLogger(__name__)

#: Grid step (cm) of the coverage maps, and of the preview while a receiver
#: is dragged.  A large arena gets a coarser grid, so that a map never has
#: more than about MAP_CELLS (PREVIEW_CELLS) cells a side.
MAP_STEP_CM = 5.0
PREVIEW_STEP_CM = 10.0
MAP_CELLS = 160
PREVIEW_CELLS = 80

#: Edits are batched for this long before the coverage is recomputed (ms).
COVERAGE_DEBOUNCE_MS = 150

#: How long hiding the panel waits for a cancelled origin measurement to end
#: (ms): one pulse in flight, bounded by the client's reply timeout.
MEASURE_STOP_MS = 4000

#: Receivers that must hear the transmitter for a fix.
NEEDED_FOR_FIX = 3

#: Two receivers closer than this (cm) share one spot: an HC-SR04 module
#: alone is 4.5 cm wide.
MIN_RECEIVER_GAP_CM = 1.0

#: Side of the view (cm) of a layout with every receiver on one spot: room
#: to place the receivers of a new arena by clicking.
EMPTY_VIEW_CM = 400.0

#: A receiver outline enclosing less than this (cm²) has no inside to judge.
MIN_AREA_CM2 = 1.0

#: Room round the receivers when the view is fitted: the coverage maps reach
#: coverage.MARGIN_CM (30 cm) beyond them.
VIEW_PADDING_CM = coverage.MARGIN_CM + 10.0

VIEW_COVERAGE, VIEW_OFF = "coverage", "off"

MAP_VIEWS = ((VIEW_COVERAGE, "Receivers hearing each spot"),
             (VIEW_OFF, "No shading"))

#: The view the panel opens with: no shading until the user asks for it.
DEFAULT_VIEW = VIEW_OFF

SNAP_STEPS = (("Snap 1 cm", 1.0), ("Snap 5 cm", 5.0), ("Snap 10 cm", 10.0), ("No snap", None))

#: The tape-measure distances (the old panel's R1..R10, renamed so they are
#: not mistaken for receivers): (name, what to measure).
TAPE_DISTANCES = (
    ("D1", "R2 → R3, along Y"),
    ("D2", "R1 → R2, along Y"),
    ("D3", "R1 → R4, along X"),
    ("D4", "R4 → R5, along Y"),
    ("D5", "R5 → R6, along Y"),
    ("D6", "R3 → R6, along X"),
    ("D7", "(0, 0) → R2, along X"),
    ("D8", "(0, 0) → R5, along X"),
    ("D9", "R2's Y (0 on the X axis)"),
    ("D10", "R5's Y (0 on the X axis)"),
)

#: What every coverage model assumes (the note under the summary).
MODEL_NOTE = ("Direct sound only: walls, obstacles and echoes are not modelled, and the "
              "transmitter is taken to radiate all round.")

#: How to read each map (the note under the summary).
VIEW_NOTES = {
    VIEW_COVERAGE: (
        "Each spot is coloured by how many receivers hear the transmitter there: a fix needs "
        "3 (yellow); a 4th lets one bad reading be rejected (yellow-green); more are green. "
        "Red: fewer than 3, no fix (dark red: none). The dashed line is the edge of the area "
        "with a fix."),
    VIEW_OFF: "No shading: choose “Receivers hearing each spot” to shade the arena. The "
              "summary above is estimated all the same.",
}

_SIDE_CSS = f"""
    QLabel {{ color: white; font: 12px Arial; background: transparent; }}
    QLineEdit {{ background-color: #222222; color: white; border: 1px solid #444444;
                 border-radius: 3px; padding: 3px 6px; font: 12px Arial; }}
    QLineEdit:focus {{ border-color: {CYAN}; }}
    QComboBox, QSpinBox {{ background-color: #222222; color: white; border: 1px solid #444444;
                 border-radius: 3px; padding: 3px 6px; font: 12px Arial; }}
    QComboBox:disabled, QSpinBox:disabled {{ background-color: #151515; color: #555555; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; }}
    QSpinBox::up-arrow, QSpinBox::down-arrow {{ width: 8px; height: 8px; }}
    QComboBox QAbstractItemView {{ background-color: #222222; color: white;
                 selection-background-color: {CYAN}; selection-color: black; }}
    QCheckBox {{ color: white; font: 12px Arial; spacing: 6px; background: transparent; }}
    QCheckBox::indicator {{ width: 13px; height: 13px; border-radius: 3px; }}
    QCheckBox::indicator:unchecked {{ background-color: #222222; border: 2px solid #666666; }}
    QCheckBox::indicator:checked {{ background-color: {CYAN}; border: 2px solid {CYAN}; }}
    QToolTip {{ background-color: #222222; color: white; border: 1px solid #555555;
                font: 11px Arial; padding: 4px; }}
"""


# ---------------------------------------------------------------------------
# Layout maths (pure, no Qt)
# ---------------------------------------------------------------------------

def positions_from_dimensions(width: float, height: float) -> list:
    """Receivers on a ``width`` x ``height`` rectangle centred on (0, 0): R1,
    R2 and R3 up the left side (corner, middle, corner), R4, R5 and R6 up the
    right side.  Returns ``[(id, (x, y))]``.

    The old panel's ``_calculate_receiver_positions_from_dimensions``
    ("Re-dimension"), unchanged.
    """
    hw, hh = width / 2.0, height / 2.0
    return [
        (0, (-hw, -hh)), (1, (-hw, 0.0)), (2, (-hw, hh)),
        (3, (hw, -hh)), (4, (hw, 0.0)), (5, (hw, hh)),
    ]


def positions_from_distances(r1, r2, r3, r4, r5, r6, r7=None, r8=None, r9=0, r10=0) -> list:
    """Receivers from the tape-measure distances D1..D10 (``r1``..``r10``,
    see ``TAPE_DISTANCES``): R1-R3 in a left column, R4-R6 in a right one.

    D3 and D6 are the widths at the bottom (R1-R4) and top (R3-R6), centred
    on the Y axis; D7 / D8 put R2 / R5 at that distance left / right of the
    Y axis (half of D6 when None); D9 / D10 are their Y; D1 / D2 (D4 / D5)
    are the Y distances from R2 to R3 and R1 (from R5 to R6 and R4).
    Returns ``[(id, (x, y))]``.

    The old panel's ``_calculate_positions_from_distances``, unchanged.
    """
    if r7 is None:
        r7 = r6 / 2.0
    if r8 is None:
        r8 = r6 / 2.0

    x3 = -r6 / 2.0
    x2 = -r7
    x1 = -r3 / 2.0
    x4 = r3 / 2.0
    x5 = r8
    x6 = r6 / 2.0
    y2 = r9
    y5 = r10
    y3 = y2 + r1
    y1 = y2 - r2
    y6 = y5 + r5
    y4 = y5 - r4

    return [
        (0, (x1, y1)), (1, (x2, y2)), (2, (x3, y3)),
        (3, (x4, y4)), (4, (x5, y5)), (5, (x6, y6)),
    ]


def distances_from_positions(positions) -> tuple:
    """The tape-measure distances D1..D10 of a layout ``[(id, (x, y))]``
    (the inverse of ``positions_from_distances`` for the layouts it builds).

    The old panel's ``_calculate_distances_from_positions``, unchanged.
    """
    pd = {p[0]: p[1] for p in positions}

    r1 = abs(pd[2][1] - pd[1][1])
    r2 = abs(pd[1][1] - pd[0][1])
    r4 = abs(pd[4][1] - pd[3][1])
    r5 = abs(pd[5][1] - pd[4][1])
    r3 = abs(pd[3][0] - pd[0][0])
    r6 = abs(pd[5][0] - pd[2][0])
    r7 = abs(pd[1][0])
    r8 = abs(pd[4][0])
    r9 = pd[1][1]
    r10 = pd[4][1]

    return r1, r2, r3, r4, r5, r6, r7, r8, r9, r10


def _spans_plane(points, span: float) -> bool:
    """True when at least three of ``points`` are not collinear (the test and
    tolerance of SettingsModule.verify_settings)."""
    threshold = COLLINEAR_TOLERANCE * span * span
    for a, b, c in combinations(points, 3):
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(cross) > threshold:
            return True
    return False


def layout_issues(positions) -> list[tuple[str, list[int]]]:
    """Why a receiver layout (``[(x, y)]`` in id order) cannot be saved, as
    ``(message, receiver ids concerned)``; [] when it can.

    These are SettingsModule.verify_settings' receiver-geometry rules, with
    its tolerances and wording, checked before anything is written (it only
    checks the saved file), plus one of the panel's own: two receivers
    cannot share a spot.
    """
    issues = []
    placed = []
    for i, (x, y) in enumerate(positions):
        x, y = float(x), float(y)
        if math.isfinite(x) and math.isfinite(y):
            placed.append((i, (x, y)))
        else:
            issues.append((f"Receiver R{i + 1} position is missing or not a pair of numbers", [i]))
    if len(placed) < 2:
        return issues
    ids = [i for i, _ in placed]
    span = max(math.dist(a, b) for (_, a), (_, b) in combinations(placed, 2))
    if span <= SAME_SPOT_CM:
        issues.append(("Receiver positions not initialized (all receivers are on the same spot)",
                       ids))
        return issues
    for (i, a), (j, b) in combinations(placed, 2):
        if math.dist(a, b) < MIN_RECEIVER_GAP_CM:
            issues.append((f"Receivers R{i + 1} and R{j + 1} are on the same spot "
                           f"({a[0]:g}, {a[1]:g}): each receiver needs its own place", [i, j]))
    if len(placed) >= 3 and not _spans_plane([p for _, p in placed], span):
        issues.append(("Receivers are all on one line; at least 3 of them must not be collinear",
                       ids))
    return issues


def layout_problems(positions) -> list[str]:
    """The messages of ``layout_issues``: [] when the layout can be saved."""
    return [message for message, _ in layout_issues(positions)]


def on_one_spot(positions) -> bool:
    """True while every receiver is on one spot: no layout yet (a new arena)."""
    pts = np.asarray(positions, dtype=float).reshape(-1, 2)
    return len(pts) == 0 or float(np.max(np.ptp(pts, axis=0))) <= SAME_SPOT_CM


def polygon_area(vertices) -> float:
    """Area (cm²) of the polygon through ``vertices`` (closed or not)."""
    v = np.asarray(vertices, dtype=float).reshape(-1, 2)
    if len(v) < 3:
        return 0.0
    x, y = v[:, 0], v[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def map_step(coords, preview: bool = False) -> float:
    """Grid step (cm) of the coverage maps of the receivers at ``coords``."""
    pts = np.asarray(coords, dtype=float).reshape(-1, 2)
    span = (float(np.max(np.ptp(pts, axis=0))) if len(pts) else 0.0) + 2.0 * coverage.MARGIN_CM
    if preview:
        return max(PREVIEW_STEP_CM, float(math.ceil(span / PREVIEW_CELLS)))
    return max(MAP_STEP_CM, float(math.ceil(span / MAP_CELLS)))


def _common_shift(old, new) -> tuple[float, float] | None:
    """``(dx, dy)`` when ``new`` is ``old`` moved by one translation (an
    origin move), else None."""
    if len(old) != len(new) or not old:
        return None
    deltas = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(old, new)]
    dx, dy = deltas[0]
    if not (math.isfinite(dx) and math.isfinite(dy)):
        return None
    if all(abs(ex - dx) <= 1e-6 and abs(ey - dy) <= 1e-6 for ex, ey in deltas):
        return dx, dy
    return None


# ---------------------------------------------------------------------------
# Coverage computation (runs on a worker thread)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoverageRequest:
    """One coverage computation: the inputs, copied, so the GUI may go on
    editing while a worker computes.

    ``coords`` / ``facing`` are the active receivers' (R1..RN); ``model``
    is the receiver model with the sampling window applied.  ``outline`` is
    the closed receiver outline: the area the summary judges (none at all
    when it encloses less than ``MIN_AREA_CM2``, as with the receivers on
    one line); None judges the receivers' box.
    """

    generation: int
    preview: bool
    coords: tuple
    facing: tuple
    model: coverage.SensorModel
    step: float
    outline: tuple | None


@dataclass
class CoverageResult:
    """The receiver-count map of a ``CoverageRequest`` (``counts[j, i]`` at
    ``(xs[i], ys[j])``) and, unless it was a preview, its
    ``coverage.summary``."""

    request: CoverageRequest
    xs: np.ndarray
    ys: np.ndarray
    counts: np.ndarray
    summary: dict | None


def compute_coverage(request: CoverageRequest) -> CoverageResult:
    """The coverage map for ``request`` (and its summary unless it is a
    preview); pure numpy, safe on a worker thread."""
    coords = np.asarray(request.coords, dtype=float).reshape(-1, 2)
    facing = list(request.facing)
    xs, ys, counts = coverage.coverage_map(coords, facing, request.model, request.step)
    summary = None
    if not request.preview:
        if request.outline is None:
            inside = None
        elif polygon_area(request.outline) < MIN_AREA_CM2:
            inside = _nowhere                     # a line has no inside (Path takes its points)
        else:
            inside = Path(np.asarray(request.outline)).contains_points
        summary = coverage.summary(coords, facing, request.model, request.step, inside=inside)
    return CoverageResult(request, xs, ys, counts, summary)


def _nowhere(points) -> np.ndarray:
    """``inside`` of an outline that encloses no area."""
    return np.zeros(len(points), dtype=bool)


def _contour_segments(ax, xs, ys, Z, level: float) -> list[np.ndarray]:
    """The ``level`` contour of ``Z`` as polylines in data coordinates (the
    contour set is made on ``ax`` and taken off again)."""
    finite = Z[np.isfinite(Z)]
    if finite.size == 0 or not finite.min() < level < finite.max():
        return []                                 # nothing crosses it: no line (and no warning)
    cs = ax.contour(xs, ys, Z, levels=[level])
    try:
        return [np.asarray(s, dtype=float) for s in cs.allsegs[0] if len(s) >= 2]
    finally:
        try:
            cs.remove()
        except AttributeError:                    # matplotlib < 3.8
            for collection in cs.collections:
                collection.remove()


def _share_text(value: float) -> str:
    return f"{100.0 * value:.0f} %"


def _style_label(label: QLabel, color: str, px: int = 11, bold: bool = False) -> None:
    """A label's colour and font size (scaled with the window)."""
    ui_scale.style(label, f"color: {color}; font: {'bold ' if bold else ''}{px}px Arial; "
                          f"background: transparent;")


# ---------------------------------------------------------------------------
# Tape-measure distances on the arena
# ---------------------------------------------------------------------------

class _TapeOverlay:
    """The tape-measure distances D1..D10 drawn on the arena (while the
    tape-measure template is open, or always when "Always show distance
    markers" is ticked; never with fewer than six receivers), as the old
    panel drew them: D1-D6 along the receiver outline, D7 / D8 from (0, 0)
    along X, D9 / D10 the Y offsets of R2 and R5 (when not 0)."""

    YELLOW = "#FFFF00"
    OFFSET_GREEN = "#00FF88"
    # (name, receiver ids, data offset of the label, horizontal alignment)
    EDGES = (("D1", 2, 1, (15, 0), "left"), ("D2", 1, 0, (15, 0), "left"),
             ("D3", 0, 3, (0, 10), "center"), ("D4", 3, 4, (-15, 0), "right"),
             ("D5", 4, 5, (-15, 0), "right"), ("D6", 5, 2, (0, -10), "center"))

    def __init__(self, ax):
        def label(color, ha, va="center"):
            return ax.text(0.0, 0.0, "", color=color, fontsize=8, fontweight="bold", zorder=9,
                           ha=ha, va=va, bbox=dict(boxstyle="round,pad=0.2", facecolor="black",
                                                   alpha=0.7, edgecolor=color))

        def line(color):
            return ax.plot([], [], color=color, linewidth=2, linestyle="--", zorder=4)[0]

        self._edge_labels = [label(self.YELLOW, ha) for _, _, _, _, ha in self.EDGES]
        self._x_lines = [line(ORANGE), line(ORANGE)]
        self._x_labels = [label(ORANGE, "center", "bottom"), label(ORANGE, "center", "bottom")]
        self._y_lines = [line(self.OFFSET_GREEN), line(self.OFFSET_GREEN)]
        self._y_labels = [label(self.OFFSET_GREEN, "right"), label(self.OFFSET_GREEN, "left")]
        self._visible = True
        self.set_visible(False)

    def _artists(self) -> list:
        return self._edge_labels + self._x_lines + self._x_labels + self._y_lines + self._y_labels

    def visible(self) -> bool:
        return self._visible

    def set_visible(self, visible: bool) -> None:
        self._visible = bool(visible)
        for artist in self._artists():
            artist.set_visible(self._visible)

    def update(self, positions: list) -> None:
        """Follow the layout ``[(x, y)]`` (id order); hidden parts stay hidden."""
        if not self._visible:
            return
        pd = dict(enumerate(positions))
        d = distances_from_positions(list(enumerate(positions)))
        for (name, a, b, (ox, oy), _), text, value in zip(self.EDGES, self._edge_labels, d[:6]):
            (x1, y1), (x2, y2) = pd[a], pd[b]
            text.set_position(((x1 + x2) / 2 + ox, (y1 + y2) / 2 + oy))
            text.set_text(f"{name}\n{value:.1f}")
        for k, (rid, name, value, side) in enumerate(((1, "D7", d[6], -8.0), (4, "D8", d[7], 8.0))):
            x, y = pd[rid]
            self._x_lines[k].set_data([0.0, x], [0.0, 0.0])
            self._x_labels[k].set_position((x / 2, 8.0))
            self._x_labels[k].set_text(f"{name}\n{value:.1f}")
            offset = abs(y) > 0.1                     # as before: drawn only when not 0
            self._y_lines[k].set_data([x, x], [0.0, y])
            self._y_labels[k].set_position((x + side, y / 2))
            self._y_labels[k].set_text(f"{'D9' if k == 0 else 'D10'}\n{y:.1f}")
            self._y_lines[k].set_visible(offset)
            self._y_labels[k].set_visible(offset)


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

def ask_receiver_count(parent, current: int) -> int | None:
    """Ask how many receivers to place (MIN_RECEIVERS..RECEIVER_COUNT,
    starting at ``current``); None if the user cancelled."""
    dlg = widgets._dialog(parent, "Place all by clicking", MAGENTA)
    layout = QVBoxLayout(dlg)
    ui_scale.margins(layout, 18, 16, 18, 14)
    ui_scale.spacing(layout, 12)
    layout.addWidget(widgets._label("How many receivers?", MAGENTA, 15, bold=True))
    note = widgets._label(
        f"Receivers to use, {MIN_RECEIVERS} to {RECEIVER_COUNT} (a fix needs "
        f"{NEEDED_FOR_FIX}). You then click the arena where each goes, R1 first. The others "
        f"stay in config.toml with their calibration but are not used.", DIM, 12, wrap=True)
    ui_scale.min_width(note, 360)
    layout.addWidget(note)
    row = QHBoxLayout()
    ui_scale.spacing(row, 8)
    spin = QSpinBox()
    spin.setRange(MIN_RECEIVERS, RECEIVER_COUNT)
    spin.setValue(max(MIN_RECEIVERS, min(int(current), RECEIVER_COUNT)))
    ui_scale.style(spin, "QSpinBox { background-color: #222222; color: white; "
                         "border: 1px solid #444444; border-radius: 3px; padding: 3px 6px; "
                         "font: 12px Arial; }")
    ui_scale.min_width(spin, 70)
    which = widgets._label("", "white", 12)

    def show_which(n: int) -> None:
        which.setText(f"receivers: R1–R{n}")

    spin.valueChanged.connect(show_which)
    show_which(spin.value())
    row.addWidget(spin)
    row.addWidget(which, 1)
    layout.addLayout(row)
    buttons, _made = widgets._button_row(dlg, [("Place", MAGENTA, "black", True),
                                               ("Cancel", "#555555", "white", False)])
    layout.addLayout(buttons)
    spin.setFocus()
    dlg.adjustSize()
    try:
        if not widgets._accepted(widgets._exec(dlg)):
            return None
        return int(spin.value())
    finally:
        dlg.deleteLater()


class SensorDialog(QDialog):
    """Edit every figure of the receiver model (``coverage.SensorModel``)
    except the sampling window, which the panel's Window field sets.

    Blank "Reception angle" or "Max distance": no such limit.  The model
    validates itself; while it refuses the input its message is shown and
    OK is disabled.  ``value()`` is the model, or None while invalid.
    """

    #: (field, caption, unit, tooltip, kind): kind "opt" (blank = no limit),
    #: "num" or "int"; signal-strength-only fields are in SIGNAL_FIELDS.
    FIELDS = (
        ("fov_half_deg", "Reception angle", "± °",
         "How far off its axis a receiver hears the transmitter, degrees either side "
         "(the HC-SR04 datasheet: ±15°). Blank: no limit (the signal decides)."),
        ("max_range_cm", "Max distance", "cm",
         "Nothing farther is heard (the HC-SR04 datasheet: 400 cm). Blank: no hard limit."),
        ("min_range_cm", "Min distance", "cm",
         "Nothing closer is heard (the HC-SR04 datasheet: 2 cm); 0 for no limit."),
        ("snr_db", "Signal margin", "dB",
         "The burst's strength over the receiver's detection threshold on axis at 1 m, "
         "before air absorption."),
        ("rx_beam_deg", "Beam width", "± °",
         "The receiver's −6 dB beam half-angle, degrees (0 to 90): how fast the signal falls "
         "off the axis."),
        ("rise_cycles", "Ring-up", "cycles",
         "The transducer's ring-up time constant, in 40 kHz cycles."),
        ("burst_cycles", "Burst length", "cycles",
         "Cycles in the transmitter's burst (whole number)."),
        ("temperature_c", "Temperature", "°C",
         "Air temperature: the speed of sound (and, for the signal, air absorption)."),
        ("humidity_pct", "Humidity", "%", "Relative humidity, 0 to 100 %."),
        ("burst_lead_ms", "Burst lead", "ms",
         "The transmitter fires this long after the receivers' trigger: the base station's "
         "burst delay plus radio and processing latency. Shorter than the receiver-ready "
         "time leaves a blind zone round each receiver."),
        ("rx_ready_ms", "Receiver ready", "ms",
         "The receivers listen from this long after their trigger."),
        ("timeout_ms", "Module timeout", "ms",
         "The HC-SR04 gives up this long after it starts listening."),
    )
    OPTIONAL_FIELDS = ("fov_half_deg", "max_range_cm")
    INT_FIELDS = ("burst_cycles",)
    SIGNAL_FIELDS = ("snr_db", "rx_beam_deg", "rise_cycles", "burst_cycles")

    def __init__(self, parent=None, model: coverage.SensorModel = DEFAULT_SENSOR,
                 accent: str = GREEN):
        super().__init__(parent)
        self.setWindowTitle("Sensor settings")
        self.setModal(True)
        ui_scale.style(self, "QDialog { background-color: black; }" + widgets._input_css(accent))
        self._base = model
        self._model: coverage.SensorModel | None = model

        layout = QVBoxLayout(self)
        ui_scale.margins(layout, 18, 16, 18, 14)
        ui_scale.spacing(layout, 10)
        layout.addWidget(widgets._label("Sensor settings", accent, 15, bold=True))
        note = widgets._label(
            f"What a receiver can hear, for the coverage estimates (saved in {SENSOR_FILENAME} "
            "in the config folder; the Calibration panel uses it too). The sampling window is "
            "set beside the estimate.", DIM, 12, wrap=True)
        ui_scale.min_width(note, 420)
        layout.addWidget(note)

        grid = QGridLayout()
        ui_scale.spacing(grid, 6)
        self._edits: dict[str, QLineEdit] = {}
        self._captions: dict[str, QLabel] = {}
        row = 0
        for name, caption, unit, tip in self.FIELDS:
            if name == "snr_db":                           # the mode goes before its own fields
                grid.addWidget(widgets._label("Detection", DIM, 12), row, 0)
                self._mode_combo = QComboBox()
                self._mode_combo.addItem("Hard limits (angle and distance only)", False)
                self._mode_combo.addItem("Signal strength over a threshold", True)
                self._mode_combo.setToolTip(
                    "Hard limits: a receiver hears everything within the reception angle and "
                    "distances above. Signal strength: the signal margin, beam width and "
                    "ring-up below decide the range as well.")
                self._mode_combo.currentIndexChanged.connect(self._on_edited)
                grid.addWidget(self._mode_combo, row, 1, 1, 2)
                row += 1
            prominent = name == "fov_half_deg"
            label = widgets._label(caption, accent if prominent else DIM, 13 if prominent else 12,
                                   bold=prominent)
            label.setToolTip(tip)
            edit = QLineEdit()
            edit.setAlignment(Qt.AlignmentFlag.AlignRight)
            edit.setToolTip(tip)
            if name in self.OPTIONAL_FIELDS:
                edit.setPlaceholderText("no limit")
            ui_scale.min_width(edit, 90)
            edit.textEdited.connect(self._on_edited)
            self._edits[name] = edit
            self._captions[name] = label
            grid.addWidget(label, row, 0)
            grid.addWidget(edit, row, 1)
            grid.addWidget(widgets._label(unit, DIM, 12), row, 2)
            row += 1
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self._describe = widgets._label("", "white", 11, wrap=True)
        layout.addWidget(self._describe)
        self._problem = widgets._label("", RED, 11, wrap=True)
        layout.addWidget(self._problem)

        reset_row = QHBoxLayout()
        reset_btn = widgets.neon_button("Reset to HC-SR04 datasheet values", "#555555", "white",
                                        font_px=11, padding=(4, 8),
                                        tooltip=DEFAULT_SENSOR.describe())
        reset_btn.clicked.connect(lambda: self.set_model(DEFAULT_SENSOR))
        reset_row.addWidget(reset_btn)
        reset_row.addStretch(1)
        layout.addLayout(reset_row)

        buttons, made = widgets._button_row(self, [("OK", GREEN, "black", True),
                                                   ("Cancel", "#555555", "white", False)])
        self._ok = made[0]
        layout.addLayout(buttons)
        self.set_model(model)
        self.adjustSize()

    def value(self) -> coverage.SensorModel | None:
        """The model the input describes, or None while it is invalid."""
        return self._model

    def set_model(self, model: coverage.SensorModel) -> None:
        """Fill the fields from ``model``."""
        for name, edit in self._edits.items():
            value = getattr(model, name)
            edit.setText("" if value is None else widgets.format_number(value, 3))
        self._mode_combo.blockSignals(True)
        self._mode_combo.setCurrentIndex(1 if model.threshold else 0)
        self._mode_combo.blockSignals(False)
        self._on_edited()

    def set_field(self, name: str, text: str) -> None:
        """Type ``text`` into field ``name`` (as the user would)."""
        self._edits[name].setText(text)
        self._on_edited()

    def set_threshold(self, on: bool) -> None:
        self._mode_combo.setCurrentIndex(1 if on else 0)

    def _on_edited(self, *_args) -> None:
        threshold = bool(self._mode_combo.currentData())
        for name in self.SIGNAL_FIELDS:
            self._edits[name].setEnabled(threshold)
            self._captions[name].setEnabled(threshold)
        changes: dict = {"threshold": threshold}
        bad = []
        for name, edit in self._edits.items():
            text = edit.text().strip()
            if not text and name in self.OPTIONAL_FIELDS:
                value = None
            else:
                value = widgets.parse_number(text)
                if value is not None and name in self.INT_FIELDS:
                    value = int(value) if float(value).is_integer() else None
            invalid = value is None and not (not text and name in self.OPTIONAL_FIELDS)
            widgets._mark_invalid(edit, invalid)
            if invalid:
                bad.append(self._captions[name].text())
            else:
                changes[name] = value
        if bad:
            self._model = None
            problem = f"Not a number: {', '.join(bad)}."
        else:
            try:
                self._model = self._base.copy(name=DEFAULT_SENSOR.name, **changes)
                problem = ""
            except ValueError as exc:
                self._model = None
                problem = str(exc).split(": ", 1)[-1]        # without the model's name
                for name, caption, _unit, _tip in self.FIELDS:   # in the dialog's words
                    problem = problem.replace(name, caption)
        self._problem.setText(problem)
        self._problem.setVisible(bool(problem))
        self._describe.setText(f"{self._model.describe()}." if self._model is not None else "")
        self._ok.setEnabled(self._model is not None)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class ArenaMakerPanel(QWidget):
    """Full-page arena configuration panel (see the module docstring).

    The panel edits a working copy of the layout: the receiver count N
    (``receiver_count()``) and the active receivers' positions and facings
    (``positions()`` / ``facings()``, R1..RN).  It keeps all six receivers'
    positions internally, so lowering N and raising it again brings the
    dormant ones back where they were.  ``is_dirty()`` tells whether the
    count or an active receiver differs from the saved layout.  The
    configuration is written by "Verify & Save" (``verify_and_save``) and by
    the origin tool.
    """

    def __init__(self, settings_module: SettingsModule, main_window):
        super().__init__()
        self._settings = settings_module
        self._main_window = main_window
        self._client = getattr(main_window, "client", None)
        self._position_lib = getattr(main_window, "position_lib", None)
        # Scoped: an unscoped background would cascade into the panel's dialogs
        self.setObjectName("arenaMakerPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        ui_scale.style(self, "QWidget#arenaMakerPanel { background-color: black; }")

        # The layout being edited and the saved one: all six receivers (id
        # order), of which the first _count (_saved_count) are active
        self._count = RECEIVER_COUNT
        self._positions: list[tuple[float, float]] = [(0.0, 0.0)] * RECEIVER_COUNT
        self._facings: list[float | None] = [None] * RECEIVER_COUNT
        self._saved_count = self._count
        self._saved_positions = list(self._positions)
        self._saved_facings = list(self._facings)
        # The receiver model in use, and as last read from sensor.toml (a model
        # that could not be saved stays in use until the file changes)
        self._sensor_saved = self._read_sensor()
        self._sensor = self._sensor_saved
        self._cal_points: list[tuple[float, float]] = []
        self._load_problem = ""                 # what could not be read from the settings
        self._config_syncs = 0                  # config_changed signals handled

        # Panel state
        self._active = False                    # shown (between on_panel_show and _hide)
        self._fitted = False                    # the view has been fitted once
        self._drag: tuple[int, float, float] | None = None   # receiver being dragged, where
        self._hover: tuple[float, float] | None = None       # the pointer over the arena
        self._advancing = False                 # "Place all" is moving to the next receiver
        self._placing: list | None = None       # "Place all": the receivers clicked so far
        self._before_placing: tuple | None = None   # (positions, facings, count) to go back to
        self._tape_dirty = False
        self._prev_d9 = 0.0                     # D9 / D10 as last shown (see _on_tape_finished)
        self._prev_d10 = 0.0

        # Coverage computation: one worker at a time, newest request pending
        self._generation = 0
        self._worker: widgets.Worker | None = None
        self._pending: bool | None = None       # a request waits (True: a preview)
        self._result: CoverageResult | None = None
        self._summary: dict | None = None
        self._window_worker: widgets.Worker | None = None
        self._footprint_cache: dict = {}        # model -> (angles, reach)

        self._build_figure()
        self._build_ui()

        self._coverage_timer = QTimer(self)
        self._coverage_timer.setSingleShot(True)
        self._coverage_timer.setInterval(COVERAGE_DEBOUNCE_MS)
        self._coverage_timer.timeout.connect(lambda: self._start_job(False))

        self._sync_with_settings()
        self._update_link_state(self._link_up())
        main_window.config_changed.connect(self._on_config_changed)
        main_window.link_changed.connect(self._on_link_changed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def receiver_count(self) -> int:
        """N, the receivers the layout being edited uses (R1..RN)."""
        return self._count

    def positions(self) -> list[tuple[float, float]]:
        """The active receivers' positions being edited, R1 first."""
        return self._positions[:self._count]

    def facings(self) -> list[float | None]:
        """The active receivers' facings being edited (None: aimed at the centroid)."""
        return self._facings[:self._count]

    def is_dirty(self) -> bool:
        """Whether the receiver count or an active receiver differs from the
        saved layout."""
        n = self._count
        return (n != self._saved_count or self._positions[:n] != self._saved_positions[:n]
                or self._facings[:n] != self._saved_facings[:n])

    def model(self) -> coverage.SensorModel:
        """The receiver model with the sampling window applied."""
        return self._sensor.copy(window_ms=float(self._window_spin.value()))

    def sensor_model(self) -> coverage.SensorModel:
        """The receiver model as set in "Sensor settings..." (its own window)."""
        return self._sensor

    def set_sensor_model(self, model: coverage.SensorModel) -> None:
        """Use ``model`` for the coverage estimate (the panel's sampling
        window still applies).  Nothing is saved: "Sensor settings..." saves
        to sensor.toml (``SettingsModule.set_sensor_model``)."""
        if not isinstance(model, coverage.SensorModel):
            raise TypeError(f"expected a coverage.SensorModel, got {model!r}")
        if model == self._sensor:
            return
        self._sensor = model
        self._on_model_changed()

    def set_layout(self, positions, facings=None) -> None:
        """Edit the layout: ``positions`` ``[(x, y)]`` or ``[(id, (x, y))]``
        of R1..RN in id order, MIN_RECEIVERS <= N <= RECEIVER_COUNT (this
        sets the receiver count), ``facings`` one per receiver (None or a
        non-finite entry: aimed at the centroid; None keeps the current
        ones).  The dormant receivers are kept.  Not saved.

        Raises:
            ValueError: Not MIN_RECEIVERS to RECEIVER_COUNT finite positions
                (and one number or None as facing each); nothing is changed
                then.
        """
        xy = [p[1] if len(p) == 2 and np.ndim(p[1]) == 1 else p for p in positions]
        pts = np.asarray(xy, dtype=float)
        n = len(pts)
        if (pts.ndim != 2 or pts.shape[1:] != (2,) or not MIN_RECEIVERS <= n <= RECEIVER_COUNT
                or not np.all(np.isfinite(pts))):
            raise ValueError(f"expected {MIN_RECEIVERS} to {RECEIVER_COUNT} finite (x, y) "
                             f"positions, got {xy!r}")
        full_facings = None
        if facings is not None:
            if len(facings) != n:
                raise ValueError(f"expected {n} facings, got {len(facings)}")
            try:                                          # checked before anything changes
                facings = [None if f is None else float(f) for f in facings]
            except (TypeError, ValueError):
                raise ValueError(f"facings must be numbers or None, got {facings!r}") from None
            full_facings = facings + self._facings[n:]
        full = [(float(x), float(y)) for x, y in pts] + self._positions[n:]
        self._apply_layout(positions=full, facings=full_facings, count=n, refit=True)

    def coverage_result(self) -> CoverageResult | None:
        """The coverage map on show (None before the first computation)."""
        return self._result

    def summary(self) -> dict | None:
        """``coverage.summary`` of the layout on show (None before the first
        full computation)."""
        return self._summary

    def verify_and_save(self) -> bool:
        """"Verify & Save": True if the layout was saved (or unchanged)."""
        return self._verify_and_save()

    # ------------------------------------------------------------------
    # Figure
    # ------------------------------------------------------------------

    def _build_figure(self) -> None:
        self._fig = Figure(facecolor="black")
        ax = self._fig.add_subplot(111)
        self._view = ArenaView(ax, title="Arena coverage", padding=VIEW_PADDING_CM)
        self._view.set_draggable(RECEIVERS, True)
        self._legend_beside: bool | None = None
        self._place_legend(beside=True)
        self._canvas = ScalingCanvas(self._fig, ref_size=(820, 780))
        self._canvas.installEventFilter(self)
        self._tape = _TapeOverlay(ax)

        self._interactor = ArenaInteractor(self._canvas, self._view, snap=1.0)
        self._interactor.set_readout_formatter(self._readout_text)
        self._interactor.clicked.connect(self._on_arena_clicked)
        self._interactor.point_pressed.connect(self._on_point_pressed)
        self._interactor.point_dragged.connect(self._on_point_dragged)
        self._interactor.drag_finished.connect(self._on_drag_finished)
        self._interactor.hovered.connect(self._on_hovered)
        self._interactor.left.connect(self._on_left)
        self._canvas.mpl_connect("scroll_event", self._on_scroll)
        self._canvas.mpl_connect("key_press_event", self._on_canvas_key)

    # ------------------------------------------------------------------
    # Qt UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Top bar
        top_bar = QWidget()
        top_layout = QHBoxLayout(top_bar)
        ui_scale.margins(top_layout, 10, 5, 10, 5)
        ui_scale.spacing(top_layout, 8)
        title = QLabel("Arena Maker")
        ui_scale.font(title, 16, bold=True)
        ui_scale.style(title, f"color: {CYAN}; background: transparent;")
        top_layout.addWidget(title)
        # A long problem (a config path, say) wraps in the room left: it never
        # pushes the buttons off a small window (full text in the tooltip)
        self._state_label = QLabel()
        self._state_label.setWordWrap(True)
        self._state_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top_layout.addWidget(self._state_label, 1)
        self._revert_btn = widgets.neon_button("Revert", "#555555", "white",
                                               tooltip="Discard the unsaved layout changes")
        self._revert_btn.clicked.connect(self._on_revert)
        top_layout.addWidget(self._revert_btn)
        self._save_btn = widgets.neon_button(
            "Verify && Save", GOLD,
            tooltip="Check the layout, save the receiver positions and facings, and verify "
                    "the whole configuration")
        self._save_btn.clicked.connect(self._verify_and_save)
        top_layout.addWidget(self._save_btn)
        back_btn = widgets.neon_button("← Back", GREEN)
        back_btn.clicked.connect(lambda: self._leave("main"))
        top_layout.addWidget(back_btn)
        self._back_btn = back_btn
        layout.addWidget(top_bar)

        body = QHBoxLayout()
        body.setSpacing(0)
        layout.addLayout(body, 1)

        # Arena: a tool row over the canvas
        arena = QWidget()
        arena_layout = QVBoxLayout(arena)
        ui_scale.margins(arena_layout, 6, 0, 4, 4)
        ui_scale.spacing(arena_layout, 4)
        tools = QHBoxLayout()
        ui_scale.spacing(tools, 6)
        self._hint = QLabel()
        self._hint.setWordWrap(True)
        _style_label(self._hint, DIM)
        tools.addWidget(self._hint, 1)
        self._cancel_place_btn = widgets.neon_button(
            "Cancel placing", MAGENTA, "black", font_px=11, padding=(3, 8),
            tooltip="Stop placing and put the previous layout back (Esc)")
        self._cancel_place_btn.clicked.connect(self._leave_pick_modes)
        self._cancel_place_btn.setVisible(False)
        tools.addWidget(self._cancel_place_btn)
        self._snap_combo = QComboBox()
        for text, step in SNAP_STEPS:
            self._snap_combo.addItem(text, step)
        self._snap_combo.setToolTip("Round clicked and dragged positions to this step")
        self._snap_combo.currentIndexChanged.connect(self._on_snap_changed)
        tools.addWidget(self._snap_combo)
        fit_btn = widgets.neon_button("Fit view", "#555555", "white", font_px=11, padding=(3, 8),
                                      tooltip="Show the whole layout (the mouse wheel zooms)")
        fit_btn.clicked.connect(self._fit_view)
        tools.addWidget(fit_btn)
        arena_layout.addLayout(tools)
        arena_layout.addWidget(self._canvas, 1)
        ui_scale.style(arena, _SIDE_CSS)
        body.addWidget(arena, 1)

        # Side panel
        side = QWidget()
        ui_scale.style(side, _SIDE_CSS)
        column = QVBoxLayout(side)
        ui_scale.margins(column, 8, 6, 8, 8)
        ui_scale.spacing(column, 8)
        column.addWidget(self._build_receivers_section())
        # (0, 0) right under the receivers it is usually measured from
        self._origin_tool = widgets.OriginTool(accent=BLUE)
        self._origin_tool.pickModeChanged.connect(self._on_origin_pick_mode)
        self._origin_tool.previewChanged.connect(self._on_origin_preview)
        self._origin_tool.originRequested.connect(self._on_origin_requested)
        self._origin_tool.measuringChanged.connect(self._on_measuring_changed)
        column.addWidget(self._origin_tool)
        column.addWidget(self._build_coverage_section())
        column.addWidget(self._build_templates_section())
        column.addStretch(1)
        scroll = widgets.scroll_area(side)
        self._side_scroll = scroll
        ui_scale.min_width(scroll, 340)
        ui_scale.max_width(scroll, 400)
        body.addWidget(scroll)
        self._update_hint()

    @staticmethod
    def _note(text: str = "", color: str = DIM, px: int = 11) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        _style_label(label, color, px)
        return label

    def _build_receivers_section(self) -> QWidget:
        box = widgets.Section("Receivers", MAGENTA)
        box.add(self._note("Select a receiver, then type its X / Y, click Pick on arena, or drag "
                           "it on the arena. Facing: degrees, 0 = +X, 90 = +Y; blank = auto "
                           "(aimed at the receivers' centroid)."))
        # One row per active receiver (set_points sets the rows; no + / −)
        self._table = widgets.PointTable(prefix="R", extra_columns=[("Facing (°)", "auto")],
                                         accent=MAGENTA, min_rows=MIN_RECEIVERS,
                                         max_rows=RECEIVER_COUNT)
        self._table.pointsEdited.connect(self._on_table_edited)
        self._table.selectionChanged.connect(self._on_selection_changed)
        self._table.pickModeChanged.connect(self._on_table_pick_mode)
        box.add(self._table)

        self._place_all_btn = widgets.neon_button(
            "Place all by clicking", MAGENTA, "black", font_px=11, padding=(4, 8), checkable=True,
            tooltip=f"Choose how many receivers to use ({MIN_RECEIVERS} to {RECEIVER_COUNT}), "
                    f"then click the arena where R1 goes, then R2, ...: every receiver must be "
                    f"placed (Esc puts the previous layout back)")
        self._place_all_btn.toggled.connect(self._on_place_all)
        box.add(self._place_all_btn)
        row = QHBoxLayout()
        ui_scale.spacing(row, 6)
        aim_btn = widgets.neon_button("Aim at…", MAGENTA, "black", font_px=11, padding=(4, 8),
                                      tooltip="Point the selected receiver at a point (X / Y, an "
                                              "offset, or a distance and angle from a reference)")
        aim_btn.clicked.connect(self._on_aim_at)
        aim_btn.setEnabled(False)                         # until a receiver is selected
        self._aim_btn = aim_btn
        auto_btn = widgets.neon_button("Auto-aim all", "#555555", "white", font_px=11,
                                       padding=(4, 8),
                                       tooltip="Aim every receiver at the receivers' centroid")
        auto_btn.clicked.connect(self._on_auto_aim)
        row.addWidget(aim_btn)
        row.addWidget(auto_btn)
        row.addStretch(1)
        box.add_layout(row)
        self._count_note = self._note()
        box.add(self._count_note)
        return box

    def _build_coverage_section(self) -> QWidget:
        box = widgets.Section("Coverage estimate", GREEN)
        grid = QGridLayout()
        ui_scale.spacing(grid, 6)
        self._view_combo = QComboBox()
        for key, text in MAP_VIEWS:
            self._view_combo.addItem(text, key)
        self._view_combo.setCurrentIndex(self._view_combo.findData(DEFAULT_VIEW))
        self._view_combo.setToolTip("Shade the arena by how many receivers hear each spot, "
                                    "or not at all (the default)")
        self._view_combo.currentIndexChanged.connect(self._on_view_changed)
        grid.addWidget(QLabel("Show"), 0, 0)
        grid.addWidget(self._view_combo, 0, 1, 1, 2)

        self._window_spin = QSpinBox()
        self._window_spin.setRange(SAMPLING_TIME_MIN_MS, SAMPLING_TIME_MAX_MS)
        self._window_spin.setSuffix(" ms")
        self._window_spin.setValue(DEFAULT_SAMPLING_TIME_MS)
        self._window_spin.setKeyboardTracking(False)
        self._window_spin.setToolTip(
            "The base station's sampling window (the ground station's H setting, set on the "
            "Base Station panel). A burst detected after it is lost, so it caps the range at "
            "about (window − 4 ms) × the speed of sound. Change it here to see its effect; "
            "Read asks the base station for the window in force.")
        self._window_spin.valueChanged.connect(self._on_window_changed)
        self._read_btn = widgets.neon_button("Read", GREEN, "black", font_px=11, padding=(3, 8),
                                             tooltip="Ask the base station for its sampling window")
        self._read_btn.clicked.connect(self._read_window)
        grid.addWidget(QLabel("Window"), 1, 0)
        grid.addWidget(self._window_spin, 1, 1)
        grid.addWidget(self._read_btn, 1, 2)
        grid.setColumnStretch(1, 1)
        box.add_layout(grid)
        self._window_status = self._note()
        self._window_status.setVisible(False)
        box.add(self._window_status)

        sensor_row = QHBoxLayout()
        ui_scale.spacing(sensor_row, 6)
        sensor_btn = widgets.neon_button(
            "Sensor settings…", GREEN, "black", font_px=11, padding=(4, 8),
            tooltip="What a receiver can hear: its reception angle, distances and the rest of "
                    f"the model (saved in {SENSOR_FILENAME} in the config folder)")
        sensor_btn.clicked.connect(self._on_sensor_settings)
        sensor_row.addWidget(sensor_btn)
        sensor_row.addStretch(1)
        box.add_layout(sensor_row)
        # The model in one line (white): what a receiver hears
        self._model_note = self._note("", "white")
        self._model_note.setTextFormat(Qt.TextFormat.PlainText)
        box.add(self._model_note)

        checks = QHBoxLayout()
        ui_scale.spacing(checks, 10)
        self._beams_check = QCheckBox("Beams")
        self._beams_check.setChecked(True)
        self._beams_check.setToolTip("Draw each receiver's reception angle as a wedge round its "
                                     "facing (its −6 dB beam when the angle has no limit)")
        self._beams_check.toggled.connect(lambda _on: self._draw_layout())
        self._footprint_check = QCheckBox("Selected receiver's reach")
        self._footprint_check.setChecked(True)
        self._footprint_check.setToolTip("Outline where the selected receiver hears the "
                                         "transmitter, with the sensor settings")
        self._footprint_check.toggled.connect(lambda _on: self._draw_selection())
        checks.addWidget(self._beams_check)
        checks.addWidget(self._footprint_check)
        checks.addStretch(1)
        box.add_layout(checks)

        # The layout's numbers
        self._summary_grid = QGridLayout()
        ui_scale.spacing(self._summary_grid, 4)
        self._summary_rows = (
            ("share_3", f"Fix possible (≥ {NEEDED_FOR_FIX} hear)"),
            ("share_4", f"One spare (≥ {NEEDED_FOR_FIX + 1} hear)"),
            ("share_all", "All receivers hear"),
            ("min_count", "Fewest hearing a spot"),
        )
        self._summary_captions: dict[str, QLabel] = {}
        self._summary_cells: dict[str, QLabel] = {}
        for row, (key, text) in enumerate(self._summary_rows):
            caption = self._note(text, "white", 11)
            self._summary_captions[key] = caption
            self._summary_grid.addWidget(caption, row, 0)
            cell = QLabel("–")
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._summary_cells[key] = cell
            self._summary_grid.addWidget(cell, row, 1)
        self._summary_grid.setColumnStretch(0, 3)
        self._summary_grid.setColumnStretch(1, 2)
        box.add_layout(self._summary_grid)
        self._summary_note = self._note()
        box.add(self._summary_note)
        # What the model assumes, and how to read the map
        self._view_note = self._note()
        box.add(self._view_note)
        box.add(self._note(MODEL_NOTE))
        self._coverage_error = self._note("", RED)
        self._coverage_error.setVisible(False)
        box.add(self._coverage_error)
        self._update_model_note()
        return box

    def _build_templates_section(self) -> QWidget:
        box = widgets.Section("Templates", CYAN)
        box.add(self._note("Six-receiver layouts: using one sets 6 receivers."))
        self._markers_check = QCheckBox("Always show distance markers")
        self._markers_check.setChecked(False)
        self._markers_check.setToolTip(
            "Draw the tape-measure distances D1–D10 on the arena all the time, not only while "
            "the tape-measure template is open (six receivers only)")
        self._markers_check.toggled.connect(lambda _on: self._update_tape_visibility())
        box.add(self._markers_check)
        box.add(self._build_rectangle_section())
        box.add(self._build_tape_section())
        return box

    def _build_rectangle_section(self) -> QWidget:
        box = widgets.Section("Rectangle W × H", CYAN, collapsible=True, expanded=False)
        self._templates_box = box
        box.add(self._note("R1–R3 up the left side, R4–R6 up the right side (corners and "
                           "middles), centred on (0, 0). Facings are kept."))
        row = QHBoxLayout()
        ui_scale.spacing(row, 6)
        self._width_edit = QLineEdit()
        self._height_edit = QLineEdit()
        for caption, edit in (("W", self._width_edit), ("H", self._height_edit)):
            edit.setPlaceholderText("cm")
            ui_scale.min_width(edit, 50)
            edit.returnPressed.connect(self._on_build_rectangle)
            row.addWidget(QLabel(caption))
            row.addWidget(edit, 1)
        build_btn = widgets.neon_button("Build", CYAN, "black", font_px=11, padding=(4, 10))
        build_btn.clicked.connect(self._on_build_rectangle)
        row.addWidget(build_btn)
        box.add_layout(row)
        self._rect_status = self._note("", RED)
        self._rect_status.setVisible(False)
        box.add(self._rect_status)
        return box

    def _build_tape_section(self) -> QWidget:
        box = widgets.Section("Tape-measure distances", CYAN, collapsible=True, expanded=False)
        self._tape_box = box
        box.toggled.connect(lambda _expanded: self._update_tape_visibility())
        box.add(self._note("Distances measured between the receivers (D1–D10, the old R1–R10; "
                           "marked on the arena while this is open). A change rebuilds the "
                           "two-column layout; D9 / D10 move R2 / R5 alone."))
        self._tape_count_note = self._note("", AMBER)
        self._tape_count_note.setVisible(False)
        box.add(self._tape_count_note)
        grid = QGridLayout()
        ui_scale.spacing(grid, 4)
        self._tape_edits: dict[str, QLineEdit] = {}
        for row, (name, what) in enumerate(TAPE_DISTANCES):
            caption = QLabel(name)
            _style_label(caption, CYAN, bold=True)
            edit = QLineEdit()
            edit.setAlignment(Qt.AlignmentFlag.AlignRight)
            ui_scale.min_width(edit, 60)
            ui_scale.max_width(edit, 90)
            edit.textEdited.connect(self._on_tape_edited)
            edit.editingFinished.connect(self._on_tape_finished)
            self._tape_edits[name] = edit
            grid.addWidget(caption, row, 0)
            grid.addWidget(edit, row, 1)
            grid.addWidget(self._note(what, DIM, 11), row, 2)
        grid.setColumnStretch(2, 1)
        box.add_layout(grid)
        self._tape_status = self._note()
        self._tape_status.setVisible(False)
        box.add(self._tape_status)
        return box

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _read_settings(self) -> tuple[int, list, list, list, str]:
        """``(count, positions, facings, cal_points, problem)`` as saved:
        the receiver count N and all six receivers (a dormant one without a
        usable position is put at (0, 0) without complaint)."""
        count = int(self._settings.number_of_receivers)
        positions, missing = [], []
        for rid in range(RECEIVER_COUNT):
            position = self._settings.get_receiver_position(rid)
            try:
                x, y = (float(v) for v in position)
                if not (math.isfinite(x) and math.isfinite(y)):
                    raise ValueError
            except (TypeError, ValueError):
                x, y = 0.0, 0.0
                if rid < count:
                    missing.append(f"R{rid + 1}")
            positions.append((x, y))
        problem = (f"config.toml has no usable position for {', '.join(missing)}"
                   if missing else "")
        facings = list(self._settings.get_receiver_facings())
        facings = (facings + [None] * RECEIVER_COUNT)[:RECEIVER_COUNT]
        try:
            cal_points = [(float(p[0]), float(p[1])) for p in self._settings.cal_points]
        except ValueError as exc:
            cal_points = []
            problem = f"{problem}; {exc}" if problem else str(exc)
        return count, positions, facings, cal_points, problem

    def _read_sensor(self) -> coverage.SensorModel:
        """The receiver model saved in sensor.toml (DEFAULT_SENSOR if missing
        or unreadable; the reason is in the settings' ``sensor_problem``)."""
        try:
            return self._settings.sensor_model()
        except Exception:                         # never let a bad file break the panel
            log.exception("Could not read the sensor settings")
            return DEFAULT_SENSOR

    def _sync_sensor(self) -> None:
        """Take the saved receiver model over if sensor.toml changed since it
        was last read (a model that could not be saved is otherwise kept)."""
        saved = self._read_sensor()
        changed = saved != self._sensor_saved
        self._sensor_saved = saved
        if changed:
            self.set_sensor_model(saved)
        else:
            self._update_model_note()                     # a problem may have come or gone

    def _sync_with_settings(self) -> None:
        """Take the saved layout (and receiver count) over.  Unsaved edits are
        kept when the saved receivers are unchanged, and moved with them after
        a pure translation (an origin move); otherwise they are dropped.  The
        receiver model follows sensor.toml."""
        self._sync_sensor()
        try:
            count, positions, facings, cal_points, problem = self._read_settings()
        except Exception as exc:                  # a broken configuration: keep what is shown
            log.exception("Could not read the settings")
            self._set_state(f"Could not read the settings: {exc}", RED)
            return
        dirty = self.is_dirty()
        n = self._saved_count                     # judged on the receivers that were saved
        shift = (_common_shift(self._saved_positions[:n], positions[:n])
                 if count == self._saved_count else None)
        dropped = False
        if not dirty:
            working, working_facings, working_count = positions, facings, count
        elif shift is not None:
            dx, dy = shift
            working = [(x + dx, y + dy) for x, y in self._positions]
            working_facings, working_count = self._facings, self._count
        else:
            working, working_facings, working_count = positions, facings, count
            dropped = True
        if self._placing is not None:
            # "Place all" runs: the layout to go back to follows the same rule,
            # the receivers clicked so far move with an origin shift, and the
            # receiver count being placed stays
            b_positions, b_facings, b_count = self._before_placing
            b_dirty = (b_count != n or b_positions[:b_count] != self._saved_positions[:b_count]
                       or b_facings[:b_count] != self._saved_facings[:b_count])
            if shift is not None:
                dx, dy = shift
                self._placing = [(x + dx, y + dy) for x, y in self._placing]
            if b_dirty and shift is not None:
                b_positions = [(x + dx, y + dy) for x, y in b_positions]
            else:
                b_positions, b_facings, b_count = list(positions), list(facings), count
            self._before_placing = (b_positions, b_facings, b_count)
            working_count = self._count
        self._saved_count = count
        self._saved_positions, self._saved_facings = list(positions), list(facings)
        self._cal_points = cal_points
        self._load_problem = problem
        moved_frame = shift is not None and shift != (0.0, 0.0)
        self._apply_layout(working, working_facings, count=working_count,
                           refit=moved_frame or not self._fitted)
        self._draw_cal_points()
        if dropped:
            self._set_state("The saved layout changed: unsaved edits were dropped", AMBER)
        if on_one_spot(positions[:count]):
            self._templates_box.set_expanded(True)          # a new arena: offer the templates

    def _on_config_changed(self) -> None:
        self._config_syncs += 1
        self._sync_with_settings()

    def _notify_config_changed(self) -> None:
        """Tell the main window that the configuration changed (it reloads the
        settings and the position library and emits config_changed, which
        brings this panel up to date); if that fails, catch up anyway."""
        before = self._config_syncs
        try:
            self._main_window.notify_config_changed()
        except Exception as exc:                          # never let a reload error escape a slot
            log.exception("notify_config_changed failed")
            widgets.error(self, "Configuration reload failed", str(exc))
        if self._config_syncs == before:
            self._sync_with_settings()

    # ------------------------------------------------------------------
    # The working layout
    # ------------------------------------------------------------------

    def _apply_layout(self, positions=None, facings=None, source: str | None = None,
                      refit: bool = False, count: int | None = None) -> None:
        """Make ``positions`` / ``facings`` (all six receivers) and ``count``
        (None: keep each) the layout being edited and refresh everything that
        shows it.  ``source`` ("table", "tape") is the widget the change came
        from, which keeps its own text."""
        if positions is not None:
            self._positions = [(float(x), float(y)) for x, y in positions]
        if facings is not None:
            self._facings = [None if f is None or not math.isfinite(float(f)) else float(f)
                             for f in facings]
        recount = count is not None and int(count) != self._count
        if count is not None:
            self._count = max(MIN_RECEIVERS, min(int(count), RECEIVER_COUNT))
        self._table.clear_row_status()
        if source != "table" or recount:
            self._table.set_points([(x, y, f) for (x, y), f in zip(self.positions(),
                                                                   self.facings())])
        if source != "tape":
            self._fill_tape()
        else:
            self._update_tape_note()
        self._update_count_texts()
        self._update_references()
        self._update_tape_visibility(draw=False)
        self._draw_layout()
        if refit or recount or not self._layout_in_view():
            self._fit_view()
        self._update_state()
        self._refresh_readout()
        self._schedule_coverage()

    def _set_receiver_count(self, n: int) -> None:
        """Use R1..Rn (MIN_RECEIVERS <= n <= RECEIVER_COUNT): the dormant
        receivers keep their place for when they come back.  Not saved.

        Raises:
            ValueError: ``n`` out of range; nothing is changed then.
        """
        if isinstance(n, bool) or int(n) != n or not MIN_RECEIVERS <= int(n) <= RECEIVER_COUNT:
            raise ValueError(f"the receiver count must be {MIN_RECEIVERS} to {RECEIVER_COUNT}, "
                             f"got {n!r}")
        if int(n) != self._count:
            self._apply_layout(count=int(n))

    def _update_count_texts(self) -> None:
        """Everything that names the receiver count."""
        n = self._count
        if n == RECEIVER_COUNT:
            dormant = ""
        elif n == RECEIVER_COUNT - 1:
            dormant = f" R{RECEIVER_COUNT} is not used (config.toml keeps it)."
        else:
            dormant = f" R{n + 1}–R{RECEIVER_COUNT} are not used (config.toml keeps them)."
        self._count_note.setText(f"Using {n} receivers (R1–R{n}).{dormant}")
        self._summary_captions["share_all"].setText(f"All {n} hear")
        if n < RECEIVER_COUNT:
            self._tape_count_note.setText(
                f"This template lays out {RECEIVER_COUNT} receivers and you use {n}: its "
                f"markers are hidden, and a change here sets {RECEIVER_COUNT} receivers.")
        self._tape_count_note.setVisible(n < RECEIVER_COUNT)

    def _update_references(self) -> None:
        """Reference points for "Offset..." and "Aim at...": the receivers
        (the table adds them itself) and the calibration points."""
        cal = {f"P{k + 1}": p for k, p in enumerate(self._cal_points)}
        self._table.set_references(cal)
        refs = {f"R{i + 1}": p for i, p in enumerate(self.positions())}
        refs.update(cal)
        self._origin_tool.set_references(refs)

    def _update_state(self) -> None:
        dirty = self.is_dirty()
        self._revert_btn.setEnabled(dirty and self._placing is None)
        if self._load_problem:
            self._set_state(self._load_problem, RED)
        elif dirty:
            self._set_state("● Unsaved changes", AMBER)
        else:
            self._set_state("Saved layout", DIM)

    def _set_state(self, text: str, color: str) -> None:
        self._state_label.setText(text)
        self._state_label.setToolTip(text)
        _style_label(self._state_label, color, 12, bold=True)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _shown_positions(self) -> list[tuple[float, float]]:
        """The active receivers as drawn: the working layout with a dragged
        receiver where the mouse has it; while "Place all" runs, only the
        receivers clicked so far."""
        if self._placing is not None:
            return list(self._placing)
        positions = self.positions()
        if self._drag is not None:
            index, x, y = self._drag
            if 0 <= index < len(positions):
                positions[index] = (x, y)
        return positions

    def _beam_deg(self, model: coverage.SensorModel) -> float:
        """Half-angle of a receiver's beam wedge for ``model``."""
        return model.fov_half_deg if model.fov_half_deg is not None else model.rx_beam_deg

    def _draw_layout(self) -> None:
        """Receivers (with facing arrows and beams), selection, overlays."""
        positions = self._shown_positions()
        self._view.set_receivers(list(enumerate(positions)),
                                 facing_deg=self.facings()[:len(positions)],
                                 show_beams=self._beams_check.isChecked(),
                                 beam_deg=self._beam_deg(self.model()))
        self._draw_selection()
        if len(positions) == RECEIVER_COUNT:              # D1..D10 describe six receivers
            self._tape.update(positions)
        self._view.draw_idle()

    def _draw_selection(self) -> None:
        """A ring round the selected receiver and, optionally, its reach."""
        row = self._table.selected()
        if self._placing is not None or not 0 <= row < self._count:
            self._view.clear_points("selected")
            self._view.clear_shapes("reach")
            self._view.draw_idle()
            return
        positions = self._shown_positions()
        self._view.set_points("selected", [positions[row]], color="none", edgecolor=GOLD,
                              marker="o", size=420, zorder=4.5)
        if self._footprint_check.isChecked() and not on_one_spot(positions):
            facing = resolve_facing(positions, self.facings())[row]
            try:
                outline = self._footprint(positions[row], facing, self.model())
            except Exception:                     # never let a drawing aid break the panel
                log.exception("Footprint of R%d failed", row + 1)
                outline = None
            if outline is not None:
                self._view.set_shapes("reach", outline, color=GOLD, linewidth=1.4, linestyle="--",
                                      legend=f"Where R{row + 1} hears")
        else:
            self._view.clear_shapes("reach")
        self._view.draw_idle()

    def _footprint(self, position, facing_deg: float, model: coverage.SensorModel) -> np.ndarray:
        """``coverage.footprint(position, facing_deg, model, n=91)``, with the
        receiver's reach per direction cached per model so the outline can
        follow a dragged receiver."""
        cached = self._footprint_cache.get(model)
        if cached is None:
            angles = np.linspace(-180.0, 180.0, 91)
            if model.fov_half_deg is not None and model.fov_half_deg < 180.0:
                angles = np.union1d(angles, [-model.fov_half_deg, model.fov_half_deg])
            cached = (angles, coverage.reach(angles, model))
            if len(self._footprint_cache) >= 16:          # a few models at a time is plenty
                self._footprint_cache.clear()
            self._footprint_cache[model] = cached
        angles, reach = cached
        x0, y0 = float(position[0]), float(position[1])
        bearing = np.radians(float(facing_deg) + angles)
        out = np.column_stack((x0 + reach * np.cos(bearing), y0 + reach * np.sin(bearing)))
        out[-1] = out[0]
        return out

    def _draw_cal_points(self) -> None:
        """The calibration points, read-only references on the arena."""
        if self._cal_points:
            self._view.set_points("cal", self._cal_points, color=GREEN, marker="D", labels="P",
                                  size=60, legend="Calibration points", draggable=False)
        else:
            self._view.clear_points("cal")
        self._view.draw_idle()

    def _show_map(self) -> None:
        """Shade the arena with the latest result in the chosen view."""
        res = self._result
        view = self._view_combo.currentData()
        if res is None or view == VIEW_OFF or on_one_spot(res.request.coords):
            self._view.clear_heatmap()
            self._view.clear_shapes("fix_edge")
            self._update_title()
            self._view.draw_idle()
            return
        self._view.set_coverage(res.xs, res.ys, res.counts, n_receivers=len(res.request.coords),
                                needed=NEEDED_FOR_FIX, alpha=0.45)
        try:
            edge = _contour_segments(self._view.ax, res.xs, res.ys, res.counts,
                                     NEEDED_FOR_FIX - 0.5)
        except Exception:                         # a drawing aid only
            log.exception("Fix-area edge failed")
            edge = []
        if edge:
            self._view.set_shapes("fix_edge", edge, color="white", closed=False, linewidth=1.4,
                                  linestyle="--", alpha=0.9, legend="Edge of the fix area")
        else:
            self._view.clear_shapes("fix_edge")
        self._update_title()
        self._view.draw_idle()

    def _update_title(self) -> None:
        view = self._view_combo.currentData()
        model = self.model()
        what = {VIEW_COVERAGE: "Receivers hearing each spot", VIEW_OFF: "Receiver layout"}[view]
        window = ("" if model.window_ms == DEFAULT_SAMPLING_TIME_MS
                  else f", {model.window_ms:g} ms window")
        reception = (f"±{model.fov_half_deg:g}°" if model.fov_half_deg is not None
                     else "no angle limit")
        if on_one_spot(self.positions()):
            self._view.set_title("No layout yet: place the receivers")
        else:
            self._view.set_title(what if view == VIEW_OFF
                                 else f"{what} ({self._count} receivers, {reception}{window})")

    def _show_summary(self, summary: dict) -> None:
        """The layout's numbers, coloured good / fair / poor."""
        self._summary = summary
        s = summary
        for key, cell in self._summary_cells.items():
            if s is None or s["points"] == 0:
                text, color = "–", DIM
            elif key == "min_count":
                value = s[key]
                text = str(value)
                color = (GREEN if value > NEEDED_FOR_FIX else
                         AMBER if value == NEEDED_FOR_FIX else RED)
            else:
                value = s[key]
                text = _share_text(value)
                if key == "share_all":
                    color = "white"
                else:
                    good, fair = (0.95, 0.75) if key == "share_3" else (0.9, 0.6)
                    color = GREEN if value >= good else AMBER if value >= fair else RED
            cell.setText(text)
            _style_label(cell, color, 12, bold=True)
        points = s["points"] if s is not None else 0
        step = self._result.request.step if self._result is not None else MAP_STEP_CM
        if points:
            receivers = s.get("receivers", self._count)
            self._summary_note.setText(
                f"Share of the area inside the outline of the {receivers} receivers ({points} "
                f"spots, {step:g} cm apart). Green: good, amber: fair, red: poor.")
        else:
            self._summary_note.setText("The receivers enclose no area yet (they are on one spot "
                                       "or one line): there is nothing to judge.")

    def _update_model_note(self) -> None:
        """What a receiver hears under the sensor settings, and how to read the map."""
        text = f"{self.model().describe()}."
        problem = getattr(self._settings, "sensor_problem", "")
        if problem:
            text += f" ({SENSOR_FILENAME} could not be used, so these are the defaults: {problem})"
        self._model_note.setText(text)
        self._model_note.setToolTip(
            f"Change it with “Sensor settings…” (saved in {SENSOR_FILENAME})")
        self._view_note.setText(VIEW_NOTES[self._view_combo.currentData()])

    # ------------------------------------------------------------------
    # View
    # ------------------------------------------------------------------

    def _place_legend(self, beside: bool) -> None:
        """Legend beside the arena on a wide canvas (the square arena leaves
        room there), under it on a squarish one, so it never covers the map."""
        if beside == self._legend_beside:
            return
        self._legend_beside = beside
        if beside:
            self._fig.subplots_adjust(left=0.08, right=0.78, top=0.94, bottom=0.07)
            self._view.set_legend_location("upper left", anchor=(1.02, 1.0), ncol=1)
        else:
            self._fig.subplots_adjust(left=0.09, right=0.97, top=0.95, bottom=0.21)
            self._view.set_legend_location("upper center", anchor=(0.5, -0.1), ncol=3)

    def eventFilter(self, obj, event) -> bool:
        if obj is self._canvas and event.type() == QEvent.Type.Resize:
            size = event.size()
            if size.width() > 1 and size.height() > 1:
                self._place_legend(size.width() >= 1.25 * size.height())
        return super().eventFilter(obj, event)

    def _layout_in_view(self) -> bool:
        x0, x1 = self._view.ax.get_xlim()
        y0, y1 = self._view.ax.get_ylim()
        return all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in self.positions())

    def _fit_view(self) -> None:
        """Fit the view to the layout; a layout with every receiver on one
        spot (a new arena) gets room to place the receivers in."""
        if on_one_spot(self.positions()):
            half = EMPTY_VIEW_CM / 2.0
            cx, cy = self._positions[0]
            self._view.set_limits(padding=0.0, include_origin=False,
                                  extra_points=[(cx - half, cy - half), (cx + half, cy + half)])
        else:
            self._view.set_limits(include_points=False,
                                  extra_points=self._cal_points or ())
        self._fitted = True
        self._view.draw_idle()

    def _on_scroll(self, event) -> None:
        """Mouse wheel: zoom round the pointer."""
        ax = self._view.ax
        if event.inaxes is not ax or event.xdata is None or not event.step:
            return
        scale = 0.8 ** float(event.step)
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        span = max(x1 - x0, y1 - y0) * scale
        if not 20.0 <= span <= 2.0 * widgets.MAX_COORD_CM:
            return
        cx, cy = event.xdata, event.ydata
        ax.set_xlim(cx + (x0 - cx) * scale, cx + (x1 - cx) * scale)
        ax.set_ylim(cy + (y0 - cy) * scale, cy + (y1 - cy) * scale)
        self._canvas.draw_idle()

    def _on_snap_changed(self, _index: int) -> None:
        self._interactor.snap = self._snap_combo.currentData()

    def _readout_text(self, x: float, y: float) -> str:
        """Hover read-out: where, and which receivers hear the transmitter there."""
        text = f"x {x:.1f}  y {y:.1f} cm"
        if self._placing is not None:                     # no layout to judge yet
            return text
        coords = np.asarray(self.positions(), dtype=float)
        heard = coverage.hears((x, y), coords, self.facings(), self.model())
        ids = np.flatnonzero(heard[0])
        if not len(ids):
            text += "  |  no receiver hears it"
        else:
            text += f"  |  heard by {len(ids)}: " + " ".join(f"R{i + 1}" for i in ids)
        return text

    def _on_hovered(self, x: float, y: float) -> None:
        self._hover = (x, y)

    def _on_left(self) -> None:
        self._hover = None

    def _refresh_readout(self) -> None:
        """Re-read the spot under the pointer after the layout, model or view
        changed (the read-out otherwise only changes when the mouse moves)."""
        if self._hover is None or self._drag is not None or not self._view.readout():
            return
        try:
            text = self._readout_text(*self._hover)
        except Exception:                         # a read-out never breaks the panel
            log.exception("Read-out failed")
            return
        if self._view.set_readout(text):
            self._view.draw_idle()

    def _update_hint(self) -> None:
        if self._placing is not None:
            k = len(self._placing)
            text = (f"Click the arena where R{k + 1} goes ({k + 1} of {self._count}). Every "
                    f"receiver must be placed; Esc or Cancel placing puts the previous layout "
                    f"back.")
            color = MAGENTA
        elif self._table.pick_mode():
            row = self._table.selected()
            name = f"R{row + 1}" if row >= 0 else "the receiver"
            step = (f" ({row + 1} of {self._count})" if self._place_all_btn.isChecked()
                    else "")
            text = f"Click the arena where {name} goes{step}. Esc or a right click cancels."
            color = MAGENTA
        elif self._origin_tool.pick_mode():
            text, color = "Click the arena at the new (0, 0). Esc or a right click cancels.", BLUE
        else:
            text = ("Drag a receiver to move it; click one to select it. "
                    "The wheel zooms; hover to see who hears a spot.")
            color = DIM
        self._hint.setText(text)
        _style_label(self._hint, color)

    # ------------------------------------------------------------------
    # Receivers: table, picking and dragging
    # ------------------------------------------------------------------

    def _on_table_edited(self, points: list) -> None:
        n = len(points)                                   # the active receivers
        self._apply_layout(positions=[(p[0], p[1]) for p in points] + self._positions[n:],
                           facings=[p[2] for p in points] + self._facings[n:], source="table")

    def _on_selection_changed(self, _row: int) -> None:
        self._aim_btn.setEnabled(self._table.selected() >= 0)
        self._draw_selection()
        self._update_hint()

    def _on_table_pick_mode(self, on: bool) -> None:
        if on:
            self._origin_tool.set_pick_mode(False)
        elif not self._advancing and self._place_all_btn.isChecked():
            self._place_all_btn.setChecked(False)         # the user switched picking off
        self._update_pick_routing()

    def _on_origin_pick_mode(self, on: bool) -> None:
        if on:
            self._place_all_btn.setChecked(False)
            self._table.set_pick_mode(False)
        self._update_pick_routing()

    def _update_pick_routing(self) -> None:
        """While a pick mode is on every press is a click (even on a receiver)."""
        picking = (self._placing is not None or self._table.pick_mode()
                   or self._origin_tool.pick_mode())
        self._interactor.pick_enabled = not picking
        self._update_hint()

    def _leave_pick_modes(self) -> None:
        self._place_all_btn.setChecked(False)
        self._table.set_pick_mode(False)
        self._origin_tool.set_pick_mode(False)

    def _on_place_all(self, on: bool) -> None:
        """"Place all by clicking": ask how many receivers to use, then place
        R1..RN in turn (``_start_placing``); switched off, it stops placing
        and puts the previous layout back."""
        if on:
            self._origin_tool.set_pick_mode(False)
            self._advancing = True                        # not the user switching "Place all" off
            try:
                self._table.set_pick_mode(False)
            finally:
                self._advancing = False
            before = (list(self._positions), list(self._facings), self._count)
            count = ask_receiver_count(self, self._count)
            if count is None:                             # cancelled: nothing to place
                self._place_all_btn.setChecked(False)
                return
            if not self._place_all_btn.isChecked():       # switched off meanwhile
                return
            self._set_receiver_count(count)
            self._start_placing(before)
        elif self._placing is not None:
            self._end_placing(finished=False)
        elif self._table.pick_mode():
            self._table.set_pick_mode(False)
        self._update_hint()

    def _start_placing(self, before: tuple) -> None:
        """Hide every receiver and lock the panel: each receiver reappears
        where it is clicked, and nothing else can be used until the last one
        is placed (or placing is cancelled, back to ``before``)."""
        self._cancel_drag()
        self._placing = []
        self._before_placing = before
        self._coverage_timer.stop()
        self._pending = None
        self._generation += 1                             # results on their way are stale
        self._set_locked(True)
        self._update_tape_visibility(draw=False)
        self._view.clear_heatmap()
        self._view.clear_shapes("fix_edge")
        self._update_pick_routing()
        self._draw_layout()

    def _end_placing(self, finished: bool) -> None:
        """Unlock the panel with the receivers clicked (``finished``) or the
        layout from before placing started."""
        placed, before = self._placing, self._before_placing
        self._placing = self._before_placing = None
        self._set_locked(False)
        if self._place_all_btn.isChecked():
            self._place_all_btn.blockSignals(True)
            self._place_all_btn.setChecked(False)
            self._place_all_btn.blockSignals(False)
        self._update_tape_visibility(draw=False)          # markers as the templates say
        self._update_pick_routing()
        if finished:
            n = len(placed)
            self._apply_layout(positions=placed + self._positions[n:])
        else:
            positions, facings, count = before
            self._apply_layout(positions=positions, facings=facings, count=count)

    def _set_locked(self, locked: bool) -> None:
        """While placing, only the arena (and its view aids) can be used."""
        for widget in (self._side_scroll, self._back_btn):
            widget.setEnabled(not locked)
        self._cancel_place_btn.setVisible(locked)
        if locked:
            self._revert_btn.setEnabled(False)
            self._save_btn.setEnabled(False)
        else:
            self._save_btn.setEnabled(not self._origin_tool.is_measuring())
            self._update_state()

    def _on_arena_clicked(self, x: float, y: float, button: int) -> None:
        if self._placing is not None:                     # only left clicks place receivers
            if button == 1:
                self._placing.append((float(x), float(y)))
                if len(self._placing) >= self._count:
                    self._end_placing(finished=True)
                else:
                    self._draw_layout()
                    self._update_hint()
            return
        if button == 3:                                   # right click: cancel picking
            self._leave_pick_modes()
            return
        if button != 1:
            return
        if self._table.pick_mode():
            row = self._table.selected()
            placing_all = self._place_all_btn.isChecked()
            self._advancing = placing_all
            try:
                placed = self._table.apply_pick(x, y)     # emits pointsEdited, leaves pick mode
            finally:
                self._advancing = False
            if placing_all and placed:
                if row + 1 < self._count:
                    self._table.select(row + 1)
                    self._table.set_pick_mode(True)
                else:
                    self._place_all_btn.setChecked(False)
            self._update_hint()
        elif self._origin_tool.pick_mode():
            self._origin_tool.apply_pick(x, y)

    def _on_point_pressed(self, group: str, index: int, _button: int) -> None:
        if group == RECEIVERS and self._table.selected() != index:
            self._table.select(index)

    def _on_point_dragged(self, group: str, index: int, x: float, y: float) -> None:
        if group != RECEIVERS:
            return
        self._drag = (index, x, y)
        self._table.set_point(index, x, y)               # shown, not yet part of the layout
        self._draw_layout()
        self._schedule_coverage(preview=True)

    def _on_drag_finished(self, group: str, index: int, x: float, y: float) -> None:
        if group != RECEIVERS:
            return
        self._drag = None
        self._pending = None                              # a full computation replaces previews
        positions = list(self._positions)
        positions[index] = (x, y)
        self._apply_layout(positions=positions)

    def _cancel_drag(self) -> None:
        """Put a receiver being dragged back where the layout has it."""
        self._interactor.cancel_drag()
        if self._drag is not None:
            index = self._drag[0]
            self._drag = None
            x, y = self._positions[index]
            self._table.set_point(index, x, y)
            self._draw_layout()

    def _on_aim_at(self) -> None:
        row = self._table.selected()
        if not 0 <= row < self._count:
            return
        active = self.positions()
        here = active[row]
        others = [p for i, p in enumerate(active) if i != row]
        centre = tuple(np.mean(others, axis=0)) if others else (0.0, 0.0)
        refs = {f"R{i + 1}": p for i, p in enumerate(active) if i != row}
        refs.update({f"P{k + 1}": p for k, p in enumerate(self._cal_points)})
        target = widgets.ask_point(self, f"Aim R{row + 1} at", initial=centre, references=refs,
                                   text=f"The point R{row + 1} should face, in the arena frame.",
                                   accent=MAGENTA)
        if target is None:
            return
        dx, dy = target[0] - here[0], target[1] - here[1]
        if math.hypot(dx, dy) < 1e-6:
            widgets.warning(self, "Aim at", f"That point is where R{row + 1} is.")
            return
        facings = list(self._facings)
        facings[row] = round(math.degrees(math.atan2(dy, dx)), 1)
        self._apply_layout(facings=facings)

    def _on_auto_aim(self) -> None:
        n = self._count                                   # the dormant ones keep theirs
        self._apply_layout(facings=[None] * n + self._facings[n:])

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and (self._placing is not None
                                                 or self._table.pick_mode()
                                                 or self._origin_tool.pick_mode()):
            self._leave_pick_modes()
            return
        super().keyPressEvent(event)

    def _on_canvas_key(self, event) -> None:
        """Esc on the arena (the canvas keeps its key presses) leaves picking."""
        if event.key == "escape":
            self._leave_pick_modes()

    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------

    def _on_build_rectangle(self) -> None:
        width = widgets.parse_number(self._width_edit.text())
        height = widgets.parse_number(self._height_edit.text())
        if width is None or height is None or width <= 0 or height <= 0:
            self._rect_status.setText("Width and height must be positive numbers (cm).")
            self._rect_status.setVisible(True)
            return
        self._rect_status.setVisible(False)
        layout = positions_from_dimensions(width, height)       # six receivers
        self._apply_layout(positions=[p for _, p in layout], count=RECEIVER_COUNT, refit=True)

    def _fill_tape(self) -> None:
        """Show the six receivers' D1..D10 (and the W x H of the active
        receivers' bounding box)."""
        values = distances_from_positions(list(enumerate(self._positions)))
        for (name, _), value in zip(TAPE_DISTANCES, values):
            self._tape_edits[name].setText(widgets.format_number(value, 3))
        # What D9 / D10 show now: a change of them moves R2 / R5 alone
        self._prev_d9 = widgets.parse_number(self._tape_edits["D9"].text()) or 0.0
        self._prev_d10 = widgets.parse_number(self._tape_edits["D10"].text()) or 0.0
        self._tape_dirty = False
        if not on_one_spot(self.positions()):
            w, h = np.ptp(np.asarray(self.positions(), dtype=float), axis=0)
            self._width_edit.setText(widgets.format_number(w, 2))
            self._height_edit.setText(widgets.format_number(h, 2))
        self._update_tape_note()

    def _update_tape_note(self) -> None:
        """Say when D1..D10 do not describe the layout (not a two-column one)."""
        layout = list(enumerate(self._positions))
        rebuilt = positions_from_distances(*distances_from_positions(layout))
        exact = all(math.dist(p, q) <= 0.01 for (_, p), q in zip(rebuilt, self._positions))
        if exact:
            self._tape_status.setVisible(False)
        else:
            self._set_tape_status("This layout is not a two-column one: D1–D10 do not describe "
                                  "it, and changing one rebuilds it as one.", AMBER)

    def _set_tape_status(self, text: str, color: str) -> None:
        self._tape_status.setText(text)
        _style_label(self._tape_status, color)
        self._tape_status.setVisible(bool(text))

    def _on_tape_edited(self, _text: str) -> None:
        self._tape_dirty = True

    def _on_tape_finished(self) -> None:
        """Rebuild the layout from D1..D10 after the user changed one."""
        if not self._tape_dirty:
            return                                        # left unchanged: keep the layout as it is
        self._tape_dirty = False
        values = []
        for name, _ in TAPE_DISTANCES:
            value = widgets.parse_number(self._tape_edits[name].text())
            if value is None:
                self._set_tape_status(f"{name} must be a number (cm).", RED)
                return
            values.append(value)
        r1, r2, r3, r4, r5, r6, r7, r8, r9, r10 = values
        # D9 / D10 move R2 / R5 alone: R1 and R3 (R4 and R6) keep their Y, as before
        if r9 != self._prev_d9:
            r2 -= self._prev_d9
            r1 += self._prev_d9
            r2 += r9
            r1 -= r9
            self._tape_edits["D1"].setText(widgets.format_number(r1, 3))
            self._tape_edits["D2"].setText(widgets.format_number(r2, 3))
            self._prev_d9 = r9
        if r10 != self._prev_d10:
            r4 -= self._prev_d10
            r5 += self._prev_d10
            r4 += r10
            r5 -= r10
            self._tape_edits["D4"].setText(widgets.format_number(r4, 3))
            self._tape_edits["D5"].setText(widgets.format_number(r5, 3))
            self._prev_d10 = r10
        if any(v <= 0 for v in (r1, r2, r3, r4, r5, r6, r7, r8)):
            self._set_tape_status("D1–D8 must be more than 0.", RED)
            return
        layout = positions_from_distances(r1, r2, r3, r4, r5, r6, r7, r8, r9, r10)
        self._apply_layout(positions=[p for _, p in layout], source="tape",
                           count=RECEIVER_COUNT)              # six receivers

    def tape_markers_visible(self) -> bool:
        """Whether D1..D10 are drawn on the arena."""
        return self._tape.visible()

    def _update_tape_visibility(self, draw: bool = True) -> None:
        """D1..D10 on the arena while the tape-measure template is open, or
        always when "Always show distance markers" is ticked; never with
        fewer than six receivers (they describe six), nor while placing."""
        shown = (self._placing is None and self._count == RECEIVER_COUNT
                 and (self._tape_box.is_expanded() or self._markers_check.isChecked()))
        if shown == self._tape.visible():
            return
        self._tape.set_visible(shown)
        if shown:
            self._tape.update(self._shown_positions())
        if draw:
            self._view.draw_idle()

    # ------------------------------------------------------------------
    # Coverage
    # ------------------------------------------------------------------

    def _on_view_changed(self, _index: int) -> None:
        self._update_model_note()
        self._show_map()                                  # the map is already computed
        self._refresh_readout()
        if self._result is None:
            self._schedule_coverage()

    def _on_sensor_settings(self) -> None:
        """"Sensor settings...": edit the receiver model in a dialog."""
        dlg = SensorDialog(self, self._sensor, accent=GREEN)
        try:
            if not widgets._accepted(widgets._exec(dlg)) or dlg.value() is None:
                return
            model = dlg.value()
        finally:
            dlg.deleteLater()
        try:
            self._settings.set_sensor_model(model)
        except (ValueError, OSError) as exc:
            log.exception("Could not save the sensor settings")
            widgets.error(self, "Sensor settings not saved",
                          f"The sensor settings could not be saved to {SENSOR_FILENAME}: {exc}\n\n"
                          "They are used until the program is closed.")
        else:
            self._sensor_saved = self._read_sensor()
        self.set_sensor_model(model)

    def _on_model_changed(self) -> None:
        self._update_model_note()
        self._draw_layout()                               # the beam wedges follow the model
        self._refresh_readout()
        self._schedule_coverage()

    def _on_window_changed(self, _value: int) -> None:
        self._update_model_note()
        self._draw_selection()                            # the window caps the reach outline
        self._refresh_readout()
        self._schedule_coverage()

    def _schedule_coverage(self, preview: bool = False) -> None:
        """Recompute the coverage for the current inputs: a preview at once
        (while dragging), a full computation after a short pause."""
        self._generation += 1
        if not self._active:
            return
        if preview:
            self._start_job(True)
        else:
            self._coverage_timer.start()

    def _start_job(self, preview: bool) -> None:
        if not self._active or self._placing is not None:   # no layout to judge while placing
            return
        if self._worker is not None:                      # one at a time: the newest waits
            self._pending = preview if self._pending is None else (self._pending and preview)
            return
        positions = self._shown_positions()
        model = self.model()
        xs, ys = outline_xy(positions)
        outline = tuple(zip(xs, ys)) if len(xs) >= 4 else None
        request = CoverageRequest(
            generation=self._generation, preview=preview, coords=tuple(positions),
            facing=tuple(self.facings()), model=model,
            step=map_step(positions, preview), outline=outline)
        self._worker = widgets.run_in_thread(
            compute_coverage, request, on_result=self._on_coverage,
            on_error=self._on_coverage_failed, on_finished=self._on_coverage_finished)

    def _on_coverage(self, result: CoverageResult) -> None:
        request = result.request
        current = request.generation == self._generation
        if not self._active or not (current or (request.preview and self._drag is not None)):
            return                                        # stale (previews may lag a drag)
        self._result = result
        self._coverage_error.setVisible(False)
        self._show_map()
        if result.summary is not None:
            self._show_summary(result.summary)

    def _on_coverage_failed(self, message: str) -> None:
        self._coverage_error.setText(f"Coverage estimate failed: {message}")
        self._coverage_error.setVisible(True)

    def _on_coverage_finished(self) -> None:
        self._worker = None
        pending, self._pending = self._pending, None
        if pending is not None and self._active:
            self._start_job(pending)

    def _stop_coverage(self) -> None:
        """No more computations; wait for one in progress (they are short)."""
        self._coverage_timer.stop()
        self._pending = None
        worker = self._worker
        if worker is not None:
            worker.cancel()
            if not worker.wait(5000):
                log.warning("Coverage computation still running after 5 s")

    # ------------------------------------------------------------------
    # Base station link: sampling window, measuring
    # ------------------------------------------------------------------

    def _link_up(self) -> bool:
        return bool(self._client is not None and getattr(self._client, "connected", False))

    def _on_link_changed(self, up: bool) -> None:
        self._update_link_state(bool(up))

    def _update_link_state(self, up: bool) -> None:
        busy = self._window_worker is not None
        self._read_btn.setEnabled(up and not busy)
        self._read_btn.setToolTip("Ask the base station for its sampling window" if up
                                  else "Needs the ground station connection")
        if up and self._client is not None and self._position_lib is not None:
            self._origin_tool.set_measure(
                partial(measure.measure_position, self._client, self._position_lib, 20))
        else:
            self._origin_tool.set_measure(None)

    def _sync_window_from_client(self) -> None:
        """Use the window the base station last reported, when known."""
        known = getattr(self._client, "sampling_time_ms", None) if self._client else None
        if known is not None and SAMPLING_TIME_MIN_MS <= int(known) <= SAMPLING_TIME_MAX_MS:
            self._window_spin.setValue(int(known))

    def _read_window(self) -> None:
        if not self._link_up() or self._window_worker is not None:
            return
        self._set_window_status("Asking the base station…", DIM)
        self._read_btn.setEnabled(False)
        self._window_worker = widgets.run_in_thread(
            self._client.sampling_time, on_result=self._on_window_read,
            on_error=lambda msg: self._set_window_status(f"Failed: {msg}", RED),
            on_finished=self._on_window_finished)

    def _on_window_read(self, result) -> None:
        if result is None:
            error = getattr(self._client, "last_error", None) or "no reply"
            self._set_window_status(f"No answer: {error}", RED)
            return
        self._window_spin.setValue(int(result.ms))
        self._set_window_status(f"The base station samples for {result.ms} ms.", GREEN)

    def _on_window_finished(self) -> None:
        self._window_worker = None
        self._update_link_state(self._link_up())

    def _set_window_status(self, text: str, color: str) -> None:
        self._window_status.setText(text)
        _style_label(self._window_status, color)
        self._window_status.setVisible(bool(text))

    # ------------------------------------------------------------------
    # Origin
    # ------------------------------------------------------------------

    def _on_origin_preview(self, point) -> None:
        if point is None:
            self._view.clear_points("origin_preview")
        else:
            self._view.set_points("origin_preview", [point], color=BLUE, marker="P", size=160,
                                  labels=["new (0, 0)"], legend="New (0, 0)")
        self._view.draw_idle()

    def _on_origin_requested(self, x: float, y: float) -> None:
        """Move (0, 0) to the current-frame point (x, y): the saved receivers,
        calibration points and barriers move with it, and so do unsaved edits
        (``_sync_with_settings`` sees a pure translation)."""
        try:
            self._settings.shift_origin(x, y)
        except Exception as exc:                          # nothing may escape a slot
            if not isinstance(exc, (ValueError, OSError)):        # the documented failures
                log.exception("Moving (0, 0) failed")
            self._origin_tool.set_status(f"(0, 0) not moved: {exc}", RED)
            return
        self._notify_config_changed()

    def _on_measuring_changed(self, measuring: bool) -> None:
        # notify_config_changed reloads the position library the measurement uses
        self._save_btn.setEnabled(not measuring and self._placing is None)

    # ------------------------------------------------------------------
    # Verify and save
    # ------------------------------------------------------------------

    def _verify_and_save(self) -> bool:
        """Save the receiver count and the active receivers' positions and
        facings (the dormant receivers' entries are left as they are)."""
        issues = layout_issues(self.positions())
        if issues:
            per_row: dict[int, list[str]] = {}
            for message, rows in issues:
                for row in rows:
                    per_row.setdefault(row, []).append(message)
            for row, messages in per_row.items():
                self._table.set_row_status(row, RED, "\n".join(messages))
            widgets.error(self, "Layout not saved",
                          "This receiver layout can't be used for positioning, so it was not "
                          "saved:", details="\n".join(message for message, _ in issues))
            return False

        n = self._count
        working, working_facings = list(self._positions), list(self._facings)
        positions, facings = working[:n], working_facings[:n]
        moved = positions != self._saved_positions[:n]
        turned = facings != self._saved_facings[:n]
        recounted = n != self._saved_count
        try:
            # Distances to the calibration points that do not match the saved
            # positions (config.toml edited by hand, say) are recomputed as well
            stale = not moved and not self._load_problem and self._cal_distances_stale(positions)
            if moved or stale:
                self._settings.set_receiver_positions(list(enumerate(positions)))
            for rid, (new, old) in enumerate(zip(facings, self._saved_facings)):
                if new != old:
                    self._settings.set_receiver_facing(rid, new)
            if recounted:
                self._settings.number_of_receivers = n
            # This is the saved layout now: the reload must not take it for the
            # old one moved by an origin shift (and so move it once more)
            self._saved_count = n
            self._saved_positions = positions + self._saved_positions[n:]
            self._saved_facings = facings + self._saved_facings[n:]
            valid, errors = self._settings.verify_settings()
            flag_changed = bool(self._settings.valid_settings) != valid
            if flag_changed:
                self._settings.valid_settings = valid
        except Exception as exc:                          # nothing may escape a slot
            if not isinstance(exc, (ValueError, OSError)):        # the documented failures
                log.exception("Saving the layout failed")
            widgets.error(self, "Save failed", f"The layout could not be saved: {exc}")
            self._notify_config_changed()                 # everyone sees what is saved now ...
            self._apply_layout(working, working_facings, count=n)   # ... and the edits stay
            return False

        if moved or stale or turned or recounted or flag_changed:
            self._notify_config_changed()
        self._update_state()

        what = (f"saved ({n} receivers, R1–R{n})" if moved or turned or recounted
                else "unchanged")
        if moved:
            note = ("\n\nReceivers that were moved for real need a new calibration "
                    "(Calibration panel): their slopes and intercepts were measured where "
                    "they stood. Their calibration distances were recomputed.")
        elif stale:
            note = ("\n\nThe calibration distances in config.toml did not match the receiver "
                    "positions: they were recomputed.")
        else:
            note = ""
        if valid:
            widgets.info(self, "Settings verified",
                         f"The receiver layout is {what} and the configuration verifies.{note}")
        else:
            widgets.warning(
                self, "Configuration incomplete",
                f"The receiver layout is {what}, but the configuration does not verify yet. "
                f"Calibration points are set on the Calibration panel.{note}",
                details="\n".join(errors))
        return True

    def _cal_distances_stale(self, positions) -> bool:
        """Whether a saved receiver's ``cal_distances`` are not its distances
        to the calibration points from ``positions`` (the saved layout of
        the active receivers, R1 first)."""
        if not self._cal_points:
            return False                                  # unreadable: nothing to compare with
        for rid, position in enumerate(positions):
            stored = (self._settings.get_receiver(rid) or {}).get("cal_distances")
            expected = [math.dist(position, point) for point in self._cal_points]
            if (not isinstance(stored, list) or len(stored) != len(expected)
                    or not all(isinstance(d, (int, float)) and not isinstance(d, bool)
                               and abs(d - e) <= 1e-6 for d, e in zip(stored, expected))):
                return True
        return False

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _on_revert(self) -> None:
        if not self.is_dirty():
            return
        if widgets.confirm(self, "Revert", "Discard the unsaved layout changes?",
                           ok_text="Discard", danger=True):
            self._apply_layout(self._saved_positions, self._saved_facings,
                               count=self._saved_count, refit=True)

    def _leave(self, panel: str) -> None:
        """Go to another panel; unsaved edits are discarded after asking."""
        if self.is_dirty():
            if not widgets.confirm(self, "Unsaved layout",
                                   "The receiver layout has unsaved changes. Leave and discard "
                                   "them?", ok_text="Discard and leave", cancel_text="Stay",
                                   danger=True):
                return
            self._apply_layout(self._saved_positions, self._saved_facings,
                               count=self._saved_count, refit=True)
        self._main_window.show_panel(panel)

    # ------------------------------------------------------------------
    # Panel lifecycle
    # ------------------------------------------------------------------

    def on_panel_show(self) -> None:
        self._active = True
        # The coverage shading starts off each time the panel opens
        self._view_combo.setCurrentIndex(self._view_combo.findData(DEFAULT_VIEW))
        self._sync_window_from_client()
        self._update_link_state(self._link_up())
        self._sync_with_settings()                        # also anything changed while hidden
        self._interactor.enable()
        self._update_pick_routing()
        if not self._fitted:
            self._fit_view()
        self._schedule_coverage()

    def on_panel_hide(self) -> None:
        self._active = False
        self._leave_pick_modes()
        self._cancel_drag()
        self._interactor.disable()
        self._stop_coverage()
        # A measurement uses the shared position library, which the panel shown
        # next may use: let it stop (it ends after its pulse in flight, usually
        # within 0.1 s, at worst after the client's reply timeout)
        if self._origin_tool.is_measuring():
            self._origin_tool.cancel_measurement()
            if not self._origin_tool.wait(MEASURE_STOP_MS):
                log.warning("The origin measurement is still running after %d ms",
                            MEASURE_STOP_MS)
