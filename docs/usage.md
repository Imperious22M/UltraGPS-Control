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
| **Calibration** | Calibration | calibration points, moving (0, 0), calibration runs, accuracy check |
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
the receivers, aims them, and estimates how well the layout covers the arena
before anything is built.

### Placing the receivers

The arena shows R1–R6 joined by their outline, each with a facing arrow and
beam. The **Receivers** table in the side panel holds their X / Y and facing.
To place a receiver:

- select its row and type X / Y;
- **Offset…** — give it as dX / dY, or as a distance and an angle (0° = +X,
  counter-clockwise), from (0, 0), another receiver or a calibration point;
- **Pick on arena**, then click where it goes;
- **Place all by clicking** — click where R1 goes, then R2, … R6 (Esc or a
  right click stops);
- drag it on the arena. The table follows the drag; the layout changes when you
  release the mouse.

The snap list rounds clicked and dragged positions to 1, 5 or 10 cm (or not at
all), the mouse wheel zooms, and **Fit view** shows the whole layout. A new
arena, with every receiver still on one spot, opens on an empty 4 m view.

**Facing (°)** is the direction a receiver points: 0 = +X, 90 = +Y. Leave it
blank (or type `auto`) to aim it at the centroid of the receivers. **Aim at…**
points the selected receiver at a point; **Auto-aim all** sets every receiver
back to automatic. The facing feeds the coverage estimate and the calibration
shading; the position solver does not use it.

### Templates

The two older ways of building a layout are kept, with the same maths as before:

- **Template: rectangle W × H** — R1–R3 up the left side and R4–R6 up the right
  side (corners and middles), centred on (0, 0). Facings are kept.
- **Template: tape-measure distances** — the ten distances below, measured
  between the receivers with a tape and drawn on the arena while the section is
  open. They are labelled D1–D10 (the old panel called them R1–R10) so that
  they are not mistaken for receivers. A change rebuilds the two-column
  layout; D9 and D10 move R2 and R5 alone.

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

Whenever the layout changes, the arena is shaded with an estimate of what it
gives. **Show** chooses the map:

- **Receivers hearing each spot** — how many receivers hear the transmitter
  there. A fix needs 3 (yellow); a 4th lets one bad reading be rejected
  (yellow-green); 5 or 6 are green. Red is fewer than 3, no fix (dark red: no
  receiver at all). The dashed line is the edge of the area with a fix.
- **Fix precision (HDOP)** — centimetres of position error per centimetre of
  range error, from the receivers that hear each spot. Green (1 or less) is
  good geometry, yellow about 2, red 3 or worse — and red where there is no
  fix.
- **No shading**.

**Model** chooses the receiver limits:

- **Simulator** (the default, the best estimate) — the UltraGPS Simulator's
  physical receiver: signal margin, beam pattern, air absorption and detection
  within the 8-cycle burst. A receiver hears the transmitter about 7.5 m away
  on its axis, 3.5 m at 45° and 2.8 m at 60° off it.
- **HC-SR04 datasheet** (the conservative check) — the sensor datasheet's
  2–400 cm within ±15°, used as hard limits. Those are echo-ranging figures:
  one way, with no echo, a receiver hears farther and wider, so the real
  system should do at least this well.

**Window** is the base station's sampling window (the `H` setting): sound that
arrives after it is lost, so a short window caps the range at about
(window − 4 ms) × the speed of sound. It takes the base station's value once
that is known (after the Base Station panel has read it); **Read** asks the base
station now. Changing the number here only shows its effect; nothing is sent.

**Beams** draws each receiver's beam (±27.5° at −6 dB for the simulator, the
±15° field of view for the datasheet). **Selected receiver's reach** outlines
where the selected receiver hears the transmitter. Hovering over the arena names
the receivers that hear that spot.

The table under the selectors judges the area inside the receivers' outline
under **both** models side by side: the share of it where a fix is possible
(≥ 3 receivers hear), where one receiver is spare (≥ 4), where all six hear, the
fewest receivers hearing any spot, and the median HDOP. For the shipped
236 × 236 cm layout the simulator has all six receivers hearing the whole arena
(median HDOP 0.83), while the datasheet limits give a fix on only about a
quarter of it. The real system should land between the two, near the simulator;
[Architecture](architecture.md#receiver-coverage-model) explains the models.

The estimate covers direct sound in an open room only: walls, obstacles and
echoes are not modelled.

### Calibration points and (0, 0)

The **Calibration points** section shows the saved points read-only: they are
edited on the Calibration panel (**Calibration panel →**). The **Set (0, 0)**
tool is the same as on the Calibration panel — see
[Moving (0, 0)](#moving-0-0).

### Verify & Save

Nothing is written until **Verify & Save**, except a (0, 0) move, which applies
at once. Verify & Save refuses a layout the position library cannot use — every
receiver on one spot, all of them on one line, or two receivers within 1 cm of
each other — and marks the offending rows red. Otherwise it saves the positions
and facings, recomputes every receiver's distances to the calibration points,
and verifies the whole configuration: `valid_settings` is set to the result,
and anything still missing (calibration points on a new arena, say) is listed.

**Revert** discards unsaved changes; leaving the panel with unsaved changes asks
first.

!!! note "Receivers moved for real need a new calibration"
    Saving a layout records where the receivers are. When they have been moved
    physically, calibrate them again on the Calibration panel.

## Calibration

Calibration turns raw tick counts into centimetres. Every receiver gets its own
line, `distance = tick × slope + intercept`, fitted from readings taken with the
transmitter standing on 2 to 4 known points: one collection per point. With two
points the line passes exactly through both readings; with three or four it is a
least-squares fit, which averages reading errors out and shows a bad reading in
its residuals.

The panel is a large arena beside four tabs — **Points**, **Origin (0,0)**,
**Run** and **Check** — with **Base Station…** and **← Back** at the top.

### Calibration points

The **Points** tab chooses where the transmitter will stand: 2 to 4 points,
P1–P4, anywhere in the arena. For each point:

- type X / Y in the table;
- **Offset…** — dX / dY, or a distance and an angle, from (0, 0), a receiver or
  another point;
- **Pick on arena**, then click the spot;
- or drag the point's marker on the arena.

**+** adds a point at a good spot and **−** removes the selected one.

The arena is shaded **green where a point calibrates best**, using the
simulator's calibration-error score. The brightest green is the lowest error on
the map; the green fades as the error grows and is gone at twice that error, and
there is none where a point may not go (outside the arena, less than 30 cm
inside the receivers' box, more than 400 cm from a receiver, or within 20 cm of
another point). What is shaded depends on the selection, and the arena's title
says which:

- with a point selected — where that point should go, given the others
  (*Green = best spots for P2, given P1 and P3*);
- with none selected — where the next point should go (click an empty spot of
  the arena to clear the selection);
- with four points and none selected — where any point does well.

The read-out under the mouse compares the spot with the best one, for example
*P2 here: score 0.84 (1.07× the best)*. **Receivers** selects the receiver
limits behind the shading and the checks (Simulator or HC-SR04 datasheet, as on
[Setup](#coverage-estimate)); **Show receiver beams** draws them.

Under the table:

- the **arena score** of the points: the RMS distance error their calibration
  would cause over the whole arena, per centimetre of error in a single
  reading. Lower is better; it is compared with the saved points.
- **Problems**, in red, which block saving and running: fewer than 2 points, a
  point outside the arena, two points closer than 20 cm, or a receiver that is
  the same distance from every point (no slope can be fitted).
- **Warnings**, in amber: a point beyond a receiver's 400 cm range, a receiver
  that cannot hear a point directly (it would calibrate on an echo, or read
  nothing), a receiver whose distances to the points differ by under 10 cm (a
  poor slope), or a point less than 30 cm inside the receivers' box.

**Find best points** searches for the set of 2, 3 or 4 points with the lowest
arena score that every receiver hears, and previews it in gold; **Use these
points** puts it in the table. **Expected error per receiver** lists each
receiver's slope and intercept error per centimetre of reading error — a
receiver wants its points at very different distances from it.

**Save points** writes the points to `config.toml`, together with every
receiver's distance to each of them; **Revert** goes back to the saved points.
Saving is refused while a calibration run or a measurement is in progress.

!!! warning "The shipped calibration points cannot calibrate R3 and R4"
    The default `config.toml` has P1 (0, 59) and P2 (−59, 0). R3 (−118, 118) is
    131.9 cm from both, and R4 (118, −118) is 212.7 cm from both: the two
    receivers lie on the line y = −x, exactly half-way between the points, so each
    reads the same tick at both and no slope can be fitted. The panel shows both points in red
    (*R3 and R4 are the same distance from every point: no slope can be fitted*)
    and will not run on them. Choose new points before calibrating:

    1. Press **Find best points** with 4 points, then **Use these points** and
       **Save points**. On the shipped 236 × 236 cm layout, with the Simulator
       model, it suggests four points about 30 cm in from the corners —
       (−88, 77), (87, 87), (−88, −88) and (87, −88) — with an arena score of
       0.65.
    2. Or place them by hand, following the shading: far apart, about 30 cm
       inside the receivers' box, heard by every receiver, and never arranged so
       that a receiver is the same distance from all of them. Use 3 or 4 points:
       with 2, the line passes through both readings exactly, so a bad reading
       cannot show.

### Moving (0, 0)

The **Origin (0,0)** tab calibrates the origin: it chooses which spot of the
floor is the arena's (0, 0). The **Set (0, 0)** tool takes the new origin

- as a click on the arena (**Pick on arena**);
- as X / Y typed in the current frame;
- as an offset from today's (0, 0) or a receiver (**Offset…**: dX / dY, or a
  distance and an angle);
- or measured: stand the transmitter on the physical mark and press
  **Measure with transmitter**; the mean of 20 fixes becomes the candidate.

The candidate is previewed on the arena. **Apply** asks for confirmation, then
translates the whole frame so that the point becomes (0, 0): every receiver,
calibration point, barrier and image overlay moves with it, in `config.toml` and
`barriers.toml` alike. Distances do not change, so the slopes and intercepts stay
valid and no new calibration is needed. Moving (0, 0) is refused during a
calibration run or an accuracy check. The Setup panel has the same tool.

### Calibration run

The **Run** tab collects once per saved calibration point:

1. Press **Start calibration**. It is refused, with the reason, when the saved
   points have problems, the ground-station link is down, or a measurement is
   running. Points edited but not saved yet are offered **Save and start**.
2. Put the transmitter on the point the prompt names — it is ringed in gold on
   the arena — and press **Collect P1**. Keep it still until every receiver has
   read its most frequent tick `calibration_reads` times (5 by default): on the
   live histograms each receiver's modal bar (green) has to reach the dashed
   line, and a gold line marks the latest tick. The progress bars count the
   same.
3. Do the same for every point. To collect a point again, choose it in the list
   and press **Collect**. **Stop** stops collecting at the current point;
   **Cancel calibration** forgets the run, and `config.toml` keeps its offsets.

The histograms are the useful diagnostic: a receiver with a broad or
double-peaked distribution is picking up reflections rather than the direct
pulse, and its calibration will be poor.

After the last point every receiver's line is fitted, and a results table
appears under the arena: the new slope and intercept next to the current ones,
the modal tick at each point, the residuals and their RMS, and a note:

| Note | Meaning |
|------|---------|
| *OK* (green) | Nothing to report |
| *slope ≤ 0* (red) | Impossible — ticks grow with distance. This receiver is not saved |
| *large residuals* (red) | RMS above 3 cm: a point was read wrong (an echo, or the transmitter off the point). Collect it again |
| *slope ±N % against now* (amber) | The slope changed by more than 10 %: worth a second look |
| *keeps its offsets* (amber) | The receiver could not be calibrated, with the reason (for example *no echo at P2, P3*); its current slope and intercept stay |

**Save offsets** writes the new slopes and intercepts to `config.toml`;
**Discard** forgets them. With two points the residuals are always zero, which is
why three or four points are recommended.

### Accuracy check

The **Check** tab measures the transmitter on a spot you know and compares.
Give the spot (type it, use **Offset…**, pick it, or drag the orange × on the
arena), put the transmitter there, choose how many **Readings** to average (20
by default) and press **Measure**. The result shows:

- the LM and CEP position errors — green up to 2 cm, amber up to 5 cm, red
  beyond — and the spread of the fixes;
- each receiver's true distance, measured distance and range error (positive:
  it reads long), which shows which receiver is off;
- on the arena, the fixes, the LM and CEP means, and the error line.

Moving the known point compares the same measurement again.

### When to recalibrate

Calibrate again when:

- receivers were physically moved or replaced;
- the burst delay changed — a `D` sweep, `E`, or `M` on the Base Station panel.
  The delay is part of every reading, so a change shifts every receiver's
  intercept by the change × the speed of sound (2000 → 120 µs is about 65 cm);
- firmware with another tick unit was flashed (16 µs Timer1 counts on the
  newest firmware, loop counts on older firmware) — every slope is then wrong;
- the accuracy check shows errors you cannot accept.

It is **not** needed after changing the trigger rate or the sampling window,
moving (0, 0), or choosing new calibration points (they matter for the next
run only).

## Position

The tracking view. The arena plot shows the receivers, the current position and
a fading trail of the last 50 fixes; six mini-plots around it show each
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
| **HDOP** | How well the receivers that passed the sanity filter pin the fix down: cm of position error per cm of range error. Green up to 1.5, gold up to 3, orange above — the receivers see the transmitter from too few directions. Red *no fix* below 3 receivers |
| **Receivers n/6** | How many receivers passed the sanity filter. The light is red below 3: the displayed position is then a stale fallback rather than a new fix |
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
