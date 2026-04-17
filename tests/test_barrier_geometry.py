import os
import sys
import tempfile

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), '..', 'libs', 'ultragps_barrier'),
)

from ultragps_barrier.core import (
    BarrierData,
    BarrierEvent,
    BarrierManager,
    BarrierType,
    TriggerMode,
    TriggerWhen,
    check_point_in_barrier,
    point_in_circle,
    point_in_polygon,
    point_near_line,
)
from ultragps_barrier.persistence import (
    load_barriers,
    save_barriers,
    validate_barrier,
)


def _make_barrier(
    name: str = 'test',
    barrier_type: BarrierType = BarrierType.POLYGON,
    trigger_mode: TriggerMode = TriggerMode.EVENT,
    trigger_when: TriggerWhen = TriggerWhen.INSIDE,
    **kwargs,
) -> BarrierData:
    """Return a minimal BarrierData with sane defaults for non-geometry fields."""
    return BarrierData(
        name=name,
        barrier_type=barrier_type,
        trigger_mode=trigger_mode,
        trigger_when=trigger_when,
        callback_name='on_test',
        color='#ff0000',
        alpha=0.5,
        **kwargs,
    )


def _manager_with(*barriers: BarrierData) -> BarrierManager:
    """Return a BarrierManager pre-loaded with the given barriers (no file I/O)."""
    mgr = BarrierManager('/tmp/nonexistent_barrier_test_dir')
    mgr.barriers = list(barriers)
    return mgr


SQUARE = [(0, 0), (100, 0), (100, 100), (0, 100)]
TRIANGLE = [(0, 0), (100, 0), (50, 100)]
L_SHAPE = [(0, 0), (100, 0), (100, 50), (50, 50), (50, 100), (0, 100)]


def test_polygon_inside():
    """Point clearly at the centre of an axis-aligned square."""
    assert point_in_polygon((50, 50), SQUARE) is True


def test_polygon_outside():
    """Point far to the right of the square."""
    assert point_in_polygon((150, 50), SQUARE) is False


def test_polygon_on_edge():
    """Point exactly on the bottom edge is considered inside."""
    assert point_in_polygon((50, 0), SQUARE) is True


def test_polygon_degenerate_empty():
    """Empty vertex list returns False (degenerate polygon)."""
    assert point_in_polygon((0, 0), []) is False


def test_polygon_degenerate_two_vertices():
    """Two vertices (a line segment, not a polygon) returns False."""
    assert point_in_polygon((50, 0), [(0, 0), (100, 0)]) is False


def test_polygon_triangle():
    """Point inside a triangle returns True."""
    assert point_in_polygon((50, 30), TRIANGLE) is True
    assert point_in_polygon((90, 90), TRIANGLE) is False


def test_polygon_concave():
    """L-shaped polygon: point in the concave notch is outside; point in the arm is inside."""
    assert point_in_polygon((75, 75), L_SHAPE) is False
    assert point_in_polygon((25, 75), L_SHAPE) is True


def test_circle_inside():
    """Point at the centre is always inside any positive-radius circle."""
    assert point_in_circle((50, 50), (50, 50), 10) is True


def test_circle_on_boundary():
    """Point exactly at radius distance lies on the boundary → inside."""
    assert point_in_circle((60, 50), (50, 50), 10) is True


def test_circle_outside():
    """Point beyond the radius is outside."""
    assert point_in_circle((65, 50), (50, 50), 10) is False


def test_circle_zero_radius():
    """Zero radius returns False (non-positive radii are invalid)."""
    assert point_in_circle((50, 50), (50, 50), 0) is False


def test_circle_negative_radius():
    """Negative radius also returns False."""
    assert point_in_circle((50, 50), (50, 50), -5) is False


def test_line_within_thickness():
    """Point within half-thickness of a horizontal segment is near."""
    assert point_near_line((50, 4), (0, 0), (100, 0), 10) is True


def test_line_outside_thickness():
    """Point beyond half-thickness is not near."""
    assert point_near_line((50, 6), (0, 0), (100, 0), 10) is False


def test_line_at_endpoint():
    """Point near an endpoint is considered near (projection clamped to segment)."""
    assert point_near_line((-3, 0), (0, 0), (100, 0), 10) is True


def test_line_angled():
    """Point perpendicular to a diagonal segment, within thickness, returns True.

    Segment (0,0)→(100,100), thickness=20 → half=10.
    (55, 45): closest point (50,50), dist=√50≈7.07 ≤ 10 → True.
    (60, 40): closest point (50,50), dist=√200≈14.14 > 10 → False.
    """
    assert point_near_line((55, 45), (0, 0), (100, 100), 20) is True
    assert point_near_line((60, 40), (0, 0), (100, 100), 20) is False


def test_line_coincident_endpoints():
    """Coincident endpoints (degenerate segment) always returns False."""
    assert point_near_line((50, 50), (50, 50), (50, 50), 100) is False


def test_line_zero_thickness():
    """Zero (non-positive) thickness always returns False."""
    assert point_near_line((50, 0), (0, 0), (100, 0), 0) is False


def test_manager_event_mode_enter_exit():
    """EVENT mode: 'enter' fires once on transition in; 'exit' fires once on transition out.
    No repeat events while the state remains unchanged."""
    barrier = _make_barrier(
        barrier_type=BarrierType.POLYGON,
        trigger_mode=TriggerMode.EVENT,
        trigger_when=TriggerWhen.INSIDE,
        vertices=SQUARE,
    )
    mgr = _manager_with(barrier)

    events = mgr.check_position(150, 50, 'lm')
    assert events == []

    events = mgr.check_position(50, 50, 'lm')
    assert len(events) == 1
    assert events[0].event_type == 'enter'
    assert events[0].barrier_name == 'test'
    assert events[0].source == 'lm'

    events = mgr.check_position(60, 60, 'lm')
    assert events == []

    events = mgr.check_position(150, 50, 'lm')
    assert len(events) == 1
    assert events[0].event_type == 'exit'

    events = mgr.check_position(200, 50, 'lm')
    assert events == []


def test_manager_continuous_mode():
    """CONTINUOUS mode fires an 'inside' event every update while inside."""
    barrier = _make_barrier(
        barrier_type=BarrierType.POLYGON,
        trigger_mode=TriggerMode.CONTINUOUS,
        trigger_when=TriggerWhen.INSIDE,
        vertices=SQUARE,
    )
    mgr = _manager_with(barrier)

    events = mgr.check_position(50, 50, 'lm')
    assert len(events) == 1
    assert events[0].event_type == 'inside'

    events = mgr.check_position(60, 60, 'lm')
    assert len(events) == 1
    assert events[0].event_type == 'inside'

    events = mgr.check_position(150, 50, 'lm')
    assert events == []


def test_manager_outside_trigger():
    """TriggerWhen.OUTSIDE: condition activates when position is OUTSIDE the barrier."""
    barrier = _make_barrier(
        barrier_type=BarrierType.POLYGON,
        trigger_mode=TriggerMode.EVENT,
        trigger_when=TriggerWhen.OUTSIDE,
        vertices=SQUARE,
    )
    mgr = _manager_with(barrier)

    events = mgr.check_position(50, 50, 'lm')
    assert events == []

    events = mgr.check_position(150, 50, 'lm')
    assert len(events) == 1
    assert events[0].event_type == 'enter'

    events = mgr.check_position(200, 50, 'lm')
    assert events == []

    events = mgr.check_position(50, 50, 'lm')
    assert len(events) == 1
    assert events[0].event_type == 'exit'


def test_manager_independent_sources():
    """LM and CEP positions are tracked independently per barrier."""
    barrier = _make_barrier(
        barrier_type=BarrierType.POLYGON,
        trigger_mode=TriggerMode.EVENT,
        trigger_when=TriggerWhen.INSIDE,
        vertices=SQUARE,
    )
    mgr = _manager_with(barrier)

    lm_events = mgr.check_position(50, 50, 'lm')
    assert len(lm_events) == 1 and lm_events[0].event_type == 'enter'

    cep_events = mgr.check_position(50, 50, 'cep')
    assert len(cep_events) == 1 and cep_events[0].event_type == 'enter'
    assert cep_events[0].source == 'cep'

    lm_events = mgr.check_position(60, 60, 'lm')
    assert lm_events == []

    cep_events = mgr.check_position(150, 50, 'cep')
    assert len(cep_events) == 1 and cep_events[0].event_type == 'exit'

    lm_events = mgr.check_position(70, 70, 'lm')
    assert lm_events == []


def test_manager_multiple_barriers():
    """Multiple barriers can trigger simultaneously in a single check_position call."""
    b1 = _make_barrier(
        name='square',
        barrier_type=BarrierType.POLYGON,
        trigger_mode=TriggerMode.EVENT,
        trigger_when=TriggerWhen.INSIDE,
        vertices=SQUARE,
    )
    b2 = _make_barrier(
        name='circle',
        barrier_type=BarrierType.CIRCLE,
        trigger_mode=TriggerMode.EVENT,
        trigger_when=TriggerWhen.INSIDE,
        center=(50, 50),
        radius=80,
    )
    mgr = _manager_with(b1, b2)

    events = mgr.check_position(50, 50, 'lm')
    assert len(events) == 2
    names = {e.barrier_name for e in events}
    assert names == {'square', 'circle'}
    assert all(e.event_type == 'enter' for e in events)


def test_manager_clear_states():
    """clear_states() resets tracking so the next update triggers 'enter' again."""
    barrier = _make_barrier(
        barrier_type=BarrierType.POLYGON,
        trigger_mode=TriggerMode.EVENT,
        trigger_when=TriggerWhen.INSIDE,
        vertices=SQUARE,
    )
    mgr = _manager_with(barrier)

    events = mgr.check_position(50, 50, 'lm')
    assert len(events) == 1 and events[0].event_type == 'enter'

    events = mgr.check_position(50, 50, 'lm')
    assert events == []

    mgr.clear_states()

    events = mgr.check_position(50, 50, 'lm')
    assert len(events) == 1 and events[0].event_type == 'enter'


def test_persistence_round_trip_polygon():
    """Save a polygon barrier, reload from TOML, verify all fields are identical."""
    original = _make_barrier(
        name='wall',
        barrier_type=BarrierType.POLYGON,
        trigger_mode=TriggerMode.EVENT,
        trigger_when=TriggerWhen.INSIDE,
        vertices=[(0, 0), (100, 0), (100, 100), (0, 100)],
    )
    fd, path = tempfile.mkstemp(suffix='.toml')
    os.close(fd)
    try:
        save_barriers(path, [original])
        loaded = load_barriers(path)

        assert len(loaded) == 1
        b = loaded[0]
        assert b.name == original.name
        assert b.barrier_type == BarrierType.POLYGON
        assert b.trigger_mode == TriggerMode.EVENT
        assert b.trigger_when == TriggerWhen.INSIDE
        assert b.callback_name == original.callback_name
        assert b.color == original.color
        assert b.alpha == original.alpha
        assert b.vertices == [(0, 0), (100, 0), (100, 100), (0, 100)]
    finally:
        os.unlink(path)


def test_persistence_round_trip_circle():
    """Save a circle barrier, reload from TOML, verify all fields are identical."""
    original = _make_barrier(
        name='zone',
        barrier_type=BarrierType.CIRCLE,
        trigger_mode=TriggerMode.CONTINUOUS,
        trigger_when=TriggerWhen.OUTSIDE,
        center=(125, 188),
        radius=50.0,
    )
    fd, path = tempfile.mkstemp(suffix='.toml')
    os.close(fd)
    try:
        save_barriers(path, [original])
        loaded = load_barriers(path)

        assert len(loaded) == 1
        b = loaded[0]
        assert b.name == original.name
        assert b.barrier_type == BarrierType.CIRCLE
        assert b.trigger_mode == TriggerMode.CONTINUOUS
        assert b.trigger_when == TriggerWhen.OUTSIDE
        assert b.center == (125, 188)
        assert b.radius == 50.0
    finally:
        os.unlink(path)


def test_persistence_round_trip_line():
    """Save a line barrier, reload from TOML, verify all fields are identical."""
    original = _make_barrier(
        name='boundary',
        barrier_type=BarrierType.LINE,
        trigger_mode=TriggerMode.EVENT,
        trigger_when=TriggerWhen.INSIDE,
        point1=(0, 100),
        point2=(251, 100),
        thickness=20.0,
    )
    fd, path = tempfile.mkstemp(suffix='.toml')
    os.close(fd)
    try:
        save_barriers(path, [original])
        loaded = load_barriers(path)

        assert len(loaded) == 1
        b = loaded[0]
        assert b.name == original.name
        assert b.barrier_type == BarrierType.LINE
        assert b.point1 == (0, 100)
        assert b.point2 == (251, 100)
        assert b.thickness == 20.0
    finally:
        os.unlink(path)


def test_persistence_missing_file():
    """Loading from a nonexistent path returns an empty list (no error)."""
    result = load_barriers('/tmp/this_file_definitely_does_not_exist_xyz.toml')
    assert result == []


def test_persistence_validate_invalid_name():
    """A barrier with an empty name fails validation."""
    barrier = _make_barrier(
        name='',
        barrier_type=BarrierType.POLYGON,
        vertices=SQUARE,
    )
    ok, msg = validate_barrier(barrier)
    assert ok is False
    assert 'name' in msg.lower()


def test_persistence_validate_invalid_polygon():
    """A polygon barrier with fewer than 3 vertices fails validation."""
    barrier = _make_barrier(
        name='bad_poly',
        barrier_type=BarrierType.POLYGON,
        vertices=[(0, 0), (100, 0)],
    )
    ok, msg = validate_barrier(barrier)
    assert ok is False
    assert 'vertices' in msg.lower() or 'polygon' in msg.lower()


def test_persistence_validate_invalid_circle():
    """A circle barrier with zero radius fails validation."""
    barrier = _make_barrier(
        name='bad_circle',
        barrier_type=BarrierType.CIRCLE,
        center=(50, 50),
        radius=0,
    )
    ok, msg = validate_barrier(barrier)
    assert ok is False
    assert 'radius' in msg.lower()


def test_persistence_round_trip_multiple():
    """Multiple barriers (mixed types) survive a save/load round-trip intact."""
    poly = _make_barrier(
        name='poly',
        barrier_type=BarrierType.POLYGON,
        vertices=SQUARE,
    )
    circ = _make_barrier(
        name='circ',
        barrier_type=BarrierType.CIRCLE,
        center=(50, 50),
        radius=30.0,
    )
    line = _make_barrier(
        name='line',
        barrier_type=BarrierType.LINE,
        point1=(0, 0),
        point2=(100, 100),
        thickness=10.0,
    )
    fd, path = tempfile.mkstemp(suffix='.toml')
    os.close(fd)
    try:
        save_barriers(path, [poly, circ, line])
        loaded = load_barriers(path)

        assert len(loaded) == 3
        names = [b.name for b in loaded]
        assert names == ['poly', 'circ', 'line']
        assert loaded[0].barrier_type == BarrierType.POLYGON
        assert loaded[1].barrier_type == BarrierType.CIRCLE
        assert loaded[2].barrier_type == BarrierType.LINE
    finally:
        os.unlink(path)
