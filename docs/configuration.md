# Configuration

UltraGPS Control keeps its settings in a **configuration directory** holding
four things:

| Item | What it holds |
|------|---------------|
| `config.toml` | receiver geometry, facing and calibration |
| `barriers.toml` | virtual barriers and image overlays |
| `sensor.toml` | the receiver model of the Setup panel's **Sensor settings…** (optional) |
| `resources/` | image files referenced by the overlays |

They travel together, because a barrier is only meaningful relative to the arena
the receivers define.

## Where the configuration is found

At startup the program looks for a `config.toml` in three places and uses the
**first** one it finds:

| Priority | Directory | Typical use |
|---------:|-----------|-------------|
| 1 | `~/.config/ultragps-control/` | your own settings |
| 2 | `./config/` (next to the source) | a development checkout |
| 3 | `/etc/ultragps-control/` | defaults from the Debian package |

`$XDG_CONFIG_HOME` is honoured if set, in which case it replaces `~/.config`.

A per-user configuration therefore always wins, and a source checkout's own
`config/` directory takes priority over the system-wide defaults — so running
from a clone never silently picks up an installed package's settings.

If no configuration exists in any of the three, a default is created in
`/etc/ultragps-control/`, falling back to `~/.config/ultragps-control/` when
`/etc` is not writable (that is, when not running as root). A freshly created
default is *not* usable as-is: every receiver and calibration point is at
(0, 0), and the program prints a warning telling you to initialise it — place
the receivers on the **Setup** panel, then set the slopes and run the
**System Origin Calibration** on the **Calibration** panel.

Pass `--config` to bypass the search entirely:

```bash
ultragps-control --config /path/to/config.toml
```

The directory containing that file becomes the configuration directory, so
`barriers.toml`, `sensor.toml` and `resources/` are read from beside it. A `config.toml` that
cannot be read — missing, unreadable or malformed — stops the program at startup
with a message naming the file (exit status 1).

!!! note "Saving from a read-only directory"
    When the active configuration lives somewhere you cannot write — the usual
    case for `/etc` after installing the package — the first save copies the
    whole directory into `~/.config/ultragps-control/` and switches to it.
    Nothing already in the user directory is overwritten. From then on the user
    copy is found first, so your settings persist and the packaged defaults stay
    pristine. The program records whether the configuration verifies each time
    it starts, so in practice this happens on the first start.

## config.toml

```toml
valid_settings = true       # set by the Setup panel once verification passes
cal_state = 1
calibration_reads = 5       # hits on the modal tick each receiver needs per collection
number_of_receivers = 6     # active receivers R1..RN, 3 to 6
serial_port = '/dev/ttyACM0'
units = 'cm'
cal_point_1 = [-88.0, 77.0]       # always equal to cal_points[0] (older readers)
cal_point_2 = [87.0, 87.0]        # always equal to cal_points[1]
cal_points = [[-88.0, 77.0], [87.0, 87.0], [-88.0, -88.0], [87.0, -88.0]]

[[receivers]]
cal_distances = [197.29, 289.91, 42.43, 207.18]  # to each calibration point, in order
origin_distance = 166.87720036002523  # to (0, 0); derived from position
id = 0
position = [-118.0, -118.0]       # where the receiver sits, in cm
facing_deg = 45.0                 # optional: where it points (0 = +X, 90 = +Y)

    [receivers.offset]
    intercept = 25.44                 # ticks -> cm
    slope = 0.272
```

There are always six `[[receivers]]` blocks, with ids 0 through 5 (R1–R6 in the
GUI), one per base-station channel. Only the first `number_of_receivers` of them
are active; the others are dormant but keep their position, facing and
calibration, so that they come back calibrated when the arena uses them again.

All coordinates and distances are **centimetres**. Positions are signed, relative
to the arena's (0, 0) — the centre of a layout built from the W × H template, or
wherever you have moved it to. The arena size is not stored; it is derived from
the receiver positions.

| Key | Meaning |
|-----|---------|
| `valid_settings` | `true` once the whole file verifies (6 receiver entries, `number_of_receivers` from 3 to 6, active receivers with positions not all on one spot or one line, 2–4 distinct calibration points, matching `cal_distances`, offsets for the active receivers). Set by **Verify & Save** on the Setup panel and checked at every start |
| `calibration_reads` | How many times each receiver must read its most frequent tick before a collection is done — the System Origin Calibration uses it |
| `cal_points` | The 2 to 4 calibration points P1–P4 of the earlier multi-point calibration, in order. No panel edits them any more; they are kept, checked by verification, and moved with (0, 0). A file without it (written before it existed) uses `[cal_point_1, cal_point_2]` |
| `cal_point_1`, `cal_point_2` | Kept equal to the first two `cal_points` on every save, for older programs that only know two points |
| `number_of_receivers` | How many receivers the arena uses, N from 3 to 6: R1 to RN (ids 0 to N − 1) are active. Chosen when placing the receivers by clicking on the Setup panel and written by **Verify & Save**. Dormant receivers (ids N to 5) are ignored by the positioning, the coverage estimate, calibration and the displays, but never removed from the file. A missing or invalid value means all six |
| `receivers[].position` | The receiver's position |
| `receivers[].facing_deg` | **Optional.** The direction the receiver points, in degrees (0 = +X, 90 = +Y). Absent means it is aimed at the centroid of the active receivers. Used by the coverage estimate, not by the position solver |
| `receivers[].cal_distances` | The receiver's distance to each calibration point, one per point in `cal_points` order. Recomputed whenever the receivers or the calibration points are saved; calibration fits against them |
| `receivers[].origin_distance` | **Derived.** The receiver's distance to (0, 0), `hypot(x, y)` of its `position`, kept for all six entries (dormant ones too). Recomputed and saved at startup and whenever positions change — saving a layout, moving (0, 0) — and only written when a value actually changed. The System Origin Calibration takes it as the true distance. Not needed for `valid_settings`; a missing value is filled in at the next start |
| `receivers[].offset` | `slope` and `intercept`. Each receiver converts ticks to centimetres with `distance = tick * slope + intercept`. Edited on the Calibration panel's **Offsets** tab (a slope must be greater than 0); the default slope is 0.272 (`DEFAULT_SLOPE` in `SettingsModule.py`). The **System Origin Calibration** writes new intercepts and leaves the slopes as they are |
| `cal_state`, `serial_port`, `units` | Kept in the file, but not used: the units are always centimetres, and the serial port is UltraGpsXY's business (its `-s` option) |

### Unknown keys are kept

Every program part that writes `config.toml` — the settings panels and the
calibration library — goes through one writer,
`ultragps_calibration.write_config`. It keeps every key, known or not: a value
added by hand, a newer program's key, or a receiver's `facing_deg` written by
another tool all survive a save, in a stable order. Floats are written at full
precision, and an unchanged file is rewritten byte for byte. Each save replaces
the file atomically (a temporary file, then a rename), so a crash never leaves
half a file.

### What moving (0, 0) rewrites

Moving the arena's origin — the **Set (0, 0)** tool on the Calibration or Setup
panel — makes the chosen point (0, 0) by translating the whole frame. It rewrites
`config.toml` and `barriers.toml`:

- `config.toml`: every receiver `position` and `origin_distance`, `cal_points`
  and `cal_point_1` / `cal_point_2`;
- `barriers.toml`: every barrier's coordinates (polygon `vertices`, circle
  `center`, line `point1` / `point2`) and every image overlay's centre.

A translation changes no distance between the receivers and the
calibration points, so `cal_distances`, slopes, intercepts and facings stay as
they are, and no new calibration is needed. `sensor.toml` holds
no coordinates and is not touched.

Everything is checked before anything is written. `barriers.toml` is rewritten in
the barrier editor's own format, so if it holds something that format does not
have — an unknown key, a malformed entry — the move is refused with a message and
neither file changes. `barriers.toml` is replaced first, then `config.toml`; if
`config.toml` cannot be written, `barriers.toml` is put back. In a read-only
directory both files go to the `~/.config` copy described above.

## sensor.toml

The receiver model — what a receiver can hear — used by the Setup panel's
coverage estimate. It is written by **OK** in the Setup panel's **Sensor
settings…** pop-up ([Usage](usage.md#coverage-estimate)) and re-read whenever
the configuration is. Every field of the pop-up except the sampling window is
stored, in a `[sensor]` table:

```toml
[sensor]
threshold = false        # true: signal-strength model; false: hard limits only
snr_db = 30.0            # burst over the detection threshold on axis at 1 m, dB
rx_beam_deg = 27.5       # receiver beam's −6 dB half-angle, degrees
rise_cycles = 6.0        # transducer ring-up, cycles of 40 kHz
burst_cycles = 8         # cycles in the transmitter's burst
temperature_c = 20.0     # air temperature, °C
humidity_pct = 50.0      # relative humidity, %
burst_lead_ms = 4.0      # transmitter fires this long after the trigger
rx_ready_ms = 3.0        # receivers listen from this long after the trigger
timeout_ms = 38.0        # the HC-SR04 gives up this long after it listens
max_range_cm = 400.0     # farthest distance heard, or "none" for no limit
fov_half_deg = 15.0      # hard field of view (half-angle), or "none"
min_range_cm = 2.0       # nearest distance heard; 0 for no limit
```

The sampling window is not stored: it is the base station's `H` setting, taken
from the base station once it is known.

The file is optional. Without it the HC-SR04 datasheet limits shown above are
used — 2–400 cm within ±15° of the receiver's axis, with the signal-strength
model off. A file that cannot be read, or holds a value out of range, is ignored
in the same way, with a warning in the log and on the panels that use it; the next
**OK** in the pop-up replaces it. Saves are atomic and, in a read-only
directory, go to the `~/.config` copy like every other save. **Reset** in the
pop-up goes back to the datasheet limits; the file changes on **OK**.

## barriers.toml

Each barrier is a `[[barrier]]` block:

```toml
[[barrier]]
name = "Goal-Barrier"
type = "polygon"            # polygon | circle | line
trigger_mode = "event"      # event (on transition) | continuous (every update)
trigger_when = "inside"     # inside | outside
callback_name = "on_barrier"
color = "#00FF00"
alpha = 0.3
vertices = [[-35.6, 8.88], [-30.9, -3.08], [-15.96, -0.09], [-18.95, 12.29]]
```

A `circle` uses `center` and `radius` instead of `vertices`; a `line` uses
`point1`, `point2`, and a `thickness` (the total proximity margin in cm, not the
half-width).

Optional `[[image]]` blocks place a picture on the arena as a backdrop — a court
diagram, say — and can be traced into a polygon barrier from the Barriers panel.
Their image files live in `resources/` beside `barriers.toml`.

Barriers are normally drawn in the GUI rather than written by hand; see
[Usage](usage.md#barriers). Entries the loader cannot read (a value of the wrong
type, coordinates that are not pairs of numbers) are skipped rather than
stopping the program.
