# Architecture

UltraGPS Control sits in the middle of the UltraGPS stack. It is a **client** of
the ground-station program and, at the same time, a **server** to any other
program that wants the position feed. Keeping those two roles distinct is the
key to reading the code.

## The stack

```
Transmitter (Arduino + UltraGPS shield)
        │  ultrasonic pulses
        ▼
6 stationary receivers  ──►  Base station (Arduino)
                                   │  USB serial
                                   ▼
                        UltraGpsXY  (ground-station host)
                                   │  TCP 9000 commands / UDP 9001 stream
                                   ▼
                        ultragps-control   ◄── this program
                                   │  TCP 8000 / UDP 8001-8003 / TCP 8004
                                   ▼
                          Downstream consumers
```

## Upstream: talking to the ground station

`ultragps_client.UltraGPSClient` maintains the link to `UltraGpsXY`. A command is
one ASCII letter, optionally followed by a space and an integer, and a newline,
sent over TCP. Ticks are the base station's counts from the trigger to the
moment each receiver heard the transmitter; `0` means it heard nothing.

| Command | Client method | Meaning | Reply |
|---------|---------------|---------|-------|
| `P` | `pulse()` | one measurement | `N: t0, t1, t2, t3, t4, t5` |
| `S` | `simulate()` | one simulated measurement (made-up ticks) | `N: t0, …, t5` |
| `C` | `continuous()` | stream one reading per trigger | none on TCP; `C: t0, …, t5` datagrams on UDP |
| `R` | `reset_transmitter()` | reboot the transmitter station over the radio | `N: R` |
| `D` | `delay_search()` | burst-delay sweep, 0–500 µs; stores the best delay | `D: <delay_us>, <mean_ticks>, <valid>/<total>` |
| `E` | `default_delay()` | store the default burst delay | `E: 2000` |
| `M` | `reset_settings()` | reset every base-station setting (10 Hz, 80 ms, 2000 µs, 38400 baud) | `M: OK` |
| `H <ms>` | `sampling_time(ms)` | sampling window, 1 ms to 80 % of the trigger period; `H 0` queries | `H: <ms>, <timer1_counts>` |
| `F <Hz>` | `trigger_rate(hz)` | trigger rate, 1–50 Hz, on both stations; sets `H` to its maximum; `F 0` queries | `F: <hz>, <timer1_top>, <sampling_ms>` |
| `B <baud>` | `baud_rate(baud)` | base-station baud rate; `B 0` queries | `B: <baud>`, or `ERR: B <baud> unsupported by host` |

Every command except `C` ends continuous mode. The protocol has a few traits the
client is built around:

- **Bare replies.** UltraGpsXY sends each reply without a newline, so replies
  can arrive concatenated and, rarely, split. The client matches each reply
  against the pattern its command expects (or `ERR: …`), drops any other
  text, and gives a reply that ends in a number 40 ms to finish arriving.
- **One command per TCP segment.** UltraGpsXY treats each segment it reads as
  one command and discards two that arrive together, so the client runs one
  exchange at a time, under one lock, with sends at least 50 ms apart.
- **One client.** UltraGpsXY accepts one TCP client by default; another
  connection gets `E: server full` and is closed. `connect()` then raises
  `GroundStationBusy`; any other failure to connect (refused, unreachable, no
  answer within the timeout, a bad host name or port) raises
  `GroundStationUnavailable`. Both derive from `GroundStationError`, which
  carries the host, port and a short reason.
- **No reply is possible.** Invalid commands get none, and UltraGpsXY drops
  every command while its serial link is not ready (for about 7 s after the port
  opens). Every command therefore has a timeout. Once connected, a command never
  raises on a network problem: it returns `None` (`False` for the yes/no
  commands) — immediately while the link is down — and sets `last_error`;
  `consecutive_timeouts` counts the commands in a row that got no reply.
- **Continuous readings.** The client registers for the UDP stream by sending a
  datagram from its UDP socket, and re-registers while an expected stream stays
  silent. `wait_for_reading(count, timeout)` hands out every reading exactly
  once, in order (the last 128 are kept); `get_latest_reading()` still returns
  the newest one.
- **Reconnecting.** `connected` reports the link; `reconnect()` tears it down
  and connects again, and never raises. A reconnected client is back in normal
  mode, so a caller that wants the stream sends `C` again.

The client also remembers the trigger rate, sampling window and burst delay
last reported in the session (`trigger_rate_hz`, `sampling_time_ms`,
`burst_delay_us`), which the panels use for their timing estimates.

### Startup check and reconnection

`graphics.py` connects before it builds anything else: it parses the options,
creates the Qt application, sets the UI scale from the screen, and calls
`connect(timeout=3.0)`. Only when that succeeds does it start the position-feed
server and open the main window with the connected client. A
`GroundStationError` is printed to stderr and shown in a Retry / Quit dialog
(Retry connects on a worker thread); Quit exits with status 1. A feed port that
is taken, or a configuration that cannot be read, also exits with status 1
after a message. Once the window is up, Ctrl+C and `SIGTERM` close it cleanly,
and the process exits with 128 + the signal number.

Once running, the main window watches `client.connected` every 250 ms and shows
the state in the status bar of every panel. When the link drops it calls
`reconnect()` on a worker thread — never two attempts at once — waiting 1, 2, 5
and then 10 s between attempts, and emits `link_changed(bool)` on every change
so that the panels can disable and re-enable their commands. The position
panel's network thread waits while the link is down (checking every 0.25 s,
without spinning), sends `C` again once it is back, and sends it again whenever
the stream stays silent for a few trigger periods.

### Known gaps in the firmware and the ground station

These are limits of the base-station firmware and UltraGpsXY, not of this
program; the panels work around them and say so:

- **No burst-delay query.** No command reports the burst delay in force. The
  Base Station panel shows the last delay reported by `D`, `E` or `M` in this
  session, and "unknown" before that.
- **No firmware version or tick-unit query.** Ticks are 16 µs Timer1 counts on
  the newest firmware and loop counts on older firmware. The calibration
  absorbs the unit, but nothing can tell the program that the unit changed:
  after flashing firmware with another tick unit every slope is wrong, and the
  receivers must be calibrated again.
- **`D` blocks the ground station.** The sweep runs 153 pulses — about 16 s at
  10 Hz, 153 s at 1 Hz — during which UltraGpsXY answers nothing else, and it
  forwards only the final line, so progress can only be estimated from the
  elapsed time (`expected_delay_search_s`). Commands from other panels wait on
  the client's lock meanwhile and time out.
- **No sequence numbers.** A late reply to an earlier command of the same kind
  cannot be told from the real one; if it arrives more than 40 ms before it,
  it is taken instead.
- **Settings changed elsewhere** — from UltraGpsXY's own console, say — are
  seen on the next read (the Base Station panel reads them every time it is
  shown).

## The solver

`ultragps_position.UltraGPSPositionLib` turns tick counts into a coordinate.

Each receiver has its own linear calibration, so ticks become centimetres via:

```
distance_i = tick_i * slope_i + intercept_i
```

Before solving, every receiver passes through a two-part sanity filter:

1. **Bounds** — a reading must be greater than 0 and no larger than
   `max_distance`. A dead or missed receiver reports 0 ticks, which calibrates to
   its negative intercept; a receiver that loses the signal can report a very
   high value. `max_distance` defaults to the greatest distance between any two
   receivers (nothing larger is physically valid inside the arena) and can be
   passed to the `UltraGPSPositionLib` constructor or changed at any time.
2. **Differential** — a jump of more than `max_differential` (50 cm by default)
   since the previous reading is treated as a bad reading. This check is skipped
   on the very first reading, which is why `reset_state()` must be called when a
   run restarts.

The survivors feed two independent solvers:

- **LM** — a closed-form ordinary-least-squares estimate seeds a
  Levenberg-Marquardt refinement (`scipy.optimize.least_squares`).
- **CEP** — receiver subsets are searched (up to 15 of them) and the one with the
  smallest circular-error-probable radius wins.

`get_position_full()` runs the filter once and derives both solutions from the
same filtered set, which is what the GUI uses. The standalone `get_position()`
and `get_position_cep()` exist for library users.

!!! warning "A returned position is not proof of a fresh fix"
    When fewer than three receivers survive the filter, both solvers fall back to
    the last known-good position rather than returning nothing. Check
    `lm_success` / `cep_success` and the length of `sane_indices` before trusting
    a coordinate.

The position panel adds the **HDOP** of each fix, `coverage.hdop` over the
receivers that passed the filter: how many centimetres of position error one
centimetre of range error causes, given where those receivers are. It is
infinite (no fix) with fewer than three.

## Calibration

`ultragps_calibration.UltraGPSCalibration` collects one run per calibration
point, 2 to 4 of them (`cal_points` in `config.toml`). During run *k* the
transmitter stands on point *k* and the library pulses until every receiver has
read its most frequent (modal) tick `calibration_reads` times; it keeps a
histogram per receiver per run, on its own thread.

`calculate_offsets()` then pairs each receiver's modal tick at every point with
its known distance to that point (`cal_distances`) and fits a least-squares
line. For two points this is exactly the old formula:

```
slope     = (dist_1 - dist_2) / (tick_1 - tick_2)
intercept = dist_1 - slope * tick_1
```

A modal tick of 0 (no echo) leaves that point out of that receiver's fit. A
receiver is skipped, and keeps its saved offsets, when fewer than two points
with an echo remain, when its ticks are all the same, or when it is the same
distance from every point it heard; `last_skipped` gives the reason, and
`last_fit` the ticks, distances and residuals of every fitted receiver.

### Choosing the points

Where the points are matters more than how many readings are taken.
`ultragps_calibration.points`, ported from the UltraGPS Simulator, scores a set
of points by the RMS distance error its calibration would cause over the whole
arena and every receiver, per centimetre of error in one modal tick:

```
score = sqrt(mean over receivers and arena positions of 1/n + (D - d̄)² / Sxx)
Sxx   = Σ (d_j - d̄)²      (d_j: a receiver's distance to point j)
```

A receiver wants its points at very different distances from it (a large
`Sxx`), centred near the distances it will measure. In the simulator the score
predicts the calibration error: the old pair of points near the centre scored
4.82 and calibrated to a 3.1 cm distance error, the best four points 0.68 and
1.1 cm. Points spread far apart, about 30 cm inside the receivers' box and heard
by every receiver are best.

The module has no idea of the room; two callables stand in for it. `inside`
says whether a point is in the arena — the panel uses the receivers' outline —
and `hears` says which receivers hear the transmitter's direct sound from
there — the panel uses `coverage.hears` with the chosen receiver model. A point
that a receiver cannot hear costs a large penalty, so a set every receiver hears
wins whenever there is one. On top of the score:

- `search()` finds the best set of 2, 3 and 4 points (**Find best points**);
- `check_points()` lists the problems that make a set unusable (a point outside
  the arena, two points under 20 cm apart, a receiver equidistant from every
  point) and the warnings (out of range, not heard, distances spread under
  10 cm, too close to the receivers' walls);
- `quality_map()` scores every spot of a grid as the next point (given the
  points already placed), and `goodness()` turns that into the 0–1 scale of the
  green shading — 1 at the best spot, 0 at twice its error.

### Moving the origin

`SettingsModule.shift_origin(x, y)` makes the current-frame point (x, y) the new
(0, 0). It subtracts (x, y) from every receiver position and calibration point
in `config.toml`, and moves every barrier and image overlay in `barriers.toml`
with `ultragps_barrier.translate_barriers_file` (built on `translate_barrier`
and `translate_overlay`). Both files are checked before either is written;
`barriers.toml` is replaced first and put back if `config.toml` then fails. It
raises `ValueError` (nothing written) or `OSError`, and the caller then calls
`notify_config_changed()`. Distances, and so the calibration, are unchanged.

## Receiver coverage model

`ultragps_position.coverage` predicts which receivers hear the transmitter
where. It is a port of the UltraGPS Simulator's direct-sound detection model for
an open room — no walls, obstacles or echoes, a transmitter radiating all round,
nominal modules — vectorised with numpy, so that a coverage map of a 4 × 4 m
arena on a 5 cm grid takes about 10 ms. One ping, timed from the base station's
trigger:

1. Every receiver's HC-SR04 is triggered and listens from 3 ms on, once its own
   burst has gone.
2. The transmitter fires 4 ms after the trigger (the firmware's 2 ms burst delay
   plus radio and processing latency).
3. The sound arrives 30 dB over the receiver's detection threshold on axis at
   1 m, less 1/r spreading, ISO 9613-1 air absorption at 40 kHz (1.3 dB/m at
   20 °C and 50 % humidity) and the receiver's beam pattern (a piston, −6 dB at
   ±27.5°, −30 dB behind).
4. The burst rings up through the transducers and must cross the threshold
   within its 8 cycles; it must also arrive before the HC-SR04 gives up (41 ms
   after its trigger) and before the base station stops sampling (the `H`
   window, 80 ms by default).

From `hears()` come `coverage_map()` (how many receivers hear each spot) and
`hdop_map()` (the fix precision there), and `summary()` condenses them into the
shares of the arena heard by at least 3, at least 4 and all receivers, the
fewest receivers at any spot and the median HDOP. A receiver without a
`facing_deg` is aimed at the centroid of all the receivers.

Two `SensorModel` presets are provided:

| | `SIMULATOR` (default) | `DATASHEET` |
|---|---|---|
| Decides by | the physical model above | hard limits: 2–400 cm within ±15° |
| Reach on axis | 7.48 m | 4.00 m |
| at 15° / 30° / 45° / 60° / 90° | 6.80 / 4.85 / 3.50 / 2.85 / 2.44 m | 4.00 m at 15°, then nothing |
| behind (120° or more) | 0.63 m | nothing |
| Timing | 4 ms burst lead, ready at 3 ms, 38 ms module timeout, `H` window | the same |
| Role | best estimate | conservative bound |

`model.copy(window_ms=…)` feeds in the base station's sampling window; the
panels do so once the window is known.

### Checked against the HC-SR04 datasheet

The receivers are HC-SR04 ranging modules. The datasheets (ElecFreaks
["Ultrasonic Ranging Module HC-SR04"](https://cdn.sparkfun.com/datasheets/Sensors/Proximity/HCSR04.pdf);
ITead Studio "Ultrasonic ranging module: HC-SR04", 2010) give, for echo ranging
off a flat target of at least 0.5 m²:

| Quantity | Datasheet value |
|----------|-----------------|
| Ranging distance | 2–400 cm (ITead: 2–500 cm) |
| Measuring angle | 15° (ITead: "effectual angle < 15°", best in a 30° cone): ±15° |
| Carrier and burst | 40 kHz, 8 cycles, after a 10 µs trigger pulse |
| Echo pulse | 150 µs – 25 ms; 38 ms high when nothing is detected |
| Resolution | 0.3 cm |
| Measurement cycle | at least 60 ms (ITead: 50 ms) |

UltraGPS uses the modules **one way**: the base station triggers all of them and
each one times the transmitter's burst, not the echo of its own. That reaches
farther and wider than the echo-ranging figures:

- **Path.** An echo travels out and back, so 4 m of range is 8 m of spreading and
  absorption plus the loss at the target. One way, a receiver 4 m away hears
  sound that has travelled 4 m and never bounced.
- **Beam.** An echo passes through the module's transmit and receive beams, so
  its angular response is the beam pattern squared: the piston's −6 dB
  half-angle shrinks from 27.5° to about 20°, close to the datasheet's ±15°. One
  way, only the receiver's beam counts.
- **Timing.** The 38 ms timeout is 6.5 m of echo round trip. One way, the
  receiver gives up 41 ms after the trigger and the transmitter fires 4 ms in,
  which leaves 37 ms: 12.7 m.

So `DATASHEET` is the conservative bound — a receiver should hear the
transmitter at least there — and `SIMULATOR` the best estimate. The simulator's
30 dB margin and 27.5° beam are its estimates, not measurements: with weaker
modules the reach falls quickly (at 24 dB, 5.3 m on axis and 2.2 m at 45°). For
the shipped 236 × 236 cm layout, with every receiver aimed at the centre, the
simulator has all six receivers hearing the whole arena (median HDOP 0.83); the
datasheet limits give a fix (3 or more receivers) on 24 % of it.

### The sampling window

Whatever the signal, a burst has to be detected before the base station stops
sampling, so the farthest distance that can be timed is about
(window − 4 ms) × the speed of sound, and never more than the module's own
41 ms allow. The window defaults to 80 ms and is at most 80 % of the trigger
period; `F` sets it to that maximum.

| Trigger rate | Longest window | Farthest timeable distance | Reach on axis (`SIMULATOR`) |
|-------------:|---------------:|---------------------------:|----------------------------:|
| 1–10 Hz | 800–80 ms | 12.7 m (the module's timeout binds) | 7.48 m |
| 20 Hz | 40 ms | 12.4 m | 7.48 m |
| 25 Hz | 32 ms | 9.6 m | 7.48 m |
| 40 Hz | 20 ms | 5.5 m | 5.47 m |
| 50 Hz | 16 ms | 4.1 m | 4.11 m |

Above about 30 Hz the window, not the signal, limits the range. The Base Station
panel compares this distance with the size of the arena.

## Downstream: serving the position feed

`ultragps_server.UltraGPSServer` binds five ports. Throughout the code these are
addressed by **name**, not by number:

| Name | Port | Protocol | Carries |
|------|------|----------|---------|
| `cmd` | 8000 | TCP | commands from consumers |
| `pos` | 8001 | UDP | `N: POS <lm\|cep> <x> <y>` |
| `nmea` | 8002 | UDP | `N: NMEA $GPGGA,...` |
| `barrier` | 8003 | UDP | barrier events |
| `barrier_tcp` | 8004 | TCP | barrier events |

Nothing is broadcast until streaming is enabled, and the only thing that enables
it is a consumer sending the command `Ready` on the `cmd` port. This keeps the
network quiet until somebody is actually listening.

A barrier event looks like:

```
N: BARRIER <name> <enter|exit|inside|outside> <x> <y> <lm|cep>
```

## Threading

| Thread | Owns | Notes |
|--------|------|-------|
| Main (Qt) | every widget and Matplotlib canvas | never waits on the network, except that hiding the Position panel waits for its thread to stop (at most one reply timeout) |
| `NetworkThread` | the read/solve/barrier/log loop | a `QThread` in `position_window.py`, running while the Position panel is shown |
| Workers | one job each: a ground-station command, a measurement, a reconnect attempt, a shading map, a point search, a coverage map | `widgets.run_in_thread`; results come back on the GUI thread |
| Calibration run | the collection for one calibration point | inside `UltraGPSCalibration.start_run`; its callbacks reach the panel through queued signals |
| Log saver | writing the `.xlsx` position log when logging stops | so that leaving the Position panel does not wait for it |
| Server threads | one accept thread per TCP port, plus send/recv per client | inside `UltraGPSServer` |
| Client threads | one TCP receive, one UDP receive | inside `UltraGPSClient` |

No thread but the main one touches a widget. Worker results and `NetworkThread`
fixes are Qt signals, which cross to the GUI thread through Qt's queued
connections — that is the only sanctioned cross-thread path in the program.
The client serialises commands with one lock, so two panels' commands never
overlap on the wire (a 16 s `D` sweep makes the others wait).

Redraws are deliberately decoupled from data arrival: a 20 Hz `QTimer` redraws
the position canvas from the latest cached values, and only when something
changed. Positions can arrive faster or slower than that without changing the
redraw cost. The shading and coverage maps are computed on workers 150 ms after
the last edit, and a result computed for points that have changed since is
dropped, so dragging a point never waits for a map.

## Adaptive graphics

`windows/ui_scale.py` turns the main window's size into one scale factor — 1.0
at the 1200 × 900 reference, clamped to 0.6–2.5 — and re-applies everything
registered with it when the factor changes: style sheets (every `px` length is
scaled), fonts, fixed and minimum widget sizes, margins and spacings, and the
application font. Resizes are debounced by 100 ms. `ScalingCanvas`, the
Matplotlib canvas every panel uses, does the same for a figure: its DPI follows
the canvas size, so text, markers and lines grow and shrink with the plot. The
window is sized from the screen at startup and has no large fixed minimum; side
panels sit in scroll areas so that they stay usable on a 1024 × 600 screen.

## Code layout

```
graphics.py            entry point: startup check, feed server, main window
SettingsModule.py      config discovery, config.toml read/write, origin shift
bin/ultragps-control   launcher used by the Debian package
config/                default config.toml, barriers.toml and resources/
windows/               the PyQt6 panels and their shared GUI helpers
libs/                  five independently installable libraries
```

| `windows/` module | What it holds |
|-------------------|---------------|
| `main_window.py` | the main window: shared objects, panel navigation, link indicator and reconnection, main menu |
| `position_window.py`, `position_logger.py` | the Position panel, its `NetworkThread`, the CSV / spreadsheet log |
| `calibration_window.py` | the Calibration panel: Points, Origin (0,0), Run, Check |
| `arena_maker_window.py` | the Setup panel: receiver layout, templates, coverage estimate |
| `base_station_window.py` | the Base Station panel |
| `barrier_drawer_window.py` | the Barriers panel |
| `arena_view.py` | shared arena drawing: receiver outline in any layout, compass rose, beams, point groups, heat maps, click / drag picking |
| `widgets.py` | shared widgets and dialogs: neon buttons, sections, point table, point dialog, origin tool, `run_in_thread` |
| `measure.py` | `measure_position` (average fixes with the transmitter standing still) and `check_point` (compare with a known point); no Qt |
| `ui_scale.py` | the scale factor and `ScalingCanvas` |

`windows/` holds the GUI and nothing else; the protocol, the maths, and the
geometry all live in `libs/`. The dependency direction is one-way — libraries
never import from `windows/` or `SettingsModule.py`, and none of them imports
Qt or Matplotlib.

| Library | Responsibility | Dependencies |
|---------|----------------|--------------|
| `ultragps_client` | TCP/UDP link to the ground station: every command, bare-reply matching, connection state and reconnection | stdlib only |
| `ultragps_position` | multilateration (`core`), and the receiver coverage model (`coverage`) | numpy, scipy |
| `ultragps_calibration` | 2- to 4-point histogram calibration, the one `config.toml` writer (`write_config`), and calibration-point scoring (`points`) | `ultragps_client`, numpy |
| `ultragps_barrier` | barrier geometry, persistence, image overlays, translation for origin moves (`transform`) | stdlib only |
| `ultragps_server` | multi-port server and the message builders | stdlib only |

## Panel lifecycle

Panels live in a `QStackedWidget` and may implement `on_panel_show()` and
`on_panel_hide()`. `show_panel()` hides the outgoing panel before showing the
incoming one, and closing the window hides them all.

!!! note "Anything a panel starts, it must stop"
    Threads and timers started in `on_panel_show()` have to be stopped in
    `on_panel_hide()`. That is how the network thread and the redraw timers avoid
    leaking across panel switches.

The main window shares its objects with every panel — `client`,
`settings_module`, `position_lib`, `cal`, `server` and `ui_scale` — and two
signals:

- `config_changed`, emitted by `notify_config_changed()`. A panel that saves
  anything that changes the receivers, facings, offsets, calibration points or
  the origin calls `notify_config_changed()`, which reloads the settings and
  both libraries; the other panels then refresh, at once or when next shown.
  The Barriers panel and the position panel's barrier manager reload
  `barriers.toml` too, since moving (0, 0) rewrites it.
- `link_changed(bool)`, emitted when the ground-station link goes down or comes
  back.

An exception that escapes a Qt slot aborts a PyQt6 program, so every slot that
can fail — network, files, numbers typed by the user — catches the error and
reports it in the panel.
