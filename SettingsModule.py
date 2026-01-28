import tomllib
import os


class SettingsModule:
    def __init__(self):
        """
        Initialize the SettingsModule by loading config.toml into memory.
        All reads reference the internal copy, all writes immediately save to file.
        """
        # Get the config file path
        current_dir = os.path.dirname(os.path.abspath(__file__))
        self._config_path = os.path.join(current_dir, 'config.toml')

        # Load the config file into internal storage
        self._config = self._load_config()

        # Arena size (calculated from receiver positions)
        self.arena_size = self._calculate_arena_size()

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
        """Load the TOML configuration file into memory."""
        with open(self._config_path, 'rb') as f:
            return tomllib.load(f)

    def _save_config(self):
        """Save the internal config to the TOML file."""
        with open(self._config_path, 'w') as f:
            self._write_toml(f, self._config)

    def _write_toml(self, f, config):
        """Write config dictionary to TOML format."""
        # Write top-level scalar values first
        for key in ['cal_state', 'calibration_reads', 'number_of_receivers', 'serial_port', 'units']:
            if key in config:
                value = config[key]
                if isinstance(value, str):
                    f.write(f"{key} = '{value}'\n")
                else:
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

    # ==================== Top-level field accessors ====================

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
