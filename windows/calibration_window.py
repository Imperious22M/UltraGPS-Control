"""Calibration panel.

Calibration fits each receiver's tick -> distance line (``distance = tick *
slope + intercept``) from readings taken with the transmitter on 2 to 4 known
points.  The panel is a large arena view beside four tabs:

* **Points**: choose the calibration points anywhere in the arena: type them,
  give them as an offset (dX / dY, or a distance and an angle) from the
  origin, a receiver or another point, pick them on the arena or drag them
  there.  The arena is shaded green where a point calibrates best
  (``ultragps_calibration.points.quality_map``, the simulator's score): with
  a point selected, where that point should go given the others; with none,
  where the next point should go (with four points, where any point does
  well).  Every point is checked (problems refuse saving and running, warnings
  are amber), "Find best points" searches for the best set, and Save writes
  the points and every receiver's ``cal_distances``.
* **Origin (0,0)**: move the arena's (0, 0) to a point picked, typed, given as
  an offset or measured with the transmitter; the receivers, the calibration
  points and the barriers are translated (``SettingsModule.shift_origin``).
* **Run**: one collection per saved point (``UltraGPSCalibration.start_run``)
  with live tick histograms, then the fitted slopes and intercepts next to the
  current ones, with their residuals and the receivers that could not be
  calibrated, and Save offsets.
* **Check**: measure the transmitter on a known point and compare
  (``windows.measure``): the position error and each receiver's range error.

Nothing waits on the network on the GUI thread: the calibration library
collects on its own thread (its callbacks come back through queued signals)
and measurements run on ``widgets.run_in_thread`` workers.  The shading and
the point search are computed on workers too, debounced, and a result older
than the points on show is dropped, so dragging a point never stalls.
"""

from __future__ import annotations

import functools
import html
import logging
import math
from dataclasses import dataclass, field

import numpy as np
from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QAbstractScrollArea, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QSizePolicy, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from matplotlib.figure import Figure
from matplotlib.path import Path
from matplotlib.ticker import MaxNLocator

from SettingsModule import SettingsModule
from ultragps_calibration import UltraGPSCalibration, points
from ultragps_position import coverage
from windows import measure, ui_scale, widgets as W
from windows.arena_view import ArenaInteractor, ArenaView, outline_xy
from windows.ui_scale import ScalingCanvas

log = logging.getLogger(__name__)

TAB_POINTS, TAB_ORIGIN, TAB_RUN, TAB_CHECK = range(4)

QUALITY_DEBOUNCE_MS = 150      # the shading is recomputed this long after the last change
QUALITY_CELLS = 60             # shading cells across the receivers' box (2.5 to 10 cm each)
HIST_REDRAW_MS = 150           # live histograms are redrawn at most this often
HIST_HALF_WIDTH = 15           # ticks shown either side of the modal tick
ORIGIN_SAMPLES = 20            # readings averaged by "Measure with transmitter"
CHECK_SAMPLES = 20             # default readings of an accuracy check
GOOD_CM = 2.0                  # a check error up to this is green, up to FAIR_CM amber, beyond red
FAIR_CM = 5.0
RESIDUAL_BAD_CM = 3.0          # a fit's RMS residual above this flags a point read wrong
SLOPE_CHANGE_WARN = 0.10       # a slope moving more than this (10 %) is worth a second look
STOP_WAIT_MS = 3000            # longest wait for a measurement to end when the panel is left

# Arena figure margins: room for the legend (Points and Check tabs) beside the
# arena on a wide canvas or under it on a tall one; none on the other tabs
_LAYOUT_LEGEND_RIGHT = dict(left=0.1, right=0.74, bottom=0.08, top=0.92)
_LAYOUT_LEGEND_BELOW = dict(left=0.12, right=0.96, bottom=0.2, top=0.94)
_LAYOUT_FULL = dict(left=0.1, right=0.97, bottom=0.08, top=0.92)
WIDE_ASPECT = 1.2              # a canvas (or the run's plot area) this much wider than tall
                               # is "wide"

# Point groups drawn on the arena (see _refresh_arena)
_GROUPS = ("saved", "best", "selected", "cal", "target", "origin_preview",
           "check_fixes", "check_lm", "check_cep", "check_true")

_PAGE_CSS = f"""
    QWidget#calPage {{ background-color: black; }}
    QLabel {{ color: white; font: 12px Arial; background: transparent; }}
    QComboBox {{ background-color: #222222; color: white; border: 1px solid #444444;
                border-radius: 3px; padding: 3px 6px; font: 12px Arial; }}
    QComboBox:disabled {{ background-color: #151515; color: #555555; }}
    QComboBox QAbstractItemView {{ background-color: #222222; color: white;
                selection-background-color: {W.GREEN}; selection-color: black; }}
    QSpinBox {{ background-color: #222222; color: white; border: 1px solid #444444;
               border-radius: 3px; padding: 2px 4px; font: 12px Arial; }}
    QSpinBox:disabled {{ background-color: #151515; color: #555555; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; }}
    QSpinBox::up-arrow, QSpinBox::down-arrow {{ width: 8px; height: 8px; }}
    QCheckBox {{ color: white; font: 12px Arial; spacing: 6px; background: transparent; }}
    QCheckBox::indicator {{ width: 11px; height: 11px; border-radius: 2px; }}
    QCheckBox::indicator:unchecked {{ background-color: #222222; border: 2px solid #666666; }}
    QCheckBox::indicator:checked {{ background-color: {W.GREEN}; border: 2px solid {W.GREEN}; }}
"""

_TABS_CSS = f"""
    QTabWidget::pane {{ border: 1px solid #333333; border-radius: 4px; background-color: black;
                       top: -1px; }}
    QTabBar {{ background-color: black; }}
    QTabBar::tab {{ background-color: #151515; color: {W.DIM}; border: 1px solid #333333;
                   border-bottom: none; border-top-left-radius: 4px; border-top-right-radius: 4px;
                   padding: 6px 9px; margin-right: 2px; font: bold 12px Arial; }}
    QTabBar::tab:selected {{ background-color: {W.GREEN}; color: black; border-color: {W.GREEN}; }}
    QTabBar::tab:hover:!selected {{ color: white; }}
"""

_TABLE_CSS = f"""
    QTableWidget {{ background-color: #111111; color: white; gridline-color: #333333;
                   border: 1px solid #333333; font: 11px Arial; }}
    QHeaderView::section {{ background-color: #1A1A1A; color: {W.DIM}; border: none;
                           border-bottom: 1px solid #333333; padding: 3px 5px;
                           font: bold 10px Arial; }}
"""

_SPLITTER_CSS = "QSplitter::handle { background-color: #1A1A1A; }"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _join(items) -> str:
    """"P1", "P1 and P2", "P1, P2 and P3"."""
    items = [str(i) for i in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _fmt_point(p) -> str:
    return f"({p[0]:.1f}, {p[1]:.1f})"


def _fmt_score(value: float) -> str:
    return f"{value:.2f}" if math.isfinite(value) else "∞"


def _fmt_signed(value: float) -> str:
    """``value`` with its sign and one decimal; a value that rounds to zero
    reads "0.0" (never "-0.0")."""
    text = f"{value:+.1f}"
    return "0.0" if float(text) == 0 else text


def _rich(text: str, color: str) -> str:
    """``text`` as rich text in ``color``."""
    return f'<span style="color:{color}">{html.escape(text)}</span>'


def _same_points(a, b, tol: float = 1e-6) -> bool:
    """Whether two point lists hold the same points in the same order."""
    if len(a) != len(b):
        return False
    return all(abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol for p, q in zip(a, b))


def _frame_shift(old_coords: np.ndarray, new_coords: np.ndarray, old_points,
                 new_points) -> tuple[float, float] | None:
    """The ``(dx, dy)`` every receiver and calibration point moved by when the
    origin was moved (``SettingsModule.shift_origin``); None when they did not
    all move alike (receivers placed anew, say) or did not move."""
    if old_coords.shape != new_coords.shape or not len(old_coords):
        return None
    d = new_coords - old_coords
    if not np.isfinite(d).all() or not np.allclose(d, d[0], rtol=0.0, atol=1e-6):
        return None
    dx, dy = float(d[0][0]), float(d[0][1])
    if math.hypot(dx, dy) < 1e-6:
        return None
    if not _same_points([(x + dx, y + dy) for x, y in old_points], new_points):
        return None
    return dx, dy


def _text(text: str = "", color: str = W.DIM, px: int = 11, bold: bool = False,
          wrap: bool = True, rich: bool = False) -> QLabel:
    """A label in the panel's style.  A wrapping label never widens its column
    (a long word in a message would push the side panel out of view)."""
    lbl = QLabel(text)
    ui_scale.style(lbl, f"color: {color}; font: {'bold ' if bold else ''}{px}px Arial; "
                        f"background: transparent;")
    if rich:
        lbl.setTextFormat(Qt.TextFormat.RichText)
    if wrap:
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return lbl


def _set_status(label: QLabel, text: str, color: str = W.DIM) -> None:
    """Show ``text`` in a status label ("" hides it)."""
    ui_scale.style(label, f"color: {color}; font: 11px Arial; background: transparent;")
    label.setText(text)
    label.setVisible(bool(text))


def _status_label() -> QLabel:
    lbl = _text()
    lbl.setVisible(False)
    return lbl


def _page() -> tuple[QWidget, QVBoxLayout]:
    """A tab page (it goes in a scroll area, which fills its background) and
    its column layout."""
    page = QWidget()
    page.setObjectName("calPage")
    ui_scale.style(page, _PAGE_CSS)
    col = QVBoxLayout(page)
    ui_scale.margins(col, 6, 8, 6, 8)
    ui_scale.spacing(col, 8)
    return page, col


def _combo() -> QComboBox:
    """A combo box that keeps fitting its items as the UI scale changes their
    font (the default only measures them when first shown)."""
    combo = QComboBox()
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
    return combo


def _row(*widgets, stretch: int | None = None) -> QHBoxLayout:
    """Widgets side by side, left-aligned, or with widget number ``stretch``
    taking the spare room."""
    row = QHBoxLayout()
    ui_scale.spacing(row, 6)
    for k, w in enumerate(widgets):
        row.addWidget(w, 1 if k == stretch else 0)
    if stretch is None:
        row.addStretch(1)
    return row


def _size_rows(header: QHeaderView, factor: float) -> None:
    """Scaled row height of a table (the minimum first: Qt clamps a new
    default size to the current minimum)."""
    size = ui_scale.scale_length(22, factor)
    header.setMinimumSectionSize(ui_scale.scale_length(16, factor))
    header.setDefaultSectionSize(size)
    for section in range(header.count()):
        if header.sectionSize(section) != size:
            header.resizeSection(section, size)


def _table(headers, rows: int, stretch=()) -> QTableWidget:
    """A read-only dark table of numbers, exactly as tall as its rows;
    the ``stretch`` columns share the spare width."""
    t = QTableWidget(rows, len(headers))
    t.setHorizontalHeaderLabels(list(headers))
    t.verticalHeader().hide()
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    t.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    head = t.horizontalHeader()
    head.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    for col in stretch:
        head.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
    t.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustToContents)
    t.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    t.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    ui_scale.style(t, _TABLE_CSS)
    ui_scale.bind(t.verticalHeader(), _size_rows, key="rows")
    return t


def _set_cell(table: QTableWidget, row: int, col: int, text: str, color: str = "white",
              tip: str = "", center: bool = False) -> None:
    item = table.item(row, col)
    if item is None:
        item = QTableWidgetItem()
        table.setItem(row, col, item)
    item.setText(text)
    item.setForeground(QColor(color))
    item.setToolTip(tip)
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter if center
                          else Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


def _point_marks(check, n: int) -> list[tuple[str | None, str]]:
    """``(colour, tooltip)`` of each of ``n`` points from a ``PointCheck``:
    red for its own problems, and every point red when the set as a whole
    can't calibrate (a receiver at the same distance from all of them); amber
    for warnings; None when the point is fine."""
    rows = check.rows if check is not None and len(check.rows) == n else [([], [])] * n
    whole_set = check is not None and bool(check.problems) and not any(bad for bad, _ in rows)
    marks = []
    for k, (bad, warn) in enumerate(rows):
        if bad or whole_set:
            marks.append((W.RED, f"P{k + 1}: " + "; ".join(bad + warn if bad else check.problems)))
        elif warn:
            marks.append((W.AMBER, f"P{k + 1}: " + "; ".join(warn)))
        else:
            marks.append((None, ""))
    return marks


def _error_color(err: float) -> str:
    """Green, amber or red for a distance or position error (cm)."""
    if not math.isfinite(err):
        return W.RED
    return W.GREEN if abs(err) <= GOOD_CM else W.AMBER if abs(err) <= FAIR_CM else W.RED


def _receiver_geometry(settings: SettingsModule) -> tuple[list[int], np.ndarray, list, str]:
    """The receivers in id order: ``(ids, (R, 2) positions, facings, problem)``.
    ``problem`` says why no calibration geometry can be computed ("" if it can)."""
    try:
        towers = sorted(settings.get_tower_coordinates(), key=lambda t: int(t[0]))
    except (TypeError, ValueError) as exc:
        return [], np.empty((0, 2)), [], f"the receivers in config.toml are unreadable ({exc})"
    ids, xy = [], []
    for rid, pos in towers:
        try:
            x, y = float(pos[0]), float(pos[1])
        except (TypeError, ValueError, IndexError):
            x = y = math.nan
        ids.append(int(rid))
        xy.append((x, y))
    coords = np.array(xy, dtype=float).reshape(-1, 2)
    facings = [settings.get_receiver_facing(rid) for rid in ids]
    if len(coords) < 3:
        problem = "fewer than 3 receivers are placed (use the Arena Maker)"
    elif not np.isfinite(coords).all():
        problem = "a receiver has no valid position (fix it in the Arena Maker)"
    elif _outline_area(coords) < 1.0:
        problem = ("the receivers are all on one spot or one line, so they enclose no arena "
                   "(place them with the Arena Maker)")
    else:
        problem = ""
    return ids, coords, facings, problem


def _outline_area(coords: np.ndarray) -> float:
    """Area (cm²) enclosed by the receivers' outline."""
    xs, ys = outline_xy(coords)
    if len(xs) < 4:
        return 0.0
    x, y = np.asarray(xs), np.asarray(ys)
    return float(abs(np.dot(x[:-1], y[1:]) - np.dot(x[1:], y[:-1])) / 2)


# ---------------------------------------------------------------------------
# Background computations (worker threads: pure functions of their inputs)
# ---------------------------------------------------------------------------

@dataclass
class _QualityJob:
    """What the green shading is computed from."""

    generation: int
    coords: np.ndarray
    fixed: list
    step: float
    inside: object
    hears: object
    target: str                 # the point it shades for: "P2", or "a point"
    title: str                  # the arena's title: "Green = best spots for P2, given P1 and P3"
    description: str            # the side panel's longer text


@dataclass
class _QualityMap:
    """A computed shading: ``Z`` the score map (lower is better), ``G`` its
    goodness (0..1, NaN where no point may go)."""

    job: _QualityJob
    xs: np.ndarray
    ys: np.ndarray
    Z: np.ndarray
    G: np.ndarray
    best: float                 # lowest score on the map (inf if none)
    floor: float                # hearing penalty every point of the map pays

    def value(self, x: float, y: float) -> float | None:
        """The score of the cell holding (x, y), or None off the map."""
        xs, ys = self.xs, self.ys
        if not len(xs) or not len(ys):
            return None
        hx = (xs[1] - xs[0]) / 2 if len(xs) > 1 else 0.5
        hy = (ys[1] - ys[0]) / 2 if len(ys) > 1 else 0.5
        if not (xs[0] - hx <= x <= xs[-1] + hx and ys[0] - hy <= y <= ys[-1] + hy):
            return None
        i = int(np.argmin(np.abs(xs - x)))
        j = int(np.argmin(np.abs(ys - y)))
        return float(self.Z[j, i])

    def describe(self, z: float | None) -> str:
        """What the score ``z`` means for the shaded point ("" off the map)."""
        if z is None:
            return ""
        target = self.job.target
        if math.isnan(z):
            return "no calibration point here"
        if math.isinf(z) or not math.isfinite(self.best):
            return f"{target} here: a receiver gets no slope"
        if z - self.floor >= points.BLOCKED_PENALTY:
            return f"{target} here: a receiver can't hear it"
        score = z - self.floor
        ratio = score / (self.best - self.floor) if self.best > self.floor else 1.0
        return f"{target} here: score {score:.2f} ({ratio:.2f}× the best)"


def _compute_quality(job: _QualityJob) -> _QualityMap:
    xs, ys, Z = points.quality_map(job.coords, fixed=job.fixed, step=job.step,
                                   inside=job.inside, hears=job.hears)
    G = points.goodness(Z)
    G[np.isnan(Z)] = np.nan               # "not a candidate" reads n/a, not 0
    finite = Z[np.isfinite(Z)]
    best = float(finite.min()) if finite.size else math.inf
    floor = 0.0
    if math.isfinite(best):
        floor = points.BLOCKED_PENALTY * math.floor(best / points.BLOCKED_PENALTY)
        if not best - floor > 0:
            floor = 0.0
    return _QualityMap(job, xs, ys, Z, G, best, floor)


def _search_sets(coords: np.ndarray, inside, hears) -> dict:
    """``points.search`` for up to ``MAX_POINTS`` points, or fewer when the
    arena has room for fewer."""
    error = None
    for n in range(points.MAX_POINTS, points.MIN_POINTS - 1, -1):
        try:
            return points.search(coords, n, inside=inside, hears=hears)
        except ValueError as exc:
            error = exc
    raise error


# ---------------------------------------------------------------------------
# Calibration run state
# ---------------------------------------------------------------------------

@dataclass
class _Results:
    """The fit after the last point of a calibration run."""

    points: list                          # the points collected at, P1 first
    offsets: dict                         # {rid: {"slope", "intercept"}} (calibrated receivers)
    fit: dict                             # UltraGPSCalibration.last_fit
    skipped: dict                         # UltraGPSCalibration.last_skipped
    modal: list                           # per point: the six modal ticks (None: no data)
    current: dict                         # {rid: (slope, intercept) | None} before saving
    saved: bool = False

    def savable(self) -> dict:
        """The offsets worth saving: every fit with a positive slope."""
        return {rid: o for rid, o in self.offsets.items() if o["slope"] > 0}


@dataclass
class _Session:
    """A calibration run in progress: one collection per saved point."""

    points: list                          # the saved points, P1 first
    coords: np.ndarray                    # the receivers when it started
    reads: int                            # calibration_reads: modal-tick hits needed
    token: int                            # identifies this session's callbacks
    done: list = field(default_factory=list)
    collecting: int = -1                  # index of the point being collected (-1: none)
    latest: list | None = None            # the latest six ticks of that collection
    reads_seen: int = 0                   # readings so far in that collection


# ---------------------------------------------------------------------------
# Live histograms
# ---------------------------------------------------------------------------

class _Histograms:
    """The tick histograms of every receiver at one calibration point, live
    while it is collected: one figure on a ``ScalingCanvas``.

    One bar per tick value within ``HIST_HALF_WIDTH`` ticks of the modal tick
    (the one calibration uses, green; the others cyan), the latest reading as
    a gold line and the reads needed (``calibration_reads``) as a dashed line:
    the point is done when every green bar reaches it.  Readings of 0 (no
    echo) and ticks off the scale are counted under each plot.
    """

    def __init__(self, receivers: int = 6):
        self.receivers = receivers
        self.figure = Figure(facecolor="black")
        self.canvas = ScalingCanvas(self.figure, ref_size=(340, 620))
        ui_scale.min_size(self.canvas, 180, 160)
        self.axes: list = []
        self.title = None
        self.columns = 0
        self.set_columns(2)

    def set_columns(self, columns: int) -> None:
        """Lay the plots out in ``columns`` columns: 2 in a tall pane beside
        the arena, 3 in a wide strip under it (the plots are drawn anew)."""
        if columns == self.columns:
            return
        self.columns = columns
        rows = max(1, math.ceil(self.receivers / columns))
        tall = rows > columns
        title = self.title.get_text() if self.axes else "Tick histograms"
        self.figure.clear()
        grid = self.figure.add_gridspec(rows, columns, left=0.13 if tall else 0.07, right=0.97,
                                        top=0.89 if tall else 0.84, bottom=0.07 if tall else 0.1,
                                        hspace=1.0 if tall else 0.85, wspace=0.42 if tall else 0.3)
        self.axes = [self.figure.add_subplot(grid[k // columns, k % columns])
                     for k in range(self.receivers)]
        self.title = self.figure.suptitle("", color=W.CYAN, fontsize=10, fontweight="bold")
        # The text keeps its size when the pane turns from tall to wide
        if tall:
            self.canvas.set_ref_size(340, 620)
        else:
            self.canvas.set_ref_size(780, 360)
        self.clear(title)

    def clear(self, title: str) -> None:
        """Empty plots under ``title``."""
        self.title.set_text(title)
        for rid, ax in enumerate(self.axes):
            ax.clear()
            self._style(ax)
            ax.set_title(f"R{rid + 1}", color=W.DIM, fontsize=9)
            ax.set_xticks([])
            ax.set_yticks([])
        self.canvas.draw_idle()

    def show(self, cal: UltraGPSCalibration, run: int, title: str, reads: int,
             latest=None) -> None:
        """Draw run ``run``'s histograms (``latest``: the newest six ticks)."""
        self.title.set_text(title)
        for rid, ax in enumerate(self.axes):
            tick = latest[rid] if latest is not None and rid < len(latest) else None
            self._draw(ax, f"R{rid + 1}", cal.get_histogram_data(rid, run), reads, tick)
        self.canvas.draw_idle()

    @staticmethod
    def _style(ax) -> None:
        ax.set_facecolor("black")
        ax.tick_params(colors="white", labelsize=7, length=2, pad=1)
        for spine in ax.spines.values():
            spine.set_color("#666666")

    def _draw(self, ax, name: str, data: dict, reads: int, latest) -> None:
        ax.clear()
        self._style(ax)
        # The calibration library's modal tick: the first most frequent value
        mode = max(data, key=data.__getitem__) if data else None
        top = data[mode] if mode is not None else 0
        silent = data.pop(0.0, 0)
        if not data:
            ax.set_title(f"{name}  no echo" if silent else name,
                         color=W.RED if silent else W.DIM, fontsize=9)
            ax.text(0.5, 0.5, f"{silent} × no echo" if silent else "no readings yet",
                    color=W.RED if silent else W.DIM, fontsize=8, ha="center", va="center",
                    transform=ax.transAxes)
            ax.set_xticks([])
            ax.set_yticks([])
            return
        ticks = np.array(sorted(data), dtype=float)
        counts = np.array([data[t] for t in ticks], dtype=float)
        centre = mode if mode else ticks[int(np.argmax(counts))]
        shown = np.abs(ticks - centre) <= HIST_HALF_WIDTH
        colors = [W.GREEN if t == mode else W.CYAN for t in ticks[shown]]
        ax.bar(ticks[shown], counts[shown], width=0.8, color=colors, alpha=0.9)
        ax.axhline(reads, color=W.GOLD, linestyle="--", linewidth=1.0)
        if latest and abs(float(latest) - centre) <= HIST_HALF_WIDTH:
            ax.axvline(float(latest), color=W.GOLD, linewidth=1.2, alpha=0.85)
        lo = min(float(ticks[shown].min()), centre - 3) - 1
        hi = max(float(ticks[shown].max()), centre + 3) + 1
        ax.set_xlim(lo, hi)
        ax.set_ylim(0, max(reads, int(counts.max()), top) * 1.3)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=3, integer=True, prune="lower"))
        ax.yaxis.set_major_locator(MaxNLocator(nbins=3, integer=True))
        notes = []
        if silent:
            notes.append(f"{silent} no echo")
        off = int(counts[~shown].sum())
        if off:
            notes.append(f"{off} off scale")
        if notes:
            ax.set_xlabel(" · ".join(notes), color=W.AMBER, fontsize=7, labelpad=1)
        if not mode:
            ax.set_title(f"{name}  no echo ×{top}", color=W.RED, fontsize=9)
        else:
            ax.set_title(f"{name}  {mode:.0f} ×{top}", color=W.GREEN if top >= reads else "white",
                         fontsize=9)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class CalibrationPanel(QWidget):
    """Full-page calibration panel: the arena beside the Points, Origin
    (0,0), Run and Check tabs (see the module docstring).

    Every config write goes through ``SettingsModule.set_cal_points`` /
    ``shift_origin`` or ``UltraGPSCalibration.save_offsets`` and is followed by
    ``main_window.notify_config_changed()``; ``config_changed`` from elsewhere
    reloads the panel (on its next show if it is hidden: unsaved point edits
    survive unless the saved points moved; a calibration run, an accuracy
    check's measurement and an origin candidate from before the receivers
    moved are dropped).  Saving points or the offsets, and moving (0, 0),
    wait for a running measurement.  ``on_panel_hide`` stops a collection,
    cancels measurements (waiting up to ``STOP_WAIT_MS`` for one in flight),
    drops pending shading and search results and leaves pick modes;
    ``on_panel_show`` re-enables the arena and refreshes.

    Args:
        cal:             The main window's ``UltraGPSCalibration``.
        settings_module: The main window's ``SettingsModule``.
        main_window:     The main window: ``client``, ``position_lib``,
                         ``show_panel``, ``notify_config_changed`` and the
                         ``config_changed`` / ``link_changed`` signals.
    """

    # The calibration library calls back from its collection thread; these
    # signals carry the callbacks to the GUI thread (queued).  The first value
    # is the session token: a callback from an abandoned session is dropped.
    _sig_reading = pyqtSignal(int, int, int)       # token, run, readings so far
    _sig_ticks = pyqtSignal(int, int, object)      # token, run, the latest six ticks
    _sig_complete = pyqtSignal(int, int)           # token, run
    _sig_failed = pyqtSignal(int, int, str)        # token, run, message

    def __init__(self, cal: UltraGPSCalibration, settings_module: SettingsModule, main_window):
        super().__init__()
        self._cal = cal
        self._settings = settings_module
        self._main_window = main_window
        # Black behind everything, without the rule cascading to the children
        # (a plain container inside a grey section would paint itself black)
        self.setObjectName("calibrationPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        ui_scale.style(self, "QWidget#calibrationPanel { background-color: black; }")

        # Receivers and calibration points (from the settings)
        self._rx_ids: list[int] = []
        self._coords = np.empty((0, 2))
        self._facings: list = []
        self._geometry_error = ""
        self._saved: list[tuple[float, float]] = []
        self._saved_error = ""
        self._points: list[tuple[float, float]] = []      # the table's (maybe unsaved) points
        self._check: points.PointCheck | None = None

        # Green shading and best-point search (worker threads)
        self._quality: _QualityMap | None = None
        self._heat_shown = False
        self._quality_gen = 0
        self._quality_worker = None
        self._quality_pending = False
        self._quality_error = ""
        self._search: dict | None = None                  # {"key": ..., "sets": {n: (n, 2)}}
        self._search_gen = 0
        self._search_worker = None
        self._preview: np.ndarray | None = None

        # Calibration run
        self._session: _Session | None = None
        self._session_token = 0
        self._results: _Results | None = None

        # Origin and accuracy check
        self._origin_candidate = None
        self._check_point = (0.0, 0.0)
        self._check_worker = None
        self._check_measured: measure.MeasureResult | None = None
        self._check_result: measure.PointCheck | None = None

        self._pick_source: str | None = None              # "points", "origin" or "check"
        self._arena_layout = None                         # (legend shown, legend place)
        self._legend_place: str | None = None             # "right" or "below"
        self._shown = False
        self._config_dirty = False
        self._own_change = False
        self._got_config_changed = False

        self._quality_timer = QTimer(self)
        self._quality_timer.setSingleShot(True)
        self._quality_timer.setInterval(QUALITY_DEBOUNCE_MS)
        self._quality_timer.timeout.connect(self._start_quality)
        self._hist_timer = QTimer(self)
        self._hist_timer.setSingleShot(True)
        self._hist_timer.setInterval(HIST_REDRAW_MS)
        self._hist_timer.timeout.connect(self._draw_histograms)

        self._build_ui()
        self._connect_signals()
        self._load_from_settings("drop")
        self._check_point = self._default_check_point()
        self._check_table.set_points([self._check_point], labels=["Known"])
        self._check_table.select(0)               # so Offset… works straight away
        self._sync_link()
        self._on_tab_changed(TAB_POINTS)

    # ------------------------------------------------------------------
    # Main-window resources
    # ------------------------------------------------------------------

    @property
    def _client(self):
        return self._main_window.client

    @property
    def _position_lib(self):
        return self._main_window.position_lib

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_top_bar())

        body = QHBoxLayout()
        ui_scale.margins(body, 6, 0, 6, 6)
        ui_scale.spacing(body, 6)
        root.addLayout(body, 1)

        # Arena, with the run's histograms beside it and its results below
        self._fig = Figure(facecolor="black")
        self._view = ArenaView(self._fig.add_subplot(111), title="Calibration points",
                               legend_loc="upper left", legend_anchor=(1.03, 1.0))
        self._canvas = ScalingCanvas(self._fig, ref_size=(700, 700))
        ui_scale.min_size(self._canvas, 240, 220)
        self._interactor = ArenaInteractor(self._canvas, self._view, snap=1.0)
        self._interactor.set_readout_formatter(self._format_readout)
        self._hist = _Histograms(self._cal.RECEIVER_COUNT)

        self._arena_split = QSplitter(Qt.Orientation.Horizontal)
        self._arena_split.addWidget(self._canvas)
        self._arena_split.addWidget(self._hist.canvas)
        self._arena_split.setStretchFactor(0, 3)
        self._arena_split.setStretchFactor(1, 2)
        self._arena_split.setChildrenCollapsible(False)
        ui_scale.call(self._arena_split, "setHandleWidth", 6)
        ui_scale.style(self._arena_split, _SPLITTER_CSS)
        main = QWidget()
        main_col = QVBoxLayout(main)
        main_col.setContentsMargins(0, 0, 0, 0)
        ui_scale.spacing(main_col, 6)
        main_col.addWidget(self._arena_split, 1)
        main_col.addWidget(self._build_results_frame())     # as tall as its rows
        body.addWidget(main, 1)

        self._tabs = QTabWidget()
        self._tabs.addTab(W.scroll_area(self._build_points_tab()), "Points")
        self._tabs.addTab(W.scroll_area(self._build_origin_tab()), "Origin (0,0)")
        self._tabs.addTab(W.scroll_area(self._build_run_tab()), "Run")
        self._tabs.addTab(W.scroll_area(self._build_check_tab()), "Check")
        self._tabs.setTabToolTip(TAB_POINTS, "Choose and save the calibration points")
        self._tabs.setTabToolTip(TAB_ORIGIN, "Move the arena's (0, 0)")
        self._tabs.setTabToolTip(TAB_RUN, "Collect at every point and fit the receivers' offsets")
        self._tabs.setTabToolTip(TAB_CHECK, "Measure a known point to check the accuracy")
        ui_scale.style(self._tabs, _TABS_CSS)
        ui_scale.min_width(self._tabs, 340)
        ui_scale.max_width(self._tabs, 410)
        body.addWidget(self._tabs)

    def _build_top_bar(self) -> QWidget:
        top = QWidget()
        bar = QHBoxLayout(top)
        ui_scale.margins(bar, 10, 5, 10, 5)
        ui_scale.spacing(bar, 10)
        title = QLabel("Calibration")
        ui_scale.font(title, 16, bold=True)
        ui_scale.style(title, f"color: {W.MAGENTA}; background: transparent;")
        bar.addWidget(title)
        self._link_chip = _text("● Ground station link down", W.RED, 12, bold=True, wrap=False)
        self._link_chip.setVisible(False)
        bar.addWidget(self._link_chip)
        bar.addStretch(1)
        base = W.neon_button("Base Station…", W.CYAN, tooltip=(
            "Trigger rate, sampling window, burst delay (D / E), raw pulses: a burst-delay change "
            "shifts every receiver's intercept, so calibrate again after one"))
        base.clicked.connect(self._show_base_station)
        bar.addWidget(base)
        back = W.neon_button("← Back", W.GREEN)
        back.clicked.connect(self._back_to_menu)
        bar.addWidget(back)
        return top

    def _build_points_tab(self) -> QWidget:
        page, col = _page()

        sec = W.section("Calibration points", W.GREEN)
        sec.add(_text("2 to 4 spots to put the transmitter on. Type X / Y, use Offset… (dX / dY, "
                      "or a distance and an angle from the origin, a receiver or another point), "
                      "turn on Pick on arena and click, or drag a point on the arena."))
        self._table = W.PointTable(prefix="P", accent=W.GREEN, min_rows=points.MIN_POINTS,
                                   max_rows=points.MAX_POINTS, add_remove=True,
                                   new_point=self._new_point)
        self._table.pointsEdited.connect(self._on_points_edited)
        self._table.selectionChanged.connect(self._on_selection_changed)
        self._table.pickModeChanged.connect(functools.partial(self._on_pick_mode, "points"))
        sec.add(self._table)
        self._score_label = _text("", "white", 12, rich=True)
        self._score_label.setToolTip(points.SCORE_TIP)
        sec.add(self._score_label)
        self._issues_label = _text("", rich=True)
        sec.add(self._issues_label)
        self._save_btn = W.neon_button("Save points", W.GREEN, tooltip=(
            "Write the points to config.toml, with every receiver's distance to them "
            "(cal_distances)"))
        self._save_btn.clicked.connect(self._save_points)
        self._revert_btn = W.neon_button("Revert", "#555555", "white",
                                         tooltip="Go back to the saved points")
        self._revert_btn.clicked.connect(self._revert_points)
        sec.add_layout(_row(self._save_btn, self._revert_btn))
        self._points_status = _status_label()
        sec.add(self._points_status)
        col.addWidget(sec)

        shade = W.section("Green shading", W.GREEN)
        self._model_combo = _combo()
        for name, model in coverage.PRESETS.items():
            self._model_combo.addItem(name)
            self._model_combo.setItemData(self._model_combo.count() - 1, model.describe(),
                                          Qt.ItemDataRole.ToolTipRole)
        self._model_combo.setToolTip("Which receiver limits decide where a receiver can hear the "
                                     "transmitter: the simulator's beam model, or the HC-SR04 "
                                     "datasheet's 2-400 cm within ±15°")
        self._model_combo.currentIndexChanged.connect(self._on_model_changed)
        shade.add_layout(_row(_text("Receivers", "white", 12, wrap=False), self._model_combo,
                              stretch=1))
        self._shade_label = _text("", rich=True)
        shade.add(self._shade_label)
        self._beams_check = QCheckBox("Show receiver beams")
        self._beams_check.setToolTip("Each receiver's facing and the half-angle of the model's "
                                     "beam")
        self._beams_check.toggled.connect(self._on_beams_toggled)
        shade.add(self._beams_check)
        col.addWidget(shade)

        best = W.section("Find best points", W.GOLD)
        best.add(_text("Searches the arena for the set with the lowest arena score that every "
                       "receiver hears (under the receiver model above)."))
        self._count_combo = _combo()
        for n in range(points.MIN_POINTS, points.MAX_POINTS + 1):
            self._count_combo.addItem(f"{n} points", n)
        self._count_combo.setCurrentIndex(points.MAX_POINTS - points.MIN_POINTS)
        self._count_combo.currentIndexChanged.connect(self._on_count_changed)
        self._find_btn = W.neon_button("Find best points", W.GOLD)
        self._find_btn.clicked.connect(self._find_best)
        best.add_layout(_row(self._count_combo, self._find_btn))
        self._best_label = _text("", rich=True)
        self._best_label.setVisible(False)
        best.add(self._best_label)
        self._use_btn = W.neon_button("Use these points", W.GOLD,
                                      tooltip="Put the previewed points in the table (then Save)")
        self._use_btn.clicked.connect(self._use_preview)
        self._discard_btn = W.neon_button("Discard", "#555555", "white")
        self._discard_btn.clicked.connect(self._discard_preview)
        best.add_layout(_row(self._use_btn, self._discard_btn))
        col.addWidget(best)

        errs = W.section("Expected error per receiver", W.CYAN, collapsible=True)
        errs.add(_text("The calibration error each receiver would get from these points, per cm "
                       "of error in a single reading: its slope error and its intercept error. "
                       "A receiver wants its points at very different distances from it."))
        self._errors_table = _table(["", "Slope ±", "Intercept ±"], self._cal.RECEIVER_COUNT,
                                    stretch=(1, 2))
        errs.add(self._errors_table)
        col.addWidget(errs)
        col.addStretch(1)
        return page

    def _build_origin_tab(self) -> QWidget:
        page, col = _page()
        self._origin = W.OriginTool(measure=self._measure_origin, accent=W.BLUE)
        self._origin.originRequested.connect(self._on_origin_requested)
        self._origin.previewChanged.connect(self._on_origin_preview)
        self._origin.pickModeChanged.connect(functools.partial(self._on_pick_mode, "origin"))
        self._origin.measuringChanged.connect(self._on_measuring_changed)
        col.addWidget(self._origin)

        info = W.section("Calibrating (0, 0)", W.BLUE)
        info.add(_text("Point at the spot that should be (0, 0): pick it on the arena, type it "
                       "in the current frame, give it as an offset from today's (0, 0) or a "
                       "receiver, or stand the transmitter on the physical mark and press "
                       f"Measure (the mean of {ORIGIN_SAMPLES} fixes)."))
        info.add(_text("Applying it moves the whole frame: receivers, calibration points, "
                       "barriers and image overlays. Distances do not change, so the receivers' "
                       "slopes and intercepts stay valid: no new calibration is needed."))
        col.addWidget(info)
        col.addStretch(1)
        return page

    def _build_run_tab(self) -> QWidget:
        page, col = _page()

        sec = W.section("Calibration run", W.GOLD)
        sec.add(_text("One collection per saved calibration point. Place the transmitter on the "
                      "point, press Collect and keep it still until every receiver has read its "
                      "most frequent tick often enough (the dashed line on the histograms)."))
        self._prompt = _text("", W.GOLD, 13, bold=True)
        sec.add(self._prompt)
        self._start_btn = W.neon_button("Start calibration", W.GOLD, padding=(6, 14))
        self._start_btn.clicked.connect(self._start_session)
        sec.add_layout(_row(self._start_btn))

        self._run_combo = _combo()
        self._run_combo.setToolTip("The point to collect next; choose a done point to collect "
                                   "it again")
        self._run_combo.currentIndexChanged.connect(self._on_run_point_changed)
        self._collect_btn = W.neon_button("Collect P1", W.GREEN)
        self._collect_btn.clicked.connect(self._collect)
        self._stop_btn = W.neon_button("Stop", W.RED, "white",
                                       tooltip="Stop collecting at this point")
        self._stop_btn.clicked.connect(lambda: self._stop_collect())
        self._run_row = QWidget()
        run_row = QHBoxLayout(self._run_row)
        run_row.setContentsMargins(0, 0, 0, 0)
        ui_scale.spacing(run_row, 6)
        for w in (self._run_combo, self._collect_btn, self._stop_btn):
            run_row.addWidget(w)
        run_row.addStretch(1)
        sec.add(self._run_row)

        self._steps_label = _text("", rich=True)
        sec.add(self._steps_label)
        self._rx_bars = []
        bars = QGridLayout()
        ui_scale.spacing(bars, 4)
        for rid in range(self._cal.RECEIVER_COUNT):
            bar = W.progress_bar(W.GREEN, text=True)
            bar.setFormat(f"R{rid + 1}")
            bar.setToolTip("Times the receiver has read its most frequent tick, of the reads "
                           "needed")
            bars.addWidget(bar, rid // 2, rid % 2)
            self._rx_bars.append(bar)
        self._bars_box = QWidget()
        self._bars_box.setLayout(bars)
        bars.setContentsMargins(0, 0, 0, 0)
        sec.add(self._bars_box)
        self._run_status = _status_label()
        sec.add(self._run_status)
        self._run_link_note = _status_label()
        sec.add(self._run_link_note)
        self._cancel_btn = W.neon_button("Cancel calibration", "#555555", "white", tooltip=(
            "Stop and forget this calibration run: config.toml keeps its offsets"))
        self._cancel_btn.clicked.connect(self._cancel_session)
        sec.add_layout(_row(self._cancel_btn))
        col.addWidget(sec)

        self._results_section = W.section("Results", W.GREEN)
        self._results_summary = _text("", rich=True)
        self._results_section.add(self._results_summary)
        self._save_offsets_btn = W.neon_button("Save offsets", W.GREEN, padding=(6, 14), tooltip=(
            "Write the new slopes and intercepts to config.toml (receivers that could not be "
            "calibrated keep theirs)"))
        self._save_offsets_btn.clicked.connect(self._save_offsets)
        self._discard_results_btn = W.neon_button("Discard", "#555555", "white")
        self._discard_results_btn.clicked.connect(self._cancel_session)
        self._results_section.add_layout(_row(self._save_offsets_btn, self._discard_results_btn))
        self._results_status = _status_label()
        self._results_section.add(self._results_status)
        col.addWidget(self._results_section)

        col.addWidget(_text("Changing the burst delay (D or E on the Base Station panel) shifts "
                            "every receiver's intercept: calibrate again after one."))
        col.addStretch(1)
        return page

    def _build_check_tab(self) -> QWidget:
        page, col = _page()

        sec = W.section("Accuracy check", W.ORANGE)
        sec.add(_text("Put the transmitter on a spot you know, give that spot here (type it, use "
                      "Offset…, pick it, or drag the orange × on the arena) and press Measure: "
                      "the measured position and every receiver's range are compared with it."))
        self._check_table = W.PointTable(prefix="", accent=W.ORANGE, min_rows=1, max_rows=1)
        self._check_table.pointsEdited.connect(self._on_check_point_edited)
        self._check_table.pickModeChanged.connect(functools.partial(self._on_pick_mode, "check"))
        sec.add(self._check_table)
        self._samples_spin = QSpinBox()
        self._samples_spin.setRange(5, 200)
        self._samples_spin.setValue(CHECK_SAMPLES)
        self._samples_spin.setToolTip("Readings averaged")
        self._measure_btn = W.neon_button("Measure", W.ORANGE, padding=(5, 16))
        self._measure_btn.clicked.connect(self._measure_check)
        sec.add_layout(_row(_text("Readings", "white", 12, wrap=False), self._samples_spin,
                            self._measure_btn))
        self._check_progress = W.progress_bar(W.ORANGE)
        self._check_progress.setVisible(False)
        sec.add(self._check_progress)
        self._check_status = _status_label()
        sec.add(self._check_status)
        self._check_link_note = _status_label()
        sec.add(self._check_link_note)
        col.addWidget(sec)

        self._check_section = W.section("Result", W.ORANGE)
        self._check_summary = _text("", "white", 12, rich=True)
        self._check_section.add(self._check_summary)
        self._check_rx_table = _table(["", "True cm", "Measured cm", "Error cm"],
                                      self._cal.RECEIVER_COUNT, stretch=(1, 2, 3))
        self._check_rx_table.setToolTip("Each receiver's mean measured distance against its true "
                                        "distance to the known point (positive: it reads long)")
        self._check_section.add(self._check_rx_table)
        self._check_section.setVisible(False)
        col.addWidget(self._check_section)
        col.addStretch(1)
        return page

    def _build_results_frame(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("calResults")
        ui_scale.style(frame, "QFrame#calResults { background-color: #0D0D0D; "
                              "border: 1px solid #333333; border-radius: 6px; }")
        lay = QVBoxLayout(frame)
        ui_scale.margins(lay, 8, 6, 8, 8)
        ui_scale.spacing(lay, 4)
        self._results_title = _text("Calibration results", W.GREEN, 13, bold=True, wrap=False)
        lay.addWidget(self._results_title)
        self._results_table = _table(["", "Slope\nnew", "Slope\nnow", "Intercept cm\nnew",
                                      "Intercept cm\nnow", "Modal ticks", "Residuals cm",
                                      "RMS\ncm", "Note"],
                                     self._cal.RECEIVER_COUNT, stretch=(8,))
        self._results_table.setToolTip(
            "distance = tick × slope + intercept. Modal ticks: each point's most frequent tick "
            "(the one the fit uses). Residuals: fitted minus known distance at each point; with 2 "
            "points the line passes through both, so only 3 or 4 points can show a bad reading.")
        lay.addWidget(self._results_table)
        self._results_frame = frame
        frame.setVisible(False)
        return frame

    def _connect_signals(self) -> None:
        self._canvas.installEventFilter(self)
        self._arena_split.installEventFilter(self)
        self._sig_reading.connect(self._on_run_reading)
        self._sig_ticks.connect(self._on_run_ticks)
        self._sig_complete.connect(self._on_run_complete)
        self._sig_failed.connect(self._on_run_failed)
        changed = getattr(self._main_window, "config_changed", None)
        if changed is not None:
            changed.connect(self._on_config_changed)
        link = getattr(self._main_window, "link_changed", None)
        if link is not None:
            link.connect(self._on_link_changed)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        it = self._interactor
        it.clicked.connect(self._on_arena_clicked)
        it.point_pressed.connect(self._on_point_pressed)
        it.point_dragged.connect(self._on_point_dragged)
        it.drag_finished.connect(self._on_drag_finished)

    # ------------------------------------------------------------------
    # Panel lifecycle
    # ------------------------------------------------------------------

    def on_panel_show(self) -> None:
        self._shown = True
        if self._config_dirty:
            self._load_from_settings("auto")
        else:
            # The checks follow the receiver model, whose sampling window the
            # Base Station panel may have changed meanwhile
            self._update_points_state()
        self._interactor.enable()
        self._sync_link()
        self._on_tab_changed(self._tabs.currentIndex())
        self._schedule_quality()

    def on_panel_hide(self) -> None:
        """Stop everything the panel runs: a collection, measurements, pick
        modes and pending computations (their results are dropped)."""
        self._shown = False
        self._interactor.disable()
        self._leave_pick_modes()
        self._quality_timer.stop()
        self._hist_timer.stop()
        self._quality_gen += 1
        if self._search_worker is not None:
            self._search_gen += 1
            self._set_points_status("Point search abandoned: you left the panel.", W.AMBER)
        s = self._session
        if s is not None and s.collecting >= 0:
            k = s.collecting + 1
            self._stop_collect(note=f"Collecting at P{k} stopped when you left the panel: press "
                                    f"Collect P{k} to collect it again.")
        worker = self._check_worker
        if worker is not None:
            worker.cancel()
            worker.wait(STOP_WAIT_MS)
        if self._origin.is_measuring():
            self._origin.cancel_measurement()
            self._origin.wait(STOP_WAIT_MS)
        # A drag cut short leaves its marker and table row where it was dragged
        self._table.set_points(self._points)
        self._check_table.set_points([self._check_point], labels=["Known"])

    def _back_to_menu(self) -> None:
        self._main_window.show_panel("main")

    def _show_base_station(self) -> None:
        try:
            self._main_window.show_panel("base_station")
        except Exception as exc:          # a missing panel must not take the app down
            log.exception("Could not show the base station panel")
            W.error(self, "Base Station", f"The Base Station panel could not be opened: {exc}")

    # ------------------------------------------------------------------
    # Settings, link and tabs
    # ------------------------------------------------------------------

    def _load_from_settings(self, edits: str = "keep") -> None:
        """(Re)read the receivers and the saved calibration points.

        ``edits`` says what happens to unsaved point edits: "keep" them,
        "drop" them, or "auto" (a change made in another panel): keep them
        unless the saved points changed (another panel moved the origin, say).
        """
        self._config_dirty = False
        old_coords, old_saved = self._coords, self._saved
        self._rx_ids, self._coords, self._facings, self._geometry_error = \
            _receiver_geometry(self._settings)
        moved = (old_coords.shape != self._coords.shape
                 or not np.array_equal(old_coords, self._coords, equal_nan=True))
        if moved:
            self._quality = None                  # the shading on show is for other receivers
        try:
            saved = [(float(p[0]), float(p[1])) for p in self._settings.cal_points]
            self._saved_error = ""
        except (ValueError, TypeError, IndexError) as exc:
            saved, self._saved_error = [], str(exc)
        unsaved = bool(self._points) and not _same_points(self._points, self._saved)
        keep = unsaved and (edits == "keep"
                            or (edits == "auto" and _same_points(saved, self._saved)))
        self._saved = saved
        if not keep:
            self._points = list(saved)
        if unsaved and not keep:
            self._set_points_status("The configuration changed meanwhile: your unsaved point "
                                    "edits were dropped.", W.AMBER)
        if moved and edits == "auto":
            self._follow_arena_change(_frame_shift(old_coords, self._coords, old_saved, saved))

        refs = self._receiver_refs()
        saved_refs = {f"P{k + 1}": p for k, p in enumerate(saved)}
        self._table.set_references(refs)
        self._table.set_points(self._points)
        self._origin.set_references({**refs, **saved_refs})
        self._check_table.set_references({**refs, **saved_refs})
        self._draw_receivers()
        self._invalidate_search()
        self._validate_session()
        self._update_points_state()
        self._fit_view()
        self._schedule_quality()

    def _follow_arena_change(self, shift: tuple[float, float] | None) -> None:
        """Another panel moved the receivers.  The accuracy check's measurement
        and the origin candidate belong to the old receivers (or the old
        frame), so they go.  When the whole frame moved by ``shift`` (the
        origin was moved there) the known point moves with it, as after a
        shift made here."""
        if shift is not None:
            self._check_point = (self._check_point[0] + shift[0],
                                 self._check_point[1] + shift[1])
            self._check_table.set_points([self._check_point], labels=["Known"])
        if self._check_measured is not None:
            self._clear_check_result()
            _set_status(self._check_status, "The arena was changed in another panel since this "
                                            "measurement: measure again.", W.AMBER)
        if self._origin.candidate() is not None:
            self._origin.clear()
            self._origin.set_status("The arena was changed in another panel: choose the new "
                                    "(0, 0) again.", W.AMBER)

    def _receiver_refs(self) -> dict:
        return {f"R{rid + 1}": (float(x), float(y))
                for rid, (x, y) in zip(self._rx_ids, self._coords)
                if math.isfinite(x) and math.isfinite(y)}

    def _on_config_changed(self) -> None:
        self._got_config_changed = True
        if self._own_change:
            self._load_from_settings("keep")
        elif self._shown:
            self._load_from_settings("auto")
        else:
            self._config_dirty = True

    def _notify_config_changed(self) -> None:
        """After a save: the main window reloads the settings, the calibration
        library's path and the position library, then emits
        ``config_changed`` (which reloads this panel)."""
        self._own_change = True
        self._got_config_changed = False
        try:
            self._main_window.notify_config_changed()
        except Exception:                 # never let a reload failure escape a slot
            log.exception("notify_config_changed failed")
        finally:
            self._own_change = False
        if not self._got_config_changed:  # no signal came back: reload by ourselves
            try:
                self._settings.reload_config()
            except (OSError, ValueError) as exc:
                log.warning("Could not reload the settings: %s", exc)
            self._load_from_settings("keep")

    def _link_up(self) -> bool:
        return bool(getattr(self._client, "connected", False))

    def _link_down_text(self, what: str = "") -> str:
        """Why the ground station can't be used now."""
        error = getattr(self._client, "last_error", None)
        text = "the ground station link is down" + (f" ({error})" if error else "")
        return f"{what}: {text}." if what else text[0].upper() + text[1:] + "."

    def _on_link_changed(self, _up: bool = True) -> None:
        self._sync_link()

    def _sync_link(self) -> None:
        """Follow the ground-station link: what needs it is disabled while it
        is down, and says why."""
        up = self._link_up()
        self._origin.set_measure(self._measure_origin if up else None)
        self._link_chip.setVisible(not up)
        self._link_chip.setToolTip("" if up else self._link_down_text())
        note = "" if up else self._link_down_text() + " Collecting and measuring wait for it."
        _set_status(self._run_link_note, note, W.RED)
        _set_status(self._check_link_note, note, W.RED)
        self._update_run_ui()
        self._update_check_ui()

    def _on_tab_changed(self, index: int) -> None:
        self._leave_pick_modes()
        run = index == TAB_RUN
        self._show_histograms_pane(run)
        self._show_results_frame(run and self._results is not None)
        self._apply_arena_layout()
        if index == TAB_POINTS:
            self._show_quality()
        elif self._heat_shown:
            self._view.clear_heatmap()
            self._heat_shown = False
        self._refresh_arena()
        if run:
            self._draw_histograms()

    def eventFilter(self, obj, event) -> bool:
        # Lay the arena and the run's plots out for the shape they are given
        if event.type() == QEvent.Type.Resize:
            if obj is self._canvas:
                self._apply_arena_layout()
            elif obj is self._arena_split:
                self._apply_run_layout()
        return False

    def _apply_arena_layout(self) -> None:
        """The arena's margins and its legend (Points and Check tabs): beside
        the arena on a wide canvas, under it on a tall one; none on the other
        tabs, which leaves the arena the whole canvas."""
        legend = self._tabs.currentIndex() in (TAB_POINTS, TAB_CHECK)
        place = self._legend_place
        if legend:
            w, h = self._canvas.width(), self._canvas.height()
            place = "right" if h <= 0 or w >= WIDE_ASPECT * h else "below"
        if (legend, place) == self._arena_layout:
            return
        self._arena_layout = (legend, place)
        if place != self._legend_place:           # (forgets where the user dragged it)
            self._legend_place = place
            if place == "right":
                self._view.set_legend_location("upper left", anchor=(1.03, 1.0), ncol=1)
            else:
                self._view.set_legend_location("upper center", anchor=(0.5, -0.1), ncol=3)
        if not legend:
            layout, anchor = _LAYOUT_FULL, "C"
        elif place == "right":
            layout, anchor = _LAYOUT_LEGEND_RIGHT, "C"
        else:
            layout, anchor = _LAYOUT_LEGEND_BELOW, "N"     # the legend takes the room below
        self._fig.subplots_adjust(**layout)
        self._view.ax.set_anchor(anchor)
        self._view.set_legend_visible(legend)
        self._view.draw_idle()

    def _apply_run_layout(self) -> None:
        """The run's histograms beside the arena when the plot area is wide,
        under it (in three columns) when it is tall."""
        size = self._arena_split.size()
        wide = size.height() <= 0 or size.width() >= WIDE_ASPECT * size.height()
        orientation = Qt.Orientation.Horizontal if wide else Qt.Orientation.Vertical
        if orientation == self._arena_split.orientation():
            return
        self._arena_split.setOrientation(orientation)
        self._hist.set_columns(2 if wide else 3)
        if self._hist.canvas.isVisible():
            total = sum(self._arena_split.sizes())
            if total > 0:
                self._arena_split.setSizes([total * 3 // 5, total - total * 3 // 5])
            self._draw_histograms()

    def _show_histograms_pane(self, visible: bool) -> None:
        """The run's histograms beside the arena (Run tab only); the arena
        keeps about three fifths of the width when they appear."""
        canvas = self._hist.canvas
        if visible == canvas.isVisible():
            return
        canvas.setVisible(visible)
        if visible:
            total = sum(self._arena_split.sizes())
            if total > 0:
                self._arena_split.setSizes([total * 3 // 5, total - total * 3 // 5])

    def _show_results_frame(self, visible: bool) -> None:
        """The results table under the arena (Run tab, once there are results)."""
        self._results_frame.setVisible(visible)

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def _model(self) -> coverage.SensorModel:
        """The receiver model chosen for the shading and the checks, with the
        base station's sampling window when it is known."""
        model = coverage.PRESETS.get(self._model_combo.currentText(), coverage.SIMULATOR)
        window = getattr(self._client, "sampling_time_ms", None)
        if window:
            try:
                model = model.copy(window_ms=float(window))
            except (TypeError, ValueError):
                pass
        return model

    def _inside_fn(self):
        """``inside(points)``: within the receivers' outline."""
        xs, ys = outline_xy(self._coords)
        if len(xs) < 4:
            return lambda pts: np.zeros(len(pts), dtype=bool)
        return Path(np.column_stack([xs, ys]), closed=True).contains_points

    def _hears_fn(self):
        """``hears(points)``: which receivers hear the transmitter there."""
        return functools.partial(coverage.hears, coords=self._coords.copy(),
                                 facing_deg=list(self._facings), model=self._model())

    def _quality_step(self) -> float:
        """Shading cell size (cm): about ``QUALITY_CELLS`` across the receivers' box."""
        finite = self._coords[np.isfinite(self._coords).all(axis=1)]
        span = float(np.ptp(finite, axis=0).max()) if len(finite) else 0.0
        return min(10.0, max(2.5, round(span / QUALITY_CELLS * 2) / 2))

    def _dirty(self) -> bool:
        """Whether the table holds point edits that are not saved."""
        return not _same_points(self._points, self._saved)

    def _default_check_point(self) -> tuple[float, float]:
        """A first known point for the check: a quarter of the way from the
        receivers' centre towards their upper-right corner (clear of the
        origin marker), on a 5 cm grid."""
        finite = self._coords[np.isfinite(self._coords).all(axis=1)]
        if not len(finite):
            return (0.0, 0.0)
        p = finite.mean(axis=0) + np.ptp(finite, axis=0) / 4
        return (float(round(p[0] / 5) * 5), float(round(p[1] / 5) * 5))

    def _fit_view(self) -> None:
        """Fit the arena to the receivers and every point on any tab: after
        each committed edit, so a point typed far off widens the view and
        correcting it brings the arena back (never during a drag)."""
        extra = list(self._points) + list(self._saved) + [self._check_point]
        self._view.set_limits(extra_points=[p for p in extra if all(map(math.isfinite, p))])

    def _draw_receivers(self) -> None:
        beams = self._beams_check.isChecked()
        model = self._model()
        half = model.fov_half_deg if model.fov_half_deg else model.rx_beam_deg
        positions = [(rid, (float(x), float(y))) for rid, (x, y) in zip(self._rx_ids, self._coords)]
        self._view.set_receivers(positions, facing_deg=list(self._facings) if beams else None,
                                 show_beams=beams, beam_deg=half)

    # ------------------------------------------------------------------
    # Points tab
    # ------------------------------------------------------------------

    def _on_points_edited(self, pts: list) -> None:
        """The table changed (typing, Offset…, a pick, + or −)."""
        self._points = [(float(p[0]), float(p[1])) for p in pts]
        self._points_changed()

    def _commit_points(self, pts: list) -> None:
        """Changed points from the arena (a drag): into the table, then as
        for an edit there."""
        self._points = [(float(p[0]), float(p[1])) for p in pts]
        self._table.set_points(self._points)
        self._points_changed()

    def _points_changed(self) -> None:
        self._set_points_status("")
        self._update_points_state()
        self._fit_view()
        self._schedule_quality()

    def _on_selection_changed(self, _row: int) -> None:
        if self._tabs.currentIndex() == TAB_POINTS:
            self._refresh_arena()
        self._schedule_quality()

    def _check_points(self, pts) -> points.PointCheck:
        """``points.check_points`` for ``pts`` in the current arena (cheap)."""
        n = len(pts)
        if self._geometry_error:
            return points.PointCheck([f"No calibration possible: {self._geometry_error}"], [],
                                     [([], []) for _ in range(n)])
        try:
            return points.check_points(pts, self._coords, inside=self._inside_fn(),
                                       hears=self._hears_fn())
        except ValueError as exc:
            return points.PointCheck([str(exc)], [], [([], []) for _ in range(n)])

    def _score(self, pts) -> float:
        if self._geometry_error or len(pts) < points.MIN_POINTS:
            return math.inf
        try:
            return points.arena_score(pts, self._coords)
        except ValueError:
            return math.inf

    def _update_points_state(self) -> None:
        """Checks, score, per-receiver errors and buttons for the table's
        points, and the arena (cheap enough for every committed edit)."""
        pts = self._points
        check = self._check = self._check_points(pts)

        self._table.clear_row_status()
        for k, (color, tip) in enumerate(_point_marks(check, len(pts))):
            if color is not None:
                self._table.set_row_status(k, color, tip)

        lines = [_rich(p, W.RED) for p in check.problems]
        lines += [_rich(w, W.AMBER) for w in check.warnings]
        if self._saved_error:
            lines.insert(0, _rich(f"The saved points are unreadable: {self._saved_error}", W.RED))
        if not lines and pts:
            lines = [_rich("No problems: these points can calibrate every receiver.", W.GREEN)]
        if check.problems and not self._dirty():
            lines.append(_rich("These saved points can't be calibrated on: change them (Find "
                               "best points suggests good ones) and save them.", W.RED))
        self._issues_label.setText("<br>".join(lines))
        self._issues_label.setToolTip("\n".join(check.problems + check.warnings))

        score, saved_score = self._score(pts), self._score(self._saved)
        text = f"Arena score <b>{_fmt_score(score)}</b>"
        if self._dirty() and self._saved:
            text += f" · saved points {_fmt_score(saved_score)}"
        elif self._saved:
            text += " (saved)"
        self._score_label.setText(text + " · lower is better")

        self._fill_errors_table()
        dirty = self._dirty()
        self._save_btn.setEnabled(dirty)
        self._revert_btn.setEnabled(dirty and bool(self._saved))
        self._save_btn.setToolTip(
            "Write the points to config.toml, with every receiver's distance to them "
            "(cal_distances)" if not check.problems else
            "These points can't be saved: " + "; ".join(check.problems))
        self._update_run_ui()
        if self._tabs.currentIndex() == TAB_POINTS:
            self._refresh_arena()

    def _fill_errors_table(self) -> None:
        def errors(pts):
            if self._geometry_error or not pts:
                return [(math.inf, math.inf)] * len(self._rx_ids)
            try:
                return points.receiver_errors(pts, self._coords)
            except ValueError:
                return [(math.inf, math.inf)] * len(self._rx_ids)

        now, saved = errors(self._points), errors(self._saved)
        t = self._errors_table
        t.setRowCount(len(now))
        for row, ((s, i), (s0, i0)) in enumerate(zip(now, saved)):
            tip = (f"Saved points: slope ±{100 * s0:.1f} %, intercept ±{i0:.1f} cm"
                   if math.isfinite(s0) else "Saved points: no slope can be fitted")
            _set_cell(t, row, 0, f"R{self._rx_ids[row] + 1}", W.CYAN, center=True)
            if math.isfinite(s):
                _set_cell(t, row, 1, f"{100 * s:.1f} %", tip=tip)
                _set_cell(t, row, 2, f"{i:.1f} cm", tip=tip)
            else:
                _set_cell(t, row, 1, "no slope", W.RED, tip)
                _set_cell(t, row, 2, "—", W.RED, tip)

    def _set_points_status(self, text: str, color: str = W.DIM) -> None:
        _set_status(self._points_status, text, color)

    def _save_points(self) -> bool:
        """Write the table's points (and every receiver's ``cal_distances``) to
        config.toml.  Refused while a calibration run uses the saved points,
        while a measurement runs (the save reloads the position library it is
        using) or when the points have problems."""
        if self._session is not None:
            self._set_points_status("Not saved: finish or cancel the calibration run first (it "
                                    "collects at the saved points).", W.RED)
            return False
        if not self._dirty():
            self._set_points_status("Nothing to save: these are the saved points.")
            return True
        if self._measuring():
            self._set_points_status("Not saved yet: wait for the measurement to finish, or "
                                    "cancel it (saving reloads the position library it uses).",
                                    W.RED)
            return False
        check = self._check_points(self._points)
        if check.problems:
            self._set_points_status("Can't save these points: " + "; ".join(check.problems) + ".",
                                    W.RED)
            return False
        try:
            self._settings.set_cal_points(self._points)
        except ValueError as exc:
            self._set_points_status(f"Could not save the points: {exc}", W.RED)
            return False
        except OSError as exc:
            # The settings changed in memory before the write failed: back to the file
            self._set_points_status(f"Could not save the points: {exc}", W.RED)
            try:
                self._settings.reload_config()
            except (OSError, ValueError):
                log.exception("Could not reload the settings")
            return False
        self._saved = list(self._points)
        self._notify_config_changed()
        names = _join(f"P{k + 1}" for k in range(len(self._points)))
        self._set_points_status(f"Saved {names} to config.toml, with every receiver's distance "
                                f"to them. Calibrate (Run tab) to use them.", W.GREEN)
        return True

    def _revert_points(self) -> None:
        self._points = list(self._saved)
        self._table.set_points(self._points)
        self._points_changed()
        self._set_points_status("Back to the saved points.")

    def _new_point(self, current) -> tuple[float, float]:
        """Where "+" puts a new point: the best spot given the others."""
        pts = [(float(p[0]), float(p[1])) for p in current]
        if not self._geometry_error:
            try:
                xs, ys, Z = points.quality_map(self._coords, fixed=pts,
                                               step=max(self._quality_step(), 5.0),
                                               inside=self._inside_fn(), hears=self._hears_fn())
                finite = np.isfinite(Z)
                if finite.any():
                    j, i = np.unravel_index(np.argmin(np.where(finite, Z, np.inf)), Z.shape)
                    return round(float(xs[i]), 1), round(float(ys[j]), 1)
            except ValueError:
                pass
        x, y = self._default_check_point()
        return x, round(y + 20.0 * len(pts), 1)

    def _on_model_changed(self, _index: int = 0) -> None:
        self._draw_receivers()
        self._invalidate_search()
        self._update_points_state()
        self._schedule_quality()
        self._view.draw_idle()

    def _on_beams_toggled(self, _on: bool) -> None:
        self._draw_receivers()
        self._view.draw_idle()

    # ------------------------------------------------------------------
    # Green shading (worker, debounced)
    # ------------------------------------------------------------------

    def _shading_target(self) -> tuple[list, str, str, str]:
        """``(fixed points, target, title, description)`` of the shading for
        the current selection: where the selected point should go given the
        others; with none selected, where to add the next point; with all
        ``MAX_POINTS`` and none selected, where any point does well."""
        pts = self._points
        n = len(pts)
        names = [f"P{k + 1}" for k in range(n)]
        sel = self._table.selected()
        if 0 <= sel < n:
            others = [nm for k, nm in enumerate(names) if k != sel]
            fixed = [p for k, p in enumerate(pts) if k != sel]
            text = f"best spots for {names[sel]}" + (f", given {_join(others)}" if others else "")
            return fixed, names[sel], f"Green = {text}", text
        if n < points.MAX_POINTS:
            target = f"P{n + 1}"
            text = f"best spots to add {target}" + (f", given {_join(names)}" if names else "")
            return list(pts), target, f"Green = {text}", text
        return ([], "a point", "Green = best spots for any point",
                "best spots for any calibration point, each paired with its best partner")

    def _schedule_quality(self) -> None:
        """Recompute the shading shortly (debounced); results computed for
        older inputs are dropped (generation counter)."""
        self._quality_gen += 1
        if self._shown:
            self._quality_timer.start()

    def _start_quality(self) -> None:
        if not self._shown:
            return
        if self._quality_worker is not None:
            self._quality_pending = True          # one at a time: the newest inputs go next
            return
        if self._geometry_error:
            self._quality, self._quality_error = None, self._geometry_error
            self._show_quality()
            return
        fixed, target, title, description = self._shading_target()
        gen = self._quality_gen
        job = _QualityJob(gen, self._coords.copy(), list(fixed), self._quality_step(),
                          self._inside_fn(), self._hears_fn(), target, title, description)
        self._quality_worker = W.run_in_thread(
            _compute_quality, job, on_result=self._on_quality,
            on_error=lambda message: self._on_quality_error(gen, message),
            on_finished=self._on_quality_finished)

    def _on_quality(self, result: _QualityMap) -> None:
        if result.job.generation != self._quality_gen or not self._shown:
            return                                # computed for points that have changed since
        self._quality, self._quality_error = result, ""
        self._show_quality()
        self._view.draw_idle()

    def _on_quality_error(self, gen: int, message: str) -> None:
        if gen != self._quality_gen or not self._shown:
            return
        self._quality, self._quality_error = None, message
        self._show_quality()

    def _on_quality_finished(self) -> None:
        self._quality_worker = None
        if self._quality_pending:
            self._quality_pending = False
            self._start_quality()

    def _show_quality(self) -> None:
        """Put the shading on the arena (Points tab) and explain it."""
        q = self._quality
        on_points = self._tabs.currentIndex() == TAB_POINTS
        if q is None:
            if self._heat_shown:
                self._view.clear_heatmap()
                self._heat_shown = False
            why = self._quality_error or "computing…"
            self._shade_label.setText(_rich(f"No shading: {why}", W.AMBER if self._quality_error
                                            else W.DIM))
            if on_points:
                self._refresh_arena()
            return
        if on_points:
            self._view.set_quality(q.xs, q.ys, q.G, lower_is_better=False, vmin=0.0, vmax=1.0,
                                   legend_label=f"Best spot for {q.job.target}",
                                   fmt=lambda g: f"goodness {g:.2f}")
            self._heat_shown = True
            self._refresh_arena()
        model = self._model()
        lines = [f"<b>Green = {html.escape(q.job.description)}</b> ({html.escape(model.name)} "
                 f"receivers). The brightest green has the lowest calibration error on the map; "
                 f"the green fades as the error grows (the read-out under the pointer compares "
                 f"it with the best) and is gone at twice that error, or where no point may go "
                 f"(outside the arena, less than {points.DEFAULT_MARGIN_CM:g} cm inside the "
                 f"receivers' box, over {points.MAX_RANGE_CM:g} cm from a receiver, or within "
                 f"{points.MIN_SEPARATION_CM:g} cm of another point)."]
        if not math.isfinite(q.best):
            lines.append(_rich("No spot on the map can be used.", W.AMBER))
        elif q.floor > 0:
            lines.append(_rich("No spot is heard by every receiver under this model: the shading "
                               "ranks the spots that the most receivers hear.", W.AMBER))
        lines.append(f"Select a point (in the table, or press its marker) to shade where it "
                     f"should go; click an empty spot of the arena to shade where to add one "
                     f"(with {points.MAX_POINTS} points: where any point does well).")
        self._shade_label.setText("<br>".join(lines))

    def _format_readout(self, x: float, y: float) -> str:
        """The hover read-out: coordinates, and on the Points tab what the
        shading says about that spot."""
        text = f"x {x:.1f}  y {y:.1f} cm"
        q = self._quality
        if self._tabs.currentIndex() != TAB_POINTS or q is None or not self._heat_shown:
            return text
        detail = q.describe(q.value(x, y))
        return f"{text}  |  {detail}" if detail else text

    # ------------------------------------------------------------------
    # Best-point search (worker)
    # ------------------------------------------------------------------

    def _search_key(self) -> tuple:
        return (self._coords.tobytes(), tuple(self._facings), self._model())

    def _invalidate_search(self) -> None:
        self._search = None
        self._preview = None
        self._search_gen += 1
        for k in range(self._count_combo.count()):
            self._count_combo.setItemText(k, f"{self._count_combo.itemData(k)} points")
        self._best_label.setVisible(False)
        self._update_search_buttons()

    def _update_search_buttons(self) -> None:
        searching = self._search_worker is not None
        self._find_btn.setEnabled(not searching and not self._geometry_error)
        self._find_btn.setText("Searching…" if searching else "Find best points")
        self._use_btn.setEnabled(self._preview is not None)
        self._discard_btn.setEnabled(self._preview is not None)

    def _find_best(self) -> None:
        if self._search_worker is not None:
            return
        if self._geometry_error:
            self._set_points_status(f"No search possible: {self._geometry_error}.", W.RED)
            return
        key = self._search_key()
        if self._search is not None and self._search["key"] == key:
            self._show_preview()
            return
        self._search_gen += 1
        gen = self._search_gen
        self._search_worker = W.run_in_thread(
            _search_sets, self._coords.copy(), self._inside_fn(), self._hears_fn(),
            on_result=lambda sets: self._on_search_result(gen, key, sets),
            on_error=lambda message: self._on_search_error(gen, message),
            on_finished=self._on_search_finished)
        self._update_search_buttons()

    def _on_search_result(self, gen: int, key: tuple, sets: dict) -> None:
        if gen != self._search_gen:
            return                                # the arena or the model changed meanwhile
        self._search = {"key": key, "sets": sets}
        for k in range(self._count_combo.count()):
            n = self._count_combo.itemData(k)
            text = f"{n} points"
            if n in sets:
                text += f" (score {_fmt_score(self._score(sets[n]))})"
            self._count_combo.setItemText(k, text)
        self._show_preview()

    def _on_search_error(self, gen: int, message: str) -> None:
        if gen == self._search_gen:
            self._set_points_status(f"No set found: {message}", W.RED)

    def _on_search_finished(self) -> None:
        self._search_worker = None
        self._update_search_buttons()

    def _on_count_changed(self, _index: int) -> None:
        if self._search is not None and self._preview is not None:
            self._show_preview()

    def _show_preview(self) -> None:
        n = self._count_combo.currentData()
        sets = self._search["sets"] if self._search is not None else {}
        if n not in sets:
            self._preview = None
            self._best_label.setText(_rich(f"No set of {n} points fits this arena.", W.AMBER))
        else:
            self._preview = np.asarray(sets[n], dtype=float)
            self._best_label.setText(
                f"Best {n} points (gold stars B1…B{n}): arena score "
                f"<b>{_fmt_score(self._score(self._preview))}</b>, yours "
                f"{_fmt_score(self._score(self._points))}. Use them, then Save points.")
        self._best_label.setVisible(True)
        self._update_search_buttons()
        if self._tabs.currentIndex() == TAB_POINTS:
            self._refresh_arena()

    def _use_preview(self) -> None:
        if self._preview is None:
            return
        self._points = [(float(x), float(y)) for x, y in self._preview]
        self._preview = None
        self._best_label.setVisible(False)
        self._table.set_points(self._points)
        self._table.select(-1)
        self._update_search_buttons()
        self._points_changed()
        self._set_points_status("The best points are in the table: press Save points to keep "
                                "them.", W.GOLD)

    def _discard_preview(self) -> None:
        self._preview = None
        self._best_label.setVisible(False)
        self._update_search_buttons()
        self._refresh_arena()

    # ------------------------------------------------------------------
    # Arena drawing and interaction
    # ------------------------------------------------------------------

    def _refresh_arena(self) -> None:
        """Draw the point groups of the current tab (and clear the others)."""
        tab = self._tabs.currentIndex()
        shown: set = set()
        if tab == TAB_POINTS:
            shown = self._draw_points_view()
        elif tab == TAB_ORIGIN:
            shown = self._draw_origin_view()
        elif tab == TAB_RUN:
            shown = self._draw_run_view()
        else:
            shown = self._draw_check_view()
        for group in _GROUPS:
            if group not in shown:
                self._view.clear_points(group)
        if tab != TAB_CHECK or self._check_result is None:
            self._view.clear_shapes("check_error")
        self._view.draw_idle()

    def _pick_title(self) -> str:
        if self._pick_source == "points":
            row = self._table.selected()
            return f"Click the arena to place P{row + 1}" if row >= 0 else "Click the arena"
        if self._pick_source == "origin":
            return "Click the arena at the new (0, 0)"
        if self._pick_source == "check":
            return "Click the arena at the known point"
        return ""

    def _draw_points_view(self) -> set:
        v = self._view
        pts = self._points
        shown = set()
        if pts:
            colors = [color or W.GREEN for color, _ in _point_marks(self._check, len(pts))]
            v.set_points("cal", pts, color=colors, labels="P", legend="Calibration points",
                         draggable=True, edgecolor="black", zorder=9.0)
            shown.add("cal")
        sel = self._table.selected()
        if 0 <= sel < len(pts):
            v.set_points("selected", [pts[sel]], color="none", edgecolor="white", marker="o",
                         size=380, zorder=8.5)
            shown.add("selected")
        if self._dirty() and self._saved:
            v.set_points("saved", self._saved, color="none", edgecolor=W.DIM, marker="D",
                         size=110, legend="Saved points", zorder=7.5)
            shown.add("saved")
        if self._preview is not None:
            v.set_points("best", self._preview, color=W.GOLD, marker="*", size=260,
                         labels=[f"B{k + 1}" for k in range(len(self._preview))],
                         legend="Best points (preview)", zorder=8.0)
            shown.add("best")
        q = self._quality
        title = self._pick_title() or (q.job.title if q is not None and self._heat_shown
                                       else "Calibration points")
        v.set_title(title)
        return shown

    def _draw_origin_view(self) -> set:
        v = self._view
        shown = set()
        if self._saved:
            v.set_points("cal", self._saved, color=W.DIM, labels="P", draggable=False, zorder=9.0)
            shown.add("cal")
        c = self._origin_candidate
        if c is not None:
            v.set_points("origin_preview", [c], color=W.BLUE, marker="P", size=200,
                         labels=["new (0, 0)"], zorder=9.5)
            shown.add("origin_preview")
            title = f"New (0, 0) at {_fmt_point(c)}: everything moves by ({-c[0]:.1f}, {-c[1]:.1f})"
        else:
            title = "Calibrate (0, 0)"
        v.set_title(self._pick_title() or title)
        return shown

    def _draw_run_view(self) -> set:
        v = self._view
        s = self._session
        pts = s.points if s is not None else self._saved
        shown = set()
        if not pts:
            v.set_title("Calibration run")
            return shown
        current = self._run_combo.currentIndex() if s is not None else -1
        if s is None:
            marks = _point_marks(self._check_points(self._saved), len(pts))
            colors = [color or W.GREEN for color, _ in marks]
        else:
            colors = [W.GOLD if k == current else W.GREEN if s.done[k] else W.DIM
                      for k in range(len(pts))]
        v.set_points("cal", pts, color=colors, labels="P", draggable=False, edgecolor="black",
                     zorder=9.0)
        shown.add("cal")
        if s is not None and 0 <= current < len(pts):
            v.set_points("target", [pts[current]], color="none", edgecolor=W.GOLD, marker="o",
                         size=900, zorder=8.5)
            shown.add("target")
            p = pts[current]
            if s.collecting >= 0:
                title = f"Collecting at P{current + 1} {_fmt_point(p)}: keep the transmitter still"
            elif s.done[current]:
                title = f"P{current + 1} {_fmt_point(p)}: collected"
            else:
                title = f"Place the transmitter at P{current + 1} {_fmt_point(p)}"
        else:
            title = "Calibration run: the saved points"
        v.set_title(title)
        return shown

    def _draw_check_view(self) -> set:
        v = self._view
        shown = set()
        if self._saved:
            v.set_points("cal", self._saved, color="#666666", labels="P", draggable=False,
                         size=60, zorder=6.0)
            shown.add("cal")
        # Legend entries, not labels: a good fix sits on top of the known point
        v.set_points("check_true", [self._check_point], color=W.ORANGE, marker="X", size=170,
                     legend="Known point (drag it)", draggable=True, zorder=9.5)
        shown.add("check_true")
        r, chk = self._check_measured, self._check_result
        title = "Accuracy check: the known point"
        if r is not None and chk is not None:
            if r.lm_fixes:
                v.set_points("check_fixes", r.lm_fixes, color="#00FFFF66", marker="o", size=12,
                             legend="LM fixes", zorder=7.0)
                shown.add("check_fixes")
            if r.lm_mean is not None:
                v.set_points("check_lm", [r.lm_mean], color="none", edgecolor=W.CYAN, marker="o",
                             size=150, legend="Measured (LM mean)", zorder=9.0)
                shown.add("check_lm")
                v.set_shapes("check_error", [[self._check_point, r.lm_mean]], color=W.CYAN,
                             closed=False, linestyle="--", linewidth=1.2)
            if r.cep_mean is not None:
                v.set_points("check_cep", [r.cep_mean], color="none", edgecolor=W.MAGENTA,
                             marker="s", size=130, legend="Measured (CEP mean)", zorder=8.8)
                shown.add("check_cep")
            if chk.lm_error is not None:
                title = f"Accuracy check: measured {chk.lm_error:.1f} cm from the known point"
        v.set_title(self._pick_title() or title)
        return shown

    def _on_arena_clicked(self, x: float, y: float, button: int) -> None:
        if button != 1:
            return
        source = self._pick_source
        if source == "points":
            self._table.apply_pick(x, y)
        elif source == "origin":
            self._origin.apply_pick(x, y)
        elif source == "check":
            self._check_table.apply_pick(x, y)
        elif self._tabs.currentIndex() == TAB_POINTS and self._table.selected() >= 0:
            self._table.select(-1)                # shade where the next point goes

    def _on_point_pressed(self, group: str, index: int, _button: int) -> None:
        if group == "cal" and self._tabs.currentIndex() == TAB_POINTS:
            if self._table.selected() != index:
                self._table.select(index)         # shade where this point should go

    def _on_point_dragged(self, group: str, index: int, x: float, y: float) -> None:
        # Shown live, committed on release (the shading for the dragged point
        # does not depend on where it is, so nothing is recomputed meanwhile)
        if group == "cal" and 0 <= index < len(self._points):
            self._table.set_point(index, x, y)
            if self._table.selected() == index:
                self._view.move_point("selected", 0, x, y)
            q = self._quality
            detail = q.describe(q.value(x, y)) if q is not None and self._heat_shown else ""
            self._view.set_readout(f"P{index + 1}: x {x:.1f}  y {y:.1f} cm"
                                   + (f"  |  {detail}" if detail else ""))
        elif group == "check_true":
            self._check_table.set_point(0, x, y)

    def _on_drag_finished(self, group: str, index: int, x: float, y: float) -> None:
        if group == "cal" and 0 <= index < len(self._points):
            pts = list(self._points)
            pts[index] = (x, y)
            self._commit_points(pts)
        elif group == "check_true":
            self._set_check_point((x, y))

    def _on_pick_mode(self, source: str, on: bool) -> None:
        """One "Pick on arena" mode at a time; while one is on, every arena
        press is a click for it (no picking or dragging of points)."""
        if on:
            for name, setter in (("points", self._table.set_pick_mode),
                                 ("origin", self._origin.set_pick_mode),
                                 ("check", self._check_table.set_pick_mode)):
                if name != source:
                    setter(False)
            self._pick_source = source
        elif self._pick_source == source:
            self._pick_source = None
        picking = self._pick_source is not None
        self._interactor.pick_enabled = not picking
        self._canvas.setCursor(Qt.CursorShape.CrossCursor if picking
                               else Qt.CursorShape.ArrowCursor)
        self._refresh_arena()

    def _leave_pick_modes(self) -> None:
        self._table.set_pick_mode(False)
        self._origin.set_pick_mode(False)
        self._check_table.set_pick_mode(False)

    # ------------------------------------------------------------------
    # Origin tab
    # ------------------------------------------------------------------

    def _measure_origin(self, cancel=None, on_progress=None):
        """``OriginTool``'s measurement, on its worker thread: the mean of
        ``ORIGIN_SAMPLES`` fixes.  Refused (the tool shows the reason) while a
        collection or another measurement uses the transmitter."""
        if self._cal.is_running:
            raise RuntimeError("a calibration collection is running: stop it first")
        if self._check_worker is not None:
            raise RuntimeError("the accuracy check is measuring")
        if not self._link_up():
            raise RuntimeError(self._link_down_text())
        return measure.measure_position(self._client, self._position_lib, ORIGIN_SAMPLES,
                                        cancel=cancel, on_progress=on_progress)

    def _on_origin_preview(self, candidate) -> None:
        self._origin_candidate = candidate
        if self._tabs.currentIndex() == TAB_ORIGIN:
            self._refresh_arena()

    def _on_origin_requested(self, x: float, y: float) -> None:
        """Make the current-frame point (x, y) the new (0, 0)."""
        if self._session is not None:
            self._refuse_origin("finish or cancel the calibration run first.")
            return
        if self._measuring():
            self._refuse_origin("wait for the accuracy check to finish.")
            return
        try:
            self._settings.shift_origin(x, y)
        except Exception as exc:                          # nothing may escape a slot
            if not isinstance(exc, (ValueError, OSError)):        # the documented failures
                log.exception("Moving (0, 0) failed")
            self._refuse_origin(str(exc))
            return
        # The panel's own unsaved geometry is in the old frame too
        self._points = [(px - x, py - y) for px, py in self._points]
        self._check_point = (self._check_point[0] - x, self._check_point[1] - y)
        self._check_table.set_points([self._check_point], labels=["Known"])
        self._clear_check_result()
        self._notify_config_changed()
        self._fit_view()
        self._origin.set_status(f"(0, 0) is now the old {_fmt_point((x, y))}: receivers, "
                                f"calibration points and barriers moved by "
                                f"({-x:.1f}, {-y:.1f}) cm.", W.GREEN)
        self._refresh_arena()

    def _refuse_origin(self, why: str) -> None:
        """Say why (0, 0) was not moved; the red status makes ``OriginTool``
        keep the point for another try."""
        self._origin.set_status(f"(0, 0) not moved: {why}", W.RED)

    def _on_measuring_changed(self, _measuring: bool = False) -> None:
        self._update_run_ui()
        self._update_check_ui()

    def _measuring(self) -> bool:
        return self._check_worker is not None or self._origin.is_measuring()

    # ------------------------------------------------------------------
    # Run tab
    # ------------------------------------------------------------------

    def _set_run_status(self, text: str, color: str = W.DIM) -> None:
        _set_status(self._run_status, text, color)

    def _start_session(self) -> None:
        """Start a calibration run at the saved points (after offering to
        save edited ones)."""
        if self._session is not None:
            return
        if self._geometry_error:
            self._set_run_status(f"Can't calibrate: {self._geometry_error}.", W.RED)
            return
        # Before offering to save edited points: "Save and start" must not save
        # and then refuse to start
        refusal = self._collect_refusal()
        if refusal:
            self._set_run_status(refusal, W.RED)
            return
        if self._dirty():
            check = self._check_points(self._points)
            if check.problems:
                self._set_run_status("Can't calibrate: the edited points are not saved, and "
                                     "can't be: " + "; ".join(check.problems) + ".", W.RED)
                return
            if not W.confirm(self, "Save the calibration points?",
                             "The calibration collects at the saved points, but the points "
                             "on the Points tab were edited. Save them and start?",
                             ok_text="Save and start"):
                return
            if not self._save_points():
                self._set_run_status("Not started: the points could not be saved (see the "
                                     "Points tab).", W.RED)
                return
        if self._saved_error or len(self._saved) < points.MIN_POINTS:
            self._set_run_status("Can't calibrate: config.toml holds no usable calibration "
                                 "points. Choose them on the Points tab.", W.RED)
            return
        check = self._check_points(self._saved)
        if check.problems:
            self._set_run_status("Can't calibrate on the saved points: "
                                 + "; ".join(check.problems) + ". Change them on the Points tab "
                                 "(Find best points suggests good ones) and save them.", W.RED)
            return
        try:
            reads = max(1, int(self._settings.calibration_reads))
        except (TypeError, ValueError):
            reads = 5
        if self._cal.config_path != self._settings.config_path:
            self._cal.config_path = self._settings.config_path
        self._session_token += 1
        n = min(len(self._saved), self._cal.MAX_POINTS)
        self._session = _Session(points=list(self._saved[:n]), coords=self._coords.copy(),
                                 reads=reads, token=self._session_token, done=[False] * n)
        self._results = None
        self._show_results_frame(False)
        for run in range(1, n + 1):
            self._cal.clear_run_data(run)
        self._fill_run_combo(0)
        warn = (" Warnings: " + "; ".join(check.warnings) + ".") if check.warnings else ""
        self._set_run_status(f"Started: {n} points, {reads} reads of the modal tick per "
                             f"receiver at each.{warn}", W.AMBER if warn else W.DIM)
        self._update_run_ui()
        self._draw_histograms()

    def _collect_refusal(self) -> str:
        """Why a collection can't start now ("" if it can)."""
        if not self._link_up():
            return self._link_down_text("Can't collect")
        if self._measuring():
            return "Can't collect while a measurement is running: wait for it or cancel it."
        if self._cal.is_running:
            return "A collection is already running."
        return ""

    def _fill_run_combo(self, select: int | None = None) -> None:
        s = self._session
        combo = self._run_combo
        index = combo.currentIndex() if select is None else select
        combo.blockSignals(True)
        combo.clear()
        if s is not None:
            for k, p in enumerate(s.points):
                mark = "  ✓" if s.done[k] else ""
                combo.addItem(f"P{k + 1} {_fmt_point(p)}{mark}")
            combo.setCurrentIndex(min(max(index, 0), len(s.points) - 1))
        combo.blockSignals(False)

    def _on_run_point_changed(self, _index: int) -> None:
        self._update_run_ui()
        self._draw_histograms()

    def _collect(self) -> None:
        """Collect at the point chosen in the combo box."""
        s = self._session
        if s is None or s.collecting >= 0:
            return
        refusal = self._collect_refusal()
        if refusal:
            self._set_run_status(refusal, W.RED)
            return
        k = self._run_combo.currentIndex()
        if not 0 <= k < len(s.points):
            return
        run, token = k + 1, s.token
        try:
            self._cal.clear_run_data(run)
            self._cal.start_run(
                run, s.reads,
                on_reading=lambda r, n: self._sig_reading.emit(token, r, n),
                on_complete=lambda r: self._sig_complete.emit(token, r),
                on_error=lambda r, msg: self._sig_failed.emit(token, r, str(msg)),
                on_tick=lambda r, ticks: self._sig_ticks.emit(token, r, list(ticks)))
        except (RuntimeError, ValueError) as exc:
            self._set_run_status(f"Could not start collecting at P{run}: {exc}", W.RED)
            return
        s.collecting, s.latest, s.reads_seen = k, None, 0
        s.done[k] = False
        self._results = None                      # a new collection invalidates the last fit
        self._show_results_frame(False)
        self._fill_run_combo()
        self._set_run_status(f"Collecting at P{run}…")
        self._update_run_ui()
        self._draw_histograms()

    def _stop_collect(self, note: str = "") -> None:
        s = self._session
        if s is None or s.collecting < 0:
            return
        self._cal.stop_run()
        k, s.collecting = s.collecting, -1
        self._set_run_status(note or f"Stopped collecting at P{k + 1}: press Collect P{k + 1} to "
                                     f"collect it again.", W.AMBER)
        self._update_run_ui()

    def _cancel_session(self) -> None:
        """Forget the calibration run (and its unsaved results)."""
        s = self._session
        if s is not None and s.collecting >= 0:
            self._cal.stop_run()
        saved = self._results is not None and self._results.saved
        self._session = None
        if not saved:
            self._results = None
            self._show_results_frame(False)
            self._set_run_status("Calibration cancelled: config.toml keeps its offsets.")
        self._hist.clear("Tick histograms")
        self._update_run_ui()

    def _validate_session(self) -> None:
        """End a run whose points or receivers changed under it (in another
        panel, while this one was hidden)."""
        s = self._session
        if s is None:
            return
        same_points = len(self._saved) == len(s.points) and _same_points(s.points, self._saved)
        same_receivers = s.coords.shape == self._coords.shape and np.array_equal(s.coords,
                                                                                  self._coords)
        if same_points and same_receivers and not self._geometry_error:
            return
        if s.collecting >= 0:
            self._cal.stop_run()
        self._session = None
        self._results = None
        self._show_results_frame(False)
        what = "calibration points" if not same_points else "receivers"
        self._set_run_status(f"The calibration run was cancelled: the {what} changed.", W.AMBER)
        self._update_run_ui()

    def _session_for(self, token: int, run: int):
        """The active session if ``token``/``run`` are its collection's, else None."""
        s = self._session
        if s is None or token != s.token or s.collecting != run - 1:
            return None
        return s

    def _on_run_reading(self, token: int, run: int, reads: int) -> None:
        s = self._session_for(token, run)
        if s is None:
            return
        s.reads_seen = reads
        self._update_run_progress()
        if not self._hist_timer.isActive():
            self._hist_timer.start()

    def _on_run_ticks(self, token: int, run: int, ticks) -> None:
        s = self._session_for(token, run)
        if s is not None:
            s.latest = list(ticks)

    def _on_run_complete(self, token: int, run: int) -> None:
        s = self._session_for(token, run)
        if s is None:
            return
        s.collecting = -1
        s.done[run - 1] = True
        if all(s.done):
            self._fill_run_combo()
            self._compute_results(run)
        else:
            nxt = next(k for k, d in enumerate(s.done) if not d)
            self._fill_run_combo(nxt)
            self._set_run_status(f"P{run} done ({s.reads_seen} readings). Move the transmitter "
                                 f"to P{nxt + 1}.", W.GREEN)
        self._update_run_ui()
        self._draw_histograms()

    def _on_run_failed(self, token: int, run: int, message: str) -> None:
        s = self._session_for(token, run)
        if s is None:
            return
        s.collecting = -1
        link = "" if self._link_up() else " " + self._link_down_text()
        self._set_run_status(f"Collecting at P{run} failed: {message}.{link} Press Collect P{run} "
                             f"to try again.", W.RED)
        self._update_run_ui()

    def _compute_results(self, run: int) -> None:
        """Fit every receiver's line once every point is collected (the last
        one, ``run``, just now)."""
        s = self._session
        try:
            offsets = self._cal.calculate_offsets()
        except Exception as exc:          # it reads config.toml: report, never crash
            log.exception("calculate_offsets failed")
            self._set_run_status(f"The offsets could not be computed: {exc}", W.RED)
            return
        current = {}
        for rid in range(self._cal.RECEIVER_COUNT):
            o = self._settings.get_receiver_offset(rid) or {}
            try:
                current[rid] = (float(o["slope"]), float(o["intercept"]))
            except (KeyError, TypeError, ValueError):
                current[rid] = None
        self._results = _Results(points=list(s.points), offsets=offsets,
                                 fit=dict(self._cal.last_fit), skipped=dict(self._cal.last_skipped),
                                 modal=[self._cal.get_modal_ticks(k + 1)
                                        for k in range(len(s.points))],
                                 current=current)
        self._fill_results()
        self._set_run_status(f"P{run} done ({s.reads_seen} readings): every receiver's line "
                             f"is fitted, see the results under the arena.", W.GREEN)

    def _result_notes(self, rid: int) -> tuple[str, str]:
        """``(note, colour)`` for one receiver's row of the results."""
        r = self._results
        new = r.offsets.get(rid)
        if new is None:
            return f"keeps its offsets: {r.skipped.get(rid, 'not calibrated')}", W.AMBER
        notes, color = [], W.GREEN
        fit = r.fit.get(rid, {})
        used = [v for v in fit.get("residuals_cm", []) if v is not None]
        if new["slope"] <= 0:
            notes.append("slope ≤ 0 is impossible (ticks grow with distance): not saved")
            color = W.RED
        if len(used) > 2 and fit.get("residual_rms_cm", 0.0) > RESIDUAL_BAD_CM:
            notes.append("large residuals: a point was read wrong (echo, or the transmitter "
                         "off the point)? Collect it again")
            color = W.RED
        cur = r.current.get(rid)
        if cur is not None and cur[0] > 0 and new["slope"] > 0:
            change = new["slope"] / cur[0] - 1.0
            if abs(change) > SLOPE_CHANGE_WARN:
                notes.append(f"slope {change:+.0%} against now")
                color = W.AMBER if color == W.GREEN else color
        return "; ".join(notes) or "OK", color

    def _fill_results(self) -> None:
        r = self._results
        t = self._results_table
        n = len(r.points)
        names = " / ".join(f"P{k + 1}" for k in range(n))
        heads = ["", "Slope\nnew", "Slope\nnow", "Intercept cm\nnew", "Intercept cm\nnow",
                 f"Modal ticks\n{names}", f"Residuals cm\n{names}", "RMS\ncm", "Note"]
        t.setHorizontalHeaderLabels(heads)
        t.setRowCount(self._cal.RECEIVER_COUNT)
        calibrated, kept, bad = [], [], []
        for rid in range(self._cal.RECEIVER_COUNT):
            note, color = self._result_notes(rid)
            new, cur, fit = r.offsets.get(rid), r.current.get(rid), r.fit.get(rid)
            ticks = [r.modal[k][rid] for k in range(n)]
            tick_text = " / ".join("—" if v is None else "no echo" if v == 0 else f"{v:.0f}"
                                   for v in ticks)
            text_color = "white" if new is not None else W.AMBER
            _set_cell(t, rid, 0, f"R{rid + 1}", W.CYAN, center=True)
            _set_cell(t, rid, 1, f"{new['slope']:.5f}" if new else "—", text_color)
            _set_cell(t, rid, 2, f"{cur[0]:.5f}" if cur else "—", W.DIM)
            _set_cell(t, rid, 3, f"{new['intercept']:.2f}" if new else "—", text_color)
            _set_cell(t, rid, 4, f"{cur[1]:.2f}" if cur else "—", W.DIM)
            _set_cell(t, rid, 5, tick_text, text_color, center=True)
            if fit:
                res = " / ".join("—" if v is None else _fmt_signed(v) for v in fit["residuals_cm"])
                used = sum(v is not None for v in fit["residuals_cm"])
                rms = f"{fit['residual_rms_cm']:.2f}" if used > 2 else "—"
            else:
                res, rms = "—", "—"
            _set_cell(t, rid, 6, res, text_color, center=True)
            _set_cell(t, rid, 7, rms, text_color)
            _set_cell(t, rid, 8, note, color, tip=note)
            t.item(rid, 8).setTextAlignment(Qt.AlignmentFlag.AlignLeft
                                            | Qt.AlignmentFlag.AlignVCenter)
            if new is None:
                kept.append(f"R{rid + 1}")
            elif new["slope"] <= 0:
                bad.append(f"R{rid + 1}")
            else:
                calibrated.append(f"R{rid + 1}")

        lines = []
        if calibrated:
            lines.append(_rich(f"Calibrated {_join(calibrated)} at {n} points.", W.GREEN))
        if kept:
            keep = "it keeps its" if len(kept) == 1 else "they keep their"
            lines.append(_rich(f"{_join(kept)} could not be calibrated: {keep} offsets (see the "
                               f"Note column).", W.AMBER))
        if bad:
            lines.append(_rich(f"{_join(bad)} got an impossible slope: not saved.", W.RED))
        if n == 2:
            lines.append(_rich("With 2 points each line passes through both, so a point read "
                               "wrong does not show: 3 or 4 points let it show.", W.DIM))
        self._results_summary.setText("<br>".join(lines))
        self._results_title.setText(f"Calibration results ({n} points)")
        self._show_results_frame(self._tabs.currentIndex() == TAB_RUN)
        _set_status(self._results_status, "")
        self._update_run_ui()

    def _save_offsets(self) -> None:
        """Write the fitted offsets to config.toml."""
        r = self._results
        if r is None or r.saved:
            return
        if self._session is not None and self._session.collecting >= 0:
            _set_status(self._results_status, "Not saved: a point is being collected.", W.RED)
            return
        if self._measuring():
            _set_status(self._results_status, "Not saved yet: wait for the measurement to "
                                              "finish (it uses the offsets in force).", W.RED)
            return
        offsets = r.savable()
        if not offsets:
            _set_status(self._results_status, "Nothing to save: no receiver could be "
                                              "calibrated.", W.RED)
            return
        try:
            self._cal.save_offsets(offsets)
        except (OSError, TypeError, ValueError) as exc:
            _set_status(self._results_status, f"Could not save the offsets: {exc}", W.RED)
            return
        r.saved = True
        self._session = None
        self._notify_config_changed()
        kept = [f"R{rid + 1}" for rid in range(self._cal.RECEIVER_COUNT) if rid not in offsets]
        text = f"Saved the offsets of {_join(f'R{rid + 1}' for rid in sorted(offsets))}."
        if kept:
            text += f" {_join(kept)} kept {'its' if len(kept) == 1 else 'their'} offsets."
        _set_status(self._results_status, text, W.GREEN)
        self._set_run_status("Calibration saved. Check the accuracy on the Check tab.", W.GREEN)
        self._results_title.setText(f"Calibration results ({len(r.points)} points): saved")
        self._update_run_ui()

    def _update_run_progress(self) -> None:
        s = self._session
        if s is None:
            for bar in self._rx_bars:
                bar.setRange(0, 1)
                bar.setValue(0)
            return
        k = s.collecting if s.collecting >= 0 else self._run_combo.currentIndex()
        run = k + 1 if 0 <= k < len(s.points) else 0
        for rid, bar in enumerate(self._rx_bars):
            count = self._cal.get_max_count(rid, run) if run else 0
            bar.setRange(0, s.reads)
            bar.setValue(min(count, s.reads))
            bar.setFormat(f"R{rid + 1}  {count}/{s.reads}")

    def _update_run_ui(self) -> None:
        """Buttons, prompt and progress of the Run tab."""
        s = self._session
        collecting = s is not None and s.collecting >= 0
        up = self._link_up()
        self._start_btn.setVisible(s is None)
        self._run_row.setVisible(s is not None)
        self._bars_box.setVisible(s is not None)
        self._cancel_btn.setVisible(s is not None)
        self._run_combo.setEnabled(s is not None and not collecting)
        k = self._run_combo.currentIndex()
        self._collect_btn.setText(f"Collect P{k + 1}" if k >= 0 else "Collect")
        self._collect_btn.setEnabled(s is not None and not collecting and up
                                     and not self._measuring())
        self._collect_btn.setToolTip("" if up else self._link_down_text())
        self._stop_btn.setEnabled(collecting)

        color = W.GOLD
        if s is None:
            n = len(self._saved)
            names = _join(f"P{j + 1}" for j in range(n))
            problems = self._check_points(self._saved).problems if n else []
            if n < points.MIN_POINTS:
                prompt, color = "Save 2 to 4 calibration points first (Points tab).", W.RED
            elif problems:
                prompt = (f"The saved points {names} can't be calibrated on: "
                          + "; ".join(problems) + ". Change them on the Points tab.")
                color = W.RED
            else:
                prompt = f"Press Start calibration to calibrate at the saved points {names}."
            steps = ""
        else:
            p = s.points[k] if 0 <= k < len(s.points) else None
            if collecting:
                prompt = (f"Collecting at P{s.collecting + 1}… keep the transmitter still until "
                          f"every receiver's green bar reaches the dashed line.")
            elif all(s.done):
                prompt = ("Every point is collected: check the results, then Save offsets. "
                          "Choose a point and press Collect to collect it again.")
            elif p is not None and s.done[k]:
                prompt = (f"P{k + 1} is collected. Choose the next point, or press Collect "
                          f"P{k + 1} to collect it again.")
            elif p is not None:
                prompt = (f"Place the transmitter at P{k + 1} {_fmt_point(p)} cm, then press "
                          f"Collect P{k + 1}.")
            else:
                prompt = ""
            marks = []
            for j in range(len(s.points)):
                if j == s.collecting:
                    marks.append(_rich(f"P{j + 1} ●", W.GOLD))
                elif s.done[j]:
                    marks.append(_rich(f"P{j + 1} ✓", W.GREEN))
                else:
                    marks.append(_rich(f"P{j + 1} ○", W.DIM))
            steps = "Points: " + "  ".join(marks)
        ui_scale.style(self._prompt, f"color: {color}; font: bold 13px Arial; "
                                     f"background: transparent;")
        self._prompt.setText(prompt)
        self._steps_label.setText(steps)
        self._steps_label.setVisible(bool(steps))
        self._update_run_progress()

        r = self._results
        self._results_section.setVisible(r is not None)
        self._save_offsets_btn.setEnabled(r is not None and not r.saved and not collecting)
        self._discard_results_btn.setVisible(r is not None and not r.saved)
        self._update_check_ui()                   # Measure waits for a collection
        if self._tabs.currentIndex() == TAB_RUN:
            self._show_results_frame(r is not None)
            self._refresh_arena()

    def _draw_histograms(self) -> None:
        """Redraw the Run tab's histograms (only while they are on show)."""
        if self._tabs.currentIndex() != TAB_RUN or not self._shown:
            return
        s = self._session
        if s is None:
            self._hist.clear("Tick histograms: start a calibration run")
            return
        k = s.collecting if s.collecting >= 0 else self._run_combo.currentIndex()
        if not 0 <= k < len(s.points):
            return
        label = f"P{k + 1} {_fmt_point(s.points[k])}"
        if s.collecting < 0 and not s.done[k] and not any(self._cal.get_max_count(rid, k + 1)
                                                          for rid in range(len(self._hist.axes))):
            self._hist.clear(f"{label}: not collected yet")
            return
        self._hist.show(self._cal, k + 1, f"{label}: ticks per receiver",
                        s.reads, s.latest if k == s.collecting else None)

    # ------------------------------------------------------------------
    # Check tab
    # ------------------------------------------------------------------

    def _on_check_point_edited(self, pts: list) -> None:
        if pts:
            self._set_check_point((float(pts[0][0]), float(pts[0][1])))

    def _set_check_point(self, p: tuple[float, float]) -> None:
        self._check_point = (float(p[0]), float(p[1]))
        self._check_table.set_point(0, *self._check_point)
        if self._check_measured is not None:
            self._show_check_result()             # the same measurement against the new truth
        self._fit_view()
        if self._tabs.currentIndex() == TAB_CHECK:
            self._refresh_arena()

    def _measure_check(self) -> None:
        """Measure (or, while measuring, cancel) the accuracy check."""
        worker = self._check_worker
        if worker is not None:
            worker.cancel()
            _set_status(self._check_status, "Cancelling…", W.AMBER)
            return
        refusal = self._measure_refusal()
        if refusal:
            _set_status(self._check_status, refusal, W.RED)
            return
        samples = int(self._samples_spin.value())
        self._check_progress.setRange(0, samples)
        self._check_progress.setValue(0)
        self._check_progress.setVisible(True)
        _set_status(self._check_status, "Measuring…")
        self._check_worker = W.run_in_thread(
            measure.measure_position, self._client, self._position_lib, samples,
            timeout_s=max(15.0, 0.5 * samples), pass_cancel=True, pass_progress=True,
            on_result=self._on_check_measured, on_error=self._on_check_failed,
            on_progress=self._on_check_progress, on_finished=self._on_check_finished)
        self._on_measuring_changed()

    def _measure_refusal(self) -> str:
        if not self._link_up():
            return self._link_down_text("Can't measure")
        if self._cal.is_running:
            return "Can't measure while a calibration collection runs: stop it first."
        if self._origin.is_measuring():
            return "Can't measure while the origin is being measured."
        return ""

    def _on_check_progress(self, value) -> None:
        if isinstance(value, tuple) and len(value) == 2 and self._check_worker is not None:
            done, total = int(value[0]), max(1, int(value[1]))
            self._check_progress.setRange(0, total)
            self._check_progress.setValue(min(done, total))
            _set_status(self._check_status, f"Measuring… {done}/{total}")

    def _on_check_measured(self, result: measure.MeasureResult) -> None:
        if result.cancelled:
            _set_status(self._check_status, "Measurement cancelled.", W.AMBER)
            return
        self._check_measured = result
        if result.position is None:
            _set_status(self._check_status, result.summary() + ".", W.RED)
        elif result.link_lost or result.timed_out:
            why = ("the ground-station link went down" if result.link_lost
                   else "not every reading came back in time")
            _set_status(self._check_status, f"Measured {result.summary()}, stopped early: {why}.",
                        W.AMBER)
        else:
            _set_status(self._check_status, f"Measured {result.summary()}.", W.GREEN)
        self._show_check_result()

    def _on_check_failed(self, message: str) -> None:
        _set_status(self._check_status, f"Measurement failed: {message}", W.RED)

    def _on_check_finished(self) -> None:
        self._check_worker = None
        self._check_progress.setVisible(False)
        self._on_measuring_changed()

    def _clear_check_result(self) -> None:
        self._check_measured = None
        self._check_result = None
        self._check_section.setVisible(False)

    def _show_check_result(self) -> None:
        r = self._check_measured
        if r is None:
            self._clear_check_result()
            return
        try:
            coords = self._position_lib.receiver_coords
            chk = measure.check_point(r, self._check_point, coords)
        except (ValueError, TypeError) as exc:
            _set_status(self._check_status, f"Could not compare: {exc}", W.RED)
            return
        self._check_result = chk
        lines = []
        if chk.lm_error is not None:
            dx, dy = chk.lm_offset
            error = _rich(f"{chk.lm_error:.1f} cm", _error_color(chk.lm_error))
            lines.append(f"Position error <b>{error}</b> (LM; dX {_fmt_signed(dx)}, "
                         f"dY {_fmt_signed(dy)})")
        else:
            lines.append(_rich("No LM fix", W.RED))
        if chk.cep_error is not None:
            lines.append("CEP " + _rich(f"{chk.cep_error:.1f} cm", _error_color(chk.cep_error)))
        if r.spread is not None:
            lines.append(f"spread ±{r.spread:.1f} cm over {r.n_ok}/{r.n_total} readings")
        if chk.rms_residual is not None:
            worst = (f", worst R{chk.worst_receiver + 1} "
                     f"{_fmt_signed(chk.residuals[chk.worst_receiver])} cm"
                     if chk.worst_receiver is not None else "")
            lines.append(f"range error RMS {chk.rms_residual:.1f} cm{worst}")
        self._check_summary.setText(" · ".join(lines))
        t = self._check_rx_table
        t.setRowCount(len(chk.true_distances))
        for rid, (true_d, res) in enumerate(zip(chk.true_distances, chk.residuals)):
            measured = r.distances_mean[rid] if rid < len(r.distances_mean) else math.nan
            _set_cell(t, rid, 0, f"R{rid + 1}", W.CYAN, center=True)
            _set_cell(t, rid, 1, f"{true_d:.1f}")
            if math.isfinite(res):
                _set_cell(t, rid, 2, f"{measured:.1f}")
                _set_cell(t, rid, 3, _fmt_signed(res), _error_color(res))
            else:
                _set_cell(t, rid, 2, "no reading", W.RED,
                          "This receiver never passed the sanity filter during the measurement")
                _set_cell(t, rid, 3, "—", W.RED)
        self._check_section.setVisible(True)
        if self._tabs.currentIndex() == TAB_CHECK:
            self._refresh_arena()

    def _update_check_ui(self) -> None:
        measuring = self._check_worker is not None
        up = self._link_up()
        self._measure_btn.setText("Cancel" if measuring else "Measure")
        self._measure_btn.setEnabled(measuring or (up and not self._cal.is_running
                                                   and not self._origin.is_measuring()))
        self._measure_btn.setToolTip("" if up else self._link_down_text())
        self._samples_spin.setEnabled(not measuring)
