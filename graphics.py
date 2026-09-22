import atexit
import sys
import argparse
import logging

import matplotlib
matplotlib.use('QtAgg')

from PyQt6.QtWidgets import QApplication
from windows.main_window import UltraGPSMainWindow
from ultragps_server import UltraGPSServer

SERVER_CMD_TCP     = "cmd"
SERVER_POS_UDP     = "pos"
SERVER_NMEA_UDP    = "nmea"
SERVER_BARRIER_TCP = "barrier_tcp"
SERVER_BARRIER_UDP = "barrier"


def main():
    parser = argparse.ArgumentParser(description="UltraGPS positioning system")
    parser.add_argument("--ip", default="127.0.0.1", help="IP address of the UltraGPS server")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.toml (default: search ~/.config/ultragps-control, "
             "./config, then /etc/ultragps-control)",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)

    server = UltraGPSServer()
    server.start_tcp_port(8000, name=SERVER_CMD_TCP)
    server.start_tcp_port(8004, name=SERVER_BARRIER_TCP)
    server.start_udp_port(8001, name=SERVER_POS_UDP)
    server.start_udp_port(8002, name=SERVER_NMEA_UDP)
    server.start_udp_port(8003, name=SERVER_BARRIER_UDP)
    atexit.register(server.stop_all)

    window = UltraGPSMainWindow(ip_address=args.ip, config_path=args.config, server=server)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
     # Enable logging
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

    main()
