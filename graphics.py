import sys
import os
import argparse

import matplotlib
matplotlib.use('QtAgg')

from PyQt6.QtWidgets import QApplication
from windows.main_window import UltraGPSMainWindow


def main():
    default_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.toml')
    parser = argparse.ArgumentParser(description="UltraGPS positioning system")
    parser.add_argument("--ip", default="127.0.0.1", help="IP address of the UltraGPS server")
    parser.add_argument("--config", default=default_config, help="Path to config.toml")
    args = parser.parse_args()

    app = QApplication(sys.argv)

    window = UltraGPSMainWindow(ip_address=args.ip, config_path=args.config)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
