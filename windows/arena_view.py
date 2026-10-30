"""Shared arena drawing for the panels' matplotlib plots.

``ArenaView`` draws one arena on a matplotlib ``Axes`` in the application's
dark style: the receivers (magenta dots labelled R1..Rn) joined by a cyan
outline, optional facing arrows and beam wedges, the (0, 0) marker with the
X/Y compass rose, named groups of points (calibration points, candidates, a
previewed origin, check points ...), named outlines (a room outline, receiver
footprints), a heat map under everything (calibration point quality,
receiver coverage) and the legend, which the user can drag out of the way.
Its setters update the existing artists in place, so a panel may call them as
often as it likes; the panel still decides when to redraw the canvas
(``draw_idle``).  The view owns its artists: never ``ax.clear()`` an arena
axes (the view would keep updating artists that are no longer drawn); clear a
group, shape set or heat map with its own ``clear_*`` instead.

``ArenaInteractor`` turns mouse events on the canvas into Qt signals: clicks in
arena coordinates, picking and dragging the points of draggable groups, and a
hover read-out of the coordinates (and the heat-map value) under the pointer.

``outline_order`` orders receivers round their centroid so the outline is a
closed, non-crossing polygon for any placement; for the default two-column
layout it is the old R1-R2-R3-R6-R5-R4 rectangle.  ``CompassRose`` is the X/Y
arrows at the origin, for panels that draw their own arena.

Coordinates are arena centimetres.  Receiver ids are 0-based; labels are
1-based (R1..R6).  Facings are degrees, 0 = +X, counter-clockwise; a receiver
without one is aimed at the centroid of all of them, as in
``ultragps_position.coverage``.  Heat maps use the simulator's grid
convention: ``Z[j, i]`` is the value at ``(xs[i], ys[j])`` with ``xs`` and
``ys`` ascending cell centres.
"""

from __future__ import annotations

import logging
import math

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

import matplotlib
from matplotlib.collections import Collection, LineCollection, PolyCollection
from matplotlib.colors import LinearSegmentedColormap, ListedColormap, Normalize, to_rgba, to_rgba_array
from matplotlib.patches import Patch, Wedge

from ultragps_position import coverage

log = logging.getLogger(__name__)

RECEIVER_COLOR = "#FF00FF"
OUTLINE_COLOR = "#00FFFF"
ORIGIN_COLOR = "#0080FF"
POINT_COLOR = "#39FF14"
TEXT_COLOR = "white"
LEGEND_STYLE = dict(facecolor="#222222", edgecolor="white", labelcolor="white")

#: Group name under which ``ArenaInteractor`` picks and drags the receivers.
RECEIVERS = "receivers"

#: Half-angle of an HC-SR04 receiver's beam (-6 dB), degrees.
DEFAULT_BEAM_DEG = 27.5

# Draw order: heat map < grid (1.5) < shapes < beams < outline < arrows
# < receivers < compass < point groups < legend < read-out.
_Z_HEATMAP = 1.0
_Z_SHAPES = 1.7
_Z_BEAMS = 1.8
_Z_OUTLINE = 2.0
_Z_FACING = 4.0
_Z_RECEIVERS = 5.0
_Z_COMPASS = 7.0
_Z_POINTS = 8.0
_Z_LEGEND = 15.0
_Z_READOUT = 20.0


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _split_positions(positions) -> tuple[list[int], np.ndarray]:
    """``[(id, (x, y))]`` (SettingsModule.get_tower_coordinates) or ``[(x, y)]``
    (or an (N, 2) array) -> (ids, (N, 2) float array).  A position with fewer
    than two numbers (``position = []`` in a hand-edited config) raises
    ValueError."""
    ids, xy = [], []
    for k, p in enumerate(positions):
        if len(p) == 2 and np.ndim(p[1]) == 1:          # (id, (x, y))
            if len(p[1]) < 2:
                raise ValueError(f"receiver position must be (x, y), got {list(p[1])!r}")
            ids.append(int(p[0]))
            xy.append((float(p[1][0]), float(p[1][1])))
        else:                                           # (x, y)
            if len(p) < 2:
                raise ValueError(f"receiver position must be (x, y), got {list(p)!r}")
            ids.append(k)
            xy.append((float(p[0]), float(p[1])))
    return ids, np.array(xy, dtype=float).reshape(-1, 2)


def outline_order(coords) -> list[int]:
    """Receivers in outline order: clockwise round their centroid, starting at
    the first one.

    ``coords`` is an (N, 2) array, ``[(x, y)]`` or ``[(id, (x, y))]``; the
    result indexes into it.  Sorting by angle round an interior point gives a
    closed outline that never crosses itself whatever the placement; close it
    by appending ``order[0]``.  For the default two-column layout (ids 0-2 on
    the left from bottom to top, 3-5 on the right) this is ``[0, 1, 2, 5, 4,
    3]``, the rectangle the panels have always drawn.  Collinear receivers
    are ordered along their line.  A receiver without a finite position is
    left out.
    """
    _, xy = _split_positions(coords)
    index = np.flatnonzero(np.all(np.isfinite(xy), axis=1))
    pts = xy[index]
    n = len(pts)
    if n < 3:
        return [int(i) for i in index]
    centred = pts - pts.mean(axis=0)
    singular = np.linalg.svd(centred, compute_uv=False)
    if singular[0] == 0.0 or singular[1] <= 1e-9 * singular[0]:
        # All on one line: walk along it
        axis = np.linalg.svd(centred)[2][0]
        proj = centred @ axis
        order = sorted(range(n), key=lambda k: (proj[k], k))
    else:
        angle = np.arctan2(centred[:, 1], centred[:, 0])
        radius = np.hypot(centred[:, 0], centred[:, 1])
        order = sorted(range(n), key=lambda k: (-round(float(angle[k]), 9), float(radius[k]), k))
        start = order.index(0)
        order = order[start:] + order[:start]
    return [int(index[k]) for k in order]


def outline_xy(coords) -> tuple[list[float], list[float]]:
    """Closed outline through the receivers: ``(xs, ys)`` ready for ``plot``."""
    _, xy = _split_positions(coords)
    order = outline_order(xy)
    if not order:
        return [], []
    order.append(order[0])
    return [float(xy[i, 0]) for i in order], [float(xy[i, 1]) for i in order]


def aim_at_centroid(coords) -> list[float]:
    """Facing (degrees, 0 = +x, counter-clockwise) of each receiver aimed at
    the centroid of all of them: the default when config.toml gives none
    (``ultragps_position.coverage.default_facing``, so the drawn beams agree
    with the coverage maps)."""
    _, xy = _split_positions(coords)
    ok = np.all(np.isfinite(xy), axis=1)
    out = np.full(len(xy), np.nan)                # no facing for a receiver that is nowhere
    out[ok] = coverage.default_facing(xy[ok])
    return [float(a) for a in out]


def resolve_facing(coords, facing_deg=None) -> list[float]:
    """Each receiver's facing: ``facing_deg[i]`` where given, else aimed at the
    centroid.  ``facing_deg`` may be None, contain None or NaN entries, or be
    shorter than the receiver list (the rest are aimed automatically)."""
    auto = aim_at_centroid(coords)
    if facing_deg is None:
        return auto
    out = []
    for i, a in enumerate(auto):
        f = facing_deg[i] if i < len(facing_deg) else None
        out.append(a if f is None or not math.isfinite(float(f)) else float(f))
    return out


# ---------------------------------------------------------------------------
# Colour maps
# ---------------------------------------------------------------------------

def quality_cmap(lower_is_better: bool = True, color: str = POINT_COLOR,
                 max_alpha: float = 0.85) -> LinearSegmentedColormap:
    """Transparent -> bright ``color`` ramp for "how good is this spot" shading.

    Without ``lower_is_better`` the low end (vmin) is transparent and the high
    end bright; with it (scores where lower is better, such as the calibration
    arena score) the ramp is reversed so the best values are the brightest.
    NaN is transparent.
    """
    r, g, b, _ = to_rgba(color)
    stops = [(0.0, (r, g, b, 0.0)),
             (0.45, (r, g, b, 0.18 * max_alpha)),
             (0.8, (r, g, b, 0.55 * max_alpha)),
             (1.0, (r, g, b, max_alpha))]
    cmap = LinearSegmentedColormap.from_list("ultragps_quality", stops)
    if lower_is_better:
        cmap = cmap.reversed()
    return cmap.with_extremes(bad=(0.0, 0.0, 0.0, 0.0))


def traffic_light_cmap(reverse: bool = False) -> LinearSegmentedColormap:
    """Red -> yellow -> green (low -> high); ``reverse`` for lower-is-better values."""
    cmap = LinearSegmentedColormap.from_list(
        "ultragps_traffic", ["#FF3030", "#FFD700", "#39FF14"])
    if reverse:
        cmap = cmap.reversed()
    return cmap.with_extremes(bad=(0.0, 0.0, 0.0, 0.0))


def coverage_colors(n_receivers: int = 6, needed: int = 3) -> list[str]:
    """One colour per receiver count 0..n_receivers: dark red for none, red
    below ``needed`` (no fix), yellow at exactly ``needed`` (a fix, no spare
    receiver to reject an outlier), yellow-green with one spare, green with two
    or more."""
    colors = []
    for k in range(n_receivers + 1):
        if k == 0:
            colors.append("#7A0000")
        elif k < needed:
            colors.append("#FF3030")
        elif k == needed:
            colors.append("#FFD700")
        elif k == needed + 1:
            colors.append("#B6FF00")
        else:
            colors.append("#39FF14")
    return colors


def coverage_cmap(n_receivers: int = 6, needed: int = 3) -> ListedColormap:
    """Discrete colormap for receiver-count maps; use it with
    ``vmin=-0.5, vmax=n_receivers + 0.5`` so each integer gets its own colour."""
    cmap = ListedColormap(coverage_colors(n_receivers, needed), name="ultragps_coverage")
    return cmap.with_extremes(bad=(0.0, 0.0, 0.0, 0.0))


def coverage_legend(n_receivers: int = 6, needed: int = 3) -> list[tuple[str, float]]:
    """``(label, count)`` legend entries matching ``coverage_colors``."""
    entries = [(f"< {needed} receivers (no fix)", max(needed - 1, 0)),
               (f"{needed} receivers", needed)]
    if n_receivers >= needed + 1:
        entries.append((f"{needed + 1} receivers", needed + 1))
    if n_receivers >= needed + 2:
        top = f"{needed + 2}" if n_receivers == needed + 2 else f"{needed + 2}-{n_receivers}"
        entries.append((f"{top} receivers", needed + 2))
    return entries


def finite_limits(Z, lo_pct: float = 0.0, hi_pct: float = 100.0) -> tuple[float, float]:
    """``(vmin, vmax)``: percentiles of the finite values of ``Z`` (0, 1 if none)."""
    values = np.asarray(Z, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return 0.0, 1.0
    vmin = float(np.percentile(values, lo_pct))
    vmax = float(np.percentile(values, hi_pct))
    if vmax <= vmin:
        vmax = vmin + max(abs(vmin) * 1e-6, 1e-9)
    return vmin, vmax


def _colormap(cmap):
    """A colormap object for a name or a colormap, with NaN transparent."""
    if isinstance(cmap, str):
        cmap = matplotlib.colormaps[cmap]
    return cmap.with_extremes(bad=(0.0, 0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# Axes styling and compass rose
# ---------------------------------------------------------------------------

def style_axes(ax, title: str | None = None, xlabel: str | None = "X Position (cm)",
               ylabel: str | None = "Y Position (cm)") -> None:
    """The arena look: black face, grey grid, white spines, ticks and labels,
    equal aspect."""
    ax.set_facecolor("black")
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.3, color="gray")
    if xlabel is not None:
        ax.set_xlabel(xlabel, color=TEXT_COLOR)
    if ylabel is not None:
        ax.set_ylabel(ylabel, color=TEXT_COLOR)
    if title is not None:
        ax.set_title(title, color=TEXT_COLOR)
    ax.tick_params(colors=TEXT_COLOR)
    for spine in ax.spines.values():
        spine.set_color(TEXT_COLOR)


class CompassRose:
    """X and Y arrows at the arena origin, 8 % of the visible span long.

    The arrows follow the axes limits by themselves (``xlim_changed`` /
    ``ylim_changed``) for as long as the ``CompassRose`` object lives
    (matplotlib holds those callbacks weakly, so keep a reference);
    ``update()`` resizes them on demand.
    """

    FRACTION = 0.08

    def __init__(self, ax, color: str = TEXT_COLOR, origin=(0.0, 0.0)):
        self.ax = ax
        self.origin = (float(origin[0]), float(origin[1]))
        arrow = dict(arrowstyle="->", color=color, lw=2, shrinkA=0, shrinkB=0)
        text = dict(color=color, fontsize=12, fontweight="bold", zorder=_Z_COMPASS)
        self._x_arrow = ax.annotate("", xy=(1.0, 0.0), xytext=(0.0, 0.0),
                                    arrowprops=arrow, zorder=_Z_COMPASS)
        self._y_arrow = ax.annotate("", xy=(0.0, 1.0), xytext=(0.0, 0.0),
                                    arrowprops=dict(arrow), zorder=_Z_COMPASS)
        self._x_text = ax.text(0.0, 0.0, "X", ha="center", va="top", **text)
        self._y_text = ax.text(0.0, 0.0, "Y", ha="right", va="center", **text)
        self._cids = [ax.callbacks.connect("xlim_changed", self._on_limits),
                      ax.callbacks.connect("ylim_changed", self._on_limits)]
        self.update()

    @property
    def artists(self) -> list:
        return [self._x_arrow, self._y_arrow, self._x_text, self._y_text]

    def update(self) -> None:
        """Size the arrows to the current axes limits."""
        x0, x1 = self.ax.get_xlim()
        y0, y1 = self.ax.get_ylim()
        al = min(abs(x1 - x0), abs(y1 - y0)) * self.FRACTION
        ox, oy = self.origin
        self._x_arrow.xy = (ox + al, oy)
        self._x_arrow.set_position((ox, oy))
        self._y_arrow.xy = (ox, oy + al)
        self._y_arrow.set_position((ox, oy))
        self._x_text.set_position((ox + al * 0.5, oy - al * 0.3))
        self._y_text.set_position((ox - al * 0.3, oy + al * 0.5))

    def set_visible(self, visible: bool) -> None:
        for artist in self.artists:
            artist.set_visible(visible)

    def visible(self) -> bool:
        return self._x_arrow.get_visible()

    def remove(self) -> None:
        """Take the compass off the axes for good."""
        for cid in self._cids:
            self.ax.callbacks.disconnect(cid)
        self._cids = []
        for artist in self.artists:
            try:
                artist.remove()
            except (ValueError, NotImplementedError):
                pass

    def _on_limits(self, _ax) -> None:
        self.update()


# ---------------------------------------------------------------------------
# Arena view
# ---------------------------------------------------------------------------

class _Group:
    """One named group of points and its artists."""

    def __init__(self, name: str):
        self.name = name
        self.xy = np.empty((0, 2))
        self.scatter = None
        self.texts: list = []
        self.marker = "D"
        self.legend: str | None = None
        self.draggable = False
        self.labels: list[str] = []


class _Shapes:
    """One named set of outlines (polygons or polylines) and its collection."""

    def __init__(self, name: str, closed: bool, collection):
        self.name = name
        self.closed = closed
        self.collection = collection
        self.vertices: list[np.ndarray] = []
        self.legend: str | None = None


class ArenaView:
    """One arena plot on a matplotlib ``Axes`` in the application's style.

    Args:
        ax:         The axes to draw on (styled here; its autoscaling is
                    switched off so overlays never move the limits).
        title:      Axes title, or None.
        xlabel, ylabel: Axis labels (None for none).
        legend_loc: Where the legend goes (matplotlib ``loc``).
        legend_anchor: Matplotlib ``bbox_to_anchor`` for the legend (axes
                    fraction), e.g. ``(1.02, 1.0)`` with ``legend_loc="upper
                    left"`` to put it outside the axes on the right; the figure
                    must leave room for it.  None: inside the axes.
        legend_ncol: Legend columns.
        padding:    Default margin round the receivers for ``set_limits``, cm.
        legend_draggable: Let the user drag the legend off whatever it covers
                    (a receiver in a corner, say); it keeps the place the user
                    gave it when it is rebuilt.  ``ArenaInteractor`` ignores
                    presses on the legend.
    """

    def __init__(self, ax, title: str | None = None, xlabel: str | None = "X Position (cm)",
                 ylabel: str | None = "Y Position (cm)", legend_loc: str = "upper right",
                 padding: float = 20.0, legend_anchor=None, legend_ncol: int = 1,
                 legend_draggable: bool = True):
        self.ax = ax
        self.padding = float(padding)
        self.legend_loc = legend_loc
        self.legend_anchor = legend_anchor
        self.legend_ncol = int(legend_ncol)
        self.legend_draggable = bool(legend_draggable)
        style_axes(ax, title, xlabel, ylabel)
        ax.set_autoscale_on(False)

        # Receivers
        self._rx_ids: list[int] = []
        self._rx_xy = np.empty((0, 2))
        self._rx_facing: list | None = None        # as given (None entries = auto)
        self._rx_show_beams = False
        self._rx_beam_deg = DEFAULT_BEAM_DEG
        self._rx_beam_range: float | None = None
        self._rx_show_coords = False
        self._rx_scatter = None
        self._rx_texts: list = []
        self._rx_outline = None
        self._rx_arrows: list = []
        self._rx_wedges: list = []
        self._rx_draggable = False

        # Origin marker and compass
        self._origin_dot = None
        self._origin_text = None
        self.compass = CompassRose(ax)

        self._groups: dict[str, _Group] = {}
        self._shapes: dict[str, _Shapes] = {}

        # Heat map
        self._heat_image = None
        self._heat: dict | None = None
        self._heat_legend: list = []                # [(rgba, label)]

        # Legend: the one drawn, where it was put, and where the user dragged it
        self._legend_visible = True
        self._legend_key = None
        self._legend = None
        self._legend_loc0 = None
        self._legend_user: tuple | None = None      # (loc, bbox_to_anchor)
        self._readout = ax.text(
            0.01, 0.01, "", transform=ax.transAxes, ha="left", va="bottom",
            color=TEXT_COLOR, fontsize=8, family="monospace", zorder=_Z_READOUT,
            bbox=dict(boxstyle="round,pad=0.25", facecolor="black", edgecolor="#555555", alpha=0.75))
        self._readout.set_visible(False)

        self.set_origin(True)
        self.set_limits()

    # ------------------------------------------------------------------
    # Receivers
    # ------------------------------------------------------------------

    def set_receivers(self, positions, facing_deg=None, show_beams: bool = False,
                      beam_deg: float = DEFAULT_BEAM_DEG, beam_range: float | None = None,
                      show_coords: bool = False) -> None:
        """Draw the receivers.

        Args:
            positions:   ``[(id, (x, y))]`` as ``SettingsModule.get_tower_coordinates``
                         returns them (or plain ``[(x, y)]``, ids 0..n-1).
            facing_deg:  None for no facing arrows, else one facing per receiver
                         (degrees, 0 = +x, counter-clockwise; None entries aim at
                         the centroid) drawn as an arrow.
            show_beams:  Draw each receiver's beam as a wedge of +/- ``beam_deg``
                         round its facing (aimed at the centroid where no facing
                         is given).
            beam_range:  Wedge radius in cm (default: 30 % of the receivers' extent).
            show_coords: Add the coordinates under each label.

        The limits are not changed: call ``set_limits`` to refit the view.
        """
        ids, xy = _split_positions(positions)
        self._rx_ids = ids
        self._rx_xy = xy
        self._rx_facing = None if facing_deg is None else [
            None if f is None else float(f) for f in facing_deg]
        self._rx_show_beams = bool(show_beams)
        self._rx_beam_deg = float(beam_deg)
        self._rx_beam_range = None if beam_range is None else float(beam_range)
        self._rx_show_coords = bool(show_coords)
        self._draw_receivers()
        self.update_legend()

    def receivers(self) -> list[tuple[int, tuple[float, float]]]:
        """The receivers as ``[(id, (x, y))]``."""
        return [(rid, (float(x), float(y))) for rid, (x, y) in zip(self._rx_ids, self._rx_xy)]

    def receiver_facing(self) -> list[float]:
        """Each receiver's facing as drawn (given or aimed at the centroid)."""
        return resolve_facing(self._rx_xy, self._rx_facing)

    def _receiver_label(self, i: int) -> str:
        label = f"R{self._rx_ids[i] + 1}"
        if self._rx_show_coords:
            x, y = self._rx_xy[i]
            label += f"\n({x:.1f}, {y:.1f})"
        return label

    def _draw_receivers(self) -> None:
        ax, xy = self.ax, self._rx_xy
        if self._rx_scatter is None:
            self._rx_scatter = ax.scatter(xy[:, 0], xy[:, 1], c=RECEIVER_COLOR, s=100,
                                          zorder=_Z_RECEIVERS, label="Receivers")
        else:
            self._rx_scatter.set_offsets(xy)

        labels = [self._receiver_label(i) for i in range(len(xy))]
        self._rx_texts = self._sync_texts(self._rx_texts, xy, labels, TEXT_COLOR, 10,
                                          _Z_RECEIVERS + 1, (6, 6))

        xs, ys = outline_xy(xy) if len(xy) >= 2 else ([], [])
        if self._rx_outline is None:
            self._rx_outline, = ax.plot(xs, ys, color=OUTLINE_COLOR, linewidth=2, alpha=0.7,
                                        zorder=_Z_OUTLINE, label="Receiver outline")
        else:
            self._rx_outline.set_data(xs, ys)

        finite = np.all(np.isfinite(xy), axis=1)
        pts = xy[finite]
        extent = max(float(np.ptp(pts[:, 0])) if len(pts) else 0.0,
                     float(np.ptp(pts[:, 1])) if len(pts) else 0.0, 50.0)
        facing = self.receiver_facing()
        # A receiver gets an arrow and a wedge only with a finite position and facing
        shown = [bool(finite[i]) and math.isfinite(facing[i]) for i in range(len(xy))]

        # Facing arrows
        want = len(xy) if self._rx_facing is not None else 0
        while len(self._rx_arrows) > want:
            self._rx_arrows.pop().remove()
        length = 0.09 * extent
        for i in range(want):
            x, y = (float(xy[i, 0]), float(xy[i, 1])) if shown[i] else (0.0, 0.0)
            t = math.radians(facing[i]) if shown[i] else 0.0
            tip = (x + length * math.cos(t), y + length * math.sin(t))
            if i < len(self._rx_arrows):
                arrow = self._rx_arrows[i]
                arrow.xy = tip
                arrow.set_position((x, y))
            else:
                arrow = ax.annotate(
                    "", xy=tip, xytext=(x, y), zorder=_Z_FACING,
                    arrowprops=dict(arrowstyle="-|>", color=RECEIVER_COLOR, lw=1.5,
                                    shrinkA=0, shrinkB=0))
                self._rx_arrows.append(arrow)
            arrow.set_visible(shown[i])

        # Beam wedges
        want = len(xy) if self._rx_show_beams else 0
        while len(self._rx_wedges) > want:
            self._rx_wedges.pop().remove()
        radius = self._rx_beam_range if self._rx_beam_range is not None else 0.3 * extent
        half = self._rx_beam_deg
        for i in range(want):
            if shown[i]:
                centre = (float(xy[i, 0]), float(xy[i, 1]))
                t1, t2 = facing[i] - half, facing[i] + half
            else:
                centre, t1, t2 = (0.0, 0.0), 0.0, 1.0
            if i < len(self._rx_wedges):
                wedge = self._rx_wedges[i]
                wedge.set_center(centre)
                wedge.set_radius(radius)
                wedge.set_theta1(t1)
                wedge.set_theta2(t2)
            else:
                wedge = Wedge(centre, radius, t1, t2, facecolor=to_rgba(RECEIVER_COLOR, 0.08),
                              edgecolor=to_rgba(RECEIVER_COLOR, 0.45), linewidth=1.0,
                              linestyle="--", zorder=_Z_BEAMS)
                ax.add_patch(wedge)
                self._rx_wedges.append(wedge)
            wedge.set_visible(shown[i])

    # ------------------------------------------------------------------
    # Origin, compass and limits
    # ------------------------------------------------------------------

    def set_origin(self, visible: bool = True) -> None:
        """Show or hide the (0, 0) marker."""
        if self._origin_dot is None:
            self._origin_dot = self.ax.scatter([0.0], [0.0], c=ORIGIN_COLOR, s=120,
                                               zorder=_Z_RECEIVERS)
            self._origin_text = self.ax.annotate(
                "(0, 0)", xy=(0.0, 0.0), xytext=(6, 6), textcoords="offset points",
                color=ORIGIN_COLOR, fontsize=9, fontweight="bold", zorder=_Z_RECEIVERS + 1)
        self._origin_dot.set_visible(visible)
        self._origin_text.set_visible(visible)

    def set_compass(self, visible: bool = True) -> None:
        """Show or hide the X/Y compass rose."""
        self.compass.set_visible(visible)

    def set_limits(self, padding: float | None = None, extra_points=(),
                   include_origin: bool = True, include_points: bool = False) -> None:
        """Fit the view to the receivers (and ``extra_points``) plus ``padding`` cm.

        ``include_origin`` keeps (0, 0) in view while its marker or the compass
        is shown; ``include_points`` also fits every point group and shape.
        """
        pad = self.padding if padding is None else float(padding)
        parts = [self._rx_xy, np.asarray(list(extra_points), dtype=float).reshape(-1, 2)]
        if include_origin and (self._origin_dot.get_visible() or self.compass.visible()):
            parts.append(np.zeros((1, 2)))
        if include_points:
            parts.extend(g.xy for g in self._groups.values())
            parts.extend(v for s in self._shapes.values() for v in s.vertices)
        pts = np.vstack(parts)
        pts = pts[np.all(np.isfinite(pts), axis=1)]
        if len(pts) == 0:
            pts = np.array([[-100.0, -100.0], [100.0, 100.0]])
        (x0, y0), (x1, y1) = pts.min(axis=0) - pad, pts.max(axis=0) + pad
        # Never collapse to a line or a point
        span = max(x1 - x0, y1 - y0, 2.0 * pad, 10.0)
        if x1 - x0 < 0.1 * span:
            cx = (x0 + x1) / 2
            x0, x1 = cx - 0.05 * span, cx + 0.05 * span
        if y1 - y0 < 0.1 * span:
            cy = (y0 + y1) / 2
            y0, y1 = cy - 0.05 * span, cy + 0.05 * span
        self.ax.set_xlim(x0, x1)
        self.ax.set_ylim(y0, y1)

    # ------------------------------------------------------------------
    # Point groups
    # ------------------------------------------------------------------

    def set_points(self, group: str, points, color=POINT_COLOR, marker: str = "D",
                   labels=None, size: float = 100.0, zorder: float = _Z_POINTS,
                   legend: str | None = None, draggable: bool | None = None,
                   edgecolor=None, text_color=None, fontsize: float = 9) -> None:
        """Draw (or update) the named group of points.

        Args:
            group:      Name, e.g. "cal", "candidate", "origin_preview", "check".
            points:     ``[(x, y), ...]`` (extra values per point are ignored).
            color:      One colour, or one per point; "none" for a hollow
                        marker (give it an ``edgecolor``).
            marker:     Matplotlib marker.
            labels:     None, a prefix ("P" gives P1..Pn) or one string per point.
            size:       Marker area in points^2 (scatter ``s``).
            legend:     Legend label for the group (None: not in the legend).
            draggable:  Let ``ArenaInteractor`` pick and drag these points
                        (None keeps the previous setting, False for a new group).
            edgecolor:  Marker edge colour (default: the face colour).
            text_color: Label colour (default: the point colour, or the edge
                        colour of a hollow marker).
        """
        xy = np.array([(float(p[0]), float(p[1])) for p in points], dtype=float).reshape(-1, 2)
        n = len(xy)
        g = self._groups.get(group)
        if g is not None and g.marker != marker:
            self.clear_points(group)
            g = None
        faces = _colors(color, n)
        edges = faces if edgecolor is None else _colors(edgecolor, n)
        if g is None:
            g = _Group(group)
            g.marker = marker
            g.scatter = self.ax.scatter(xy[:, 0], xy[:, 1], s=size, marker=marker,
                                        zorder=zorder, linewidths=1.0)
            self._groups[group] = g
        g.xy = xy
        g.scatter.set_offsets(xy)
        g.scatter.set_sizes(np.full(max(n, 1), float(size)))
        g.scatter.set_facecolors(faces)
        g.scatter.set_edgecolors(edges)
        g.scatter.set_zorder(zorder)
        g.legend = legend
        g.scatter.set_label(legend if legend else "_nolegend_")
        if draggable is not None:
            g.draggable = bool(draggable)

        if labels is None:
            g.labels = []
        elif isinstance(labels, str):
            g.labels = [f"{labels}{k + 1}" for k in range(n)]
        else:
            g.labels = [str(s) for s in labels][:n]
        if text_color is None:                    # a hollow marker's label takes its edge colour
            texts_c = np.where(faces[:, 3:4] > 0.0, faces, edges)
        else:
            texts_c = _colors(text_color, n)
        g.texts = self._sync_texts(g.texts, xy[:len(g.labels)], g.labels, texts_c, fontsize,
                                   zorder + 0.5, (7, 7))
        self.update_legend()

    def clear_points(self, group: str) -> None:
        """Remove the named group (no-op if it does not exist)."""
        g = self._groups.pop(group, None)
        if g is None:
            return
        for artist in [g.scatter] + g.texts:
            try:
                artist.remove()
            except (ValueError, NotImplementedError):
                pass
        self.update_legend()

    def has_points(self, group: str) -> bool:
        return group in self._groups

    def groups(self) -> list[str]:
        """Names of the point groups, in drawing order."""
        return list(self._groups)

    def points(self, group: str) -> list[tuple[float, float]]:
        """The group's points (``RECEIVERS`` gives the receiver positions)."""
        if group == RECEIVERS:
            xy = self._rx_xy
        else:
            g = self._groups.get(group)
            xy = g.xy if g is not None else np.empty((0, 2))
        return [(float(x), float(y)) for x, y in xy]

    def point_label(self, group: str, index: int) -> str:
        """The label drawn next to a point ("" if none)."""
        if group == RECEIVERS:
            return f"R{self._rx_ids[index] + 1}" if 0 <= index < len(self._rx_ids) else ""
        g = self._groups.get(group)
        if g is None or not 0 <= index < len(g.labels):
            return ""
        return g.labels[index]

    def move_point(self, group: str, index: int, x: float, y: float) -> None:
        """Move one point of a group (or receiver ``index`` for ``RECEIVERS``)
        without touching anything else; used while dragging."""
        if group == RECEIVERS:
            if 0 <= index < len(self._rx_xy):
                self._rx_xy[index] = (x, y)
                self._draw_receivers()
            return
        g = self._groups.get(group)
        if g is None or not 0 <= index < len(g.xy):
            return
        g.xy[index] = (x, y)
        g.scatter.set_offsets(g.xy)
        if index < len(g.texts):
            g.texts[index].xy = (x, y)

    def set_draggable(self, group: str, draggable: bool = True) -> None:
        """Let ``ArenaInteractor`` drag the group (``RECEIVERS`` for the receivers)."""
        if group == RECEIVERS:
            self._rx_draggable = bool(draggable)
        elif group in self._groups:
            self._groups[group].draggable = bool(draggable)

    def draggable_groups(self) -> list[str]:
        """Groups ``ArenaInteractor`` may pick, the receivers first."""
        names = [RECEIVERS] if self._rx_draggable and len(self._rx_xy) else []
        return names + [name for name, g in self._groups.items() if g.draggable and len(g.xy)]

    def marker_radius_px(self, group: str) -> float:
        """Radius of the group's markers on screen, in display pixels."""
        if group == RECEIVERS:
            artist = self._rx_scatter
        else:
            g = self._groups.get(group)
            artist = g.scatter if g is not None else None
        if artist is None or len(artist.get_sizes()) == 0:
            return 0.0
        return math.sqrt(float(np.max(artist.get_sizes()))) / 2.0 * self.ax.figure.dpi / 72.0

    # ------------------------------------------------------------------
    # Shapes
    # ------------------------------------------------------------------

    def set_shapes(self, name: str, shapes, color=OUTLINE_COLOR, closed: bool = True,
                   fill_alpha: float = 0.0, linewidth: float = 1.5, linestyle="-",
                   alpha: float = 1.0, legend: str | None = None,
                   zorder: float = _Z_SHAPES) -> None:
        """Draw (or update) the named set of outlines, under the receivers.

        Args:
            name:       E.g. "arena" (a room outline the user clicked), or
                        "footprints" (``ultragps_position.coverage.footprint``
                        of every receiver).
            shapes:     One ``(K, 2)`` list of vertices, or a list of them.  A
                        polygon need not repeat its first vertex.
            color:      One colour, or one per shape.
            closed:     Polygons (True) or open polylines (False).
            fill_alpha: Fill opacity of polygons (0: outline only).
            linewidth:  Line width in points; ``linestyle`` as matplotlib's.
            alpha:      Opacity of the lines.
            legend:     Legend label for the set (None: not in the legend).
        """
        verts = _shape_list(shapes)
        s = self._shapes.get(name)
        if s is not None and s.closed != bool(closed):
            self.clear_shapes(name)
            s = None
        edges = _colors(color, len(verts))
        edges[:, 3] *= float(alpha)
        if s is None:
            collection = (PolyCollection(verts, closed=True) if closed
                          else LineCollection(verts))
            self.ax.add_collection(collection, autolim=False)
            s = _Shapes(name, bool(closed), collection)
            self._shapes[name] = s
        elif closed:
            s.collection.set_verts(verts)
        else:
            s.collection.set_segments(verts)
        collection = s.collection
        collection.set_edgecolor(edges)
        if closed:
            faces = _colors(color, len(verts))
            faces[:, 3] *= float(fill_alpha)
            collection.set_facecolor(faces)
        collection.set_linewidth(float(linewidth))
        collection.set_linestyle(linestyle)
        collection.set_zorder(zorder)
        collection.set_label(legend if legend else "_nolegend_")
        s.vertices = verts
        s.legend = legend
        self.update_legend()

    def clear_shapes(self, name: str) -> None:
        """Remove the named set of outlines (no-op if it does not exist)."""
        s = self._shapes.pop(name, None)
        if s is None:
            return
        try:
            s.collection.remove()
        except (ValueError, NotImplementedError):
            pass
        self.update_legend()

    def has_shapes(self, name: str) -> bool:
        return name in self._shapes

    # ------------------------------------------------------------------
    # Heat map
    # ------------------------------------------------------------------

    def set_heatmap(self, xs, ys, Z, cmap="viridis", vmin: float | None = None,
                    vmax: float | None = None, alpha: float = 0.6, legend=None, fmt=None,
                    posinf: str = "max", interpolation: str = "nearest") -> None:
        """Shade the arena with ``Z[j, i]`` at ``(xs[i], ys[j])`` (cell centres,
        ascending) under the receivers and points.

        Args:
            cmap:     Colormap or its name (NaN is always transparent).
            vmin, vmax: Colour limits (default: the finite range of ``Z``).
            alpha:    Opacity of the whole image (multiplies the colormap's).
            legend:   ``[(label, value)]`` legend entries in the colour of ``value``.
            fmt:      ``fmt(value) -> str`` for the hover read-out; it is not
                      called for a NaN cell (masked, e.g. outside the room),
                      which reads "n/a".
            posinf:   "max" draws +inf in the top colour (no fix: the worst),
                      "bad" leaves it transparent.
        """
        xs = np.asarray(xs, dtype=float).ravel()
        ys = np.asarray(ys, dtype=float).ravel()
        Z = np.array(Z, dtype=float)
        if Z.shape != (len(ys), len(xs)) or Z.size == 0:
            raise ValueError(f"Z must have shape (len(ys), len(xs)) = ({len(ys)}, {len(xs)}), "
                             f"got {Z.shape}")
        auto_lo, auto_hi = finite_limits(Z)
        vmin = auto_lo if vmin is None else float(vmin)
        vmax = auto_hi if vmax is None else float(vmax)
        if vmax <= vmin:
            vmax = vmin + 1e-9
        shown = Z.copy()
        shown[np.isposinf(shown)] = vmax if posinf == "max" else np.nan
        shown[np.isneginf(shown)] = vmin
        cm = _colormap(cmap)
        data = np.ma.masked_invalid(shown)
        extent = _cell_extent(xs, ys)
        if self._heat_image is None:
            xlim, ylim = self.ax.get_xlim(), self.ax.get_ylim()
            self._heat_image = self.ax.imshow(
                data, cmap=cm, vmin=vmin, vmax=vmax, extent=extent, origin="lower",
                interpolation=interpolation, alpha=alpha, zorder=_Z_HEATMAP, aspect="equal")
            self.ax.set_xlim(xlim)
            self.ax.set_ylim(ylim)
        else:
            im = self._heat_image
            im.set_data(data)
            im.set_extent(extent)
            im.set_cmap(cm)
            im.set_clim(vmin, vmax)
            im.set_alpha(alpha)
            im.set_interpolation(interpolation)
            im.set_visible(True)
        self._heat = dict(xs=xs, ys=ys, Z=Z, fmt=fmt, extent=extent)
        norm = Normalize(vmin, vmax)
        self._heat_legend = [(tuple(float(c) for c in cm(norm(value))), float(alpha), str(label))
                             for label, value in (legend or [])]
        self.update_legend()

    def set_quality(self, xs, ys, Z, lower_is_better: bool = True, hi_pct: float = 50.0,
                    legend_label: str | None = "Better spot (greener)", fmt=None,
                    vmin: float | None = None, vmax: float | None = None) -> None:
        """Green "best spots" shading: brightest at the best value, fading to
        transparent at the ``hi_pct`` percentile of the finite values (the worse
        rest stays unshaded).  NaN and inf (not a usable spot) stay transparent.

        For a raw ``ultragps_calibration.points.quality_map`` score (lower is
        better) the defaults fit; for its ``goodness`` (0..1, higher is better)
        pass ``lower_is_better=False, vmin=0.0, vmax=1.0``.  Explicit ``vmin`` /
        ``vmax`` replace the percentile limits.
        """
        Z = np.asarray(Z, dtype=float)
        if lower_is_better:
            lo, hi = finite_limits(Z, 0.0, hi_pct)
        else:
            lo, hi = finite_limits(Z, 100.0 - hi_pct, 100.0)
        vmin = lo if vmin is None else float(vmin)
        vmax = hi if vmax is None else float(vmax)
        best = vmin if lower_is_better else vmax
        legend = [(legend_label, best)] if legend_label else None
        self.set_heatmap(xs, ys, Z, quality_cmap(lower_is_better), vmin, vmax, alpha=1.0,
                         legend=legend, fmt=fmt, posinf="bad")

    def set_coverage(self, xs, ys, counts, n_receivers: int = 6, needed: int = 3,
                     alpha: float = 0.35, legend: bool = True, fmt=None) -> None:
        """Receiver-count shading: red where fewer than ``needed`` receivers
        cover a point, yellow at exactly ``needed``, greens above."""
        if fmt is None:
            def fmt(v):
                count = f"{v:.0f}"
                return f"{count} receiver" + ("" if count == "1" else "s")
        entries = coverage_legend(n_receivers, needed) if legend else None
        self.set_heatmap(xs, ys, counts, coverage_cmap(n_receivers, needed), -0.5,
                         n_receivers + 0.5, alpha=alpha, legend=entries, fmt=fmt, posinf="bad")

    def clear_heatmap(self) -> None:
        """Remove the heat map and its legend entries."""
        if self._heat_image is not None:
            self._heat_image.remove()
            self._heat_image = None
        self._heat = None
        self._heat_legend = []
        self.update_legend()

    def has_heatmap(self) -> bool:
        return self._heat is not None

    def heatmap_value(self, x: float, y: float) -> float | None:
        """The heat-map value of the cell holding (x, y), or None outside it."""
        if self._heat is None:
            return None
        x0, x1, y0, y1 = self._heat["extent"]
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return None
        xs, ys = self._heat["xs"], self._heat["ys"]
        i = int(np.argmin(np.abs(xs - x)))
        j = int(np.argmin(np.abs(ys - y)))
        return float(self._heat["Z"][j, i])

    def heatmap_text(self, x: float, y: float) -> str:
        """The heat-map value at (x, y) as the read-out shows it ("" outside
        the map, "n/a" on a NaN cell)."""
        value = self.heatmap_value(x, y)
        if value is None:
            return ""
        if math.isnan(value):
            return "n/a"
        fmt = self._heat.get("fmt")
        if fmt is not None:
            try:
                return str(fmt(value))
            except Exception:                 # never let a formatter break the hover
                log.exception("Heat-map formatter failed for %r", value)
        return f"{value:.3g}"

    # ------------------------------------------------------------------
    # Legend and read-out
    # ------------------------------------------------------------------

    def set_legend_visible(self, visible: bool) -> None:
        self._legend_visible = bool(visible)
        self.update_legend(force=True)

    def set_legend_location(self, loc="upper right", anchor=None, ncol: int | None = None) -> None:
        """Put the legend at matplotlib ``loc`` (relative to ``anchor``, a
        ``bbox_to_anchor`` in axes fractions; None: the axes), forgetting where
        the user dragged it."""
        self.legend_loc = loc
        self.legend_anchor = anchor
        if ncol is not None:
            self.legend_ncol = int(ncol)
        self._legend_user = None
        self._legend = None                   # its place is not to be carried over
        self.update_legend(force=True)

    def update_legend(self, force: bool = False) -> None:
        """Rebuild the legend from the receivers, the outline, the labelled
        point groups and shapes and the heat-map entries (only when they
        changed)."""
        handles, labels = [], []
        if self._rx_scatter is not None and len(self._rx_xy):
            handles.append(self._rx_scatter)
            labels.append("Receivers")
        if self._rx_outline is not None and len(self._rx_xy) >= 2:
            handles.append(self._rx_outline)
            labels.append("Receiver outline")
        for g in self._groups.values():
            if g.legend and len(g.xy):
                handles.append(g.scatter)
                labels.append(g.legend)
        for s in self._shapes.values():
            if s.legend and s.vertices:
                handles.append(s.collection)
                labels.append(s.legend)
        # Artists by identity and look, heat-map entries by colour
        signature = [_handle_signature(h) for h in handles]
        for rgba, alpha, label in self._heat_legend:
            handles.append(Patch(facecolor=rgba, edgecolor="none", alpha=alpha))
            labels.append(label)
            signature.append((rgba, alpha))
        key = (self._legend_visible, tuple(labels), tuple(signature))
        if key == self._legend_key and not force:
            return
        self._legend_key = key
        loc, anchor = self._legend_placement()
        old = self.ax.get_legend()
        if old is not None:
            old.set_draggable(False)
            old.remove()
        self._legend = None
        if not handles or not self._legend_visible:
            return
        legend = self.ax.legend(handles, labels, loc=loc, fontsize=8, framealpha=0.85,
                                bbox_to_anchor=anchor, ncol=self.legend_ncol, **LEGEND_STYLE)
        legend.set_zorder(_Z_LEGEND)          # over the receiver and point labels
        if self.legend_draggable:
            legend.set_draggable(True)
        self._legend = legend
        self._legend_loc0 = getattr(legend, "_loc", None)

    def legend_contains(self, event) -> bool:
        """Whether the mouse ``event`` is on the visible legend."""
        legend = self.ax.get_legend()
        if legend is None or not legend.get_visible():
            return False
        try:
            return bool(legend.contains(event)[0])
        except Exception:                     # an event from another canvas, say
            return False

    def _legend_placement(self) -> tuple:
        """``(loc, bbox_to_anchor)`` for a new legend: where the user dragged
        the current one (matplotlib then stores its lower-left corner in
        ``_loc``, relative to its ``bbox_to_anchor``), else the configured place."""
        old = self._legend
        if old is not None and old is self.ax.get_legend():
            loc = getattr(old, "_loc", None)
            if isinstance(loc, tuple) and loc != self._legend_loc0:
                # A new legend reads its anchor box in axes fractions
                box = self.ax.transAxes.inverted().transform_bbox(old.get_bbox_to_anchor())
                self._legend_user = (loc, tuple(float(v) for v in box.bounds))
        if self._legend_user is not None:
            return self._legend_user
        return self.legend_loc, self.legend_anchor

    def set_readout(self, text: str) -> bool:
        """Text in the lower-left corner of the axes ("" hides it).  Returns
        whether it changed (and so whether the canvas needs a redraw)."""
        if text == self._readout.get_text() and bool(text) == self._readout.get_visible():
            return False
        self._readout.set_text(text)
        self._readout.set_visible(bool(text))
        return True

    def readout(self) -> str:
        return self._readout.get_text() if self._readout.get_visible() else ""

    def set_title(self, title: str) -> None:
        self.ax.set_title(title, color=TEXT_COLOR)

    def draw_idle(self) -> None:
        """Ask the canvas for a redraw."""
        canvas = self.ax.figure.canvas
        if canvas is not None:
            canvas.draw_idle()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _sync_texts(self, texts: list, xy: np.ndarray, labels: list, colors, fontsize: float,
                    zorder: float, offset: tuple) -> list:
        """Reuse ``texts`` (annotations at offset points) for ``labels`` at ``xy``."""
        n = len(labels)
        while len(texts) > n:
            texts.pop().remove()
        colors = _colors(colors, n) if n else []
        for i in range(n):
            x, y = float(xy[i, 0]), float(xy[i, 1])
            if i < len(texts):
                text = texts[i]
                text.xy = (x, y)
                text.set_text(labels[i])
                text.set_color(colors[i])
                text.set_fontsize(fontsize)
                text.set_zorder(zorder)
            else:
                texts.append(self.ax.annotate(
                    labels[i], xy=(x, y), xytext=offset, textcoords="offset points",
                    color=colors[i], fontsize=fontsize, fontweight="bold", zorder=zorder,
                    ha="left", va="bottom"))
        return texts


def _colors(color, n: int) -> np.ndarray:
    """``color`` (one colour or one per point) as an (n, 4) RGBA array;
    "none" is fully transparent."""
    if isinstance(color, str) and color.strip().lower() == "none":
        return np.zeros((n, 4))                       # to_rgba_array gives no row for "none"
    arr = to_rgba_array(color)
    if len(arr) == 1:
        return np.repeat(arr, n, axis=0)
    if len(arr) < n:
        raise ValueError(f"{len(arr)} colours for {n} points")
    return arr[:n]


def _handle_signature(artist) -> tuple:
    """Identity and look of a legend handle.  A legend entry copies the look
    of its artist when the legend is made, so a restyled artist (a group
    given another colour, say) needs a new legend."""
    if not isinstance(artist, Collection):            # the outline: never restyled
        return (id(artist),)
    first = [np.asarray(artist.get_facecolor(), dtype=float).reshape(-1)[:4],
             np.asarray(artist.get_edgecolor(), dtype=float).reshape(-1)[:4],
             np.asarray(artist.get_linewidth(), dtype=float).reshape(-1)[:1]]
    if hasattr(artist, "get_sizes"):
        first.append(np.asarray(artist.get_sizes(), dtype=float).reshape(-1)[:1])
    return (id(artist),) + tuple(tuple(np.round(v, 4).tolist()) for v in first) + (
        repr(artist.get_linestyle()[:1]),)


def _shape_list(shapes) -> list[np.ndarray]:
    """``shapes`` (one ``(K, 2)`` vertex list or a list of them) as a list of
    ``(K, 2)`` float arrays."""
    if isinstance(shapes, np.ndarray):
        if shapes.size == 0:
            return []
        if shapes.ndim == 2:
            return [shapes.astype(float).reshape(-1, 2)]
        return [np.asarray(v, dtype=float).reshape(-1, 2) for v in shapes]
    shapes = list(shapes)
    if not shapes:
        return []
    first = np.asarray(shapes[0], dtype=float)
    if first.ndim == 1:                               # one shape given as [(x, y), ...]
        return [np.asarray(shapes, dtype=float).reshape(-1, 2)]
    return [np.asarray(v, dtype=float).reshape(-1, 2) for v in shapes]


def _cell_extent(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float, float, float]:
    """Image extent (cell edges) of a grid of ascending cell centres."""
    hx = (xs[-1] - xs[0]) / (len(xs) - 1) / 2 if len(xs) > 1 else None
    hy = (ys[-1] - ys[0]) / (len(ys) - 1) / 2 if len(ys) > 1 else None
    hx = hx if hx else (hy or 0.5)
    hy = hy if hy else hx
    return (float(xs[0] - hx), float(xs[-1] + hx), float(ys[0] - hy), float(ys[-1] + hy))


# ---------------------------------------------------------------------------
# Mouse interaction
# ---------------------------------------------------------------------------

class ArenaInteractor(QObject):
    """Mouse interaction with an ``ArenaView`` on a matplotlib Qt canvas.

    Signals (arena cm; ``button`` 1 = left, 2 = middle, 3 = right):

    ``clicked(x, y, button)``
        A press on the arena that is not on a draggable point (any press
        while ``pick_enabled`` is off).
    ``double_clicked(x, y, button)``
        The second press of a double click (``clicked`` came with the first).
    ``point_pressed(group, index, button)``
        A press on a point of a draggable group; with the left button (and
        ``drag_enabled``) it starts a drag.
    ``point_dragged(group, index, x, y)``
        While dragging, each time the point moves.  The view has already
        moved it (unless ``move_points`` is False).
    ``drag_finished(group, index, x, y)``
        The release that ends a drag that moved the point.
    ``hovered(x, y)``
        The pointer moved over the arena (no button dragging a point).
    ``left()``
        The pointer left the arena.

    Points are picked within ``pick_px`` logical pixels at scale 1 (scaled
    with a ``ScalingCanvas``'s factor, and with the figure DPI otherwise) or
    within their marker, whichever is larger; a point without a finite
    position is never picked.  With ``snap`` (cm) every coordinate is rounded
    to that step.  The read-out in the lower-left corner shows the
    coordinates under the pointer and the heat-map value there.  A press on
    the legend is left to the legend (the user drags it away): it is neither
    a click nor a pick.

    ``pick_enabled`` (default True) can be switched off while a "Pick on
    arena" mode routes the next click elsewhere: every press is then a
    ``clicked``, even one on a draggable point (say, a receiver chosen as the
    new origin), and nothing is dragged.  ``drag_enabled`` False keeps the
    picking (``point_pressed``) but never drags.

    Created disabled: panels call ``enable()`` in ``on_panel_show`` and
    ``disable()`` in ``on_panel_hide``.
    """

    clicked = pyqtSignal(float, float, int)
    double_clicked = pyqtSignal(float, float, int)
    point_pressed = pyqtSignal(str, int, int)
    point_dragged = pyqtSignal(str, int, float, float)
    drag_finished = pyqtSignal(str, int, float, float)
    hovered = pyqtSignal(float, float)
    left = pyqtSignal()

    #: Pixels (at 100 dpi) the pointer must travel before a press becomes a drag.
    DRAG_START_PX = 3.0

    def __init__(self, canvas, view: ArenaView, snap: float | None = None,
                 pick_px: float = 10.0, readout: bool = True, parent: QObject | None = None):
        super().__init__(parent if parent is not None else canvas)
        self._canvas = canvas
        self._view = view
        self._snap = None
        self.snap = snap
        self.pick_px = float(pick_px)
        self._readout = bool(readout)
        self.pick_enabled = True
        self.drag_enabled = True
        self.move_points = True
        self._formatter = None
        self._cids: list[int] = []
        self._drag: dict | None = None
        self._legend_press = False                # a press on the legend is in progress
        self._inside = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @property
    def view(self) -> ArenaView:
        return self._view

    @property
    def snap(self) -> float | None:
        """Snapping step in cm (None: no snapping)."""
        return self._snap

    @snap.setter
    def snap(self, step: float | None) -> None:
        if step is not None and (not math.isfinite(float(step)) or float(step) <= 0):
            raise ValueError("snap must be None or a positive step in cm")
        self._snap = None if step is None else float(step)

    @property
    def readout(self) -> bool:
        """Show the coordinates under the pointer in the lower-left corner."""
        return self._readout

    @readout.setter
    def readout(self, on: bool) -> None:
        self._readout = bool(on)
        if not self._readout and self._view.set_readout(""):
            self._canvas.draw_idle()

    def set_readout_formatter(self, fn) -> None:
        """``fn(x, y) -> str`` for the hover read-out (None: the default)."""
        self._formatter = fn

    def is_enabled(self) -> bool:
        return bool(self._cids)

    def dragging(self) -> tuple[str, int] | None:
        """``(group, index)`` of the point being dragged, or None."""
        if self._drag is None or not self._drag["moved"]:
            return None
        return self._drag["group"], self._drag["index"]

    def enable(self) -> None:
        """Start listening to the canvas's mouse events."""
        if self._cids:
            return
        connect = self._canvas.mpl_connect
        self._cids = [
            connect("button_press_event", self._on_press),
            connect("button_release_event", self._on_release),
            connect("motion_notify_event", self._on_motion),
            connect("axes_leave_event", self._on_leave),
            connect("figure_leave_event", self._on_leave),
        ]

    def disable(self) -> None:
        """Stop listening; drops a drag in progress and the read-out."""
        for cid in self._cids:
            self._canvas.mpl_disconnect(cid)
        self._cids = []
        self._drag = None
        self._legend_press = False
        self._inside = False
        if self._view.set_readout(""):
            self._canvas.draw_idle()

    def cancel_drag(self) -> None:
        """Forget a drag in progress (the point stays where it is)."""
        self._drag = None

    # ------------------------------------------------------------------
    # Coordinates
    # ------------------------------------------------------------------

    def snap_xy(self, x: float, y: float) -> tuple[float, float]:
        """(x, y) rounded to the snapping step (unchanged without one)."""
        if self._snap is None:
            return float(x), float(y)
        s = self._snap
        return round(round(x / s) * s, 6), round(round(y / s) * s, 6)

    def _raw_xy(self, event, allow_outside: bool = False) -> tuple[float, float] | None:
        ax = self._view.ax
        if event.inaxes is ax and event.xdata is not None and event.ydata is not None:
            return float(event.xdata), float(event.ydata)
        if allow_outside and event.x is not None and event.y is not None:
            x, y = ax.transData.inverted().transform((event.x, event.y))
            return float(x), float(y)
        return None

    def _scale(self) -> float:
        """Display pixels per logical pixel at scale 1 (event coordinates are
        display pixels: physical ones on a high-DPI screen)."""
        factor = getattr(self._canvas, "scale_factor", None)
        if factor is None:                        # a plain canvas: its DPI says it all
            return self._view.ax.figure.dpi / 100.0
        return float(factor) * float(getattr(self._canvas, "device_pixel_ratio", 1) or 1)

    def hit_test(self, px: float, py: float) -> tuple[str, int] | None:
        """``(group, index)`` of the draggable point nearest to display pixel
        (px, py) within the pick radius, or None."""
        ax = self._view.ax
        best = None
        radius = self.pick_px * self._scale()
        for name in self._view.draggable_groups():
            pts = self._view.points(name)
            if not pts:
                continue
            disp = ax.transData.transform(np.asarray(pts, dtype=float))
            dist = np.hypot(disp[:, 0] - px, disp[:, 1] - py)
            dist[~np.isfinite(dist)] = np.inf     # an unplaced point (NaN) is never picked
            i = int(np.argmin(dist))
            if dist[i] <= max(radius, self._view.marker_radius_px(name)):
                if best is None or dist[i] < best[2]:
                    best = (name, i, float(dist[i]))
        return None if best is None else (best[0], best[1])

    # ------------------------------------------------------------------
    # Matplotlib event handlers
    # ------------------------------------------------------------------

    def _on_press(self, event) -> None:
        if self._view.legend_contains(event):
            self._legend_press = True             # matplotlib drags the legend
            return
        raw = self._raw_xy(event)
        if raw is None:
            return
        button = int(event.button) if event.button is not None else 0
        x, y = self.snap_xy(*raw)
        if event.dblclick:
            self.double_clicked.emit(x, y, button)
            return
        hit = self.hit_test(event.x, event.y) if self.pick_enabled else None
        if hit is None:
            self.clicked.emit(x, y, button)
            return
        group, index = hit
        self.point_pressed.emit(group, index, button)
        if button == 1 and self.drag_enabled:
            pts = self._view.points(group)
            if index < len(pts):
                px_, py_ = pts[index]
                self._drag = dict(group=group, index=index, dx=px_ - raw[0], dy=py_ - raw[1],
                                  start=(event.x, event.y), moved=False, last=(px_, py_))

    def _on_motion(self, event) -> None:
        if self._legend_press:
            return
        drag = self._drag
        if drag is not None:
            self._drag_to(event, drag)
            return
        raw = self._raw_xy(event)
        if raw is None:
            if self._inside:
                self._on_leave(event)
            return
        x, y = self.snap_xy(*raw)
        self._inside = True
        if self.readout and self._view.set_readout(self._format(x, y)):
            self._canvas.draw_idle()
        self.hovered.emit(x, y)

    def _drag_to(self, event, drag: dict) -> None:
        raw = self._raw_xy(event, allow_outside=True)
        if raw is None:
            return
        if not drag["moved"]:
            sx, sy = drag["start"]
            if math.hypot(event.x - sx, event.y - sy) < self.DRAG_START_PX * self._scale():
                return
            drag["moved"] = True
        x, y = self.snap_xy(raw[0] + drag["dx"], raw[1] + drag["dy"])
        if (x, y) == drag["last"]:
            return
        drag["last"] = (x, y)
        group, index = drag["group"], drag["index"]
        if self.move_points:
            self._view.move_point(group, index, x, y)
        if self.readout:
            label = self._view.point_label(group, index)
            self._view.set_readout(f"{label + ': ' if label else ''}x {x:.1f}  y {y:.1f} cm")
        self._canvas.draw_idle()
        self.point_dragged.emit(group, index, x, y)

    def _on_release(self, event) -> None:
        self._legend_press = False
        drag, self._drag = self._drag, None
        if drag is not None and drag["moved"]:
            x, y = drag["last"]
            self.drag_finished.emit(drag["group"], drag["index"], x, y)

    def _on_leave(self, _event) -> None:
        if self._drag is not None or not self._inside:
            return
        self._inside = False
        if self.readout and self._view.set_readout(""):
            self._canvas.draw_idle()
        self.left.emit()

    def _format(self, x: float, y: float) -> str:
        if self._formatter is not None:
            try:
                text = self._formatter(x, y)
            except Exception:                 # never let a formatter break the hover
                log.exception("Read-out formatter failed at (%r, %r)", x, y)
                text = None
            if text is not None:
                return str(text)
        text = f"x {x:.1f}  y {y:.1f} cm"
        value = self._view.heatmap_text(x, y)
        return f"{text}  |  {value}" if value else text
