from ControlModule import CommsModule, ControlModule
#from ControlModule import NetworkClass
from GraphicsModule import PositionWindow, MainWindow
from GraphicsModule import GraphicsModule

import argparse
import os
import time

def main():
    default_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.toml')

    parser = argparse.ArgumentParser(description="UltraGPS positioning system")
    parser.add_argument("--ip", default="127.0.0.1", help="IP address of the UltraGPS server (default: 127.0.0.1)")
    parser.add_argument("--config", default=default_config, help=f"Path to config.toml (default: {default_config})")
    args = parser.parse_args()

    testGraphics = GraphicsModule(ip_address=args.ip, config_path=args.config)

    testGraphics.start_tk_window()
    testGraphics.show_main_window()  # Show main menu first

    # Main loop will block all execution. All async tasks should begin prior to this call
    testGraphics.tkinter_main()

if __name__ == "__main__":
    main()