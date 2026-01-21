import tomllib
import os

class SettingsModule:
    def __init__(self):
        # ALL units are in cm
        self.arena_size = (125.7*2, 188.2*2) # Width x Height of the arena
        pass
    def get_tower_coordinates(self):
        """
        Returns a tuple of the receiver id's coordinates in the toml
        settings file

        Returns:   
            (tower id, (tower_x, tower_y))
        """
        # Get the directory where this file is located
        current_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(current_dir, 'config.toml')
        
        # Load the TOML configuration file
        with open(config_path, 'rb') as f:
            config = tomllib.load(f)
        
        # Extract positions from receivers array
        positions = []
        for receiver in config.get('receivers', []):
            if 'position' and 'id' in receiver:
                positions.append((receiver['id'],tuple(receiver['position']) ) )
        
        return positions