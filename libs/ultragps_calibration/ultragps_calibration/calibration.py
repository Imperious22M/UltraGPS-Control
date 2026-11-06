"""
UltraGPS Calibration Library — core module
==========================================
Standalone multi-point histogram calibration library for the UltraGPS system.

Drives the UltraGPS hardware via an ``UltraGPSClient`` instance to collect
tick readings at 2 to :data:`MAX_CAL_POINTS` (4) known calibration points,
then computes per-receiver linear calibration offsets (slope + intercept) and
writes them back to ``config.toml``.

Calibration points
------------------
The points come from ``cal_points`` in config.toml (``[[x, y], ...]``, P1
first) or, in configs written before ``cal_points`` existed, from
``cal_point_1`` / ``cal_point_2``.  Each receiver's ``cal_distances`` holds its
pre-computed Euclidean distance to every point, in the same order.  Run *k*
collects readings with the transmitter on point *k*.

The calibration formula
-----------------------
For each receiver, the modal (most frequent) tick of every run is paired with
the known distance to that run's point, and a least-squares line is fitted
through the ``(tick, distance)`` pairs:

.. code-block:: text

    slope     = sum((tick_k - mean_tick) * (dist_k - mean_dist))
                / sum((tick_k - mean_tick) ** 2)
    intercept = mean_dist - slope * mean_tick

For two points this is exactly the line through both of them, the formula the
library has always used:

.. code-block:: text

    slope     = (dist_1 - dist_2) / (tick_1 - tick_2)
    intercept = dist_1 - slope * tick_1

The resulting parameters map raw ticks to centimetres via:

.. code-block:: text

    distance_cm = tick * slope + intercept

A modal tick of 0 is the firmware's "no echo": that point is left out of that
receiver's fit.  A receiver is skipped (and keeps its previous offsets) when a
run has no data at all, when fewer than two points with different ticks
remain, or when it is at the same distance from every point it heard (the
fitted slope would be 0).  :attr:`UltraGPSCalibration.last_fit` and
:attr:`UltraGPSCalibration.last_skipped` describe the last fit for display.

Active receivers
----------------
A pulse always carries six ticks, but only the first ``number_of_receivers``
receivers of config.toml (ids ``0 .. N-1``, N from 3 to 6) are active: a run
completes once they have met the threshold, and only they get offsets fitted
and saved.  The dormant receivers' offsets in config.toml are left untouched.

Two usage modes
---------------

**Step-by-step** (designed for GUI integration):

    from ultragps_calibration import UltraGPSCalibration
    from ultragps_client import UltraGPSClient

    client = UltraGPSClient(host="192.168.1.100")
    client.connect()

    cal = UltraGPSCalibration("path/to/config.toml", client)

    # One run per calibration point.  The GUI shows the run-k dialog
    # ("place the transmitter on P<k>"), then:
    cal.clear_run_data(k)
    cal.start_run(k, min_reads=5,
                  on_reading=lambda run, n: gui_update_histograms(run),
                  on_complete=lambda run: gui_handle_run_complete(run))

    # When the last run (k == len(cal.cal_points)) finishes:
    offsets = cal.calculate_and_save_offsets()

    # Query histogram data at any time for display:
    data = cal.get_histogram_data(receiver_id=0, run_num=1)
    modal_tick = cal.get_most_frequent(receiver_id=0, run_num=1)

**Single collection** (no calibration points needed): one run slot collects
the modal ticks with the transmitter on any one spot, the same way a
calibration run does (most frequent tick per receiver, ``min_reads`` hits on
it for every active receiver).  The Calibration panel's System Origin
Calibration collects at (0, 0) like this and computes the intercepts itself;
nothing is fitted or saved by the library:

    cal.clear_run_data(1)
    cal.start_run(1, min_reads=cal.calibration_reads,
                  on_complete=lambda run: done(cal.get_modal_ticks(run)))

**Full automated** (headless / scripted use):

    offsets = cal.run_full_calibration(
        min_reads=5,
        on_between_runs=lambda run: input(f"Move transmitter to P{run}, then press Enter") or True,
        on_reading=lambda run, n: print(f"Run {run}: {n} reads collected"),
    )

Saving
------
Offsets are written with :func:`write_config`, the one config.toml writer of
the application (``SettingsModule`` uses it too).  It keeps every key it finds,
including ones this library does not know about, and replaces the file
atomically.

.. note::

    **GUI callers**: ``on_reading``, ``on_complete``, ``on_error`` and
    ``on_tick`` are invoked from the background collection thread, **not** the
    GUI main thread.  Schedule any GUI updates through your framework's
    thread-safe mechanism — e.g. emit a Qt signal from the callback.
"""

from __future__ import annotations

import datetime
import inspect
import math
import numbers
import os
import re
import secrets
import stat
import threading
import tomllib
from collections.abc import Callable
from typing import Optional


#: Fewest calibration points a calibration can use.
MIN_CAL_POINTS: int = 2

#: Most calibration points (one collection run each).
MAX_CAL_POINTS: int = 4

#: Receiver channels in a pulse reading (ids 0-5, R1-R6 in the GUI).
RECEIVER_CHANNELS: int = 6

#: Fewest active receivers (``number_of_receivers``) a config may set.
MIN_RECEIVERS: int = 3

# A receiver whose known distances to the points it heard have a sum of
# squared deviations at or below this (cm²) is equally far from all of them,
# so no slope can be fitted (the criterion points.py uses as well).
_FLAT_SXX_CM2: float = 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_config(config_path: str) -> dict:
    """Load and return config.toml as a Python dict.

    Args:
        config_path: Absolute or relative path to ``config.toml``.

    Raises:
        FileNotFoundError: If the file does not exist.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
    """
    path = os.path.abspath(config_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _config_active_receivers(config: dict) -> int:
    """Number of active receivers of a loaded config.

    ``number_of_receivers`` clamped to ``[MIN_RECEIVERS, RECEIVER_CHANNELS]``;
    every channel when the key is missing or not an integer.  The active
    receivers are ids ``0 .. N-1``; the others stay in config.toml (offsets
    included) but take no part in a calibration.
    """
    value = config.get("number_of_receivers")
    if isinstance(value, bool) or not isinstance(value, int):
        return RECEIVER_CHANNELS
    return max(MIN_RECEIVERS, min(value, RECEIVER_CHANNELS))


def _config_cal_points(config: dict) -> list[list[float]]:
    """Calibration points ``[[x, y], ...]`` of a loaded config.

    ``cal_points`` when present, else ``[cal_point_1, cal_point_2]`` (configs
    written before ``cal_points`` existed).
    """
    if "cal_points" in config:
        points = config["cal_points"]
    else:
        points = [config.get("cal_point_1", [0.0, 0.0]),
                  config.get("cal_point_2", [0.0, 0.0])]
    return [[float(p[0]), float(p[1])] for p in points]


# ─────────────────────────────────────────────────────────────────────────────
# Config writer
# ─────────────────────────────────────────────────────────────────────────────

#: Top-level keys in the order config.toml has always been written.  Any other
#: key follows them: plain values first, then tables, then ``[[receivers]]``.
CONFIG_KEY_ORDER: tuple[str, ...] = (
    "valid_settings", "cal_state", "calibration_reads", "number_of_receivers",
    "serial_port", "units", "cal_point_1", "cal_point_2", "cal_points",
)

# Key order inside each table of config.toml, by table path (the entries of
# an array of tables share their array's path).  Keys that are not listed,
# such as anything added by hand, follow in their own order; sub-tables always
# come after a table's values, the listed ones first.
_TABLE_KEY_ORDER: dict[tuple[str, ...], tuple[str, ...]] = {
    (): CONFIG_KEY_ORDER,
    ("receivers",): ("cal_distances", "origin_distance", "id", "position", "facing_deg",
                     "offset"),
    ("receivers", "offset"): ("intercept", "slope"),
}

# Arrays of tables written after every other top-level table.
_LAST_TOP_LEVEL_ARRAYS: tuple[str, ...] = ("receivers",)

# Sub-tables are indented by this much per level, as config.toml always was.
_INDENT = "    "

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")

# Characters a TOML literal ('...') string cannot hold.
_LITERAL_UNSAFE = re.compile(r"['\x00-\x08\x0a-\x1f\x7f]")

_BASIC_ESCAPES = {
    '"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t",
    "\n": "\\n", "\f": "\\f", "\r": "\\r",
}


def write_config(path: str, config: dict) -> None:
    """Write a config dict to *path* as TOML, keeping every key.

    The one config.toml writer of the application: the calibration library
    and ``SettingsModule`` both save through it, so neither drops what the
    other (or a person editing the file) added.

    Layout — unchanged from earlier versions for the keys they knew, so that
    rewriting an unchanged config.toml reproduces it byte for byte:

    * the known top-level keys first, in :data:`CONFIG_KEY_ORDER`, then any
      other top-level value, then any other top-level table or array of
      tables, then the ``[[receivers]]`` array of tables;
    * in each receiver ``cal_distances``, ``origin_distance``, ``id``,
      ``position``, then
      ``facing_deg`` and any other value, then the (indented)
      ``[receivers.offset]`` sub-table and any other sub-tables.

    Values: floats are written with ``repr`` so they read back bit for bit
    (``nan``, ``inf`` and ``-inf`` included), ints stay ints, bools are
    ``true`` / ``false``, strings are quoted and escaped as needed, lists may
    nest or be empty, tables inside lists become inline tables, and dates /
    times use ISO 8601.  For any dict of these types, ``tomllib`` reads the
    file back as the same dict (tuples come back as lists).  NumPy scalars
    and arrays are written as the equivalent Python values.

    The text is formatted completely before the file is touched, then written
    to a temporary file in the same directory and moved over *path* with
    ``os.replace``, so readers never see a half-written file and a failure
    leaves the old file intact.  A symlinked *path* is written through (the
    link is kept); the file keeps its permissions and, as far as the process
    may set them, its owner and group.  Only a writable file in a read-only
    directory, where no temporary file can be made, is rewritten in place.

    Args:
        path:   Path of the TOML file to write (normally ``config.toml``).
        config: Full configuration dict, e.g. as loaded with ``tomllib`` and
                modified.

    Raises:
        TypeError:       A key is not a string or a value has no TOML form
                         (``None``, a time with a UTC offset, an offset that
                         is not whole minutes ...).  The file is not touched.
        PermissionError: *path* exists but is read-only.
        OSError:         The file could not be written.
    """
    _atomic_write_text(path, _format_config(config))


# Earlier versions only had this private writer; UltraGPS-Simulator's
# engine/calibration.py still calls it by this name.
_write_config = write_config


def _format_config(config: dict) -> str:
    """The TOML text :func:`write_config` writes for *config*."""
    if not isinstance(config, dict):
        raise TypeError(f"config must be a dict, got {type(config).__name__}")
    lines: list[str] = []
    _format_table_body(lines, (), config)
    return "\n".join(lines) + "\n"


def _format_table_body(lines: list[str], path: tuple[str, ...], table: dict) -> None:
    """Append one table's ``key = value`` lines, then its sub-tables and
    arrays of tables (each with its header), to *lines*."""
    indent = _INDENT * max(len(path) - 1, 0)
    order = _TABLE_KEY_ORDER.get(path, ())
    values, tables, arrays = _split_table(table)

    for key, value in _in_order(values, order):
        lines.append(f"{indent}{_format_key(key)} = {_format_value(value)}")
    if values or path:
        lines.append("")

    for key, sub_table in _in_order(tables, order):
        _format_header(lines, path + (key,), array=False)
        _format_table_body(lines, path + (key,), sub_table)

    array_items = _in_order(arrays, order)
    if not path:
        array_items.sort(key=lambda item: item[0] in _LAST_TOP_LEVEL_ARRAYS)
    for key, entries in array_items:
        for entry in entries:
            _format_header(lines, path + (key,), array=True)
            _format_table_body(lines, path + (key,), entry)


def _format_header(lines: list[str], path: tuple[str, ...], array: bool) -> None:
    """Append a ``[table]`` or ``[[array.of.tables]]`` header line."""
    indent = _INDENT * (len(path) - 1)
    name = ".".join(_format_key(key) for key in path)
    lines.append(f"{indent}[[{name}]]" if array else f"{indent}[{name}]")


def _split_table(table: dict) -> tuple[dict, dict, dict]:
    """Split a table into plain values, sub-tables and arrays of tables."""
    values: dict = {}
    tables: dict = {}
    arrays: dict = {}
    for key, value in table.items():
        if not isinstance(key, str):
            raise TypeError(f"TOML keys must be strings, got {key!r}")
        if isinstance(value, dict):
            tables[key] = value
        elif (isinstance(value, (list, tuple)) and value
              and all(isinstance(item, dict) for item in value)):
            arrays[key] = value
        else:
            values[key] = value
    return values, tables, arrays


def _in_order(items: dict, order: tuple[str, ...]) -> list[tuple]:
    """``items`` as ``(key, value)`` pairs: the keys in *order* first, in
    that order, then the rest in their own order."""
    head = [(key, items[key]) for key in order if key in items]
    tail = [(key, value) for key, value in items.items() if key not in order]
    return head + tail


def _format_key(key: str) -> str:
    """A key as TOML: bare when it can be, otherwise a quoted string."""
    if not isinstance(key, str):
        raise TypeError(f"TOML keys must be strings, got {key!r}")
    return key if _BARE_KEY.fullmatch(key) else _basic_string(key)


def _format_value(value) -> str:
    """A value as an inline TOML value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, numbers.Real):
        return _format_float(float(value))
    if isinstance(value, str):
        # Literal strings ('...') as config.toml always had, unless the text
        # needs escaping.
        return f"'{value}'" if not _LITERAL_UNSAFE.search(value) else _basic_string(value)
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{_format_key(key)} = {_format_value(item)}" for key, item in value.items()
        ) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_value(item) for item in value) + "]"
    if isinstance(value, (datetime.date, datetime.time)):
        return _format_date_time(value)
    if hasattr(value, "tolist"):
        # NumPy scalars and arrays (np.float64's own repr is not TOML).
        return _format_value(value.tolist())
    raise TypeError(f"Cannot write {type(value).__name__} value {value!r} to TOML")


def _format_float(value: float) -> str:
    """A float as TOML: the shortest text that reads back as exactly *value*."""
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return repr(value)


def _format_date_time(value: datetime.date | datetime.time) -> str:
    """A date, time or datetime as TOML (ISO 8601).

    TOML times carry no UTC offset and its offsets are whole minutes; a value
    that would need either is refused rather than written as invalid TOML.
    """
    if isinstance(value, (datetime.datetime, datetime.time)):
        offset = value.utcoffset()
        if offset is not None and isinstance(value, datetime.time):
            raise TypeError(f"Cannot write {value!r} to TOML: a TOML time has no UTC offset")
        if offset is not None and offset % datetime.timedelta(minutes=1):
            raise TypeError(
                f"Cannot write {value!r} to TOML: its UTC offset is not a whole number of minutes"
            )
    return value.isoformat()


def _basic_string(text: str) -> str:
    """A double-quoted TOML string with every character that needs it escaped."""
    out = []
    for char in text:
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
    it over the target with ``os.replace``.  Symlinks are followed, the
    target's permissions are kept (a new file gets the umask's) and so are
    its owner and group as far as allowed (:func:`_keep_owner`), and a
    read-only target is refused rather than replaced.  A writable file in a
    read-only directory, where no temporary file can be made, is rewritten
    in place.
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
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return

    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
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

    Best effort: root keeps both, anyone else can keep a group they belong to
    (e.g. a lab's shared config directory).  Without this, saving once with
    ``sudo`` would leave a root-owned config.toml that the user's own runs
    can no longer save — the in-place writers of earlier versions never
    changed a file's owner.
    """
    if not hasattr(os, "chown"):
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


def _takes_run_number(callback: Callable) -> bool:
    """True when *callback* has a required positional parameter.

    Tells the ``on_between_runs(next_run)`` form of
    :meth:`UltraGPSCalibration.run_full_calibration` from the original
    zero-argument ``on_between_runs()`` form.
    """
    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False  # No introspectable signature: use the original form
    return any(
        p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
        for p in parameters
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main library class
# ─────────────────────────────────────────────────────────────────────────────

class UltraGPSCalibration:
    """Standalone multi-point histogram calibration library for the UltraGPS system.

    Manages tick-count histograms across one calibration run per calibration
    point (2 to :attr:`MAX_POINTS`), computes per-receiver linear offsets, and
    writes them back to ``config.toml``.

    Each instance represents one calibration session.  Clear histogram data
    between sessions with :meth:`clear_run_data`.

    Args:
        config_path: Path to the UltraGPS ``config.toml`` file.  Used both to
                     read calibration geometry (``cal_points`` /
                     ``cal_point_*``, ``cal_distances``,
                     ``calibration_reads``) and to write the computed offsets
                     back to disk.
        client:      A **connected** ``UltraGPSClient`` instance.  The library
                     calls ``client.pulse()`` to collect tick readings from the
                     hardware.
    """

    RECEIVER_COUNT: int = RECEIVER_CHANNELS
    """Number of receivers expected per pulse reading (the wire format; see
    :attr:`active_receiver_count` for how many take part)."""

    MAX_POINTS: int = MAX_CAL_POINTS
    """Maximum number of calibration points (one collection run each)."""

    HISTOGRAM_UPDATE_INTERVAL: int = 5
    """How many reads between successive ``on_reading`` callback invocations."""

    def __init__(self, config_path: str, client) -> None:
        self._config_path: str = os.path.abspath(config_path)
        self._client = client

        # Histogram store: run_num → receiver_id → {tick_value: count}
        self._histogram: dict[int, dict[int, dict[float, int]]] = {
            run: {i: {} for i in range(self.RECEIVER_COUNT)}
            for run in range(1, self.MAX_POINTS + 1)
        }

        self._running: bool = False
        self._current_run: int = 0
        # Bumped by every start_run(): a collection thread only acts for the
        # run it was started for (see _collection_loop).
        self._run_id: int = 0
        # Re-entrant: a pulse's six readings are added under one hold of it.
        self._lock = threading.RLock()
        self._cal_thread: Optional[threading.Thread] = None

        self.last_fit: dict[int, dict] = {}
        """Details of the last :meth:`calculate_offsets` for every receiver
        it calibrated: ``{receiver_id: {...}}`` with

        ``'ticks'``
            Modal tick at each calibration point, P1 first (``0`` = no echo).
        ``'distances'``
            Known distance (cm) to each calibration point.
        ``'residuals_cm'``
            ``fitted - known`` distance (cm) at each point, ``None`` for a
            point left out of the fit (no echo there).
        ``'residual_rms_cm'``
            RMS of the residuals of the points used (0 for two points).
        """

        self.last_skipped: dict[int, str] = {}
        """Receivers the last :meth:`calculate_offsets` could not calibrate:
        ``{receiver_id: reason}``."""

    # ─── Histogram data management ────────────────────────────────────────

    def _check_run_num(self, run_num: int) -> None:
        """Raise ``ValueError`` unless *run_num* is 1 to :attr:`MAX_POINTS`."""
        if not 1 <= run_num <= self.MAX_POINTS:
            raise ValueError(f"run_num must be 1 to {self.MAX_POINTS}, got {run_num}")

    def clear_run_data(self, run_num: int) -> None:
        """Clear all collected histogram data for a run.

        Call this before starting a new data-collection run to discard any
        readings left over from a previous session.

        Args:
            run_num: 1 to :attr:`MAX_POINTS` — which run's data to clear.
        """
        self._check_run_num(run_num)
        with self._lock:
            self._histogram[run_num] = {i: {} for i in range(self.RECEIVER_COUNT)}

    def add_reading(self, receiver_id: int, tick_value: float, run_num: int) -> None:
        """Record one tick reading into the histogram for a receiver and run.

        The collection thread calls this automatically during :meth:`start_run`.
        Callers may also call it directly when managing data collection outside
        this library.

        Args:
            receiver_id: Receiver index (0–5).
            tick_value:  Tick count from :meth:`UltraGPSClient.pulse`.
            run_num:     1 to :attr:`MAX_POINTS` — which calibration run this
                         reading belongs to.
        """
        self._check_run_num(run_num)
        with self._lock:
            bucket = self._histogram[run_num][receiver_id]
            bucket[tick_value] = bucket.get(tick_value, 0) + 1

    def get_most_frequent(self, receiver_id: int, run_num: int) -> Optional[float]:
        """Return the modal tick value for a receiver in a given run.

        This is the tick paired with the run's known distance when
        :meth:`calculate_offsets` fits the receiver's line.

        Args:
            receiver_id: Receiver index (0–5).
            run_num:     1 to :attr:`MAX_POINTS`.

        Returns:
            The most-frequent tick value, or ``None`` if no data has been
            collected.
        """
        self._check_run_num(run_num)
        with self._lock:
            data = dict(self._histogram[run_num][receiver_id])
        if not data:
            return None
        return max(data, key=data.__getitem__)

    def get_modal_ticks(self, run_num: int) -> list[Optional[float]]:
        """Return the modal tick of every receiver for a run.

        Args:
            run_num: 1 to :attr:`MAX_POINTS`.

        Returns:
            ``[tick_R1, ..., tick_R6]`` in receiver-id order; ``None`` for a
            receiver with no data in that run, ``0.0`` for one that heard no
            echo most of the time.
        """
        return [self.get_most_frequent(i, run_num) for i in range(self.RECEIVER_COUNT)]

    def get_max_count(self, receiver_id: int, run_num: int) -> int:
        """Return the highest count seen for any single tick value.

        Used internally to check whether a receiver has met the ``min_reads``
        threshold.  Also useful for GUI progress indicators.

        Args:
            receiver_id: Receiver index (0–5).
            run_num:     1 to :attr:`MAX_POINTS`.

        Returns:
            Maximum count across all tick values, or 0 if no data exists.
        """
        self._check_run_num(run_num)
        with self._lock:
            data = dict(self._histogram[run_num][receiver_id])
        return max(data.values(), default=0)

    def get_histogram_data(self, receiver_id: int, run_num: int) -> dict[float, int]:
        """Return a snapshot of the full histogram for a receiver and run.

        Returns a copy so the caller can safely iterate without holding the
        internal lock.

        Args:
            receiver_id: Receiver index (0–5).
            run_num:     1 to :attr:`MAX_POINTS`.

        Returns:
            ``{tick_value: count}`` dict.
        """
        self._check_run_num(run_num)
        with self._lock:
            return dict(self._histogram[run_num][receiver_id])

    # ─── Config access ────────────────────────────────────────────────────

    @property
    def config_path(self) -> str:
        """Absolute path of the config.toml this session reads and writes.

        May be reassigned, e.g. after the application switched to a writable
        per-user copy of a read-only config.
        """
        return self._config_path

    @config_path.setter
    def config_path(self, path: str) -> None:
        self._config_path = os.path.abspath(path)

    @property
    def cal_points(self) -> list[list[float]]:
        """All calibration points ``[[x, y], ...]`` (cm), P1 first.

        ``cal_points`` from config.toml, or ``[cal_point_1, cal_point_2]`` if
        it is absent.  Run *k* collects with the transmitter on point *k*.
        """
        return _config_cal_points(_load_config(self._config_path))

    @property
    def cal_point_1(self) -> list[float]:
        """Calibration point 1 coordinates ``[x, y]`` (cm), from config.toml."""
        config = _load_config(self._config_path)
        if "cal_point_1" in config:
            return list(config["cal_point_1"])
        points = _config_cal_points(config)
        return points[0] if points else [0.0, 0.0]

    @property
    def cal_point_2(self) -> list[float]:
        """Calibration point 2 coordinates ``[x, y]`` (cm), from config.toml."""
        config = _load_config(self._config_path)
        if "cal_point_2" in config:
            return list(config["cal_point_2"])
        points = _config_cal_points(config)
        return points[1] if len(points) > 1 else [0.0, 0.0]

    @property
    def active_receiver_count(self) -> int:
        """Number of active receivers, ``number_of_receivers`` in config.toml
        (3 to :attr:`RECEIVER_COUNT`; all of them when the key is missing or
        the file cannot be read).

        Read from the file on every access, so it follows a reload or a new
        :attr:`config_path`.  Only receivers ``0 .. N-1`` count towards a
        run's completion and get offsets fitted and saved; the others' ticks
        are still recorded but their offsets are never touched.
        """
        try:
            return _config_active_receivers(_load_config(self._config_path))
        except (OSError, tomllib.TOMLDecodeError):
            return self.RECEIVER_COUNT

    @property
    def calibration_reads(self) -> int:
        """Default minimum reads per receiver, from ``calibration_reads`` in config.toml."""
        return int(_load_config(self._config_path).get("calibration_reads", 5))

    def get_cal_distances(self, receiver_id: int) -> Optional[list[float]]:
        """Return the known calibration distances for a receiver.

        These are the pre-computed Euclidean distances from the receiver to
        each calibration point (same order as :attr:`cal_points`), stored in
        config.toml as ``cal_distances``.

        Args:
            receiver_id: Receiver index (0–5).

        Returns:
            ``[dist_to_P1, dist_to_P2, ...]``, or ``None`` if the receiver is
            not found in the config.
        """
        config = _load_config(self._config_path)
        for r in config.get("receivers", []):
            if r.get("id") == receiver_id:
                return list(r.get("cal_distances", []))
        return None

    # ─── Step-by-step API (for GUI integration) ───────────────────────────

    def start_run(
        self,
        run_num: int,
        min_reads: int,
        on_reading: Optional[Callable[[int, int], None]] = None,
        on_complete: Optional[Callable[[int], None]] = None,
        on_error: Optional[Callable[[int, str], None]] = None,
        on_tick: Optional[Callable[[int, list], None]] = None,
        max_consecutive_failures: int = 5,
    ) -> None:
        """Start a calibration data-collection run in a background thread.

        The thread calls :meth:`UltraGPSClient.pulse` repeatedly, accumulates
        tick counts via :meth:`add_reading`, and stops once every active
        receiver (:attr:`active_receiver_count`, read when the run starts) has
        at least *min_reads* counts on its modal tick value.

        Args:
            run_num:     1 to :attr:`MAX_POINTS` — which calibration run to
                         collect (run *k* belongs to calibration point *k*).
            min_reads:   Minimum number of modal-value hits per receiver before
                         the run completes.
            on_reading:  Optional callback invoked every
                         :attr:`HISTOGRAM_UPDATE_INTERVAL` reads, plus once
                         more after the final read.  Signature::

                             on_reading(run_num: int, read_count: int) -> None

                         Designed for triggering histogram redraws in a GUI.
            on_complete: Optional callback invoked once when the run finishes
                         naturally (i.e. not when stopped via
                         :meth:`stop_run` or ended by an error).  Signature::

                             on_complete(run_num: int) -> None

            on_error:    Optional callback invoked once when the run ends
                         because ``client.pulse()`` raised, or after
                         *max_consecutive_failures* pulses in a row returned
                         no reading (not for a run stopped via
                         :meth:`stop_run`).  Signature::

                             on_error(run_num: int, message: str) -> None

            on_tick:     Optional callback invoked with the latest raw ticks
                         alongside each ``on_reading``.  Signature::

                             on_tick(run_num: int, ticks: list) -> None

            max_consecutive_failures: Pulses in a row without a reading before
                         the run gives up.

            All callbacks are called from the background thread.
            ``on_complete`` and ``on_error`` are called once the library is
            idle again, so they may start the next run.

        Raises:
            ValueError:  If *run_num* is not 1 to :attr:`MAX_POINTS`.
            RuntimeError: If a run is already in progress.
        """
        self._launch_run(run_num, min_reads, on_reading, on_complete, on_error,
                         on_tick, max_consecutive_failures)

    def stop_run(self) -> None:
        """Request the current collection run to stop early.

        The run ends at once: :attr:`is_running` turns ``False`` and a new
        run may be started straight away.  The background thread finishes
        its current pulse call, discards that reading and exits cleanly.
        Neither ``on_complete`` nor ``on_error`` is called for a run stopped
        this way.
        """
        with self._lock:
            self._running = False
            self._current_run = 0

    # ─── Offset calculation and saving ────────────────────────────────────

    def calculate_offsets(self) -> dict[int, dict[str, float]]:
        """Calculate per-receiver slope and intercept from collected histogram data.

        For each receiver, the modal tick of run *k* is paired with the
        receiver's known distance to calibration point *k* (``cal_distances``)
        for every point in config.toml, and a least-squares line is fitted
        through the pairs:

        .. code-block:: text

            slope     = sum((t_k - t_mean) * (d_k - d_mean)) / sum((t_k - t_mean) ** 2)
            intercept = d_mean - slope * t_mean

        which for two points is ``slope = (d_1 - d_2) / (t_1 - t_2)``,
        ``intercept = d_1 - slope * t_1``.  Points where the receiver's modal
        tick is 0 (no echo) are left out of its fit.  Only the active
        receivers (``number_of_receivers``) are fitted.  Receivers with missing
        data, with fewer than two usable points, with identical ticks at
        every point or at the same distance from every point they heard (the
        slope would be 0) are skipped with a printed warning (and listed in
        :attr:`last_skipped`).  :attr:`last_fit` receives the details of every
        fit.

        Returns:
            ``{receiver_id: {'slope': float, 'intercept': float}}`` for every
            active receiver that produced valid results.
        """
        offsets: dict[int, dict[str, float]] = {}
        fits: dict[int, dict] = {}
        skipped: dict[int, str] = {}

        config = _load_config(self._config_path)
        n = min(len(_config_cal_points(config)), self.MAX_POINTS)

        for recv_id in range(_config_active_receivers(config)):
            # Look up known distances from config
            cal_distances: list[float] = []
            for r in config.get("receivers", []):
                if r.get("id") == recv_id:
                    cal_distances = list(r.get("cal_distances", []))
                    break

            result = self._fit_receiver(recv_id, cal_distances, n)
            if isinstance(result, str):
                skipped[recv_id] = result
                print(f"[calibration] Receiver {recv_id + 1}: {result} — skipped")
                continue

            offsets[recv_id], fits[recv_id] = result
            print(
                f"[calibration] Receiver {recv_id + 1}: "
                f"slope={offsets[recv_id]['slope']:.4f}, "
                f"intercept={offsets[recv_id]['intercept']:.4f}, "
                f"residual RMS={fits[recv_id]['residual_rms_cm']:.2f} cm"
            )

        self.last_fit = fits
        self.last_skipped = skipped
        return offsets

    def _fit_receiver(
        self, recv_id: int, cal_distances: list[float], n: int,
    ) -> tuple[dict[str, float], dict] | str:
        """Fit one receiver's line through its first *n* calibration points.

        Returns ``(offset, fit)`` — the ``{'slope', 'intercept'}`` dict and its
        :attr:`last_fit` entry — or, when the receiver cannot be calibrated,
        the reason as a string.
        """
        if n < MIN_CAL_POINTS:
            return f"config.toml has {n} calibration point(s), at least {MIN_CAL_POINTS} are needed"
        if len(cal_distances) < n:
            return "cal_distances missing or incomplete"
        try:
            dists = [float(d) for d in cal_distances[:n]]
        except (TypeError, ValueError):
            return "cal_distances are not all numbers"

        ticks = [self.get_most_frequent(recv_id, run) for run in range(1, n + 1)]
        if any(tick is None for tick in ticks):
            return "no histogram data for one or more runs"

        used = [k for k, tick in enumerate(ticks) if tick != 0]   # 0 = no echo
        if len(used) < MIN_CAL_POINTS:
            silent = ", ".join(f"P{k + 1}" for k, tick in enumerate(ticks) if tick == 0)
            return f"no echo at {silent}"
        if len({ticks[k] for k in used}) == 1:
            return f"identical tick values at every point ({ticks[used[0]]})"
        # Equal distances make the least-squares slope exactly 0 whatever the
        # ticks: a receiver that would read one distance everywhere.
        used_dists = [dists[k] for k in used]
        d_mean = sum(used_dists) / len(used_dists)
        if sum((d - d_mean) ** 2 for d in used_dists) <= _FLAT_SXX_CM2:
            return (f"the same distance ({d_mean:.1f} cm) from every calibration point "
                    "it heard: no slope can be fitted")

        slope, intercept = self._fit_line([ticks[k] for k in used], used_dists)
        residuals = [
            slope * ticks[k] + intercept - dists[k] if k in used else None
            for k in range(n)
        ]
        rms = math.sqrt(sum(residuals[k] ** 2 for k in used) / len(used))
        fit = {
            "ticks": ticks,
            "distances": dists,
            "residuals_cm": residuals,
            "residual_rms_cm": rms,
        }
        return {"slope": slope, "intercept": intercept}, fit

    @staticmethod
    def _fit_line(ticks: list[float], dists: list[float]) -> tuple[float, float]:
        """Least-squares ``(slope, intercept)`` of distance against tick.

        For two points this is the line through both of them.
        """
        n = len(ticks)
        t_mean = sum(ticks) / n
        d_mean = sum(dists) / n
        slope = (
            sum((t - t_mean) * (d - d_mean) for t, d in zip(ticks, dists))
            / sum((t - t_mean) ** 2 for t in ticks)
        )
        intercept = d_mean - slope * t_mean
        return slope, intercept

    def save_offsets(self, offsets: dict[int, dict[str, float]]) -> None:
        """Write calibration offsets to config.toml.

        Reads the current config, updates only the ``[receivers.offset]`` block
        for each active receiver (``number_of_receivers``) present in
        *offsets*, and writes the full config back to disk with
        :func:`write_config`.  All other config values — including the
        dormant receivers' offsets and keys this library does not know — are
        preserved.

        Args:
            offsets: ``{receiver_id: {'slope': float, 'intercept': float}}``
                     as returned by :meth:`calculate_offsets`.
        """
        config = _load_config(self._config_path)
        active = _config_active_receivers(config)
        for r in config.get("receivers", []):
            recv_id = r.get("id")
            if recv_id in offsets and isinstance(recv_id, int) and 0 <= recv_id < active:
                if "offset" not in r:
                    r["offset"] = {}
                r["offset"]["slope"]     = float(offsets[recv_id]["slope"])
                r["offset"]["intercept"] = float(offsets[recv_id]["intercept"])
        write_config(self._config_path, config)

    def calculate_and_save_offsets(self) -> dict[int, dict[str, float]]:
        """Calculate offsets from collected data and write them to config.toml.

        Convenience wrapper around :meth:`calculate_offsets` +
        :meth:`save_offsets`.

        Returns:
            The calculated ``{receiver_id: {'slope': float, 'intercept': float}}``
            dict (same as :meth:`calculate_offsets`).
        """
        offsets = self.calculate_offsets()
        self.save_offsets(offsets)
        return offsets

    # ─── Full automated calibration ───────────────────────────────────────

    def run_full_calibration(
        self,
        min_reads: Optional[int] = None,
        on_between_runs: Optional[Callable[..., bool]] = None,
        on_reading: Optional[Callable[[int, int], None]] = None,
    ) -> dict[int, dict[str, float]]:
        """Run the complete calibration sequence synchronously.

        Collects one run per calibration point (``len(cal_points)``, at most
        :attr:`MAX_POINTS`), optionally waiting before each run after the
        first for the caller to confirm the transmitter has been moved, then
        calculates and saves the offsets.

        This method blocks until calibration is finished or aborted.  For
        GUI-driven flows where each run is started in response to a user
        action use :meth:`start_run` directly instead.

        Args:
            min_reads:       Minimum reads per receiver per run.  Defaults to
                             the value of ``calibration_reads`` in config.toml.
            on_between_runs: Called synchronously on the calling thread before
                             every run after the first.  Should return
                             ``True`` to proceed, or ``False`` to abort.  If
                             ``None`` the library proceeds automatically.
                             Either form is accepted:

                             * ``on_between_runs() -> bool`` (the original
                               form), or
                             * ``on_between_runs(next_run: int) -> bool`` —
                               a callable with a required positional
                               parameter receives the number of the run about
                               to start (2, 3, ...), i.e. the calibration
                               point to move the transmitter to.

                             Typical usage::

                                 def prompt(run):
                                     ans = input(f"Move transmitter to P{run}. Ready? [y/n] ")
                                     return ans.strip().lower() == "y"

                                 cal.run_full_calibration(on_between_runs=prompt)

            on_reading:      Forwarded to :meth:`start_run` for every run.
                             Invoked every :attr:`HISTOGRAM_UPDATE_INTERVAL`
                             reads from the background thread.

                             Signature: ``on_reading(run_num: int, read_count: int) -> None``

        Returns:
            Calculated and saved offsets ``{receiver_id: {'slope': float,
            'intercept': float}}``, or an empty dict if calibration was
            aborted via *on_between_runs* or a run was stopped with
            :meth:`stop_run`.

        Raises:
            ValueError:   If config.toml has fewer than two calibration points.
            RuntimeError: If a run ended with an error (see ``on_error`` of
                          :meth:`start_run`); nothing is saved.
        """
        if min_reads is None:
            min_reads = self.calibration_reads

        n = min(len(self.cal_points), self.MAX_POINTS)
        if n < MIN_CAL_POINTS:
            raise ValueError(
                f"config.toml has {n} calibration point(s), at least {MIN_CAL_POINTS} are needed"
            )
        pass_run_number = on_between_runs is not None and _takes_run_number(on_between_runs)

        # ── One run per calibration point, gated in between ───────────────
        for run in range(1, n + 1):
            if run > 1 and on_between_runs is not None:
                proceed = on_between_runs(run) if pass_run_number else on_between_runs()
                if not proceed:
                    return {}

            self.clear_run_data(run)
            outcome: dict[str, object] = {}
            worker = self._launch_run(
                run, min_reads,
                on_reading=on_reading,
                on_complete=lambda run_num: outcome.setdefault("complete", True),
                on_error=lambda run_num, message: outcome.setdefault("error", message),
            )
            try:
                worker.join()
            except BaseException:
                self.stop_run()  # e.g. Ctrl+C: don't leave the run collecting
                raise

            if "error" in outcome:
                raise RuntimeError(f"Calibration run {run} failed: {outcome['error']}")
            if "complete" not in outcome:
                return {}  # Stopped via stop_run()

        # ── Calculate and persist offsets ─────────────────────────────────
        return self.calculate_and_save_offsets()

    # ─── Properties ───────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """``True`` while a data-collection run is active in the background."""
        with self._lock:
            return self._running

    @property
    def current_run(self) -> int:
        """Run number (1 to :attr:`MAX_POINTS`) currently being collected, or 0 if idle."""
        with self._lock:
            return self._current_run

    # ─── Internal ─────────────────────────────────────────────────────────

    def _launch_run(
        self,
        run_num: int,
        min_reads: int,
        on_reading: Optional[Callable[[int, int], None]] = None,
        on_complete: Optional[Callable[[int], None]] = None,
        on_error: Optional[Callable[[int, str], None]] = None,
        on_tick: Optional[Callable[[int, list], None]] = None,
        max_consecutive_failures: int = 5,
    ) -> threading.Thread:
        """Start the collection thread for a run (see :meth:`start_run`) and
        return it."""
        self._check_run_num(run_num)
        active = self.active_receiver_count

        with self._lock:
            if self._running:
                raise RuntimeError("A calibration run is already in progress")
            self._run_id += 1
            run_id = self._run_id
            self._running = True
            self._current_run = run_num
            thread = threading.Thread(
                target=self._collection_loop,
                args=(run_id, run_num, min_reads, on_reading, on_complete, on_error, on_tick,
                      max_consecutive_failures, active),
                daemon=True,
                name=f"ultragps_cal_run_{run_num}",
            )
            self._cal_thread = thread

        try:
            thread.start()
        except BaseException:
            self._release_run(run_id)
            raise
        return thread

    def _owns_run(self, run_id: int) -> bool:
        """``True`` while run *run_id* is the active run: not stopped with
        :meth:`stop_run` and not replaced by a newer run."""
        with self._lock:
            return self._running and self._run_id == run_id

    def _release_run(self, run_id: int) -> bool:
        """Mark the library idle if run *run_id* is still the active run.

        Returns ``True`` if it was; ``False`` when :meth:`stop_run` ended it
        already (a newer run that may have started since is left alone).
        """
        with self._lock:
            if not self._owns_run(run_id):
                return False
            self._running = False
            self._current_run = 0
            return True

    def _pulse_values(self, ticks) -> Optional[list[float]]:
        """One pulse's ticks as floats, or ``None`` unless it holds a finite
        number for every receiver (``client.pulse()`` returns ``None`` on a
        timeout)."""
        if ticks is None:
            return None
        try:
            values = [float(tick) for tick in ticks]
        except (TypeError, ValueError):
            return None
        if len(values) != self.RECEIVER_COUNT or not all(map(math.isfinite, values)):
            return None
        return values

    def _record_pulse(self, run_id: int, run_num: int, values: list[float]) -> bool:
        """Add one pulse's ticks to run *run_num*'s histograms — all of them,
        and only while run *run_id* is still the active run.  Returns whether
        they were added (``False`` once :meth:`stop_run` ended the run)."""
        with self._lock:
            if not self._owns_run(run_id):
                return False
            for recv_id, tick in enumerate(values):
                self.add_reading(recv_id, tick, run_num)
            return True

    def _collection_loop(
        self,
        run_id: int,
        run_num: int,
        min_reads: int,
        on_reading: Optional[Callable[[int, int], None]],
        on_complete: Optional[Callable[[int], None]],
        on_error: Optional[Callable[[int, str], None]],
        on_tick: Optional[Callable[[int, list], None]],
        max_consecutive_failures: int,
        active: Optional[int] = None,
    ) -> None:
        """Background thread: collect pulse readings until the threshold is met.

        Issues one discard pulse first to flush any stale network buffer, then
        loops calling ``client.pulse()`` until each of the first *active*
        receivers (default: all) has at least *min_reads* counts on its modal
        tick value.

        The thread acts for run *run_id* only.  Once :meth:`stop_run` has
        ended that run — even if a new run was started while this thread still
        waited for a pulse — it drops the pulse in flight and exits without
        touching the library's state or calling ``on_complete`` /
        ``on_error``.  Otherwise it marks the library idle *before* calling
        ``on_complete`` / ``on_error``, so those may start the next run.
        """
        completed_naturally = False
        error: Optional[str] = None
        if active is None:
            active = self.RECEIVER_COUNT
        try:
            # Discard one reading to flush the network buffer (mirrors GraphicsModule)
            try:
                self._client.pulse()
            except Exception as exc:
                print(f"[calibration] Warning: flush pulse failed: {exc}")

            read_count = 0
            consecutive_failures = 0
            last_ticks: Optional[list] = None

            # Ends when stop_run() ended this run — skip on_complete
            while self._owns_run(run_id):
                try:
                    ticks = self._client.pulse()
                except Exception as exc:
                    print(f"[calibration] Error during pulse: {exc}")
                    error = str(exc)
                    break

                values = self._pulse_values(ticks)
                if values is not None:
                    if not self._record_pulse(run_id, run_num, values):
                        break  # Stopped while the pulse was in flight: drop it
                    consecutive_failures = 0
                    last_ticks = list(ticks)
                    read_count += 1
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        print(
                            f"[calibration] Run {run_num}: {max_consecutive_failures} "
                            "consecutive failures — aborting"
                        )
                        error = f"Server not responding ({max_consecutive_failures} consecutive timeouts)"
                        break

                # Periodic reading callback
                if read_count > 0 and read_count % self.HISTOGRAM_UPDATE_INTERVAL == 0:
                    if on_reading is not None:
                        try:
                            on_reading(run_num, read_count)
                        except Exception as exc:
                            print(f"[calibration] on_reading callback error: {exc}")
                    if on_tick is not None and last_ticks is not None:
                        try:
                            on_tick(run_num, last_ticks)
                        except Exception as exc:
                            print(f"[calibration] on_tick callback error: {exc}")

                # Check whether all active receivers have met the threshold
                all_met = all(
                    self.get_max_count(i, run_num) >= min_reads
                    for i in range(active)
                )
                if all_met:
                    completed_naturally = True
                    break

            # Final reading callback to flush any unseen reads
            if completed_naturally and on_reading is not None and read_count > 0:
                try:
                    on_reading(run_num, read_count)
                except Exception as exc:
                    print(f"[calibration] on_reading callback error: {exc}")
        except Exception as exc:
            # Whatever went wrong, the run must end rather than leave the
            # library "running" for good (every later start_run would fail).
            print(f"[calibration] Run {run_num} failed: {exc!r}")
            completed_naturally, error = False, str(exc) or type(exc).__name__
        finally:
            active = self._release_run(run_id)

        if not active:
            return  # Ended by stop_run(): no on_complete / on_error

        if error is not None:
            if on_error is not None:
                try:
                    on_error(run_num, error)
                except Exception:
                    pass
        elif completed_naturally and on_complete is not None:
            try:
                on_complete(run_num)
            except Exception as exc:
                print(f"[calibration] on_complete callback error: {exc}")
