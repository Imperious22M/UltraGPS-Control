# UltraGPS positioning simulator

A desktop simulator of the UltraGPS positioning system. It fakes the six
HC-SR04 receivers, feeds their tick counts into the **real**
`ultragps_position` library from `libs/`, and shows every positioning algorithm
solving the same frame side by side.

![Simulator window](screenshot.png)

## Run it

From the repository root, with NumPy, SciPy and Matplotlib installed (the
`.venv` made by `./install.sh` already has them):

```bash
python3 simulator/app.py
python3 simulator/app.py --config path/to/config.toml --max-differential 50 --seed 1
```

It reads receiver positions and calibration from `config/config.toml` by
default, so the arena matches the one the control program uses.

## What you can do

| Control | Effect |
|---------|--------|
| Click the arena | The transmitter moves there at the set speed |
| **Circle path** | The transmitter drives a 100 cm circle |
| **Receivers** R0–R5 | Switch a receiver off (it reports 0 ticks, as unused pins do) |
| **Algorithms** | Show or hide each solver's estimate |
| Noise σ | Gaussian range noise in cm |
| Dropouts | Chance a reading is missed and reported as 0 ticks |
| Bad values | Chance a reading is a random wrong range (multipath, EMI) |
| Air temp | Speed-of-sound change against the 20 °C calibration |
| Speed | How fast the transmitter moves |

The panel on the right shows each algorithm's current and RMS error against the
true position, how many frames it failed, which receivers the library's filter
kept, which subset CEP picked, and the raw ticks and distances per receiver.

On the arena, each receiver's colour shows what its sensor did this frame
(green ok, grey off, orange missed or out of range, red bad value), a thick
outline marks the receivers CEP used, and each ring is the measured range —
solid if the library kept that reading, dotted red if its filter dropped it.

## Algorithms shown

All three come from `ultragps_position` and see the same filtered receivers,
because the simulator calls `get_position_full()` exactly as
`windows/position_window.py` does.

- **OLS** — linear least squares on the squared-range differences. This is the
  seed both other solvers start from.
- **LM** — the OLS seed refined by Levenberg-Marquardt over all kept receivers.
- **CEP** — tries receiver subsets and keeps the one with the smallest Circular
  Error Probable, falling back to the last good fix when out of bounds.

## Sensor model

`sensor_model.py` turns a true position into ticks: straight-line range,
speed-of-sound scaling for temperature, Gaussian noise, then the faults above.
Ranges beyond the HC-SR04's 400 cm read as 0 ticks. Distances are converted to
ticks by inverting each receiver's calibration (`tick = (d - intercept) / slope`),
so the library's own `tick * slope + intercept` recovers them.

## Code layout

| File | Role |
|------|------|
| `sensor_model.py` | Physics and faults: position → ticks |
| `solvers.py` | Imports the library from `libs/` and runs each algorithm on one frame |
| `simulation.py` | Run state: moves the transmitter, steps sensor + solvers, tracks error |
| `app.py` | Matplotlib window and controls |
| `test_simulator.py` | Headless checks: `python3 -m unittest simulator/test_simulator.py` |
