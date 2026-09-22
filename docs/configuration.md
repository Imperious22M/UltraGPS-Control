# Configuration

UltraGPS Control keeps its settings in a **configuration directory** holding
three things:

| Item | What it holds |
|------|---------------|
| `config.toml` | receiver geometry and calibration |
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
default is *not* usable as-is: every receiver position is zero, and the program
prints a warning telling you to initialise it from the **Setup** panel.

Pass `--config` to bypass the search entirely:

```bash
ultragps-control --config /path/to/config.toml
```

The directory containing that file becomes the configuration directory, so
`barriers.toml` and `resources/` are read from beside it.

!!! note "Saving from a read-only directory"
    When the active configuration lives somewhere you cannot write — the usual
    case for `/etc` after installing the package — the first save copies the
    whole directory into `~/.config/ultragps-control/` and switches to it.
    Nothing already in the user directory is overwritten. From then on the user
    copy is found first, so your settings persist and the packaged defaults stay
    pristine.

## config.toml

```toml
valid_settings = true       # set by the Setup panel once verification passes
cal_state = 1               # how far calibration has progressed
calibration_reads = 5       # readings per receiver per calibration run
number_of_receivers = 6
serial_port = '/dev/ttyACM0'
units = 'cm'
cal_point_1 = [0.0, 76.0]   # known positions used for calibration
cal_point_2 = [-76.0, 0.0]

[[receivers]]
id = 0
position = [-127.0, -186.94]          # where the receiver sits, in cm
cal_distances = [292.004, 193.772]    # distance to cal_point_1 and _2

    [receivers.offset]
    slope = 0.9630612464305284        # ticks -> cm
    intercept = -139.44725418428027
```

There are six `[[receivers]]` blocks, with ids 0 through 5.

All coordinates and distances are **centimetres**, with the origin at the centre
of the arena — receiver positions are signed. The arena size is not stored; it is
derived from the receiver positions.

The `offset` values are what calibration produces. Each receiver converts ticks
to centimetres with `distance = tick * slope + intercept`.

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
[Usage](usage.md).
