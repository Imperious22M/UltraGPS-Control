# Configuration

UltraGPS Control keeps its settings in a **configuration directory** holding
three things:

| Item | What it holds |
|------|---------------|
| `config.toml` | receiver geometry, facing and calibration |
| `barriers.toml` | virtual barriers and image overlays |
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
the receivers on the **Setup** panel, then choose calibration points and
calibrate on the **Calibration** panel.

Pass `--config` to bypass the search entirely:

```bash
ultragps-control --config /path/to/config.toml
```

The directory containing that file becomes the configuration directory, so
`barriers.toml` and `resources/` are read from beside it. A `config.toml` that
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
calibration_reads = 5       # hits on the modal tick each receiver needs per point
number_of_receivers = 6
serial_port = '/dev/ttyACM0'
units = 'cm'
cal_point_1 = [-88.0, 77.0]       # always equal to cal_points[0] (older readers)
cal_point_2 = [87.0, 87.0]        # always equal to cal_points[1]
cal_points = [[-88.0, 77.0], [87.0, 87.0], [-88.0, -88.0], [87.0, -88.0]]

[[receivers]]
cal_distances = [197.29, 289.91, 42.43, 207.18]  # to each calibration point, in order
id = 0
position = [-118.0, -118.0]       # where the receiver sits, in cm
facing_deg = 45.0                 # optional: where it points (0 = +X, 90 = +Y)

    [receivers.offset]
    intercept = -20.651537789998116   # ticks -> cm
    slope = 0.3758116957204082
```

There are six `[[receivers]]` blocks, with ids 0 through 5 (R1–R6 in the GUI).

All coordinates and distances are **centimetres**. Positions are signed, relative
to the arena's (0, 0) — the centre of a layout built from the W × H template, or
wherever you have moved it to. The arena size is not stored; it is derived from
the receiver positions.

| Key | Meaning |
|-----|---------|
| `valid_settings` | `true` once the whole file verifies (6 receivers with positions not all on one spot or one line, 2–4 distinct calibration points, matching `cal_distances`, offsets). Set by **Verify & Save** on the Setup panel and checked at every start |
| `calibration_reads` | How many times each receiver must read its most frequent tick before a calibration point is done |
| `cal_points` | The 2 to 4 calibration points P1–P4, in order. Written by **Save points** on the Calibration panel. A file without it (written before it existed) uses `[cal_point_1, cal_point_2]` |
| `cal_point_1`, `cal_point_2` | Kept equal to the first two `cal_points` on every save, for older programs that only know two points |
| `receivers[].position` | The receiver's position |
| `receivers[].facing_deg` | **Optional.** The direction the receiver points, in degrees (0 = +X, 90 = +Y). Absent means it is aimed at the centroid of all the receivers. Used by the coverage estimate and the calibration-point shading, not by the position solver |
| `receivers[].cal_distances` | The receiver's distance to each calibration point, one per point in `cal_points` order. Recomputed whenever the receivers or the calibration points are saved; calibration fits against them |
| `receivers[].offset` | `slope` and `intercept`: what calibration produces. Each receiver converts ticks to centimetres with `distance = tick * slope + intercept` |
| `cal_state`, `number_of_receivers`, `serial_port`, `units` | Kept in the file, but not used: there are always six receivers, the units are always centimetres, and the serial port is UltraGpsXY's business (its `-s` option) |

!!! warning "The shipped calibration points need replacing"
    The default `config.toml` still has only `cal_point_1 = [0.0, 59.0]` and
    `cal_point_2 = [-59.0, 0.0]`. R3 and R4 are the same distance from both, so
    no calibration on them can fit those two receivers; the Calibration panel
    refuses them. See [Calibration points](usage.md#calibration-points) for
    choosing better ones.

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
**both** files of the configuration directory:

- `config.toml`: every receiver `position`, `cal_points` and
  `cal_point_1` / `cal_point_2`;
- `barriers.toml`: every barrier's coordinates (polygon `vertices`, circle
  `center`, line `point1` / `point2`) and every image overlay's centre.

A translation changes no distance, so `cal_distances`, slopes, intercepts and
facings stay as they are, and no new calibration is needed.

Everything is checked before anything is written. `barriers.toml` is rewritten in
the barrier editor's own format, so if it holds something that format does not
have — an unknown key, a malformed entry — the move is refused with a message and
neither file changes. `barriers.toml` is replaced first, then `config.toml`; if
`config.toml` cannot be written, `barriers.toml` is put back. In a read-only
directory both files go to the `~/.config` copy described above.

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
