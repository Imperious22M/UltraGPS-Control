"""
UltraGPS Barrier Library — core module
=======================================
Pure-Python data model and geometry engine for virtual barriers in the
UltraGPS indoor positioning system.

Defines three barrier shapes — polygon, circle, and line — along with
the ray-casting, distance-check, and point-to-segment algorithms needed
to determine whether a tracked position lies inside (or near) each shape.

Quick-start
-----------
    from ultragps_barrier.core import (
        BarrierType, TriggerMode, TriggerWhen, BarrierData,
        check_point_in_barrier,
    )

    fence = BarrierData(
        name="lab_boundary",
        barrier_type=BarrierType.POLYGON,
        trigger_mode=TriggerMode.EVENT,
        trigger_when=TriggerWhen.OUTSIDE,
        callback_name="on_boundary_exit",
        color="#ff0000",
        alpha=0.3,
        vertices=[(0, 0), (251, 0), (251, 376), (0, 376)],
    )

    pos = (125.0, 188.0)
    inside = check_point_in_barrier(pos, fence)  # True

All coordinates are in **centimetres**, matching the rest of the UltraGPS
system.  No external dependencies beyond the Python 3.11+ standard library.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class BarrierType(Enum):
    """Shape of a virtual barrier."""
    POLYGON = "polygon"
    CIRCLE = "circle"
    LINE = "line"


class TriggerMode(Enum):
    """How the barrier fires its callback.

    ``EVENT``      — fire once on each inside/outside transition.
    ``CONTINUOUS`` — fire every position update while the condition holds.
    """
    EVENT = "event"
    CONTINUOUS = "continuous"


class TriggerWhen(Enum):
    """Which spatial condition activates the barrier.

    ``INSIDE``  — trigger when the tracked point is *inside* the barrier.
    ``OUTSIDE`` — trigger when the tracked point is *outside* the barrier.
    """
    INSIDE = "inside"
    OUTSIDE = "outside"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BarrierData:
    """Complete description of a single virtual barrier.

    Only the fields relevant to the barrier's :pyattr:`barrier_type` need to
    be populated — the rest default to ``None``.

    Args:
        name:          Human-readable identifier.
        barrier_type:  One of :class:`BarrierType`.
        trigger_mode:  One of :class:`TriggerMode`.
        trigger_when:  One of :class:`TriggerWhen`.
        callback_name: Name of the callback to invoke when triggered.
        color:         Hex colour string for GUI rendering (e.g. ``"#ff0000"``).
        alpha:         Opacity for GUI rendering (0.0 – 1.0).
        vertices:      ``list[(x, y)]`` — polygon vertices (≥ 3 required).
        center:        ``(x, y)`` — circle centre.
        radius:        Circle radius in cm.
        point1:        ``(x, y)`` — first endpoint of a line barrier.
        point2:        ``(x, y)`` — second endpoint of a line barrier.
        thickness:     Line proximity margin in cm (total width, not half).
    """
    name: str
    barrier_type: BarrierType
    trigger_mode: TriggerMode
    trigger_when: TriggerWhen
    callback_name: str
    color: str
    alpha: float
    # Polygon fields (None for other types)
    vertices: list = None
    # Circle fields (None for other types)
    center: tuple = None
    radius: float = None
    # Line fields (None for other types)
    point1: tuple = None
    point2: tuple = None
    thickness: float = None


# ---------------------------------------------------------------------------
# Geometry — ray-casting point-in-polygon
# ---------------------------------------------------------------------------

def point_in_polygon(point: tuple, vertices: list) -> bool:
    """Return True if *point* is inside the polygon defined by *vertices*.

    Uses the ray-casting algorithm: cast a horizontal ray from *point*
    toward +infinity and count the number of polygon edge crossings.
    An odd count means the point is inside.

    Points lying exactly on an edge are considered **inside**.

    Args:
        point:    ``(x, y)`` coordinate to test.
        vertices: ``list[(x, y)]`` polygon vertices in order (minimum 3).

    Returns:
        ``True`` if *point* is inside or on the boundary, ``False`` if
        outside or if the polygon is degenerate (fewer than 3 vertices).
    """
    if len(vertices) < 3:
        return False

    px, py = float(point[0]), float(point[1])
    n = len(vertices)

    # --- quick on-edge check via point-to-segment distance ----------------
    for i in range(n):
        x1, y1 = float(vertices[i][0]), float(vertices[i][1])
        x2, y2 = float(vertices[(i + 1) % n][0]), float(vertices[(i + 1) % n][1])
        if _point_on_segment(px, py, x1, y1, x2, y2):
            return True

    # --- ray-casting (horizontal ray to +inf) -----------------------------
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = float(vertices[i][0]), float(vertices[i][1])
        xj, yj = float(vertices[j][0]), float(vertices[j][1])

        # Does the edge from j→i straddle the horizontal ray at py?
        if ((yi > py) != (yj > py)) and (
            px < (xj - xi) * (py - yi) / (yj - yi) + xi
        ):
            inside = not inside
        j = i

    return inside


def _point_on_segment(
    px: float, py: float,
    x1: float, y1: float,
    x2: float, y2: float,
    eps: float = 1e-9,
) -> bool:
    """Return True if (px, py) lies on the segment (x1,y1)→(x2,y2)."""
    # Cross product ≈ 0 means collinear
    cross = (py - y1) * (x2 - x1) - (px - x1) * (y2 - y1)
    if abs(cross) > eps * max(1.0, abs(x2 - x1), abs(y2 - y1)):
        return False
    # Dot product within segment range
    if min(x1, x2) - eps <= px <= max(x1, x2) + eps:
        if min(y1, y2) - eps <= py <= max(y1, y2) + eps:
            return True
    return False


# ---------------------------------------------------------------------------
# Geometry — point-in-circle
# ---------------------------------------------------------------------------

def point_in_circle(point: tuple, center: tuple, radius: float) -> bool:
    """Return True if *point* is inside or on the boundary of the circle.

    Args:
        point:  ``(x, y)`` coordinate to test.
        center: ``(x, y)`` centre of the circle.
        radius: Circle radius in cm.  Must be positive.

    Returns:
        ``True`` if the Euclidean distance from *point* to *center* is
        ≤ *radius*.  Returns ``False`` for non-positive radii.
    """
    if radius <= 0:
        return False
    px, py = float(point[0]), float(point[1])
    cx, cy = float(center[0]), float(center[1])
    dist = math.sqrt((px - cx) ** 2 + (py - cy) ** 2)
    return dist <= radius


# ---------------------------------------------------------------------------
# Geometry — point-near-line (point-to-segment distance)
# ---------------------------------------------------------------------------

def point_near_line(
    point: tuple,
    point1: tuple,
    point2: tuple,
    thickness: float,
) -> bool:
    """Return True if *point* is within *thickness/2* of the line segment.

    Projects *point* onto the infinite line through *point1* → *point2*,
    clamps the projection to the segment endpoints, then checks whether
    the perpendicular distance is within the half-thickness margin.

    Args:
        point:     ``(x, y)`` coordinate to test.
        point1:    ``(x, y)`` first endpoint of the segment.
        point2:    ``(x, y)`` second endpoint of the segment.
        thickness: Total width of the proximity band (cm).  The point is
                   considered "near" if it is within ``thickness / 2`` of
                   the segment.  Must be positive.

    Returns:
        ``True`` if within the margin.  Returns ``False`` for degenerate
        segments (coincident endpoints) or non-positive thickness.
    """
    if thickness <= 0:
        return False

    px, py = float(point[0]), float(point[1])
    x1, y1 = float(point1[0]), float(point1[1])
    x2, y2 = float(point2[0]), float(point2[1])

    dx, dy = x2 - x1, y2 - y1
    seg_len_sq = dx * dx + dy * dy

    if seg_len_sq == 0.0:
        # Degenerate segment — endpoints coincide
        return False

    # Parameter t of the projection onto the infinite line (clamped to [0,1])
    t = ((px - x1) * dx + (py - y1) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))

    # Closest point on the segment
    closest_x = x1 + t * dx
    closest_y = y1 + t * dy

    dist = math.sqrt((px - closest_x) ** 2 + (py - closest_y) ** 2)
    return dist <= thickness / 2.0


# ---------------------------------------------------------------------------
# Dispatch — check point against any barrier type
# ---------------------------------------------------------------------------

def check_point_in_barrier(point: tuple, barrier: BarrierData) -> bool:
    """Dispatch to the correct geometry function based on *barrier.barrier_type*.

    Args:
        point:   ``(x, y)`` coordinate to test.
        barrier: A :class:`BarrierData` instance describing the barrier.

    Returns:
        ``True`` if *point* is inside (or near, for lines) the barrier.
        Returns ``False`` for unknown barrier types or if the relevant
        geometry fields are ``None``.
    """
    if barrier.barrier_type == BarrierType.POLYGON:
        if barrier.vertices is None:
            return False
        return point_in_polygon(point, barrier.vertices)

    if barrier.barrier_type == BarrierType.CIRCLE:
        if barrier.center is None or barrier.radius is None:
            return False
        return point_in_circle(point, barrier.center, barrier.radius)

    if barrier.barrier_type == BarrierType.LINE:
        if (barrier.point1 is None or barrier.point2 is None
                or barrier.thickness is None):
            return False
        return point_near_line(point, barrier.point1, barrier.point2,
                               barrier.thickness)

    # Unknown barrier type
    return False


# ---------------------------------------------------------------------------
# Barrier event
# ---------------------------------------------------------------------------

@dataclass
class BarrierEvent:
    """A single barrier trigger event.

    Args:
        barrier_name:  Name of the barrier that triggered.
        callback_name: Callback name registered on the barrier (e.g. "on_barrier").
        event_type:    One of "enter", "exit", "inside", "outside".
        position:      (x, y) position that triggered the event.
        source:        Position source — "lm" or "cep".
    """
    barrier_name: str
    callback_name: str
    event_type: str
    position: tuple
    source: str


# ---------------------------------------------------------------------------
# Barrier manager — state-tracking engine
# ---------------------------------------------------------------------------

class BarrierManager:
    """Tracks vehicle position against a set of virtual barriers.

    Loads barriers from barriers.toml, checks each position update against
    all barriers, and returns BarrierEvent objects when conditions are met.

    State is tracked independently for each (barrier_name, source) pair,
    so LM and CEP positions can trigger barriers independently.

    Args:
        config_dir: Directory containing barriers.toml.
    """

    def __init__(self, config_dir: str) -> None:
        self._config_dir = config_dir
        self.barriers: list[BarrierData] = []
        # State: key=(barrier_name, source), value=bool (True=condition currently met)
        self._states: dict[tuple[str, str], bool] = {}

    def load_barriers(self) -> None:
        """Load barriers from barriers.toml in config_dir. Silent if file missing."""
        # Import here to avoid circular imports
        from .persistence import load_barriers as _load
        import os
        filepath = os.path.join(self._config_dir, 'barriers.toml')
        self.barriers = _load(filepath)
        self._states.clear()

    def reload_barriers(self) -> None:
        """Re-read barriers from disk (hot-reload after drawer saves)."""
        self.load_barriers()

    def clear_states(self) -> None:
        """Reset all tracking states. Call on panel show or position reset."""
        self._states.clear()

    def check_position(self, x: float, y: float, source: str) -> list[BarrierEvent]:
        """Check position against all barriers and return triggered events.

        Args:
            x:      X coordinate in cm.
            y:      Y coordinate in cm.
            source: Position source identifier — "lm" or "cep".

        Returns:
            List of BarrierEvent objects for any barriers that triggered.
            Empty list if no barriers triggered.
        """
        events = []
        point = (x, y)

        for barrier in self.barriers:
            key = (barrier.name, source)

            in_region = check_point_in_barrier(point, barrier)

            if barrier.trigger_when == TriggerWhen.INSIDE:
                condition_met = in_region
            else:
                condition_met = not in_region

            prev_state = self._states.get(key, False)

            if barrier.trigger_mode == TriggerMode.EVENT:
                if condition_met and not prev_state:
                    events.append(BarrierEvent(
                        barrier_name=barrier.name,
                        callback_name=barrier.callback_name,
                        event_type="enter",
                        position=point,
                        source=source,
                    ))
                elif not condition_met and prev_state:
                    events.append(BarrierEvent(
                        barrier_name=barrier.name,
                        callback_name=barrier.callback_name,
                        event_type="exit",
                        position=point,
                        source=source,
                    ))
            else:
                if condition_met:
                    event_type = "inside" if barrier.trigger_when == TriggerWhen.INSIDE else "outside"
                    events.append(BarrierEvent(
                        barrier_name=barrier.name,
                        callback_name=barrier.callback_name,
                        event_type=event_type,
                        position=point,
                        source=source,
                    ))

            self._states[key] = condition_met

        return events
