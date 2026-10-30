"""
SettingsModule — the application's view of config.toml.

Loads config.toml into memory, offers accessors for every setting and writes
the file back on each change through ``ultragps_calibration.write_config``,
which keeps every key it finds (known or not, e.g. a receiver's
``facing_deg``).  Also locates the config directory (user -> local -> system)
and moves the arena's origin, which translates barriers.toml as well.
"""

import copy
import math
import os
import shutil
import stat
import tempfile
import tomllib
from itertools import combinations

from ultragps_barrier import translate_barriers_file
from ultragps_calibration import MAX_CAL_POINTS, MIN_CAL_POINTS, write_config

#: Directory name used under ~/.config and /etc.
APP_NAME = 'ultragps-control'

#: Name of the main settings file inside a config directory.
CONFIG_FILENAME = 'config.toml'

#: Name of the barrier / image overlay file, next to config.toml.
BARRIERS_FILENAME = 'barriers.toml'

#: Number of receivers in the system (ids 0-5, labelled R1-R6 in the GUI).
RECEIVER_COUNT = 6

#: Points closer together than this (cm) count as the same spot.
SAME_SPOT_CM = 1e-6

#: Three receivers count as collinear when twice their triangle's area is
#: below this fraction of the squared span of all receivers.
COLLINEAR_TOLERANCE = 1e-6


def user_config_dir():
    """Per-user config directory: $XDG_CONFIG_HOME/ultragps-control (or ~/.config/...)."""
    xdg = os.environ.get('XDG_CONFIG_HOME')
    if not xdg:
        xdg = os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(xdg, APP_NAME)


def local_config_dir():
    """In-tree config directory, used when running from a source checkout."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config')


def system_config_dir():
    """System-wide config directory, populated by the Debian package."""
    return os.path.join('/etc', APP_NAME)


def config_search_path():
    """Config directories in priority order: user, then local, then system."""
    return [user_config_dir(), local_config_dir(), system_config_dir()]


def find_config_dir():
    """Return the first directory on the search path that holds a config.toml.

    Returns None if no existing configuration was found anywhere.
    """
    for directory in config_search_path():
        if os.path.isfile(os.path.join(directory, CONFIG_FILENAME)):
            return directory
    return None


def resolve_config_path():
    """Locate config.toml, or pick where a fresh default should be created.

    Search order is user (~/.config) -> local (./config) -> system (/etc); the
    first directory containing a config.toml wins.  When nothing is found the
    default is created in the system directory, falling back to the user
    directory when /etc is not writable (i.e. when not running as root).
    """
    found = find_config_dir()
    if found is not None:
        return os.path.join(found, CONFIG_FILENAME)

    target = system_config_dir()
    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        target = user_config_dir()
        os.makedirs(target, exist_ok=True)
    return os.path.join(target, CONFIG_FILENAME)


# ==================== Value helpers ====================

def _is_number(value):
    """True for a finite int or float (a bool is not a number here)."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _stored_point(value):
    """A stored [x, y] value as an (x, y) tuple of floats.

    Returns None when the value is not a pair of finite numbers.
    """
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(_is_number(v) for v in value)):
        return (float(value[0]), float(value[1]))
    return None


def _input_point(value, what):
    """Coerce caller input (any (x, y) pair of numbers) to [x, y] floats.

    Raises:
        ValueError: naming *what*, if the value is not a pair of finite numbers.
    """
    try:
        if isinstance(value, (str, bytes)):
            raise TypeError
        x, y = value
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be an (x, y) pair of numbers, got {value!r}") from None
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"{what} must be finite, got {value!r}")
    return [x, y]


def _distance(a, b):
    """Euclidean distance between two (x, y) points (cm); inf, never an
    OverflowError, for points too far apart to measure."""
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _receiver_label(receiver, index):
    """GUI label of a stored receiver (R1-R6), by id when it has a usable one."""
    receiver_id = receiver.get('id') if isinstance(receiver, dict) else None
    if isinstance(receiver_id, int) and not isinstance(receiver_id, bool):
        return f"R{receiver_id + 1}"
    return f"#{index + 1}"


def _replace_file(path, data):
    """Atomically replace the file at *path* with the bytes *data*, keeping its
    permissions and, as far as this process may, its owner and group."""
    target = os.path.realpath(path)
    directory, name = os.path.split(target)
    original = os.stat(target)
    fd, tmp_path = tempfile.mkstemp(prefix=f'.{name}.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        for uid in (original.st_uid, -1):
            try:
                os.chown(tmp_path, uid, original.st_gid)
                break
            except OSError:
                continue
        os.chmod(tmp_path, stat.S_IMODE(original.st_mode))
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class SettingsModule:
    def __init__(self, config_path=None):
        """
        Initialize the SettingsModule by loading config.toml into memory.

        With no argument the file is located via resolve_config_path(); pass
        config_path to point at a specific file (the --config option).
        If no config.toml exists, creates one with default empty values.
        All reads reference the internal copy, all writes immediately save to file.
        """
        if config_path is None:
            self._config_path = resolve_config_path()
        else:
            self._config_path = os.path.abspath(config_path)

        # Load the config file into internal storage (or create default if missing)
        self._config = self._load_config()

        # Verify the config file
        self.valid_settings, err = self.verify_settings()

        # Print warning if settings not initialized
        if not self.valid_settings:
            print("Config file settings not initialized:")
            for message in err:
                print(f"  {message}")

    def _get_default_config(self):
        """
        Return a default configuration with empty/uninitialized values.
        These values require initialization before the system can be used.
        """
        return {
            'valid_settings': False,
            'cal_state': 0,
            'calibration_reads': 5,
            'number_of_receivers': RECEIVER_COUNT,
            'serial_port': '/dev/ttyACM0',
            'units': 'cm',
            'cal_point_1': [0.0, 0.0],
            'cal_point_2': [0.0, 0.0],
            'cal_points': [[0.0, 0.0], [0.0, 0.0]],
            'receivers': [
                {
                    'id': receiver_id,
                    'position': [0.0, 0.0],
                    'cal_distances': [0.0, 0.0],
                    'offset': {'slope': 0.0, 'intercept': 0.0},
                }
                for receiver_id in range(RECEIVER_COUNT)
            ],
        }

    def _load_config(self):
        """
        Load the TOML configuration file into memory.
        If the file doesn't exist, create it with default values.
        """
        if not os.path.exists(self._config_path):
            # Create default config and save it
            config = self._get_default_config()
            self._config = config
            self._save_config()
            print("Created config file, please initialize!")
            return config

        with open(self._config_path, 'rb') as f:
            print("Loaded config file...")
            return tomllib.load(f)

    def _save_config(self):
        """Save the internal config to the TOML file (every key is kept)."""
        self._ensure_writable()
        write_config(self._config_path, self._config)

    def _ensure_writable(self):
        """Make sure the active config directory can be written to.

        A packaged install resolves to /etc/ultragps-control, which an ordinary
        user cannot write to.  Rather than failing the save, copy the whole
        config directory (config.toml, barriers.toml and resources/) into the
        per-user directory and switch to it, so the user's edits land in
        ~/.config and take priority from then on.
        """
        current_dir = self.config_dir
        if not os.path.isdir(current_dir):
            # An explicit --config path may point somewhere that does not exist
            # yet; create it rather than treating it as unwritable.
            try:
                os.makedirs(current_dir, exist_ok=True)
            except OSError:
                pass
        if os.access(current_dir, os.W_OK):
            return

        target_dir = user_config_dir()
        os.makedirs(target_dir, exist_ok=True)

        # Seed the user directory from the read-only one, without clobbering
        # anything the user already has there.
        if os.path.isdir(current_dir):
            for entry in os.listdir(current_dir):
                source = os.path.join(current_dir, entry)
                destination = os.path.join(target_dir, entry)
                if os.path.exists(destination):
                    continue
                try:
                    if os.path.isdir(source):
                        shutil.copytree(source, destination)
                    else:
                        shutil.copy2(source, destination)
                except OSError as err:
                    print(f"Could not copy {source} to {target_dir}: {err}")

        print(f"{current_dir} is not writable, switching to {target_dir}")
        self._config_path = os.path.join(target_dir, CONFIG_FILENAME)

    def reload_config(self):
        """Reload the configuration from the file."""
        self._config = self._load_config()

    def verify_settings(self):
        """
        Verify that all required settings are initialized and consistent.

        Checks:
        - Exactly 6 receivers (ids 0-5), each with a numeric (x, y) position
        - The receivers are not all on the same spot (the uninitialized
          default) and at least 3 of them are not collinear
        - 2 to 4 calibration points, numeric and pairwise distinct, with
          cal_point_1 / cal_point_2 equal to the first two
        - Each receiver has one calibration distance per calibration point
          and its offset parameters (slope and intercept)

        A receiver or calibration point at exactly (0, 0) is fine: the origin
        can be put anywhere, a receiver included.

        Returns:
            tuple (is_valid: bool, errors: list of str), the messages naming
            receivers R1-R6 and calibration points P1-P4 as the GUI does
        """
        errors = []

        # Check number of receivers
        receivers = self._config.get('receivers', [])
        count = len(receivers) if isinstance(receivers, list) else 0
        if count != RECEIVER_COUNT:
            errors.append(f"Expected {RECEIVER_COUNT} receivers, found {count}")

        # Calibration points (P1..Pn)
        raw_points = self._raw_cal_points()
        point_count = len(raw_points)
        if not MIN_CAL_POINTS <= point_count <= MAX_CAL_POINTS:
            errors.append(
                f"Expected {MIN_CAL_POINTS} to {MAX_CAL_POINTS} calibration points, found {point_count}")
        points = [_stored_point(p) for p in raw_points]
        for k, point in enumerate(points, start=1):
            if point is None:
                errors.append(f"Calibration point P{k} is not a pair of numbers")
        for (j, a), (k, b) in combinations(enumerate(points, start=1), 2):
            if a is not None and b is not None and _distance(a, b) <= SAME_SPOT_CM:
                errors.append(
                    f"Calibration points P{j} and P{k} are on the same spot "
                    f"({a[0]:g}, {a[1]:g})")
        if 'cal_points' in self._config:
            for index, key in enumerate(('cal_point_1', 'cal_point_2')):
                if key not in self._config or index >= len(points) or points[index] is None:
                    continue
                legacy = _stored_point(self._config[key])
                if legacy is None or _distance(legacy, points[index]) > SAME_SPOT_CM:
                    errors.append(
                        f"{key} does not match calibration point P{index + 1}; "
                        "save the calibration points again")

        # Check each receiver
        positions = []
        for i in range(RECEIVER_COUNT):
            label = f"R{i + 1}"
            receiver = self._find_receiver(i)
            if receiver is None:
                errors.append(f"Receiver {label} not found")
                continue

            position = _stored_point(receiver.get('position'))
            if position is None:
                errors.append(f"Receiver {label} position is missing or not a pair of numbers")
            else:
                positions.append(position)

            cal_dist = receiver.get('cal_distances')
            if not isinstance(cal_dist, list) or not all(_is_number(d) for d in cal_dist):
                errors.append(f"Receiver {label} calibration distances are missing or not numbers")
            elif len(cal_dist) != point_count:
                errors.append(
                    f"Receiver {label} has {len(cal_dist)} calibration distance(s), expected "
                    f"{point_count} (one per calibration point)")

            # Check offset values exist
            offset = receiver.get('offset')
            if (not isinstance(offset, dict) or not _is_number(offset.get('slope'))
                    or not _is_number(offset.get('intercept'))):
                errors.append(f"Receiver {label} offset parameters missing")

        # Receiver geometry
        if len(positions) >= 2:
            span = max(_distance(a, b) for a, b in combinations(positions, 2))
            if not math.isfinite(span):
                errors.append("Receiver positions are out of range (too far apart to measure)")
            elif span <= SAME_SPOT_CM:
                errors.append(
                    "Receiver positions not initialized (all receivers are on the same spot)")
            elif len(positions) == RECEIVER_COUNT and not self._spans_plane(positions, span):
                errors.append(
                    "Receivers are all on one line; at least 3 of them must not be collinear")

        is_valid = len(errors) == 0
        return (is_valid, errors)

    @staticmethod
    def _spans_plane(positions, span):
        """True when at least three of the positions are not collinear."""
        threshold = COLLINEAR_TOLERANCE * span * span
        for a, b, c in combinations(positions, 3):
            cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            if abs(cross) > threshold:
                return True
        return False

    # ==================== Top-level field accessors ====================

    @property
    def config_path(self):
        """Absolute path to the active config.toml."""
        return self._config_path

    @property
    def config_dir(self):
        """Directory holding config.toml, barriers.toml and resources/."""
        return os.path.dirname(self._config_path)

    @property
    def valid_settings(self):
        """Get whether the settings have been validated/initialized."""
        return self._config.get('valid_settings', False)

    @valid_settings.setter
    def valid_settings(self, value):
        """Set the valid_settings flag and save to file."""
        self._config['valid_settings'] = bool(value)
        self._save_config()

    @property
    def arena_size(self):
        """
        Get the arena size (width, height) in cm: the width and height of the
        bounding box of the receivers' positions, whatever their layout.
        Always recalculates from current receiver positions; (0.0, 0.0) when no
        receiver has a usable position.
        """
        positions = [_stored_point(r.get('position')) for r in self._receivers()]
        positions = [p for p in positions if p is not None]
        if not positions:
            return (0.0, 0.0)
        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        return (round(max(xs) - min(xs), 2), round(max(ys) - min(ys), 2))

    @property
    def cal_state(self):
        """Get the calibration state."""
        return self._config.get('cal_state', 0)

    @cal_state.setter
    def cal_state(self, value):
        """Set the calibration state and save to file."""
        self._config['cal_state'] = int(value)
        self._save_config()

    @property
    def calibration_reads(self):
        """Get the number of calibration reads."""
        return self._config.get('calibration_reads', 5)

    @calibration_reads.setter
    def calibration_reads(self, value):
        """Set the number of calibration reads and save to file."""
        self._config['calibration_reads'] = int(value)
        self._save_config()

    @property
    def number_of_receivers(self):
        """Get the number of receivers."""
        return self._config.get('number_of_receivers', RECEIVER_COUNT)

    @number_of_receivers.setter
    def number_of_receivers(self, value):
        """Set the number of receivers and save to file."""
        self._config['number_of_receivers'] = int(value)
        self._save_config()

    @property
    def serial_port(self):
        """Get the serial port path."""
        return self._config.get('serial_port', '/dev/ttyACM0')

    @serial_port.setter
    def serial_port(self, value):
        """Set the serial port path and save to file."""
        self._config['serial_port'] = str(value)
        self._save_config()

    @property
    def units(self):
        """Get the units string."""
        return self._config.get('units', 'cm')

    @units.setter
    def units(self, value):
        """Set the units string and save to file."""
        self._config['units'] = str(value)
        self._save_config()

    # ==================== Calibration points ====================

    def _raw_cal_points(self):
        """The calibration points as stored: cal_points, or [cal_point_1,
        cal_point_2] in configs written before cal_points existed."""
        if 'cal_points' in self._config:
            points = self._config['cal_points']
            return list(points) if isinstance(points, list) else []
        return [self._config.get('cal_point_1', [0.0, 0.0]),
                self._config.get('cal_point_2', [0.0, 0.0])]

    @property
    def cal_points(self):
        """
        Get the calibration points [[x, y], ...] in cm, P1 first (2 to 4).

        Read from cal_points, or [cal_point_1, cal_point_2] in configs written
        before cal_points existed.  Raises ValueError if a stored point is not
        a pair of numbers (verify_settings() reports that without raising).
        """
        points = []
        for k, value in enumerate(self._raw_cal_points(), start=1):
            point = _stored_point(value)
            if point is None:
                raise ValueError(
                    f"Calibration point P{k} in {self._config_path} is not a pair of numbers: {value!r}")
            points.append(list(point))
        return points

    def set_cal_points(self, points):
        """
        Set the calibration points and save to file.

        Writes cal_points, keeps cal_point_1 / cal_point_2 equal to the first
        two (for older readers) and recomputes every receiver's cal_distances
        (one per point, in the same order) - all in one save.

        Args:
            points: 2 to 4 (x, y) pairs in cm, P1 first

        Raises:
            ValueError: wrong number of points, or a point that is not a pair
                of finite numbers.  Nothing is changed then.
        """
        new_points = [_input_point(p, f"Calibration point P{k}")
                      for k, p in enumerate(points, start=1)]
        if not MIN_CAL_POINTS <= len(new_points) <= MAX_CAL_POINTS:
            raise ValueError(
                f"Expected {MIN_CAL_POINTS} to {MAX_CAL_POINTS} calibration points, "
                f"got {len(new_points)}")

        self._config['cal_points'] = new_points
        self._config['cal_point_1'] = list(new_points[0])
        self._config['cal_point_2'] = list(new_points[1])
        self._update_cal_distances(self._receivers())
        self._save_config()

    def _set_cal_point(self, index, value):
        """Replace calibration point P<index+1>, keeping the others, and save."""
        point = _input_point(value, f"Calibration point P{index + 1}")
        points = []
        for raw in self._raw_cal_points()[:MAX_CAL_POINTS]:
            stored = _stored_point(raw)
            points.append(list(stored) if stored is not None else [0.0, 0.0])
        while len(points) < MIN_CAL_POINTS:
            points.append([0.0, 0.0])
        points[index] = point
        self.set_cal_points(points)

    def _update_cal_distances(self, receivers):
        """Recompute cal_distances (one per calibration point) of the given
        stored receivers.  Receivers without a usable position keep theirs;
        nothing is recomputed while the stored calibration points are unusable
        (verify_settings() reports them)."""
        try:
            points = self.cal_points
        except ValueError:
            return
        for receiver in receivers:
            position = _stored_point(receiver.get('position'))
            if position is not None:
                receiver['cal_distances'] = [_distance(position, p) for p in points]

    @property
    def cal_point_1(self):
        """Get calibration point 1 (P1) coordinates [x, y]."""
        if 'cal_point_1' in self._config:
            return list(self._config['cal_point_1'])
        raw_points = self._raw_cal_points()
        return list(raw_points[0]) if raw_points else [0.0, 0.0]

    @cal_point_1.setter
    def cal_point_1(self, value):
        """Set calibration point 1 (P1), keeping cal_points and every
        receiver's cal_distances in step, and save to file."""
        self._set_cal_point(0, value)

    @property
    def cal_point_2(self):
        """Get calibration point 2 (P2) coordinates [x, y]."""
        if 'cal_point_2' in self._config:
            return list(self._config['cal_point_2'])
        raw_points = self._raw_cal_points()
        return list(raw_points[1]) if len(raw_points) > 1 else [0.0, 0.0]

    @cal_point_2.setter
    def cal_point_2(self, value):
        """Set calibration point 2 (P2), keeping cal_points and every
        receiver's cal_distances in step, and save to file."""
        self._set_cal_point(1, value)

    # ==================== Origin ====================

    def shift_origin(self, x, y):
        """
        Make the current-frame point (x, y) the arena's new (0, 0).

        Translates the whole frame by (-x, -y): every receiver position and
        every calibration point (cal_points and cal_point_1/2) in config.toml,
        and every barrier (polygon vertices, circle centre, line end points)
        and image overlay centre in barriers.toml of the same config
        directory.  A translation changes no distance, so cal_distances,
        slopes, intercepts and facings stay as they are.

        Everything is checked before anything is written: bad coordinates, a
        stored position or calibration point that is not a pair of numbers, or
        a barriers.toml that cannot be rewritten without losing something
        raise and leave both files untouched.  barriers.toml is replaced
        first, then config.toml (each atomically); if config.toml cannot be
        written, barriers.toml is put back.  When the config directory is
        read-only (packaged /etc install) both files are written to the
        per-user copy _ensure_writable() switches to.

        Call main_window.notify_config_changed() afterwards so the position
        library and the panels pick up the new frame.

        Args:
            x: X of the new origin in the current frame (cm)
            y: Y of the new origin in the current frame (cm)

        Raises:
            ValueError: invalid coordinates or stored geometry, or a
                barriers.toml that would lose content (nothing written)
            OSError: a file could not be read or written
        """
        origin_x, origin_y = _input_point((x, y), "The new origin")
        if origin_x == 0.0 and origin_y == 0.0:
            return

        def moved(value, what):
            point = _stored_point(value)
            if point is None:
                raise ValueError(f"{what} is not a pair of numbers: {value!r}")
            return [point[0] - origin_x, point[1] - origin_y]

        shifted = copy.deepcopy(self._config)
        receivers = shifted.get('receivers', [])
        for index, receiver in enumerate(receivers if isinstance(receivers, list) else []):
            if isinstance(receiver, dict) and 'position' in receiver:
                receiver['position'] = moved(
                    receiver['position'],
                    f"Receiver {_receiver_label(receiver, index)} position")
        for key in ('cal_point_1', 'cal_point_2'):
            if key in shifted:
                shifted[key] = moved(shifted[key], key)
        if 'cal_points' in shifted:
            if not isinstance(shifted['cal_points'], list):
                raise ValueError(f"cal_points is not a list: {shifted['cal_points']!r}")
            shifted['cal_points'] = [moved(p, f"Calibration point P{k}")
                                     for k, p in enumerate(shifted['cal_points'], start=1)]

        self._ensure_writable()
        barriers_path = os.path.join(self.config_dir, BARRIERS_FILENAME)
        original_barriers = None
        if os.path.exists(barriers_path):
            with open(barriers_path, 'rb') as f:
                original_barriers = f.read()

        # Checks the file and writes nothing if it cannot be rewritten safely
        translate_barriers_file(barriers_path, -origin_x, -origin_y)
        try:
            write_config(self._config_path, shifted)
        except BaseException:
            if original_barriers is not None:
                try:
                    _replace_file(barriers_path, original_barriers)
                except OSError as err:
                    print(f"Could not restore {barriers_path}: {err}")
            raise
        self._config = shifted

    # ==================== Receivers accessors ====================

    def _receivers(self):
        """The stored receiver tables (the live dicts, not copies)."""
        receivers = self._config.get('receivers', [])
        if not isinstance(receivers, list):
            return []
        return [r for r in receivers if isinstance(r, dict)]

    def _find_receiver(self, receiver_id):
        """The stored receiver table with this id (not a copy), or None."""
        for receiver in self._receivers():
            if receiver.get('id') == receiver_id:
                return receiver
        return None

    def _require_receiver(self, receiver_id):
        """The stored receiver table with this id; ValueError if there is none."""
        receiver = self._find_receiver(receiver_id)
        if receiver is None:
            raise ValueError(f"Receiver with id {receiver_id} not found")
        return receiver

    @property
    def receivers(self):
        """Get the list of all receivers (read-only copy)."""
        return list(self._config.get('receivers', []))

    def get_receiver(self, receiver_id):
        """
        Get a specific receiver by ID.

        Args:
            receiver_id: The receiver ID (0-5)

        Returns:
            dict with receiver data or None if not found
        """
        receiver = self._find_receiver(receiver_id)
        return dict(receiver) if receiver is not None else None

    def get_receiver_position(self, receiver_id):
        """
        Get the position of a specific receiver.

        Args:
            receiver_id: The receiver ID (0-5)

        Returns:
            tuple (x, y) or None if not found
        """
        receiver = self.get_receiver(receiver_id)
        if receiver and 'position' in receiver:
            return tuple(receiver['position'])
        return None

    def set_receiver_position(self, receiver_id, x, y):
        """
        Set the position of a specific receiver, recompute its calibration
        distances (one per calibration point) and save to file.

        Args:
            receiver_id: The receiver ID (0-5)
            x: X coordinate
            y: Y coordinate

        Raises:
            ValueError: unknown receiver, or coordinates that are not finite numbers
        """
        receiver = self._require_receiver(receiver_id)
        receiver['position'] = _input_point((x, y), f"Receiver R{receiver_id + 1} position")
        self._update_cal_distances([receiver])
        self._save_config()

    def set_receiver_positions(self, positions):
        """
        Set the positions of several receivers at once, recompute their
        calibration distances and save to file once.

        Args:
            positions: [(receiver_id, (x, y)), ...] (the get_tower_coordinates()
                format) or {receiver_id: (x, y)}

        Raises:
            ValueError: an unknown receiver or a bad coordinate pair; nothing
                is changed then
        """
        items = positions.items() if isinstance(positions, dict) else positions
        updates = []
        for entry in items:
            try:
                receiver_id, point = entry
            except (TypeError, ValueError):
                raise ValueError(f"Expected (receiver_id, (x, y)) pairs, got {entry!r}") from None
            receiver = self._require_receiver(receiver_id)
            updates.append((receiver, _input_point(point, f"Receiver R{receiver_id + 1} position")))
        if not updates:
            return

        for receiver, position in updates:
            receiver['position'] = position
        self._update_cal_distances([receiver for receiver, _ in updates])
        self._save_config()

    def get_receiver_facing(self, receiver_id):
        """
        Get the direction a receiver faces (facing_deg).

        Args:
            receiver_id: The receiver ID (0-5)

        Returns:
            Degrees counter-clockwise from +x (0 = +x, 90 = +y), or None when
            the receiver has no usable facing_deg - it is then aimed at the
            centroid of all receivers - or is not found
        """
        receiver = self._find_receiver(receiver_id)
        if receiver is None or not _is_number(receiver.get('facing_deg')):
            return None
        return float(receiver['facing_deg'])

    def set_receiver_facing(self, receiver_id, degrees):
        """
        Set the direction a receiver faces and save to file.

        Args:
            receiver_id: The receiver ID (0-5)
            degrees: Degrees counter-clockwise from +x, or None to remove
                facing_deg (aim the receiver at the centroid of all receivers)

        Raises:
            ValueError: unknown receiver, or degrees that are not a finite number
        """
        receiver = self._require_receiver(receiver_id)
        if degrees is None:
            receiver.pop('facing_deg', None)
        else:
            try:
                value = float(degrees)
            except (TypeError, ValueError):
                value = math.nan
            if not math.isfinite(value):
                raise ValueError(
                    f"Receiver R{receiver_id + 1} facing must be a finite number, got {degrees!r}")
            receiver['facing_deg'] = value
        self._save_config()

    def get_receiver_facings(self):
        """
        Get every receiver's facing in id order (R1 first).

        Returns:
            list of 6 entries: degrees, or None for a receiver aimed at the
            centroid (or missing)
        """
        return [self.get_receiver_facing(i) for i in range(RECEIVER_COUNT)]

    def get_receiver_cal_distances(self, receiver_id):
        """
        Get the calibration distances of a specific receiver.

        Args:
            receiver_id: The receiver ID (0-5)

        Returns:
            list of calibration distances or None if not found
        """
        receiver = self.get_receiver(receiver_id)
        if receiver and 'cal_distances' in receiver:
            return list(receiver['cal_distances'])
        return None

    def set_receiver_cal_distances(self, receiver_id, distances):
        """
        Set the calibration distances of a specific receiver and save to file.

        Args:
            receiver_id: The receiver ID (0-5)
            distances: list of calibration distance values
        """
        receiver = self._require_receiver(receiver_id)
        receiver['cal_distances'] = [float(d) for d in distances]
        self._save_config()

    def get_receiver_offset(self, receiver_id):
        """
        Get the offset parameters of a specific receiver.

        Args:
            receiver_id: The receiver ID (0-5)

        Returns:
            dict with 'slope' and 'intercept' or None if not found
        """
        receiver = self.get_receiver(receiver_id)
        if receiver and 'offset' in receiver:
            return dict(receiver['offset'])
        return None

    def set_receiver_offset(self, receiver_id, slope, intercept):
        """
        Set the offset parameters of a specific receiver and save to file.

        Args:
            receiver_id: The receiver ID (0-5)
            slope: The slope value
            intercept: The intercept value
        """
        receiver = self._require_receiver(receiver_id)
        if 'offset' not in receiver:
            receiver['offset'] = {}
        receiver['offset']['slope'] = float(slope)
        receiver['offset']['intercept'] = float(intercept)
        self._save_config()

    def get_receiver_slope(self, receiver_id):
        """Get the slope offset of a specific receiver."""
        offset = self.get_receiver_offset(receiver_id)
        if offset:
            return offset.get('slope', 0.0)
        return None

    def set_receiver_slope(self, receiver_id, slope):
        """Set the slope offset of a specific receiver and save to file."""
        receiver = self._require_receiver(receiver_id)
        if 'offset' not in receiver:
            receiver['offset'] = {'slope': 0.0, 'intercept': 0.0}
        receiver['offset']['slope'] = float(slope)
        self._save_config()

    def get_receiver_intercept(self, receiver_id):
        """Get the intercept offset of a specific receiver."""
        offset = self.get_receiver_offset(receiver_id)
        if offset:
            return offset.get('intercept', 0.0)
        return None

    def set_receiver_intercept(self, receiver_id, intercept):
        """Set the intercept offset of a specific receiver and save to file."""
        receiver = self._require_receiver(receiver_id)
        if 'offset' not in receiver:
            receiver['offset'] = {'slope': 0.0, 'intercept': 0.0}
        receiver['offset']['intercept'] = float(intercept)
        self._save_config()

    # ==================== Legacy method for compatibility ====================

    def get_tower_coordinates(self):
        """
        Returns a tuple of the receiver id's coordinates in the toml
        settings file.

        Returns:
            list of (tower_id, (tower_x, tower_y))
        """
        positions = []
        for receiver in self._receivers():
            if 'position' in receiver and 'id' in receiver:
                positions.append((receiver['id'], tuple(receiver['position'])))
        return positions
