from ControlModule import CommsModule, ControlModule
from ControlModule import NetworkClass
from GraphicsModule import PositionWindow, MainWindow
from GraphicsModule import GraphicsModule

import argparse
import time

def main():
    parser = argparse.ArgumentParser(description="UltraGPS positioning system")
    parser.add_argument("--ip", default="127.0.0.1", help="IP address of the UltraGPS server (default: 127.0.0.1)")
    args = parser.parse_args()

    testGraphics = GraphicsModule(ip_address=args.ip)

    testGraphics.start_tk_window()
    testGraphics.show_main_window()  # Show main menu first

    # Main loop will block all execution. All async tasks should begin prior to this call
    testGraphics.tkinter_main()

if __name__ == "__main__":
    main()