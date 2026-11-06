"""Calibration panel.

Each receiver turns ticks into centimetres with ``distance = tick * slope +
intercept``.  The panel is a large arena view (the active receivers and the
arena's (0, 0), always marked) beside three tabs:

* **Origin (0,0)**: move the arena's (0, 0) to a point picked, typed, given as
  an offset or measured with the transmitter.  A measured point is shown on
  the arena and the panel asks straight away whether it becomes the new
  (0, 0).  The receivers, the calibration points and the barriers are
  translated and config.toml is saved (``SettingsModule.shift_origin``, which
  also recomputes every receiver's ``origin_distance``).
* **Offsets**: every active receiver's slope and intercept, editable, with
  its distance to (0, 0) on the arena map for reference.  "Set slopes to
  default" fills in ``SettingsModule.DEFAULT_SLOPE``; Save writes them all
  at once (``SettingsModule.set_receiver_offsets``).
* **System Origin Calibration**: with the transmitter on (0, 0), one
  collection (``UltraGPSCalibration.start_run`` in one run slot: the most
  frequent tick of every receiver, ``calibration_reads`` hits on it for each
  active receiver) with live tick histograms.  For this measurement the
  intercepts are taken as 0 and the slopes kept, so a receiver measured
  ``modal_tick * slope``; its new intercept is

      new_intercept = origin_distance (arena map) - modal_tick * slope

  A dialog compares every current intercept with the new one (and the
  percent difference) and asks whether to save them; only the intercepts
  change.  Receivers with no reading are reported and keep theirs.

Nothing waits on the network on the GUI thread: the calibration library
collects on its own thread (its callbacks come back through queued signals)
and the origin measurement runs on a ``widgets.run_in_thread`` worker.
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
    QAbstractItemView, QAbstractScrollArea, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QSizePolicy, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator

from SettingsModule import DEFAULT_SLOPE, SettingsModule
from ultragps_calibration import UltraGPSCalibration
from windows import measure, ui_scale, widgets as W
from windows.arena_view import ArenaInteractor, ArenaView
from windows.ui_scale import ScalingCanvas

log = logging.getLogger(__name__)

TAB_ORIGIN, TAB_OFFSETS, TAB_SYSTEM = range(3)

ORIGIN_RUN = 1                 # the calibration library's run slot the origin collection uses
HIST_REDRAW_MS = 150           # live histograms are redrawn at most this often
HIST_HALF_WIDTH = 15           # ticks shown either side of the modal tick
ORIGIN_SAMPLES = 20            # readings averaged by "Measure with transmitter"
STOP_WAIT_MS = 3000            # longest wait for a measurement to end when the panel is left
MIN_ACTIVE = 3                 # fewest active receivers (SettingsModule.MIN_RECEIVERS)
SAME_TOL = 1e-9                # values this close count as unchanged
WIDE_ASPECT = 1.2              # a plot area this much wider than tall is "wide"

# Arena figure margins (no legend: the arena takes the whole canvas)
_LAYOUT_FULL = dict(left=0.1, right=0.97, bottom=0.08, top=0.92)

# Point groups drawn on the arena (see _refresh_arena)
_GROUPS = ("origin_preview", "target")

_PAGE_CSS = f"""
    QWidget#calPage {{ background-color: black; }}
    QLabel {{ color: white; font: 12px Arial; background: transparent; }}
    QLineEdit {{ background-color: #222222; color: white; border: 1px solid #444444;
                border-radius: 3px; padding: 2px 5px; font: 12px Arial; }}
    QLineEdit:focus {{ border-color: {W.GREEN}; }}
    QLineEdit:disabled {{ background-color: #151515; color: #555555; border-color: #2A2A2A; }}
"""

_INVALID_EDIT_CSS = f"QLineEdit {{ border: 1px solid {W.RED}; }}"

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
    """"R1", "R1 and R2", "R1, R2 and R3"."""
    items = [str(i) for i in items]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _fmt_point(p) -> str:
    return f"({p[0]:.1f}, {p[1]:.1f})"


def _fmt_signed(value: float, decimals: int = 1) -> str:
    """``value`` with its sign; a value that rounds to zero reads "0.0" (never
    "-0.0")."""
    text = f"{value:+.{decimals}f}"
    return f"{0:.{decimals}f}" if float(text) == 0 else text


def _num_text(value: float) -> str:
    """A saved slope or intercept as the editor shows it (exact for the
    values people type; the editor keeps the saved value itself for a field
    left as shown)."""
    text = f"{value:.10g}"
    return "0" if text == "-0" else text


def _percent_change(new: float, current: float) -> float | None:
    """``(new - current) / |current| * 100``; None when ``current`` is 0."""
    if current == 0:
        return None
    return (new - current) / abs(current) * 100.0


def _fmt_percent(value: float | None) -> str:
    return "n/a" if value is None else f"{_fmt_signed(value)} %"


def _same(a: dict, b: dict) -> bool:
    """Whether two ``{key: float | None}`` dicts hold the same values."""
    if a.keys() != b.keys():
        return False
    for k, va in a.items():
        vb = b[k]
        if va is None or vb is None:
            if va is not vb:
                return False
        elif not math.isclose(va, vb, rel_tol=0.0, abs_tol=SAME_TOL):
            return False
    return True


def _rich(text: str, color: str) -> str:
    """``text`` as rich text in ``color``."""
    return f'<span style="color:{color}">{html.escape(text)}</span>'


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


def _active_count(settings: SettingsModule, channels: int) -> int:
    """How many receivers are active (ids 0 .. N-1): ``number_of_receivers``,
    clamped to ``MIN_ACTIVE`` .. ``channels``; all of them if it is unreadable.
    The dormant ones keep their calibration in config.toml but are not shown."""
    try:
        n = settings.number_of_receivers
    except (AttributeError, TypeError, ValueError):
        return channels
    if isinstance(n, bool) or not isinstance(n, int):
        return channels
    return min(max(n, MIN_ACTIVE), channels)


def _receiver_positions(settings: SettingsModule,
                        active: int) -> tuple[list[int], np.ndarray, str]:
    """The active receivers (ids below ``active``) in id order: ``(ids, (R, 2)
    positions, problem)``; ``problem`` says why a receiver has no usable
    position ("" if every one has)."""
    try:
        towers = sorted(settings.get_tower_coordinates(), key=lambda t: int(t[0]))
        towers = [t for t in towers if int(t[0]) < active]
    except (TypeError, ValueError) as exc:
        return [], np.empty((0, 2)), f"the receivers in config.toml are unreadable ({exc})"
    ids, xy = [], []
    for rid, pos in towers:
        try:
            x, y = float(pos[0]), float(pos[1])
        except (TypeError, ValueError, IndexError):
            x = y = math.nan
        ids.append(int(rid))
        xy.append((x, y))
    coords = np.array(xy, dtype=float).reshape(-1, 2)
    if len(coords) < active:
        problem = f"config.toml has {len(coords)} of the {active} active receivers"
    elif not np.isfinite(coords).all():
        problem = "a receiver has no valid position (fix it in the Arena Maker)"
    else:
        problem = ""
    return ids, coords, problem


# ---------------------------------------------------------------------------
# System origin calibration state
# ---------------------------------------------------------------------------

@dataclass
class _OriginRow:
    """One receiver's outcome of a system origin calibration."""

    tick: float | None                    # modal tick (None: no data, 0: no echo)
    measured: float | None                # tick × slope (intercept taken as 0)
    distance: float                       # its distance to (0, 0) on the arena map
    current: float                        # the intercept in force
    new: float | None                     # distance - measured (None: no reading)

    @property
    def change(self) -> float | None:
        return None if self.new is None else _percent_change(self.new, self.current)


@dataclass
class _OriginRun:
    """A system origin calibration: one collection at (0, 0), then the new
    intercepts.  The receivers' slopes, intercepts and distances to (0, 0)
    are those in force when it started: a change to any of them elsewhere
    ends it."""

    token: int                            # identifies this run's callbacks
    reads: int                            # calibration_reads: modal-tick hits needed
    ids: list                             # the active receivers
    slopes: dict                          # {rid: slope}: kept
    intercepts: dict                      # {rid: intercept in force}: taken as 0 meanwhile
    distances: dict                       # {rid: origin_distance} from the arena map
    collecting: bool = True
    latest: list | None = None            # the latest six ticks
    reads_seen: int = 0                   # readings so far
    rows: dict = field(default_factory=dict)  # {rid: _OriginRow} once done
    saved: bool = False

    @property
    def done(self) -> bool:
        return bool(self.rows)

    def usable(self) -> dict:
        """``{rid: new intercept}`` of every receiver that gave a reading."""
        return {rid: r.new for rid, r in self.rows.items() if r.new is not None}

    def missing(self) -> list:
        """The receivers that gave no reading."""
        return [rid for rid, r in self.rows.items() if r.new is None]


# ---------------------------------------------------------------------------
# Live histograms
# ---------------------------------------------------------------------------

class _Histograms:
    """The tick histograms of every receiver for one collection, live while
    it runs: one figure on a ``ScalingCanvas``.

    One bar per tick value within ``HIST_HALF_WIDTH`` ticks of the modal tick
    (the one used, green; the others cyan), the latest reading as a gold line
    and the reads needed (``calibration_reads``) as a dashed line: the
    collection is done when every green bar reaches it.  Readings of 0 (no
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

    def set_receivers(self, receivers: int) -> None:
        """Show ``receivers`` plots (the active receivers; drawn anew)."""
        if receivers == self.receivers:
            return
        self.receivers = receivers
        columns, self.columns = self.columns, 0
        self.set_columns(columns)

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
    """Full-page calibration panel: the arena beside the Origin (0,0),
    Offsets and System Origin Calibration tabs (see the module docstring).

    Every config write goes through ``SettingsModule.shift_origin`` or
    ``set_receiver_offsets`` and is followed by
    ``main_window.notify_config_changed()``; ``config_changed`` from elsewhere
    reloads the panel (on its next show if it is hidden): unsaved offset
    edits survive unless the saved offsets changed, and a system origin
    calibration whose receivers, slopes, intercepts or distances to (0, 0)
    changed is ended.  Moving (0, 0) and saving offsets wait for a running
    collection.  ``on_panel_hide`` stops a collection, cancels the origin
    measurement (waiting up to ``STOP_WAIT_MS``) and leaves pick mode.  Only
    the active receivers (``number_of_receivers``: R1 .. RN) are shown and
    calibrated; the dormant ones keep their offsets.

    Args:
        cal:             The main window's ``UltraGPSCalibration``.
        settings_module: The main window's ``SettingsModule``.
        main_window:     The main window: ``client``, ``position_lib``,
                         ``show_panel``, ``notify_config_changed`` and the
                         ``config_changed`` / ``link_changed`` signals.
    """

    # The calibration library calls back from its collection thread; these
    # signals carry the callbacks to the GUI thread (queued).  The first value
    # is the run token: a callback from an abandoned run is dropped.
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

        # Receivers (from the settings): only the active ones are shown
        self._n_active = _active_count(settings_module, cal.RECEIVER_COUNT)
        self._rx_ids: list[int] = []
        self._coords = np.empty((0, 2))
        self._geometry_error = ""

        # Offsets editor: the saved values and the text shown for them
        self._offsets_saved: dict = {}            # {rid: (slope, intercept) | None}
        self._offsets_shown: dict = {}            # {rid: (slope text, intercept text)}

        # System origin calibration
        self._origin_run: _OriginRun | None = None
        self._run_token = 0

        # Origin tab
        self._origin_candidate = None
        self._measured_candidate = None           # the point the transmitter was measured at

        self._picking = False
        self._shown = False
        self._config_dirty = False
        self._own_change = False
        self._got_config_changed = False

        self._hist_timer = QTimer(self)
        self._hist_timer.setSingleShot(True)
        self._hist_timer.setInterval(HIST_REDRAW_MS)
        self._hist_timer.timeout.connect(self._draw_histograms)

        self._build_ui()
        self._connect_signals()
        self._load_from_settings("drop")
        self._sync_link()
        self._on_tab_changed(TAB_ORIGIN)

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

        # Arena, with the collection's histograms beside it and its table below
        self._fig = Figure(facecolor="black")
        self._view = ArenaView(self._fig.add_subplot(111), title="Calibration")
        self._view.set_legend_visible(False)
        self._fig.subplots_adjust(**_LAYOUT_FULL)
        self._canvas = ScalingCanvas(self._fig, ref_size=(700, 700))
        ui_scale.min_size(self._canvas, 240, 220)
        self._interactor = ArenaInteractor(self._canvas, self._view, snap=1.0)
        self._hist = _Histograms(self._n_active)

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
        self._tabs.addTab(W.scroll_area(self._build_origin_tab()), "Origin (0,0)")
        self._tabs.addTab(W.scroll_area(self._build_offsets_tab()), "Offsets")
        self._tabs.addTab(W.scroll_area(self._build_system_tab()), "System Origin Calibration")
        self._tabs.setTabToolTip(TAB_ORIGIN, "Move the arena's (0, 0)")
        self._tabs.setTabToolTip(TAB_OFFSETS, "Every receiver's slope and intercept")
        self._tabs.setTabToolTip(TAB_SYSTEM, "Measure at (0, 0) and compute every receiver's "
                                             "intercept")
        ui_scale.style(self._tabs, _TABS_CSS)
        ui_scale.min_width(self._tabs, 340)
        ui_scale.max_width(self._tabs, 430)
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

    def _build_origin_tab(self) -> QWidget:
        page, col = _page()
        self._origin = W.OriginTool(measure=self._measure_origin, accent=W.BLUE)
        self._origin.originRequested.connect(self._on_origin_requested)
        self._origin.previewChanged.connect(self._on_origin_preview)
        self._origin.pickModeChanged.connect(self._on_pick_mode)
        self._origin.measuringChanged.connect(self._on_measuring_changed)
        col.addWidget(self._origin)

        info = W.section("Calibrating (0, 0)", W.BLUE)
        info.add(_text("Stand the transmitter on the physical (0, 0) mark and press Measure "
                       f"with transmitter (the mean of {ORIGIN_SAMPLES} fixes): the measured "
                       "point is shown on the arena and you are asked whether it becomes the "
                       "new (0, 0). Or pick the spot on the arena, type it in the current frame "
                       "or give it as an offset from today's (0, 0) or a receiver, then Apply."))
        info.add(_text("Moving (0, 0) moves the whole frame: receivers, calibration points, "
                       "barriers and image overlays, saved in config.toml with every receiver's "
                       "new distance to (0, 0). Slopes and intercepts are not changed."))
        col.addWidget(info)
        col.addStretch(1)
        return page

    def _build_offsets_tab(self) -> QWidget:
        page, col = _page()

        sec = W.section("Receiver offsets", W.GREEN)
        sec.add(_text("distance = tick × slope + intercept (cm). Edit any value, then Save: "
                      "every change is written to config.toml at once. Slopes must be above 0."))
        grid = QGridLayout()
        ui_scale.spacing(grid, 5)
        for c, head in enumerate(("", "Slope", "Intercept cm", "To (0, 0) cm", "")):
            grid.addWidget(_text(head, W.DIM, 10, bold=True, wrap=False), 0, c)
        self._offset_rows = []
        for rid in range(self._cal.RECEIVER_COUNT):
            name = _text(f"R{rid + 1}", W.CYAN, 12, bold=True, wrap=False)
            slope, intercept = QLineEdit(), QLineEdit()
            slope.setToolTip("cm per tick: above 0")
            intercept.setToolTip("cm added to every distance (may be negative)")
            for edit in (slope, intercept):
                ui_scale.min_width(edit, 70)
                edit.textEdited.connect(self._on_offset_edited)
            dist = _text("—", W.DIM, 11, wrap=False)
            dist.setToolTip("Distance from this receiver to (0, 0) on the arena map "
                            "(origin_distance in config.toml)")
            default = W.neon_button("Default", "#555555", "white", font_px=10, padding=(2, 6),
                                    tooltip=f"Set this slope to {DEFAULT_SLOPE:g}")
            default.clicked.connect(functools.partial(self._set_default_slopes, [rid]))
            widgets = (name, slope, intercept, dist, default)
            for c, w in enumerate(widgets):
                grid.addWidget(w, rid + 1, c)
            self._offset_rows.append(widgets)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        sec.add_layout(grid)

        self._defaults_btn = W.neon_button(
            f"Set slopes to default ({DEFAULT_SLOPE:g})", W.GOLD, tooltip=(
                f"Put {DEFAULT_SLOPE:g} in every active receiver's slope (press Save to keep it)"))
        self._defaults_btn.clicked.connect(lambda: self._set_default_slopes(None))
        sec.add_layout(_row(self._defaults_btn))
        self._offsets_save_btn = W.neon_button("Save", W.GREEN, padding=(5, 16), tooltip=(
            "Write the slopes and intercepts to config.toml"))
        self._offsets_save_btn.clicked.connect(self._save_offsets)
        self._offsets_revert_btn = W.neon_button("Revert", "#555555", "white",
                                                 tooltip="Go back to the saved values")
        self._offsets_revert_btn.clicked.connect(self._revert_offsets)
        sec.add_layout(_row(self._offsets_save_btn, self._offsets_revert_btn))
        self._offsets_status = _status_label()
        sec.add(self._offsets_status)
        col.addWidget(sec)

        col.addWidget(_text("The distances to (0, 0) come from the arena map: they are "
                            "recomputed whenever the receivers or (0, 0) move and when the "
                            "program starts. The System Origin Calibration tab uses them to "
                            "compute the intercepts."))
        col.addStretch(1)
        return page

    def _build_system_tab(self) -> QWidget:
        page, col = _page()

        sec = W.section("System Origin Calibration", W.GOLD)
        sec.add(_text("Computes every receiver's intercept from one collection with the "
                      "transmitter on (0, 0). For this measurement only, the intercepts are "
                      "taken as 0 and the slopes kept: each receiver measures modal tick × "
                      "slope. Keep the transmitter still until every receiver has read its "
                      "most frequent tick often enough (the dashed line on the histograms)."))
        self._sys_prompt = _text("", W.GOLD, 13, bold=True)
        sec.add(self._sys_prompt)
        self._sys_start_btn = W.neon_button("Start", W.GOLD, padding=(6, 16))
        self._sys_start_btn.clicked.connect(self._start_origin_run)
        self._sys_stop_btn = W.neon_button("Stop", W.RED, "white",
                                           tooltip="Stop collecting (nothing is saved)")
        self._sys_stop_btn.clicked.connect(lambda: self._stop_origin_run())
        sec.add_layout(_row(self._sys_start_btn, self._sys_stop_btn))

        self._rx_bars = []
        bars = QGridLayout()
        ui_scale.spacing(bars, 4)
        for rid in range(self._cal.RECEIVER_COUNT):
            bar = W.progress_bar(W.GREEN, text=True)
            bar.setFormat(f"R{rid + 1}")
            bar.setToolTip("Times the receiver has read its most frequent tick, of the reads "
                           "needed")
            bars.addWidget(bar, rid // 2, rid % 2)
            bar.setVisible(rid < self._n_active)
            self._rx_bars.append(bar)
        self._bars_box = QWidget()
        self._bars_box.setLayout(bars)
        bars.setContentsMargins(0, 0, 0, 0)
        sec.add(self._bars_box)
        self._sys_status = _status_label()
        sec.add(self._sys_status)
        self._sys_link_note = _status_label()
        sec.add(self._sys_link_note)
        col.addWidget(sec)

        self._sys_result = W.section("New intercepts", W.GREEN)
        self._sys_summary = _text("", rich=True)
        self._sys_result.add(self._sys_summary)
        self._sys_review_btn = W.neon_button("Save new intercepts…", W.GREEN, tooltip=(
            "Compare the new intercepts with the current ones and choose whether to save them"))
        self._sys_review_btn.clicked.connect(self._ask_save_intercepts)
        self._sys_discard_btn = W.neon_button("Discard", "#555555", "white",
                                              tooltip="Forget this result (nothing is saved)")
        self._sys_discard_btn.clicked.connect(self._discard_origin_run)
        self._sys_result.add_layout(_row(self._sys_review_btn, self._sys_discard_btn))
        col.addWidget(self._sys_result)

        col.addWidget(_text("new intercept = distance to (0, 0) on the arena map − modal tick × "
                            "slope. The slopes are not changed. Changing the burst delay (D or E "
                            "on the Base Station panel) shifts every intercept: calibrate again "
                            "after one."))
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
        self._results_title = _text("Recorded at (0, 0)", W.GREEN, 13, bold=True, wrap=False)
        lay.addWidget(self._results_title)
        self._results_table = _table(["", "Modal tick", "Measured cm\n(tick × slope)",
                                      "Map cm\nto (0, 0)", "Intercept cm\nnow",
                                      "Intercept cm\nnew", "Difference"],
                                     self._n_active, stretch=(2, 3, 4, 5, 6))
        self._results_table.setToolTip(
            "Measured: the receiver's most frequent tick × its slope (the intercept taken as 0). "
            "New intercept = map distance − measured. Difference = (new − now) / |now| × 100.")
        lay.addWidget(self._results_table)
        self._results_frame = frame
        frame.setVisible(False)
        return frame

    def _connect_signals(self) -> None:
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
        self._interactor.clicked.connect(self._on_arena_clicked)

    # ------------------------------------------------------------------
    # Panel lifecycle
    # ------------------------------------------------------------------

    def on_panel_show(self) -> None:
        self._shown = True
        if self._config_dirty:
            self._load_from_settings("auto")
        self._interactor.enable()
        self._sync_link()
        self._on_tab_changed(self._tabs.currentIndex())

    def on_panel_hide(self) -> None:
        """Stop everything the panel runs: a collection, the origin
        measurement and pick mode."""
        self._shown = False
        self._interactor.disable()
        self._origin.set_pick_mode(False)
        self._hist_timer.stop()
        run = self._origin_run
        if run is not None and run.collecting:
            self._stop_origin_run(note="Collecting stopped when you left the panel: press Start "
                                       "to collect again.")
        if self._origin.is_measuring():
            self._origin.cancel_measurement()
            self._origin.wait(STOP_WAIT_MS)

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
        """(Re)read the receivers and their offsets.

        ``edits`` says what happens to unsaved offset edits: "keep" them,
        "drop" them, or "auto" (a change made in another panel): keep them
        unless the saved offsets changed.
        """
        self._config_dirty = False
        old_coords = self._coords
        self._set_active_count(_active_count(self._settings, self._cal.RECEIVER_COUNT))
        self._rx_ids, self._coords, self._geometry_error = \
            _receiver_positions(self._settings, self._n_active)
        moved = (old_coords.shape != self._coords.shape
                 or not np.array_equal(old_coords, self._coords, equal_nan=True))
        if moved and edits == "auto" and self._origin.candidate() is not None:
            self._origin.clear()
            self._origin.set_status("The arena was changed in another panel: choose the new "
                                    "(0, 0) again.", W.AMBER)
        self._origin.set_references(self._receiver_refs())
        self._view.set_receivers([(rid, (float(x), float(y)))
                                  for rid, (x, y) in zip(self._rx_ids, self._coords)])
        self._load_offsets(edits)
        self._validate_origin_run()
        self._update_system_ui()
        self._fit_view()
        self._refresh_arena()

    def _set_active_count(self, n: int) -> None:
        """Follow a change of the number of active receivers: the histograms,
        progress bars, editor rows and table show R1 .. Rn only."""
        if n == self._n_active:
            return
        self._n_active = n
        self._hist.set_receivers(n)
        for rid, bar in enumerate(self._rx_bars):
            bar.setVisible(rid < n)
        self._results_table.setRowCount(n)

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

    def _reload_after_failed_save(self) -> None:
        """A save that failed to write left the settings changed in memory:
        back to the file."""
        try:
            self._settings.reload_config()
        except (OSError, ValueError):
            log.exception("Could not reload the settings")
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
        note = "" if up else self._link_down_text() + " Start waits for it."
        _set_status(self._sys_link_note, note, W.RED)
        self._update_system_ui()

    def _on_tab_changed(self, index: int) -> None:
        self._origin.set_pick_mode(False)
        system = index == TAB_SYSTEM
        self._show_histograms_pane(system)
        self._results_frame.setVisible(system and self._origin_run is not None)
        self._refresh_arena()
        if system:
            self._draw_histograms()

    def eventFilter(self, obj, event) -> bool:
        # Lay the histograms out for the shape the plot area is given
        if event.type() == QEvent.Type.Resize and obj is self._arena_split:
            self._apply_split_layout()
        return False

    def _apply_split_layout(self) -> None:
        """The histograms beside the arena when the plot area is wide, under
        it (in three columns) when it is tall."""
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
        """The histograms beside the arena (System Origin Calibration tab
        only); the arena keeps about three fifths of the width."""
        canvas = self._hist.canvas
        if visible == canvas.isVisible():
            return
        canvas.setVisible(visible)
        if visible:
            total = sum(self._arena_split.sizes())
            if total > 0:
                self._arena_split.setSizes([total * 3 // 5, total - total * 3 // 5])

    def _fit_view(self) -> None:
        """Fit the arena to the receivers, (0, 0) and the origin candidate."""
        extra = [self._origin_candidate] if self._origin_candidate is not None else []
        self._view.set_limits(extra_points=[p for p in extra if all(map(math.isfinite, p))])

    # ------------------------------------------------------------------
    # Arena drawing and interaction
    # ------------------------------------------------------------------

    def _refresh_arena(self) -> None:
        """Draw the point groups of the current tab (and clear the others).
        The (0, 0) marker is always on show."""
        tab = self._tabs.currentIndex()
        v = self._view
        shown: set = set()
        if tab == TAB_ORIGIN:
            c = self._origin_candidate
            if c is not None:
                v.set_points("origin_preview", [c], color=W.BLUE, marker="P", size=200,
                             labels=["new (0, 0)"], zorder=9.5)
                shown.add("origin_preview")
                title = (f"New (0, 0) at {_fmt_point(c)}: everything moves by "
                         f"({-c[0]:.1f}, {-c[1]:.1f})")
            else:
                title = "Calibrate (0, 0)"
            if self._picking:
                title = "Click the arena at the new (0, 0)"
        elif tab == TAB_SYSTEM:
            v.set_points("target", [(0.0, 0.0)], color="none", edgecolor=W.GOLD, marker="o",
                         size=900, zorder=8.5)
            shown.add("target")
            run = self._origin_run
            if run is not None and run.collecting:
                title = "Collecting at (0, 0): keep the transmitter still"
            elif run is not None and run.done:
                title = "Recorded at (0, 0)"
            else:
                title = "Place the transmitter at (0, 0)"
        else:
            title = "Receivers and (0, 0)"
        for group in _GROUPS:
            if group not in shown:
                v.clear_points(group)
        v.set_title(title)
        v.draw_idle()

    def _on_arena_clicked(self, x: float, y: float, button: int) -> None:
        if button == 1 and self._picking:
            self._origin.apply_pick(x, y)

    def _on_pick_mode(self, on: bool) -> None:
        """While picking, every arena press is a click for the origin tool."""
        self._picking = bool(on)
        self._interactor.pick_enabled = not self._picking
        self._canvas.setCursor(Qt.CursorShape.CrossCursor if self._picking
                               else Qt.CursorShape.ArrowCursor)
        self._refresh_arena()

    # ------------------------------------------------------------------
    # Origin tab
    # ------------------------------------------------------------------

    def _measure_origin(self, cancel=None, on_progress=None):
        """``OriginTool``'s measurement, on its worker thread: the mean of
        ``ORIGIN_SAMPLES`` fixes.  Refused (the tool shows the reason) while a
        collection uses the transmitter."""
        if self._cal.is_running:
            raise RuntimeError("a calibration collection is running: stop it first")
        if not self._link_up():
            raise RuntimeError(self._link_down_text())
        return measure.measure_position(self._client, self._position_lib, ORIGIN_SAMPLES,
                                        cancel=cancel, on_progress=on_progress)

    def _on_origin_preview(self, candidate) -> None:
        self._origin_candidate = candidate
        if candidate is not None and self._origin.is_measuring():
            self._measured_candidate = candidate          # set by the measurement's result
        self._fit_view()
        self._refresh_arena()

    def _on_measuring_changed(self, measuring: bool = False) -> None:
        if measuring:
            self._measured_candidate = None
        elif self._measured_candidate is not None:
            # Once the tool has finished with its worker: show the point, then ask
            QTimer.singleShot(0, self._confirm_measured_origin)
        self._update_system_ui()

    def _confirm_measured_origin(self) -> None:
        """The transmitter was measured: the point is on the arena; ask whether
        it becomes the new (0, 0)."""
        c, self._measured_candidate = self._measured_candidate, None
        if c is None or not self._shown or self._origin.candidate() != c:
            return
        x, y = c
        if self._tabs.currentIndex() != TAB_ORIGIN:
            self._tabs.setCurrentIndex(TAB_ORIGIN)
        self._refresh_arena()
        self._canvas.draw()                               # on show before the dialog
        if math.hypot(x, y) < 0.05:
            self._origin.set_status(f"Measured {_fmt_point(c)}: (0, 0) is already there.",
                                    W.GREEN)
            return
        if not W.confirm(self, "Move (0, 0)",
                         f"Make ({x:.1f}, {y:.1f}) the new (0, 0)?\n\n"
                         f"The transmitter was measured there (the blue cross on the arena). "
                         f"Every receiver position, calibration point and barrier (and image "
                         f"overlay) moves by ({-x:.1f}, {-y:.1f}) cm so this point becomes "
                         f"(0, 0), and config.toml is saved with every receiver's new distance "
                         f"to (0, 0). Slopes and intercepts are not changed.",
                         ok_text="Make it (0, 0)", cancel_text="Not now"):
            self._origin.set_status(f"Measured {_fmt_point(c)}: not applied. Press Apply to "
                                    f"make it (0, 0), or Clear.", W.DIM)
            return
        if self._move_origin(x, y):
            self._origin.clear()
            self._origin.set_status(f"(0, 0) is now the measured {_fmt_point(c)}: receivers, "
                                    f"calibration points and barriers moved by "
                                    f"({-x:.1f}, {-y:.1f}) cm.", W.GREEN)

    def _on_origin_requested(self, x: float, y: float) -> None:
        """Apply: make the current-frame point (x, y) the new (0, 0)."""
        if self._move_origin(x, y):
            self._origin.set_status(f"(0, 0) is now the old {_fmt_point((x, y))}: receivers, "
                                    f"calibration points and barriers moved by "
                                    f"({-x:.1f}, {-y:.1f}) cm.", W.GREEN)

    def _move_origin(self, x: float, y: float) -> bool:
        """``SettingsModule.shift_origin(x, y)`` (saved in config.toml), then
        the reload; a refusal says why in red (the tool keeps the point)."""
        run = self._origin_run
        if run is not None and run.collecting:
            self._refuse_origin("stop the System Origin Calibration first.")
            return False
        try:
            self._settings.shift_origin(x, y)
        except Exception as exc:                          # nothing may escape a slot
            if not isinstance(exc, (ValueError, OSError)):        # the documented failures
                log.exception("Moving (0, 0) failed")
            self._refuse_origin(str(exc))
            if isinstance(exc, OSError):
                self._reload_after_failed_save()
            return False
        self._notify_config_changed()
        self._fit_view()
        self._refresh_arena()
        return True

    def _refuse_origin(self, why: str) -> None:
        """Say why (0, 0) was not moved; the red status makes ``OriginTool``
        keep the point for another try."""
        self._origin.set_status(f"(0, 0) not moved: {why}", W.RED)

    # ------------------------------------------------------------------
    # Offsets tab
    # ------------------------------------------------------------------

    def _read_offset(self, rid: int) -> tuple[float, float] | None:
        """A receiver's saved ``(slope, intercept)``, or None if unreadable."""
        o = self._settings.get_receiver_offset(rid) or {}
        try:
            slope, intercept = float(o["slope"]), float(o["intercept"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(slope) and math.isfinite(intercept)):
            return None
        return slope, intercept

    def _load_offsets(self, edits: str) -> None:
        """Fill the editor from the saved offsets (keeping unsaved edits as
        ``edits`` says, see ``_load_from_settings``) and show every active
        receiver's distance to (0, 0)."""
        saved = {rid: self._read_offset(rid) for rid in range(self._n_active)}
        dirty = self._offsets_dirty()
        same = saved == self._offsets_saved
        keep = dirty and same and edits in ("keep", "auto")
        self._offsets_saved = saved
        for rid, (_name, slope, intercept, dist, default) in enumerate(self._offset_rows):
            active = rid < self._n_active
            for w in self._offset_rows[rid]:
                w.setVisible(active)
            if not active:
                continue
            d = self._settings.get_origin_distance(rid)
            dist.setText("—" if d is None else f"{d:.1f}")
            if keep:
                continue
            o = saved[rid]
            texts = ("", "") if o is None else (_num_text(o[0]), _num_text(o[1]))
            slope.setText(texts[0])
            intercept.setText(texts[1])
            self._offsets_shown[rid] = texts
        if dirty and not keep:
            self._set_offsets_status("The offsets changed meanwhile: your unsaved edits were "
                                     "dropped.", W.AMBER)
        unreadable = [f"R{rid + 1}" for rid, o in saved.items() if o is None]
        if unreadable and not keep:
            self._set_offsets_status(f"{_join(unreadable)}: no readable offset in config.toml "
                                     f"(type one and Save).", W.AMBER)
        self._update_offsets_ui()

    def _offsets_dirty(self) -> bool:
        """Whether the editor holds text that differs from the saved values
        (a row not filled yet counts as unchanged)."""
        for rid, (_n, slope, intercept, _d, _b) in enumerate(self._offset_rows[:self._n_active]):
            shown = self._offsets_shown.get(rid)
            if shown is not None and (slope.text().strip(), intercept.text().strip()) != shown:
                return True
        return False

    def _offset_edits(self) -> tuple[dict, list]:
        """``({rid: (slope, intercept)}, problems)`` from the editor; a field
        left as shown keeps the saved value exactly."""
        values, problems = {}, []
        for rid, (_n, slope_edit, icpt_edit, _d, _b) in enumerate(
                self._offset_rows[:self._n_active]):
            shown = self._offsets_shown.get(rid, ("", ""))
            saved = self._offsets_saved.get(rid)
            row = []
            for k, (edit, what) in enumerate(((slope_edit, "slope"), (icpt_edit, "intercept"))):
                text = edit.text().strip()
                if saved is not None and text == shown[k]:
                    value = saved[k]
                else:
                    value = W.parse_number(text)
                bad = value is None or (k == 0 and value <= 0)
                ui_scale.style(edit, _INVALID_EDIT_CSS if bad else "")
                if value is None:
                    problems.append(f"R{rid + 1} {what} must be a number")
                elif bad:
                    problems.append(f"R{rid + 1} slope must be above 0")
                row.append(value)
            if None not in row:
                values[rid] = (row[0], row[1])
        return values, problems

    def _on_offset_edited(self, _text: str = "") -> None:
        self._set_offsets_status("")
        self._update_offsets_ui()

    def _update_offsets_ui(self) -> None:
        _values, problems = self._offset_edits()
        dirty = self._offsets_dirty()
        self._offsets_save_btn.setEnabled(dirty)
        self._offsets_revert_btn.setEnabled(dirty)
        self._offsets_save_btn.setToolTip("Write the slopes and intercepts to config.toml"
                                          if not problems else "; ".join(problems))

    def _set_offsets_status(self, text: str, color: str = W.DIM) -> None:
        _set_status(self._offsets_status, text, color)

    def _set_default_slopes(self, rids=None) -> None:
        """Put ``DEFAULT_SLOPE`` in the slope of ``rids`` (None: every active
        receiver); saved on Save."""
        rids = range(self._n_active) if rids is None else [r for r in rids if r < self._n_active]
        for rid in rids:
            self._offset_rows[rid][1].setText(_num_text(DEFAULT_SLOPE))
        self._update_offsets_ui()
        if self._offsets_dirty():
            self._set_offsets_status(f"Slopes set to {DEFAULT_SLOPE:g} in the editor: press "
                                     f"Save to keep them.", W.GOLD)
        else:
            self._set_offsets_status(f"The saved slopes are already {DEFAULT_SLOPE:g}.")

    def _revert_offsets(self) -> None:
        self._offsets_shown = {}
        self._load_offsets("drop")
        self._set_offsets_status("Back to the saved values.")

    def _save_offsets(self) -> bool:
        """Write the editor's slopes and intercepts (the changed receivers)
        to config.toml in one save."""
        run = self._origin_run
        if run is not None and run.collecting:
            self._set_offsets_status("Not saved: the System Origin Calibration is collecting "
                                     "(it uses the slopes in force).", W.RED)
            return False
        if self._origin.is_measuring():
            self._set_offsets_status("Not saved yet: wait for the origin measurement to finish "
                                     "(it uses the offsets in force).", W.RED)
            return False
        values, problems = self._offset_edits()
        if problems:
            self._set_offsets_status("Not saved: " + "; ".join(problems) + ".", W.RED)
            return False
        changed = {rid: {"slope": s, "intercept": i} for rid, (s, i) in values.items()
                   if self._offsets_saved.get(rid) != (s, i)}
        if not changed:
            self._set_offsets_status("Nothing to save: these are the saved values.")
            self._offsets_shown = {}
            self._load_offsets("drop")
            return True
        try:
            self._settings.set_receiver_offsets(changed)
        except ValueError as exc:
            self._set_offsets_status(f"Not saved: {exc}", W.RED)
            return False
        except OSError as exc:
            self._set_offsets_status(f"Could not save the offsets: {exc}", W.RED)
            self._reload_after_failed_save()
            return False
        self._offsets_shown = {}                  # the editor shows the saved values anew
        self._notify_config_changed()
        self._load_offsets("drop")
        names = _join(f"R{rid + 1}" for rid in sorted(changed))
        self._set_offsets_status(f"Saved the offsets of {names} to config.toml.", W.GREEN)
        return True

    # ------------------------------------------------------------------
    # System Origin Calibration tab
    # ------------------------------------------------------------------

    def _set_sys_status(self, text: str, color: str = W.DIM) -> None:
        _set_status(self._sys_status, text, color)

    def _current_inputs(self) -> tuple[dict, dict, dict, list]:
        """``(slopes, intercepts, distances, problems)`` of the active
        receivers as the settings hold them now."""
        slopes, intercepts, distances, problems = {}, {}, {}, []
        for rid in range(self._n_active):
            o = self._read_offset(rid)
            d = self._settings.get_origin_distance(rid)
            slopes[rid] = o[0] if o is not None else None
            intercepts[rid] = o[1] if o is not None else None
            distances[rid] = d
            if o is None:
                problems.append(f"R{rid + 1} has no readable slope and intercept (Offsets tab)")
            elif o[0] <= 0:
                problems.append(f"R{rid + 1}'s slope is not above 0 (Offsets tab)")
            if d is None:
                problems.append(f"R{rid + 1} has no position on the arena map (Arena Maker)")
        return slopes, intercepts, distances, problems

    def _start_refusal(self) -> str:
        """Why a collection can't start now ("" if it can)."""
        if not self._link_up():
            return self._link_down_text("Can't collect")
        if self._origin.is_measuring():
            return "Can't collect while the origin is being measured: wait for it or cancel it."
        if self._cal.is_running:
            return "A collection is already running."
        return ""

    def _start_origin_run(self) -> None:
        """Collect at (0, 0) with the transmitter there."""
        run = self._origin_run
        if run is not None and run.collecting:
            return
        refusal = self._start_refusal()
        if refusal:
            self._set_sys_status(refusal, W.RED)
            return
        slopes, intercepts, distances, problems = self._current_inputs()
        if problems:
            self._set_sys_status("Can't calibrate: " + "; ".join(problems) + ".", W.RED)
            return
        try:
            reads = max(1, int(self._settings.calibration_reads))
        except (TypeError, ValueError):
            reads = 5
        if self._cal.config_path != self._settings.config_path:
            self._cal.config_path = self._settings.config_path
        self._run_token += 1
        token = self._run_token
        run = _OriginRun(token=token, reads=reads, ids=list(range(self._n_active)),
                         slopes=slopes, intercepts=intercepts, distances=distances)
        try:
            self._cal.clear_run_data(ORIGIN_RUN)
            self._cal.start_run(
                ORIGIN_RUN, reads,
                on_reading=lambda r, n: self._sig_reading.emit(token, r, n),
                on_complete=lambda r: self._sig_complete.emit(token, r),
                on_error=lambda r, msg: self._sig_failed.emit(token, r, str(msg)),
                on_tick=lambda r, ticks: self._sig_ticks.emit(token, r, list(ticks)))
        except (RuntimeError, ValueError) as exc:
            self._set_sys_status(f"Could not start collecting: {exc}", W.RED)
            return
        self._origin_run = run
        note = (" The Offsets tab has unsaved edits: the saved slopes are used."
                if self._offsets_dirty() else "")
        self._set_sys_status(f"Collecting at (0, 0): {reads} reads of the modal tick per "
                             f"receiver.{note}", W.AMBER if note else W.DIM)
        self._results_frame.setVisible(self._tabs.currentIndex() == TAB_SYSTEM)
        self._fill_origin_table()
        self._update_system_ui()
        self._draw_histograms()

    def _stop_origin_run(self, note: str = "") -> None:
        run = self._origin_run
        if run is None or not run.collecting:
            return
        self._cal.stop_run()
        run.collecting = False
        self._set_sys_status(note or "Stopped: nothing was saved. Press Start to collect again.",
                             W.AMBER)
        self._update_system_ui()

    def _discard_origin_run(self) -> None:
        run = self._origin_run
        if run is not None and run.collecting:
            self._cal.stop_run()
        self._origin_run = None
        self._results_frame.setVisible(False)
        self._hist.clear("Tick histograms")
        self._set_sys_status("Discarded: config.toml keeps its intercepts.")
        self._update_system_ui()

    def _validate_origin_run(self) -> None:
        """End a run whose receivers, slopes, distances to (0, 0) or (before
        it was saved) intercepts changed under it (in another panel)."""
        run = self._origin_run
        if run is None:
            return
        slopes, intercepts, distances, _problems = self._current_inputs()
        changed = []
        if list(range(self._n_active)) != run.ids:
            changed.append("number of receivers")
        else:
            if not _same(slopes, run.slopes):
                changed.append("slopes")
            if not _same(distances, run.distances):
                changed.append("receivers' distances to (0, 0)")
            if not run.saved and not _same(intercepts, run.intercepts):
                changed.append("intercepts")
        if not changed:
            return
        if run.collecting:
            self._cal.stop_run()
        self._origin_run = None
        self._results_frame.setVisible(False)
        self._hist.clear("Tick histograms")
        self._set_sys_status(f"The System Origin Calibration was ended: the {_join(changed)} "
                             f"changed. Press Start to collect again.", W.AMBER)

    def _run_for(self, token: int, run_num: int) -> _OriginRun | None:
        """The run collecting now if ``token``/``run_num`` are its own, else None."""
        run = self._origin_run
        if run is None or token != run.token or run_num != ORIGIN_RUN or not run.collecting:
            return None
        return run

    def _on_run_reading(self, token: int, run_num: int, reads: int) -> None:
        run = self._run_for(token, run_num)
        if run is None:
            return
        run.reads_seen = reads
        self._update_progress()
        self._fill_origin_table()
        if not self._hist_timer.isActive():
            self._hist_timer.start()

    def _on_run_ticks(self, token: int, run_num: int, ticks) -> None:
        run = self._run_for(token, run_num)
        if run is not None:
            run.latest = list(ticks)

    def _on_run_failed(self, token: int, run_num: int, message: str) -> None:
        run = self._run_for(token, run_num)
        if run is None:
            return
        run.collecting = False
        link = "" if self._link_up() else " " + self._link_down_text()
        self._set_sys_status(f"Collecting failed: {message}.{link} Press Start to try again.",
                             W.RED)
        self._update_system_ui()

    def _on_run_complete(self, token: int, run_num: int) -> None:
        run = self._run_for(token, run_num)
        if run is None:
            return
        run.collecting = False
        run.rows = self._compute_rows(run)
        self._fill_origin_table()
        self._update_system_ui()
        self._draw_histograms()
        usable, missing = run.usable(), run.missing()
        if not usable:
            self._set_sys_status(f"Done ({run.reads_seen} readings), but no receiver gave a "
                                 f"reading: nothing to save. Check the transmitter and press "
                                 f"Start again.", W.RED)
            return
        text = f"Done ({run.reads_seen} readings): new intercepts for {len(usable)} receivers."
        if missing:
            text += (f" {_join(f'R{rid + 1}' for rid in missing)} gave no reading and "
                     f"{'keeps its' if len(missing) == 1 else 'keep their'} intercept.")
        self._set_sys_status(text, W.AMBER if missing else W.GREEN)
        if self._shown:
            self._ask_save_intercepts()

    def _compute_rows(self, run: _OriginRun) -> dict:
        """Every receiver's modal tick, measured distance (tick × slope, the
        intercept taken as 0) and new intercept (map distance − measured)."""
        rows = {}
        for rid in run.ids:
            tick = self._cal.get_most_frequent(rid, ORIGIN_RUN)
            measured = new = None
            if tick:                                      # None: no data; 0: no echo
                measured = float(tick) * run.slopes[rid]
                new = run.distances[rid] - measured
            rows[rid] = _OriginRow(tick=tick, measured=measured, distance=run.distances[rid],
                                   current=run.intercepts[rid], new=new)
        return rows

    def _fill_origin_table(self) -> None:
        run = self._origin_run
        t = self._results_table
        if run is None:
            return
        t.setRowCount(len(run.ids))
        for row, rid in enumerate(run.ids):
            r = run.rows.get(rid)
            if r is None:                                 # live: the modal tick so far
                tick = self._cal.get_most_frequent(rid, ORIGIN_RUN)
                measured = float(tick) * run.slopes[rid] if tick else None
                new = change = None
            else:
                tick, measured, new, change = r.tick, r.measured, r.new, r.change
            _set_cell(t, row, 0, f"R{rid + 1}", W.CYAN, center=True)
            if tick is None:
                _set_cell(t, row, 1, "—", W.DIM)
            elif tick == 0:
                _set_cell(t, row, 1, "no echo", W.RED)
            else:
                _set_cell(t, row, 1, f"{tick:.0f}")
            _set_cell(t, row, 2, "—" if measured is None else f"{measured:.1f}",
                      "white" if measured is not None else W.DIM)
            _set_cell(t, row, 3, f"{run.distances[rid]:.1f}", W.DIM)
            _set_cell(t, row, 4, f"{run.intercepts[rid]:.2f}", W.DIM)
            if r is not None and new is None:
                _set_cell(t, row, 5, "no reading", W.RED,
                          "This receiver gave no reading: it keeps its intercept")
                _set_cell(t, row, 6, "—", W.RED)
            else:
                _set_cell(t, row, 5, "—" if new is None else f"{new:.2f}",
                          W.GREEN if new is not None else W.DIM)
                _set_cell(t, row, 6, "—" if new is None else _fmt_percent(change),
                          "white" if new is not None else W.DIM,
                          "" if change is not None or new is None else
                          "The current intercept is 0: no percent difference")
        title = "Recorded at (0, 0)"
        if run.collecting:
            title += ": collecting…"
        elif run.saved:
            title += ": saved"
        self._results_title.setText(title)

    def _ask_save_intercepts(self) -> bool:
        """Compare the current intercepts with the new ones and, if the user
        agrees, save the new ones (slopes unchanged)."""
        run = self._origin_run
        if run is None or not run.done or run.saved:
            return False
        usable = run.usable()
        if not usable:
            W.warning(self, "No new intercepts", "No receiver gave a reading at (0, 0): there "
                                                 "is nothing to save.")
            return False
        missing = run.missing()
        text = ("New y-intercepts from the transmitter at (0, 0): new intercept = the receiver's "
                "distance to (0, 0) on the arena map − modal tick × slope. Only the intercepts "
                "are saved; the slopes are not changed.")
        if missing:
            text += (f"\n\n{_join(f'R{rid + 1}' for rid in missing)} gave no reading and "
                     f"{'keeps its' if len(missing) == 1 else 'keep their'} intercept.")
        text += "\n\nSave the new intercepts to config.toml?"
        dlg = W.message_dialog(self, "Save the new intercepts?", text, W.CYAN,
                               [("Save intercepts", W.GREEN, "black", True),
                                ("Don't save", "#555555", "white", False)])
        table = _table(["", "Intercept cm\ncurrent", "Intercept cm\nnew", "Difference"],
                       len(usable), stretch=(1, 2, 3))
        for row, rid in enumerate(sorted(usable)):
            r = run.rows[rid]
            _set_cell(table, row, 0, f"R{rid + 1}", W.CYAN, center=True)
            _set_cell(table, row, 1, f"{r.current:.2f}")
            _set_cell(table, row, 2, f"{r.new:.2f}", W.GREEN)
            _set_cell(table, row, 3, _fmt_percent(r.change), tip=(
                "(new − current) / |current| × 100" if r.change is not None else
                "The current intercept is 0: no percent difference"))
        dlg.layout().insertWidget(2, table)
        dlg.adjustSize()
        if not W._run(dlg):
            self._set_sys_status("The new intercepts were not saved: press Save new "
                                 "intercepts… to review them again, or Discard.", W.DIM)
            return False
        return self._save_intercepts()

    def _save_intercepts(self) -> bool:
        run = self._origin_run
        if run is None or run.saved:
            return False
        if self._origin.is_measuring():
            self._set_sys_status("Not saved yet: wait for the origin measurement to finish.",
                                 W.RED)
            return False
        self._validate_origin_run()               # the settings may have moved meanwhile
        if self._origin_run is not run:
            return False
        offsets = {rid: {"slope": run.slopes[rid], "intercept": new}
                   for rid, new in run.usable().items()}
        try:
            self._settings.set_receiver_offsets(offsets)
        except ValueError as exc:
            self._set_sys_status(f"Not saved: {exc}", W.RED)
            return False
        except OSError as exc:
            self._set_sys_status(f"Could not save the intercepts: {exc}", W.RED)
            self._reload_after_failed_save()
            return False
        run.saved = True
        self._notify_config_changed()
        names = _join(f"R{rid + 1}" for rid in sorted(offsets))
        self._set_sys_status(f"Saved the new intercepts of {names} to config.toml (slopes "
                             f"unchanged).", W.GREEN)
        self._fill_origin_table()
        self._update_system_ui()
        return True

    def _update_progress(self) -> None:
        run = self._origin_run
        for rid, bar in enumerate(self._rx_bars[:self._n_active]):
            if run is None:
                bar.setRange(0, 1)
                bar.setValue(0)
                bar.setFormat(f"R{rid + 1}")
                continue
            count = self._cal.get_max_count(rid, ORIGIN_RUN)
            bar.setRange(0, run.reads)
            bar.setValue(min(count, run.reads))
            bar.setFormat(f"R{rid + 1}  {count}/{run.reads}")

    def _update_system_ui(self) -> None:
        """Buttons, prompt and progress of the System Origin Calibration tab."""
        run = self._origin_run
        collecting = run is not None and run.collecting
        up = self._link_up()
        refusal = self._start_refusal() if not collecting else ""
        self._sys_start_btn.setEnabled(not collecting and not refusal)
        self._sys_start_btn.setToolTip(refusal or "Collect with the transmitter at (0, 0)")
        self._sys_start_btn.setText("Start again" if run is not None and not collecting
                                    else "Start")
        self._sys_stop_btn.setEnabled(collecting)
        self._bars_box.setVisible(run is not None)
        if collecting:
            prompt = ("Collecting at (0, 0)… keep the transmitter still until every receiver's "
                      "green bar reaches the dashed line.")
        elif run is not None and run.saved:
            prompt = "The new intercepts are saved."
        elif run is not None and run.done:
            prompt = "Done: review the new intercepts and choose whether to save them."
        else:
            prompt = "Place the transmitter at (0, 0), then press Start."
        self._sys_prompt.setText(prompt)
        if not up and not collecting:
            self._sys_start_btn.setToolTip(self._link_down_text())
        self._update_progress()

        done = run is not None and run.done
        self._sys_result.setVisible(done)
        if done:
            usable, missing = run.usable(), run.missing()
            lines = []
            if usable:
                lines.append(_rich(f"New intercepts for {_join(f'R{r + 1}' for r in usable)} "
                                   f"(see the table under the arena).", W.GREEN))
            if missing:
                lines.append(_rich(f"{_join(f'R{r + 1}' for r in missing)}: no reading, "
                                   f"intercept kept.", W.AMBER))
            if run.saved:
                lines.append(_rich("Saved to config.toml.", W.GREEN))
            self._sys_summary.setText("<br>".join(lines))
            self._sys_review_btn.setEnabled(bool(usable) and not run.saved)
            self._sys_discard_btn.setText("Close" if run.saved else "Discard")
        if self._tabs.currentIndex() == TAB_SYSTEM:
            self._results_frame.setVisible(run is not None)
            self._refresh_arena()

    def _draw_histograms(self) -> None:
        """Redraw the histograms (only while they are on show)."""
        if self._tabs.currentIndex() != TAB_SYSTEM or not self._shown:
            return
        run = self._origin_run
        if run is None:
            self._hist.clear("Tick histograms: place the transmitter at (0, 0) and press Start")
            return
        self._hist.show(self._cal, ORIGIN_RUN, "At (0, 0): ticks per receiver", run.reads,
                        run.latest if run.collecting else None)
