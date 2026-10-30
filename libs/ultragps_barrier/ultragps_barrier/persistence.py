"""
Barrier persistence for ultragps_barrier: the ``[[barrier]]`` sections of
barriers.toml (``[[image]]`` sections are handled in :mod:`.images`).
"""

from __future__ import annotations

import os
import secrets
import stat
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

    return _barriers_from_data(data)


def _barriers_from_data(data: dict) -> list[BarrierData]:
    """Build the barriers of an already parsed barriers.toml.

    Malformed or invalid entries are skipped with a printed warning, and so is
    a ``barrier`` value that is not a list of tables (``barrier = 5``).
    """
    barriers: list[BarrierData] = []
    for entry in _section(data, 'barrier'):
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
                    [_point(v) for v in entry['vertices']]
                    if 'vertices' in entry else None
                ),
                center=(
                    _point(entry['center'])
                    if 'center' in entry else None
                ),
                radius=float(entry['radius']) if 'radius' in entry else None,
                point1=(
                    _point(entry['point1'])
                    if 'point1' in entry else None
                ),
                point2=(
                    _point(entry['point2'])
                    if 'point2' in entry else None
                ),
                thickness=(
                    float(entry['thickness'])
                    if 'thickness' in entry else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
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


def _section(data: dict, key: str) -> list:
    """The entries of the ``[[key]]`` sections of a parsed barriers.toml.

    A value of another kind under that key (``barrier = 5`` written by hand)
    holds no entries: it is skipped with a printed warning.  The translate
    check (``translate_barriers_file``) still sees it in the file and refuses
    to rewrite a file that would lose it.
    """
    entries = data.get(key, [])
    if isinstance(entries, list):
        return entries
    print(f"Warning: skipping {key!r}: expected [[{key}]] sections, got {entries!r}")
    return []


def _point(value) -> tuple[float, float]:
    """A stored ``[x, y]`` as an ``(x, y)`` tuple of floats.

    Raises ValueError or TypeError unless *value* is a pair of numbers, so
    that the loaders skip such an entry instead of the geometry and the
    writers failing on it later.
    """
    x, y = value
    return (float(x), float(y))


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def save_barriers(filepath: str, barriers: list[BarrierData]) -> None:
    """Save barriers to a TOML file as [[barrier]] array-of-tables.

    The file is replaced atomically and holds only the barriers afterwards —
    use :func:`ultragps_barrier.save_all` to keep the ``[[image]]`` sections.
    """
    _atomic_write_text(filepath, _barriers_toml(barriers))


def _barriers_toml(barriers: list[BarrierData]) -> str:
    """The ``[[barrier]]`` sections for *barriers*, as barriers.toml text."""
    lines: list[str] = []
    for barrier in barriers:
        lines.append("[[barrier]]")
        lines.append(f'name = {_toml_string(barrier.name)}')
        lines.append(f'type = {_toml_string(barrier.barrier_type.value)}')
        lines.append(f'trigger_mode = {_toml_string(barrier.trigger_mode.value)}')
        lines.append(f'trigger_when = {_toml_string(barrier.trigger_when.value)}')
        lines.append(f'callback_name = {_toml_string(barrier.callback_name)}')
        lines.append(f'color = {_toml_string(barrier.color)}')
        lines.append(f'alpha = {float(barrier.alpha)}')

        if barrier.barrier_type == BarrierType.POLYGON:
            verts = [[float(v[0]), float(v[1])] for v in barrier.vertices]
            lines.append(f'vertices = {verts}')

        elif barrier.barrier_type == BarrierType.CIRCLE:
            cx, cy = float(barrier.center[0]), float(barrier.center[1])
            lines.append(f'center = [{cx}, {cy}]')
            lines.append(f'radius = {float(barrier.radius)}')

        elif barrier.barrier_type == BarrierType.LINE:
            p1x, p1y = float(barrier.point1[0]), float(barrier.point1[1])
            p2x, p2y = float(barrier.point2[0]), float(barrier.point2[1])
            lines.append(f'point1 = [{p1x}, {p1y}]')
            lines.append(f'point2 = [{p2x}, {p2y}]')
            lines.append(f'thickness = {float(barrier.thickness)}')

        lines.append('')
    return "".join(line + "\n" for line in lines)


# ---------------------------------------------------------------------------
# TOML helpers (shared with images.py)
# ---------------------------------------------------------------------------

_BASIC_ESCAPES = {
    '"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t",
    "\n": "\\n", "\f": "\\f", "\r": "\\r",
}


def _toml_string(text: str) -> str:
    """*text* as a double-quoted TOML string, escaped where needed."""
    out = []
    for char in str(text):
        if char in _BASIC_ESCAPES:
            out.append(_BASIC_ESCAPES[char])
        elif char < " " or char == "\x7f":
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def _atomic_write_text(path: str, text: str) -> None:
    """Replace the file at *path* with *text* (UTF-8) in one step.

    Writes a temporary file next to the target, flushes it to disk and moves
    it over the target with ``os.replace``, so a reader never sees a
    half-written file and a failed write leaves the old one intact.  Symlinks
    are followed, the target's permissions are kept (a new file gets the
    umask's) and so are its owner and group as far as allowed
    (:func:`_keep_owner`), and a read-only target is refused rather than
    replaced.  A writable file in a read-only directory, where no temporary
    file can be made, is rewritten in place.
    """
    target = os.path.realpath(path)
    if os.path.exists(target) and not os.access(target, os.W_OK):
        raise PermissionError(f"File is read-only: {target}")

    directory, name = os.path.split(target)
    try:
        fd, tmp_path = _create_temp_file(directory, name)
    except PermissionError:
        if not os.path.isfile(target):
            raise
        with open(target, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)
        return

    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(target):
            original = os.stat(target)
            _keep_owner(tmp_path, original)
            os.chmod(tmp_path, stat.S_IMODE(original.st_mode))
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _keep_owner(path: str, original: os.stat_result) -> None:
    """Give *path* the owner and group of the file it is about to replace.

    Best effort: root keeps both, anyone else can keep a group they belong to.
    Without this, saving once with ``sudo`` would leave a root-owned file the
    user's own runs can no longer save; the in-place writes of earlier
    versions never changed a file's owner.
    """
    if not hasattr(os, 'chown'):
        return
    for uid in (original.st_uid, -1):
        try:
            os.chown(path, uid, original.st_gid)
            return
        except OSError:
            continue


def _create_temp_file(directory: str, name: str) -> tuple[int, str]:
    """Create a new, uniquely named hidden file next to *name* in *directory*.

    Returns ``(fd, path)``.  The file is created with mode ``0o666`` minus
    the umask, like any new file.
    """
    while True:
        tmp_path = os.path.join(directory, f".{name}.{secrets.token_hex(6)}.tmp")
        try:
            return os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666), tmp_path
        except FileExistsError:
            continue
