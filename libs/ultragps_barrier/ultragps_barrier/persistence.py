from __future__ import annotations

import os
import tomllib

from .core import BarrierData, BarrierType, TriggerMode, TriggerWhen


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_barrier(barrier: BarrierData) -> tuple[bool, str]:
    """Validate a BarrierData object. Returns (is_valid, error_message)."""
    if not barrier.name:
        return False, "barrier name must not be empty"

    if not isinstance(barrier.barrier_type, BarrierType):
        return False, f"invalid barrier_type: {barrier.barrier_type!r}"

    if not isinstance(barrier.trigger_mode, TriggerMode):
        return False, f"invalid trigger_mode: {barrier.trigger_mode!r}"

    if not isinstance(barrier.trigger_when, TriggerWhen):
        return False, f"invalid trigger_when: {barrier.trigger_when!r}"

    if not (0.0 <= barrier.alpha <= 1.0):
        return False, f"alpha {barrier.alpha!r} must be between 0.0 and 1.0"

    if barrier.barrier_type == BarrierType.POLYGON:
        if barrier.vertices is None or len(barrier.vertices) < 3:
            return False, "polygon barrier requires at least 3 vertices"

    elif barrier.barrier_type == BarrierType.CIRCLE:
        if barrier.center is None:
            return False, "circle barrier requires center"
        if barrier.radius is None or barrier.radius <= 0:
            return False, "circle barrier requires radius > 0"

    elif barrier.barrier_type == BarrierType.LINE:
        if barrier.point1 is None or barrier.point2 is None:
            return False, "line barrier requires point1 and point2"
        if barrier.thickness is None or barrier.thickness <= 0:
            return False, "line barrier requires thickness > 0"
        if tuple(barrier.point1) == tuple(barrier.point2):
            return False, "line barrier point1 and point2 must differ"

    return True, ""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_barriers(filepath: str) -> list[BarrierData]:
    """Load barriers from a TOML file. Returns [] if file doesn't exist."""
    if not os.path.exists(filepath):
        return []

    with open(filepath, 'rb') as f:
        data = tomllib.load(f)

    barriers: list[BarrierData] = []
    for entry in data.get('barrier', []):
        try:
            barrier = BarrierData(
                name=entry['name'],
                barrier_type=BarrierType(entry['type']),
                trigger_mode=TriggerMode(entry['trigger_mode']),
                trigger_when=TriggerWhen(entry['trigger_when']),
                callback_name=entry['callback_name'],
                color=entry['color'],
                alpha=float(entry['alpha']),
                vertices=(
                    [tuple(v) for v in entry['vertices']]
                    if 'vertices' in entry else None
                ),
                center=(
                    tuple(entry['center'])
                    if 'center' in entry else None
                ),
                radius=float(entry['radius']) if 'radius' in entry else None,
                point1=(
                    tuple(entry['point1'])
                    if 'point1' in entry else None
                ),
                point2=(
                    tuple(entry['point2'])
                    if 'point2' in entry else None
                ),
                thickness=(
                    float(entry['thickness'])
                    if 'thickness' in entry else None
                ),
            )
        except (KeyError, ValueError) as exc:
            print(f"Warning: skipping malformed barrier entry: {exc}")
            continue

        ok, msg = validate_barrier(barrier)
        if not ok:
            print(
                f"Warning: skipping invalid barrier "
                f"{entry.get('name', '?')!r}: {msg}"
            )
            continue

        barriers.append(barrier)

    return barriers


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def save_barriers(filepath: str, barriers: list[BarrierData]) -> None:
    """Save barriers to a TOML file as [[barrier]] array-of-tables."""
    with open(filepath, 'w') as f:
        for barrier in barriers:
            f.write("[[barrier]]\n")
            f.write(f'name = "{barrier.name}"\n')
            f.write(f'type = "{barrier.barrier_type.value}"\n')
            f.write(f'trigger_mode = "{barrier.trigger_mode.value}"\n')
            f.write(f'trigger_when = "{barrier.trigger_when.value}"\n')
            f.write(f'callback_name = "{barrier.callback_name}"\n')
            f.write(f'color = "{barrier.color}"\n')
            f.write(f'alpha = {barrier.alpha}\n')

            if barrier.barrier_type == BarrierType.POLYGON:
                verts = [[float(v[0]), float(v[1])] for v in barrier.vertices]
                f.write(f'vertices = {verts}\n')

            elif barrier.barrier_type == BarrierType.CIRCLE:
                cx, cy = float(barrier.center[0]), float(barrier.center[1])
                f.write(f'center = [{cx}, {cy}]\n')
                f.write(f'radius = {float(barrier.radius)}\n')

            elif barrier.barrier_type == BarrierType.LINE:
                p1x, p1y = float(barrier.point1[0]), float(barrier.point1[1])
                p2x, p2y = float(barrier.point2[0]), float(barrier.point2[1])
                f.write(f'point1 = [{p1x}, {p1y}]\n')
                f.write(f'point2 = [{p2x}, {p2y}]\n')
                f.write(f'thickness = {float(barrier.thickness)}\n')

            f.write('\n')
