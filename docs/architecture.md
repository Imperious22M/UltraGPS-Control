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

`ultragps_client.UltraGPSClient` maintains the link to `UltraGpsXY`. Commands are
a single ASCII letter followed by a newline, sent over TCP:

| Command | Meaning | Response |
|---------|---------|----------|
| `P` | Pulse — trigger one measurement | `N: <6 ticks>` over TCP |
| `S` | Simulate — one measurement from simulated data | `N: <6 ticks>` over TCP |
| `C` | Continuous — switch to streaming | `C: <6 ticks>` over UDP, repeatedly |

The client keeps a receive thread on each socket. In continuous mode the newest
UDP reading is cached and handed out by `get_latest_reading()`; in pulse mode
`pulse()` blocks until the matching TCP response arrives or the timeout expires.

## The solver

`ultragps_position.UltraGPSPositionLib` turns tick counts into a coordinate.

Each receiver has its own linear calibration, so ticks become centimetres via:

```
distance_i = tick_i * slope_i + intercept_i
```

Before solving, every receiver passes through a two-part sanity filter:

1. **Bounds** — a reported distance larger than the greatest distance between any
   two receivers cannot be physically valid inside the arena.
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
| Main (Qt) | every widget and Matplotlib canvas | never blocked by I/O |
| `NetworkThread` | the poll/solve/barrier loop | a `QThread` in `position_window.py` |
| Server threads | one accept thread per TCP port, plus send/recv per client | inside `UltraGPSServer` |
| Client threads | one TCP receive, one UDP receive | inside `UltraGPSClient` |

`NetworkThread` never touches a widget. It emits Qt signals, which cross to the
GUI thread through Qt's queued connections — that is the only sanctioned
cross-thread path in the program.

Redraws are deliberately decoupled from data arrival: a 20 Hz `QTimer` redraws
the canvas from whatever the latest cached values are. Positions can arrive
faster or slower than that without changing the redraw cost.

## Code layout

```
graphics.py            entry point: starts the server, opens the window
SettingsModule.py      config discovery + read/write of config.toml
bin/ultragps-control   launcher used by the Debian package
config/                default config.toml and barriers.toml
windows/               one PyQt6 panel per screen
libs/                  five independently installable libraries
```

`windows/` holds the GUI and nothing else; the protocol, the maths, and the
geometry all live in `libs/`. The dependency direction is one-way — libraries
never import from `windows/` or `SettingsModule.py`.

| Library | Responsibility | Dependencies |
|---------|----------------|--------------|
| `ultragps_client` | TCP/UDP link to the ground station | stdlib only |
| `ultragps_position` | calibration and multilateration | numpy, scipy |
| `ultragps_calibration` | two-point histogram calibration | `ultragps_client` |
| `ultragps_barrier` | barrier geometry, persistence, image overlays | stdlib only |
| `ultragps_server` | multi-port server and the message builders | stdlib only |

## Panel lifecycle

Panels live in a `QStackedWidget` and may implement `on_panel_show()` and
`on_panel_hide()`. `show_panel()` hides the outgoing panel before showing the
incoming one, and closing the window hides them all.

!!! note "Anything a panel starts, it must stop"
    Threads and timers started in `on_panel_show()` have to be stopped in
    `on_panel_hide()`. That is how the network thread and the redraw timers avoid
    leaking across panel switches.
