# Usage

## Before you start

The [`UltraGpsXY`](https://imperious22m.github.io/UltraGPS-Ground/) ground-station
program must already be running on the host that has the base-station Arduino
plugged into it, for example:

```bash
./build/UltraGpsXY -s /dev/ttyACM0      # in the UltraGPS-Ground checkout
```

UltraGPS Control connects to it over the network; it does not talk to the
hardware directly.

## Running

```bash
ultragps-control                        # installed from the .deb
./run.sh                                # from a development checkout
```

### Options

| Option | Default | Effect |
|--------|---------|--------|
| `--ip <address>` | `127.0.0.1` | Address (or host name) of the ground-station host |
| `--ground-tcp-port <port>` | `9000` | UltraGpsXY's TCP command port (its `-t` option) |
| `--ground-udp-port <port>` | `9001` | UltraGpsXY's UDP stream port (its `-u` option) |
| `--config <path>` | search path | Use a specific `config.toml` |
| `-v`, `--verbose` | off | Log debug messages (the default level is INFO) |
| `-h`, `--help` | — | Show usage |

```bash
ultragps-control --ip 192.168.1.100
ultragps-control --ip 192.168.1.100 --ground-tcp-port 9100 --ground-udp-port 9101
ultragps-control --config ~/arenas/gym/config.toml
```

With no `--config`, the settings file is looked up in `~/.config`, then `./config`,
then `/etc` — see [Configuration](configuration.md).

The program connects to the ground station's TCP and UDP ports on the `--ip`
host, and binds its own ports `8000`–`8004` locally.

### The startup check

The first thing the program does is connect to the ground station, waiting up
to 3 s for an answer. If it cannot, it does not start with a half-working window:
it prints the problem to the terminal and shows a dialog that says
*UltraGPS Control can't run without the ground station program (UltraGpsXY)*,
with the address it tried and the reason:

- **Not running or unreachable** — for example *connection refused
  (UltraGpsXY is not running there)*, *no answer within 3 s* or *cannot
  resolve the host name*. The dialog shows a command that starts UltraGpsXY on
  the expected ports (`./build/UltraGpsXY -s /dev/ttyACM0 -t 9000 -u 9001`)
  and the options that point UltraGPS Control elsewhere.
- **Busy** — UltraGpsXY is running, but another program (usually another
  UltraGPS Control) holds its only client slot. Close that program, or restart
  UltraGpsXY with a higher `--max-clients`.

**Retry** connects again without freezing the dialog; **Quit** — or Escape,
closing the dialog, or Ctrl+C in the terminal — exits with status 1.

Two other problems stop the start the same way, with a message and exit
status 1: a position-feed port (`8000`–`8004`) that is already in use, usually
by another UltraGPS Control on the same computer, and a `config.toml` that
cannot be read (missing, unreadable or malformed). Correct the file, or start
with `--config` pointing at another one.

### Exit status

| Status | When |
|-------:|------|
| `0` | The window was closed |
| `1` | UltraGPS Control could not start (see above) |
| `130` | Ctrl+C in the terminal once the program runs (128 + `SIGINT`) |
| `143` | `SIGTERM` once the program runs (128 + `SIGTERM`) |

Ctrl+C and `SIGTERM` close the window cleanly, as its close button does: the
position log is finished and the network connections are closed.

## The main window

### Main menu

The window opens on a menu with a live server-status panel and five buttons. The
status panel shows the address the server bound to, whether streaming is on, each
port with its connected-client count, and the total.

| Button | Panel | What it is for |
|--------|-------|----------------|
| **Position** | Position Tracking | live tracking |
| **Calibration** | Calibration | moving (0, 0), slopes and intercepts, System Origin Calibration |
| **Setup** | Arena Maker | receiver layout, facings and the coverage estimate |
| **Barriers** | Barrier Drawer | drawing virtual barriers |
| **Base Station** | Base Station | trigger rate, sampling window, burst delay, tests and resets |

Every panel has a **← Back** button to the menu. A panel stops its background
work when you leave it, so the network thread only runs while the Position
panel is open.

### The link indicator

The status bar at the bottom of every panel shows the link to the ground
station:

| Indicator | Meaning |
|-----------|---------|
| Green — *Ground station 127.0.0.1:9000 connected* | Connected, and the base station answers |
| Amber — *… connected — base station not answering (N timeouts)* | UltraGpsXY answers the connection but not the commands: the base station is unplugged or rebooting, or UltraGpsXY's serial link is not ready yet |
| Red — *… LOST — reconnecting (attempt n in s s)…* | The connection dropped. It is re-established in the background, waiting 1, 2, 5 and then 10 s between attempts; **Reconnect now** tries at once |

Hover over the indicator for the last error. While the link is down the panels
disable the commands that need it, and nothing crashes; when it comes back the
Position panel restarts its stream by itself.

### Screen size

The window opens sized to the screen, and everything in it — text, buttons,
tables and plots — scales with the window. The layout was designed for
1200 × 900; it stays usable down to 1024 × 600, where the side panels scroll,
and grows in proportion up to 4K. Resize the window to make everything larger
or smaller.

## Setup

The **Setup** button opens the Arena Maker. Start here on a new arena: it places
the receivers, chooses how many of them the arena uses, aims them, and
estimates how well the layout covers the arena before anything is built.

### Placing the receivers

The base station has six receiver channels, and an arena uses **3 to 6** of
them: R1 up to RN, where N is the number of active receivers. The arena shows
the active receivers joined by their outline, each with a facing arrow and
beam. The **Receivers** table in the side panel holds their X / Y and facing.
To place a receiver:

- select its row and type X / Y;
- **Offset…** — give it as dX / dY, or as a distance and an angle (0° = +X,
  counter-clockwise), from (0, 0), another receiver or a calibration point;
- **Pick on arena**, then click where it goes;
- **Place all by clicking** — first asks how many receivers the arena uses
  (3 to 6), then lets you click where R1 goes, then R2, … up to RN (Esc or a
  right click stops);
- drag it on the arena. The table follows the drag; the layout changes when you
  release the mouse.

The number of receivers is saved with the layout (**Verify & Save**) as
`number_of_receivers` in `config.toml`. Choosing fewer than six does not delete
anything: the receivers left out are dormant. They keep their entries in
`config.toml` — position, facing, slope and intercept — but are ignored by the
positioning, the coverage estimate, calibration and the displays until they are
made active again, when they come back with their old calibration.

The snap list rounds clicked and dragged positions to 1, 5 or 10 cm (or not at
all), the mouse wheel zooms, and **Fit view** shows the whole layout. A new
arena, with every receiver still on one spot, opens on an empty 4 m view.

**Facing (°)** is the direction a receiver points: 0 = +X, 90 = +Y. Leave it
blank (or type `auto`) to aim it at the centroid of the receivers. **Aim at…**
points the selected receiver at a point; **Auto-aim all** sets every receiver
back to automatic. The facing feeds the coverage estimate; the position solver
does not use it.

### Setting (0, 0)

The **Set (0, 0)** tool sits directly under the Receivers section. It is the
same tool as on the Calibration panel — see [Moving (0, 0)](#moving-0-0) — and,
unlike the rest of the panel, applies at once.

### Templates

The two older ways of building a six-receiver layout are kept, with the same
maths as before:

- **Template: rectangle W × H** — R1–R3 up the left side and R4–R6 up the right
  side (corners and middles), centred on (0, 0). Facings are kept.
- **Template: tape-measure distances** — the ten distances below, measured
  between the receivers with a tape. They are labelled D1–D10 (the old panel
  called them R1–R10) so that they are not mistaken for receivers. A change
  rebuilds the two-column layout; D9 and D10 move R2 and R5 alone.

The distance markers are drawn on the arena only while the tape-measure template
is open (its tab clicked). Tick **Always show distance markers** to keep them on
the arena with the template closed; it is off by default.

| Distance | What to measure |
|----------|-----------------|
| D1 | R2 → R3, along Y |
| D2 | R1 → R2, along Y |
| D3 | R1 → R4, along X |
| D4 | R4 → R5, along Y |
| D5 | R5 → R6, along Y |
| D6 | R3 → R6, along X |
| D7 | (0, 0) → R2, along X |
| D8 | (0, 0) → R5, along X |
| D9 | R2's Y (0 on the X axis) |
| D10 | R5's Y (0 on the X axis) |

### Coverage estimate

The coverage estimate shades the arena with how many of the active receivers
hear the transmitter at each spot. It is **off** when the panel opens; turn it
on with **Show: Receivers hearing each spot** in the **Coverage estimate**
section, and from then on the shading follows
every change of the layout. A fix needs 3 receivers (yellow); a 4th lets one
bad reading be rejected (yellow-green); 5 or 6 are green. Red is fewer than 3,
no fix (dark red: no receiver at all). The dashed line is the edge of the area
with a fix.

One sensor model decides what a receiver hears. **Sensor settings…** opens a
pop-up where every variable of it can be edited: the reception angle (a hard
field of view either side of the receiver's axis), the distances (the farthest
and the nearest a receiver hears), the detection mode — hard limits only, or the
signal strength over a threshold, with its signal margin, beam width, ring-up
and burst length — the air temperature and humidity, and the timing (burst
lead, receiver ready time and module timeout). Blank reception angle or maximum
distance means no such limit. **Reset to HC-SR04 datasheet values** goes back to
the defaults: hard limits of 2–400 cm within ±15° of the receiver's axis. A line
under the button sums up what the current model hears.

**OK** applies the settings and saves them to `sensor.toml` in the
configuration directory (see [Configuration](configuration.md#sensortoml)),
so they are used again the next time the program starts. If the file cannot be written the panel
says why and keeps the settings for this session only.

**Window** is the base station's sampling window (the `H` setting): sound that
arrives after it is lost, so a short window caps the range at about
(window − 4 ms) × the speed of sound. It takes the base station's value once
that is known, and **Read** asks the base station for it. Changing it here only
shows its effect: nothing is sent to the base station, and it is not saved.

**Beams** draws each receiver's reception angle as a wedge round its facing
(its beam when the angle has no limit). **Selected receiver's reach** outlines
where the selected receiver hears the transmitter. Hovering over the arena names
the receivers that hear that spot.

The summary under the selectors judges the area inside the receivers' outline:
the share of it where a fix is possible (≥ 3 receivers hear), where one receiver
is spare (≥ 4), where every active receiver hears, and the fewest receivers
hearing any spot. With the default sensor settings (the datasheet's ±15°), the
shipped 236 × 236 cm layout, every receiver aimed at the centre, has a fix on
only about a quarter of the arena; the datasheet figures are for echo ranging
and are conservative for the one-way use here, so widen the reception angle in
**Sensor settings…** when the receivers are known to hear farther off axis.
[Architecture](architecture.md#receiver-coverage-model) explains the model.

The estimate covers direct sound in an open room only: walls, obstacles and
echoes are not modelled.

### Verify & Save

Nothing is written until **Verify & Save**, except a (0, 0) move, which applies
at once. Verify & Save refuses a layout the position library cannot use — every
active receiver on one spot, all of them on one line, or two active receivers
within 1 cm of each other — and marks the offending rows red; dormant receivers
are not checked. Otherwise it saves the positions, the facings and the number
of receivers, recomputes every receiver's distances to the calibration points
and to (0, 0) (`origin_distance`),
and verifies the whole configuration: `valid_settings` is set to the result,
and anything still missing (calibration points on a new arena, say) is listed.

**Revert** discards unsaved changes; leaving the panel with unsaved changes asks
first.

!!! note "Receivers moved for real need a new calibration"
    Saving a layout records where the receivers are. When they have been moved
    physically, run the System Origin Calibration again on the Calibration
    panel.

## Calibration

Calibration turns raw tick counts into centimetres. Every receiver has its own
line, `distance = tick × slope + intercept`. The slope is the same for every
receiver in practice — 0.272 cm per tick, the value the receivers are calibrated
to as of November 2026 (`DEFAULT_SLOPE` in `SettingsModule.py`) — and the
intercept takes up what differs from one receiver to the next. The panel edits
both by hand, and measures new intercepts with the transmitter standing on the
arena's (0, 0).

The panel is a large arena beside three tabs — **Origin (0,0)**, **Offsets** and
**System Origin Calibration** — with **Base Station…** and **← Back** at the
top. The arena shows the active receivers and, on every tab, the origin (0, 0)
with a marker and a label.

### Moving (0, 0)

The **Origin (0,0)** tab chooses which spot of the floor is the arena's (0, 0).
The **Set (0, 0)** tool takes the new origin

- as a click on the arena (**Pick on arena**);
- as X / Y typed in the current frame;
- as an offset from today's (0, 0) or a receiver (**Offset…**: dX / dY, or a
  distance and an angle);
- or measured: stand the transmitter on the physical mark and press
  **Measure with transmitter**. The mean of 20 fixes is shown on the arena, and
  the panel asks *Make (x, y) the new (0, 0)?*; **Make it (0, 0)** moves the origin there
  (**Not now** keeps the point on the arena for **Apply**).

A picked, typed or offset origin is previewed on the arena, and **Apply** asks
for confirmation. Either way the whole frame is translated so that the point
becomes (0, 0): every receiver, calibration point, barrier and image overlay
moves with it, in `config.toml` and `barriers.toml` alike, and each receiver's
distance to the new origin (`origin_distance`) is recomputed and saved. The
distances between receivers do not change, so the slopes and intercepts stay
valid. The Setup panel has the same tool.

### Offsets

The **Offsets** tab is a table of the active receivers, R1 to RN, with each
one's **Slope** and **Intercept** (cm) as saved in `config.toml`, and its
distance to (0, 0) from the arena map, read-only, for reference.

- Edit a slope or an intercept by typing in its cell. Values must be numbers,
  and a slope must be greater than 0.
- **Set slopes to default (0.272)** fills every active receiver's slope with
  0.272. Like any edit, it is only written by **Save**.
- **Save** writes every changed row to `config.toml` at once. If any value is invalid,
  nothing is written and the panel says which.
- **Revert** goes back to the saved values.

Dormant receivers keep their slope and intercept and are not shown.

### System Origin Calibration

The **System Origin Calibration** tab measures a new intercept for every active
receiver, using (0, 0) as the known point:

1. Put the transmitter on the arena's (0, 0); the panel says so and highlights
   the origin on the arena.
2. Press **Start**. It needs the ground-station link and is disabled, with the
   reason, while the link is down. **Stop** ends the collection, and leaving
   the panel stops it too.
3. Keep the transmitter still. The panel collects the same way the earlier
   calibration runs did: it pulses until every active receiver has read its most
   frequent tick `calibration_reads` times (5 by default). Live histograms show
   each receiver's ticks, and a table shows the distance each receiver has
   recorded.
4. When the collection is done, the new intercepts are computed and the panel
   asks whether to save them.

For this measurement only, every intercept is taken as 0 and the current slopes
are kept, so a receiver's measured distance to the origin is its most frequent
tick times its slope. The new intercept is the difference between the distance
the arena map gives and the distance measured:

```
measured_i      = modal_tick_i × slope_i
new_intercept_i = origin_distance_i − measured_i
```

`origin_distance_i` is receiver *i*'s distance to (0, 0) from its position in
`config.toml`. For example, R1 at (−118, −118) is 166.88 cm from the origin; if
its most frequent tick is 520 at a slope of 0.272, it measured 141.44 cm, and its
new intercept is 25.44 cm.

The save prompt lists, for each receiver, the **current intercept**, the **new
intercept** and the **difference** in percent,
`(new − current) / |current| × 100` (*n/a* when the current intercept is 0).
**Save intercepts** writes the new intercepts to `config.toml` and leaves the slopes as they
are; **Don't save** writes nothing (**Save new intercepts…** shows the prompt again). A receiver that read nothing at (0, 0) is named in
the prompt and keeps its intercept.

Because the measurement uses the current slopes, set them first — on the
**Offsets** tab, with **Set slopes to default (0.272)** if in doubt — and then
run the origin calibration.

The histograms are the useful diagnostic: a receiver with a broad or
double-peaked distribution is picking up reflections rather than the direct
pulse, and its intercept will be poor.

### When to recalibrate

Run the System Origin Calibration again when:

- receivers were physically moved or replaced (save the new layout on the
  Setup panel first, so that the distances to (0, 0) are right);
- the burst delay changed — a `D` sweep, `E`, or `M` on the Base Station panel.
  The delay is part of every reading, so a change shifts every receiver's
  intercept by the change × the speed of sound (2000 → 120 µs is about 65 cm).

When firmware with another tick unit is flashed (16 µs Timer1 counts on the
newest firmware, loop counts on older firmware) every slope is wrong: set the
slopes on the **Offsets** tab first, then run the origin calibration.

Nothing needs recalibrating after changing the trigger rate or the sampling
window, or after moving (0, 0).

## Position

The tracking view. The arena plot shows the receivers, the current position and
a fading trail of the last 50 fixes; mini-plots around it show each active
receiver's recent distance history, each with a light that is green while that
receiver passes the sanity filter.

Two solutions are drawn at once — **LM** (green) and **CEP** (yellow), one
inside the other so that both show when they agree (see
[Architecture](architecture.md)). Comparing them is the quickest way to judge
whether a fix is trustworthy: when they disagree, at least one receiver is
feeding bad data.

The bar under the plot:

| Read-out | Meaning |
|----------|---------|
| **Multilat** / **CEP** | The two fixes, in cm. The light by CEP is green when this reading gave a valid CEP position. **Show Multilateration** / **Show CEP** hide either one |
| **Receivers n/N** | How many of the N active receivers passed the sanity filter. The light is red below 3: the displayed position is then a stale fallback rather than a new fix |
| **FPS** / **Pos/s** | Canvas redraws per second / readings solved per second |

The top line shows the tracking state — *Tracking: continuous stream (UDP)*,
*Tracking: pulse mode (TCP)*, *Starting the continuous stream…*, *No reply to
the pulses: is the base station running?* or *Ground station link down:
tracking paused until it reconnects* — and, next to it, barrier alerts and
configuration problems.

**Continuous (UDP)**, ticked by default, has the base station stream every
reading over UDP, and every reading is processed exactly once. Untick it for
**pulse** mode: one TCP request per fix. Continuous is faster; pulse is easier
to reason about when debugging. The panel restarts the stream by itself after a
lost link, or when it goes silent for a few trigger periods.

Barriers are drawn on the arena and flash when they trigger. **Calibration** and
**Arena Maker** jump to those panels.

### Logging

While **Log to file** is ticked (the default), every fix is recorded to a
`log` folder: `log/` beside the application when running from a source
checkout, or `~/.local/share/ultragps-control/log` for an installed package.
Each time the position view starts, or logging is switched back on, a new pair
of files is created, named after the start time:

- `positions_<date>_<time>.csv` — written row by row, so it survives a crash.
- `positions_<date>_<time>.xlsx` — the same rows as a spreadsheet, saved when
  logging stops (leaving the view, unticking the box, or closing the app).

Columns: `timestamp_s` (monotonic seconds since the log started, always
increasing), `mode` (continuous or pulse), the six raw `ticks_N`, the six
calibrated `distance_N_cm`, the LM position and success flag, the CEP position
and success flag, and the receivers that passed the sanity filter.

## Base Station

The Base Station panel shows and changes the settings of the base station — the
Arduino that triggers the receivers and drives the transmitter station over the
radio — through UltraGpsXY, and explains what each one means for range, update
rate and calibration. It reads the trigger rate, sampling window and baud rate
every time it is shown, and on **Refresh**. Every change asks for confirmation
first. Commands run one at a time, in the background, and the buttons are
disabled while one runs or while the link is down.

| Button | Command | What it does to the hardware | Recalibrate? |
|--------|---------|------------------------------|--------------|
| **Refresh** | `F 0`, `H 0`, `B 0` | Reads the trigger rate, sampling window and baud rate; changes nothing | No |
| **Set rate…** | `F <Hz>` | Sets the trigger rate, 1–50 Hz, on both stations: how many readings per second the system takes. The transmitter reboots if its rate changes, and the sampling window goes to its maximum | No |
| **Set window…** | `H <ms>` | Sets how long the base station listens after each trigger: 1 ms up to 80 % of the trigger period (**Max** fills in the longest). Sound that arrives later is lost; that receiver reads 0 | No |
| *Baud rate* | `B 0` | Read-only. Changing it reboots the base station and UltraGpsXY must follow, so Control does not offer it | — |
| **Run delay sweep (D)…** | `D` | Tries every burst delay from 0 to 500 µs in 10 µs steps, 3 pulses each, and stores the one with the most valid readings (on a tie, the shortest). A 0 µs winner is stored as 1 µs; with no valid reading at all nothing is stored | Yes, when the delay changed |
| **Restore 2000 µs (E)…** | `E` | Stores the default burst delay, 2000 µs | Yes, when the delay changed |
| **Pulse (P)** | `P` | Fires the transmitter once and shows each receiver's raw ticks (0 = no echo) and the distance the saved calibration makes of them; out-of-range distances are flagged | No |
| **Simulated (S)** | `S` | The base station reports made-up ticks (351 to 450) without the sensors: checks the link | No |
| **Live (C)** | `C` | Continuous mode: one reading per trigger, with the reading rate. **Stop live (C)** ends it | No |
| **Reset transmitter (R)…** | `R` | Reboots the transmitter station over the radio; it misses a few readings. Try it when every receiver reads 0 | No |
| **Reset base-station settings (M)…** | `M` | Restores the factory defaults: 10 Hz, 80 ms window, 2000 µs burst delay, 38400 baud | Yes, when the delay was not 2000 µs |

**What these settings mean** shows the update rate; the farthest distance the
sampling window can time, (window − about 4 ms) × the speed of sound but never
beyond the 41 ms after which the HC-SR04 itself gives up (12.7 m); the arena's
farthest receiver-to-receiver or receiver-to-point distance against it, with a
red warning and the window (or rate) needed when the window is too short; a
timeline of one trigger period; and how long a sweep takes. Above about 30 Hz
the window, not the signal, limits the range: at 50 Hz the window is at most
16 ms, and nothing beyond about 4.1 m can be timed.

The **burst delay** is how long after each trigger the transmitter fires, so
that the receivers are already listening when its sound arrives. The firmware
cannot report the delay in force, so the panel shows the last one reported in
this session (by `D`, `E` or `M`) and where it came from. After a change it
shows how far every receiver's distances moved (for example *every receiver now
reads about 65 cm too short*) with a **Recalibrate…** button; the banner clears
when new offsets are saved.

!!! warning "The delay sweep blocks the ground station"
    A `D` sweep takes 153 pulses — about 16 s at 10 Hz, 2½ minutes at 1 Hz — and
    UltraGpsXY answers nothing else until it ends, so commands from the other
    panels time out meanwhile. If you leave the panel during a sweep, it finishes
    in the background and its result is shown when you come back.

## Barriers

An interactive editor for the virtual zones that fire events when the
transmitter enters or leaves them. The arena takes most of the panel, with
**New Barrier**, **Finish**, **Cancel**, **Clear All Changes** and **Save All**
under it; the side panel holds the barrier list (**Delete Barrier**,
**Clear All**), the selected barrier's **Properties** and the **Images**.

- **Polygon** — click to place vertices, close the shape to finish.
- **Circle** — click the centre, drag out the radius.
- **Line** — click both endpoints; `thickness` sets how close counts as crossing.

For each barrier you set a name, colour and opacity, and choose:

| Setting | Options | Meaning |
|---------|---------|---------|
| `trigger_when` | `inside` / `outside` | which condition is the active one |
| `trigger_mode` | `event` / `continuous` | fire once on transition, or on every update while it holds |
| `callback_name` | any string | passed through in the event, for consumers to dispatch on |

You can also import an image — a court or field diagram — as a backdrop
(**Import**), position and rotate it, and trace its outline into a polygon
barrier automatically (**→ Barrier**). The **Virtual Ruler** measures the
distance between two clicked points.

Barriers are saved to `barriers.toml` in the configuration directory, and images
are copied into `resources/` beside it. The panel reloads both from disk each
time it is shown and whenever the configuration changes — moving (0, 0)
translates the barriers — so unsaved edits are discarded when you leave the
panel: press **Save All** first.

## Consuming the position feed

Other programs receive the feed over the ports listed in
[Architecture](architecture.md). Nothing is sent until a consumer enables
streaming by sending `Ready` on the command port:

```python
import socket

# Enable streaming.
cmd = socket.create_connection(("127.0.0.1", 8000))
cmd.sendall(b"Ready\n")          # server replies "<Ok>"

# Receive positions.
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.bind(("", 8001))
udp.sendto(b"register", ("127.0.0.1", 8001))   # register as a listener

while True:
    data, _ = udp.recvfrom(1024)
    print(data.decode().strip())    # N: POS lm -12.500 43.200
```

The NMEA stream on port 8002 carries a standard `$GPGGA` sentence, so software
that already speaks NMEA can treat the arena as a GPS receiver. The satellite
count field is reused to report how many receivers passed the sanity filter.
