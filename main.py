from ControlModule import CommsModule, ControlModule
from ControlModule import NetworkClass
from GraphicsModule import PositionWindow
from GraphicsModule import GraphicsModule

import time

def main():
    # C-like main class to test various position discrimination techniques
    #    self.control_module = controlModule = ControlModule("127.0.0.1")
    testGraphics = GraphicsModule()

    testGraphics.start_tk_window()
    testGraphics.show_position_window()

    # Main loop will block all execution. All async tasks should begin prior to this call
    testGraphics.tkinter_main()

if __name__ == "__main__":
    main()