# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

UltraGPS-Python is a real-time ultra-wideband (UWB) positioning system that calculates the 2D position of a mobile transmitter using distance measurements from 6 stationary receivers via multilateration algorithms. The system provides live visualization through a Tkinter/Matplotlib GUI.

## Running the Application

```bash
# Activate virtual environment (from project root)
source venv/bin/activate

# Run main application
python main.py
```

The application requires a running UltraGPS server at 127.0.0.1 with UDP ports 8000 (control), 8002 (distances), and 8003 (serial).

## Architecture

### Module Structure

**ControlModule.py** - UDP networking layer
- `ControlModule`: High-level interface for UltraGPS server communication
- `CommsModule`: Thread-based UDP send/receive with queue-based async communication
- `NetworkClass`: Low-level socket wrapper

**GraphicsModule.py** - GUI and visualization
- `GraphicsModule`: Main controller managing Tkinter window and threading
- `PositionWindow`: Matplotlib-based 2D visualization with position trail (last 50 points)

**PositionModule.py** - Mathematical positioning
- `PositionModule`: Core multilateration using OLS initial estimate + non-linear LS refinement (Levenberg-Marquardt)
- `StablePositionEstimator`, `CEPPositioning`, `StabilizedPositioningSystem`: Experimental algorithms for improved accuracy

**SettingsModule.py** - Configuration parser for config.toml

### Data Flow

```
UltraGPS Server (UDP 8000/8002/8003)
    ↓
ControlModule (request distances)
    ↓
PositionModule (multilateration math)
    ↓
GraphicsModule (Tkinter + Matplotlib display)
```

### Threading Model

- Main thread: Tkinter event loop (blocking via `root.mainloop()`)
- Position update thread: Polls ControlModule, runs multilateration, updates GUI
- UDP send/receive threads: Daemon threads in CommsModule

Thread-safe communication uses `queue.Queue` for GUI updates. All matplotlib canvas updates go through `_refresh_animations()` at 10ms intervals.

## Configuration

**config.toml** contains receiver calibration data:
- 6 receivers with id 0-5
- Each has: position [x, y] in cm, calibration distances, offset parameters (slope/intercept)
- Arena size: 251.4 x 376.4 cm (hardcoded in SettingsModule)

## Key Implementation Details

- Receiver count hardcoded to 6 throughout
- Current multilateration uses subset [0,2,3,4,5] (receiver 1 excluded)
- Units are centimeters
- GUI labels are 1-indexed (id+1) to match physical receiver labels
- Position trail displays last 50 points via `collections.deque`

## Dependencies

Key packages: numpy, scipy (least_squares), matplotlib, tkinter (builtin), tomllib, filterpy (Kalman filter, optional)
