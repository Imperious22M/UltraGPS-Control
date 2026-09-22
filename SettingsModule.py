import os
import shutil
import tomllib

#: Directory name used under ~/.config and /etc.
APP_NAME = 'ultragps-control'

#: Name of the main settings file inside a config directory.
CONFIG_FILENAME = 'config.toml'


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
            print("Config file settings not initialized, arena size set to 0")
            if err:
                print(err)

    def _get_default_config(self):
        """
        Return a default configuration with empty/uninitialized values.
        These values require initialization before the system can be used.
        """
        return {
            'valid_settings': False,
            'cal_state': 0,
            'calibration_reads': 5,
            'number_of_receivers': 6,
            'serial_port': '/dev/ttyACM0',
            'units': 'cm',
            'cal_point_1': [0.0, 0.0],
            'cal_point_2': [0.0, 0.0],
            'receivers': [
                {
                    'id': 0,
                    'position': [0.0, 0.0],
                    'cal_distances': [0.0, 0.0],
                    'offset': {'slope': 0.0, 'intercept': 0.0}
                },
                {
                    'id': 1,
                    'position': [0.0, 0.0],
                    'cal_distances': [0.0, 0.0],
                    'offset': {'slope': 0.0, 'intercept': 0.0}
                },
                {
                    'id': 2,
                    'position': [0.0, 0.0],
                    'cal_distances': [0.0, 0.0],
                    'offset': {'slope': 0.0, 'intercept': 0.0}
                },
                {
                    'id': 3,
                    'position': [0.0, 0.0],
                    'cal_distances': [0.0, 0.0],
                    'offset': {'slope': 0.0, 'intercept': 0.0}
                },
                {
                    'id': 4,
                    'position': [0.0, 0.0],
                    'cal_distances': [0.0, 0.0],
                    'offset': {'slope': 0.0, 'intercept': 0.0}
                },
                {
                    'id': 5,
                    'position': [0.0, 0.0],
                    'cal_distances': [0.0, 0.0],
                    'offset': {'slope': 0.0, 'intercept': 0.0}
                }
            ]
        }

    def _calculate_arena_size(self):
        """
        Calculate the arena size (width x height) from receiver positions.

        Width (x-plane): max of:
          - |receiver 3 - receiver 6| x distance (ids 2 and 5)
          - |receiver 1 - receiver 4| x distance (ids 0 and 3)

        Height (y-plane): max of:
          - |receiver 3 - receiver 1| y distance (ids 2 and 0)
          - |receiver 6 - receiver 4| y distance (ids 5 and 3)

        Returns:
            tuple (width, height) in the configured units (cm)
        """
        # Get positions for relevant receivers (real-world labels 1,3,4,6 = ids 0,2,3,5)
        pos_1 = self.get_receiver_position(0)  # Receiver 1
        pos_3 = self.get_receiver_position(2)  # Receiver 3
        pos_4 = self.get_receiver_position(3)  # Receiver 4
        pos_6 = self.get_receiver_position(5)  # Receiver 6

        # Return zero size if positions are not set
        if not all([pos_1, pos_3, pos_4, pos_6]):
            return (0.0, 0.0)

        # Calculate width (x-plane distances)
        width_3_6 = abs(pos_3[0] - pos_6[0])  # |receiver 3 x - receiver 6 x|
        width_1_4 = abs(pos_1[0] - pos_4[0])  # |receiver 1 x - receiver 4 x|
        width = round(max(width_3_6, width_1_4), 2)

        # Calculate height (y-plane distances)
        height_3_1 = abs(pos_3[1] - pos_1[1])  # |receiver 3 y - receiver 1 y|
        height_6_4 = abs(pos_6[1] - pos_4[1])  # |receiver 6 y - receiver 4 y|
        height = round(max(height_3_1, height_6_4), 2)

        return (width, height)

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
        """Save the internal config to the TOML file."""
        self._ensure_writable()
        with open(self._config_path, 'w') as f:
            self._write_toml(f, self._config)

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

    def _write_toml(self, f, config):
        """Write config dictionary to TOML format."""
        # Write top-level scalar values first
        for key in ['valid_settings', 'cal_state', 'calibration_reads', 'number_of_receivers', 'serial_port', 'units']:
            if key in config:
                value = config[key]
                if isinstance(value, str):
                    f.write(f"{key} = '{value}'\n")
                elif isinstance(value, bool):
                    f.write(f"{key} = {str(value).lower()}\n")
                else:
                    f.write(f"{key} = {value}\n")

        # Write calibration points (arrays)
        for key in ['cal_point_1', 'cal_point_2']:
            if key in config:
                value = config[key]
                f.write(f"{key} = {value}\n")

        f.write("\n")

        # Write receivers array
        for receiver in config.get('receivers', []):
            f.write("[[receivers]]\n")
            if 'cal_distances' in receiver:
                f.write(f"cal_distances = {receiver['cal_distances']}\n")
            if 'id' in receiver:
                f.write(f"id = {receiver['id']}\n")
            if 'position' in receiver:
                f.write(f"position = {receiver['position']}\n")
            f.write("\n")
            if 'offset' in receiver:
                f.write("    [receivers.offset]\n")
                f.write(f"    intercept = {receiver['offset']['intercept']}\n")
                f.write(f"    slope = {receiver['offset']['slope']}\n")
            f.write("\n")

    def reload_config(self):
        """Reload the configuration from the file."""
        self._config = self._load_config()

    def verify_settings(self):
        """
        Verify that all required settings have been initialized.

        Checks:
        - All 6 receivers exist
        - All receiver positions are non-zero (at least one coordinate)
        - All receiver calibration distances are set
        - All receiver offsets have been set
        - Calibration points are set (not both zeros)

        Returns:
            tuple (is_valid: bool, errors: list of str)
        """
        errors = []

        # Check number of receivers
        receivers = self._config.get('receivers', [])
        if len(receivers) != 6:
            errors.append(f"Expected 6 receivers, found {len(receivers)}")

        # Check each receiver
        for i in range(6):
            receiver = self.get_receiver(i)
            if receiver is None:
                errors.append(f"Receiver {i} (label {i+1}) not found")
                continue

            # Check position is set (not both zeros)
            pos = receiver.get('position', [0.0, 0.0])
            if pos[0] == 0.0 and pos[1] == 0.0:
                errors.append(f"Receiver {i} (label {i+1}) position not initialized")

            # Check calibration distances
            cal_dist = receiver.get('cal_distances', [0.0, 0.0])
            if all(d == 0.0 for d in cal_dist):
                errors.append(f"Receiver {i} (label {i+1}) calibration distances not set")

            # Check offset values exist
            offset = receiver.get('offset', {})
            if 'slope' not in offset or 'intercept' not in offset:
                errors.append(f"Receiver {i} (label {i+1}) offset parameters missing")

        # Check calibration points are set
        cal_p1 = self._config.get('cal_point_1', [0.0, 0.0])
        cal_p2 = self._config.get('cal_point_2', [0.0, 0.0])
        if cal_p1[0] == 0.0 and cal_p1[1] == 0.0:
            errors.append("Calibration point 1 not initialized")
        if cal_p2[0] == 0.0 and cal_p2[1] == 0.0:
            errors.append("Calibration point 2 not initialized")

        is_valid = len(errors) == 0
        return (is_valid, errors)

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
        Get the arena size (width, height) calculated from receiver positions.
        Always recalculates from current receiver positions.
        """
        return self._calculate_arena_size()

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
        return self._config.get('number_of_receivers', 6)

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

    @property
    def cal_point_1(self):
        """Get calibration point 1 coordinates [x, y]."""
        return self._config.get('cal_point_1', [0.0, 0.0])

    @cal_point_1.setter
    def cal_point_1(self, value):
        """Set calibration point 1 coordinates and save to file."""
        self._config['cal_point_1'] = [float(value[0]), float(value[1])]
        self._save_config()

    @property
    def cal_point_2(self):
        """Get calibration point 2 coordinates [x, y]."""
        return self._config.get('cal_point_2', [0.0, 0.0])

    @cal_point_2.setter
    def cal_point_2(self, value):
        """Set calibration point 2 coordinates and save to file."""
        self._config['cal_point_2'] = [float(value[0]), float(value[1])]
        self._save_config()

    # ==================== Receivers accessors ====================

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
        for receiver in self._config.get('receivers', []):
            if receiver.get('id') == receiver_id:
                return dict(receiver)
        return None

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
        Set the position of a specific receiver and save to file.

        Args:
            receiver_id: The receiver ID (0-5)
            x: X coordinate
            y: Y coordinate
        """
        for receiver in self._config.get('receivers', []):
            if receiver.get('id') == receiver_id:
                receiver['position'] = [float(x), float(y)]
                self._save_config()
                return
        raise ValueError(f"Receiver with id {receiver_id} not found")

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
        for receiver in self._config.get('receivers', []):
            if receiver.get('id') == receiver_id:
                receiver['cal_distances'] = [float(d) for d in distances]
                self._save_config()
                return
        raise ValueError(f"Receiver with id {receiver_id} not found")

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
        for receiver in self._config.get('receivers', []):
            if receiver.get('id') == receiver_id:
                if 'offset' not in receiver:
                    receiver['offset'] = {}
                receiver['offset']['slope'] = float(slope)
                receiver['offset']['intercept'] = float(intercept)
                self._save_config()
                return
        raise ValueError(f"Receiver with id {receiver_id} not found")

    def get_receiver_slope(self, receiver_id):
        """Get the slope offset of a specific receiver."""
        offset = self.get_receiver_offset(receiver_id)
        if offset:
            return offset.get('slope', 0.0)
        return None

    def set_receiver_slope(self, receiver_id, slope):
        """Set the slope offset of a specific receiver and save to file."""
        for receiver in self._config.get('receivers', []):
            if receiver.get('id') == receiver_id:
                if 'offset' not in receiver:
                    receiver['offset'] = {'slope': 0.0, 'intercept': 0.0}
                receiver['offset']['slope'] = float(slope)
                self._save_config()
                return
        raise ValueError(f"Receiver with id {receiver_id} not found")

    def get_receiver_intercept(self, receiver_id):
        """Get the intercept offset of a specific receiver."""
        offset = self.get_receiver_offset(receiver_id)
        if offset:
            return offset.get('intercept', 0.0)
        return None

    def set_receiver_intercept(self, receiver_id, intercept):
        """Set the intercept offset of a specific receiver and save to file."""
        for receiver in self._config.get('receivers', []):
            if receiver.get('id') == receiver_id:
                if 'offset' not in receiver:
                    receiver['offset'] = {'slope': 0.0, 'intercept': 0.0}
                receiver['offset']['intercept'] = float(intercept)
                self._save_config()
                return
        raise ValueError(f"Receiver with id {receiver_id} not found")

    # ==================== Legacy method for compatibility ====================

    def get_tower_coordinates(self):
        """
        Returns a tuple of the receiver id's coordinates in the toml
        settings file.

        Returns:
            list of (tower_id, (tower_x, tower_y))
        """
        positions = []
        for receiver in self._config.get('receivers', []):
            if 'position' in receiver and 'id' in receiver:
                positions.append((receiver['id'], tuple(receiver['position'])))
        return positions
