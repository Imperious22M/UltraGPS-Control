"""
Rigid translations of barriers and image overlays for ultragps_barrier.

Used when the arena's origin moves: every coordinate in barriers.toml has to
move with the receivers so that each barrier stays on the same spot of the
floor.  A translation by ``(dx, dy)`` *adds* ``(dx, dy)`` to every
coordinate; making the point ``(x, y)`` the new origin is a translation by
``(-x, -y)``.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib

from .core import BarrierData, ImageOverlay
from .images import _images_from_data, _images_toml, save_all
from .persistence import _barriers_from_data, _barriers_toml


def translate_barrier(barrier: BarrierData, dx: float, dy: float) -> BarrierData:
    """Return a copy of *barrier* moved by ``(dx, dy)`` cm.

    Every coordinate the barrier has moves — polygon vertices, circle centre,
    line end points — while the radius, the thickness and every
    non-geometric field stay as they are.  *barrier* itself is not modified.
    """
    dx, dy = float(dx), float(dy)

    def moved(point):
        return (float(point[0]) + dx, float(point[1]) + dy)

    return dataclasses.replace(
        barrier,
        vertices=None if barrier.vertices is None else [moved(v) for v in barrier.vertices],
        center=None if barrier.center is None else moved(barrier.center),
        point1=None if barrier.point1 is None else moved(barrier.point1),
        point2=None if barrier.point2 is None else moved(barrier.point2),
    )


def translate_overlay(overlay: ImageOverlay, dx: float, dy: float) -> ImageOverlay:
    """Return a copy of *overlay* with its centre moved by ``(dx, dy)`` cm.

    Size and rotation are unchanged; *overlay* itself is not modified.
    """
    return dataclasses.replace(
        overlay,
        center_x=float(overlay.center_x) + float(dx),
        center_y=float(overlay.center_y) + float(dy),
    )


def translate_barriers_file(path: str, dx: float, dy: float) -> None:
    """Move every barrier and image overlay in a barriers.toml by ``(dx, dy)`` cm.

    Loads the ``[[barrier]]`` and ``[[image]]`` sections of the one file,
    translates them (:func:`translate_barrier`, :func:`translate_overlay`)
    and writes both back with :func:`ultragps_barrier.save_all` — the barrier
    drawer's own format — replacing the file atomically.  A missing file is
    left alone.

    The file is checked before anything is written: if it cannot be parsed,
    or if rewriting it would lose or change anything besides the coordinates
    — an entry the loader skips as malformed, a key or section the format does
    not have — nothing is written and an exception says what is in the way.

    Raises:
        tomllib.TOMLDecodeError: The file is not valid TOML.
        ValueError:              Rewriting the file would lose content.
        OSError:                 The file could not be read or written.
    """
    if not os.path.exists(path):
        return

    with open(path, 'rb') as f:
        data = tomllib.load(f)
    barriers = _barriers_from_data(data)
    images = _images_from_data(data)

    # What the file would hold after a load/save round trip without the move.
    # An empty ``barrier = []`` / ``image = []`` holds nothing it could lose.
    rewritten = tomllib.loads(_barriers_toml(barriers) + _images_toml(images))
    original = {key: value for key, value in data.items()
                if not (key in ("barrier", "image") and value == [])}
    lost = _round_trip_losses(original, rewritten, "")
    if lost:
        shown = ", ".join(lost[:5]) + (f" and {len(lost) - 5} more" if len(lost) > 5 else "")
        raise ValueError(
            f"{path}: rewriting this file would lose or change {shown}; "
            "fix or remove those entries, then try again"
        )

    save_all(
        path,
        [translate_barrier(barrier, dx, dy) for barrier in barriers],
        [translate_overlay(overlay, dx, dy) for overlay in images],
    )


def _round_trip_losses(original, rewritten, where: str) -> list[str]:
    """Places (``barrier[2].center``, 1-indexed) where *rewritten* lacks or
    differs from something in *original*.  Keys only *rewritten* has (defaults
    the writer fills in) are fine."""
    if isinstance(original, dict):
        if not isinstance(rewritten, dict):
            return [where or "the file"]
        lost: list[str] = []
        for key, value in original.items():
            place = f"{where}.{key}" if where else str(key)
            if key not in rewritten:
                lost.append(place)
            else:
                lost.extend(_round_trip_losses(value, rewritten[key], place))
        return lost

    if (isinstance(original, list) and original
            and all(isinstance(entry, dict) for entry in original)):
        if not isinstance(rewritten, list):
            return [where]
        lost = []
        for index, entry in enumerate(original):
            place = f"{where}[{index + 1}]"
            if index >= len(rewritten):
                lost.append(place)
            else:
                lost.extend(_round_trip_losses(entry, rewritten[index], place))
        return lost

    return [] if original == rewritten else [where]
