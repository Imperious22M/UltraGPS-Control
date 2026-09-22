# Usage

## Before you start

The [`UltraGpsXY`](https://imperious22m.github.io/UltraGPS-Ground/) ground-station
program must already be running on the host that has the base-station Arduino
plugged into it. UltraGPS Control connects to it over the network; it does not
talk to the hardware directly.

## Running

```bash
ultragps-control                        # installed from the .deb
./run.sh                                # from a development checkout
```

### Options

| Option | Default | Effect |
|--------|---------|--------|
| `--ip <address>` | `127.0.0.1` | Address of the ground-station host |
| `--config <path>` | search path | Use a specific `config.toml` |
| `-h`, `--help` | — | Show usage |

```bash
ultragps-control --ip 192.168.1.100
ultragps-control --config ~/arenas/gym/config.toml
```

With no `--config`, the settings file is looked up in `~/.config`, then `./config`,
then `/etc` — see [Configuration](configuration.md).

The program connects to TCP `9000` and UDP `9001` on the `--ip` host, and binds
its own ports `8000`–`8004` locally.

## The main menu

The window opens on a menu with a live server-status panel and four buttons. The
status panel shows the address the server bound to, whether streaming is on, each
port with its connected-client count, and the total.

| Panel | What it is for |
|-------|----------------|
| **Position** | live tracking |
| **Calibration** | two-run receiver calibration |
| **Setup** | arena and receiver geometry |
| **Barriers** | drawing virtual barriers |

A panel stops its background work when you leave it, so the network thread only
runs while the Position panel is open.

## Setup

Start here on a new arena. The panel builds the receiver layout from the
distances *between* receivers, which is far easier to measure accurately than
absolute coordinates.

1. Enter the measured inter-receiver distances, or give the arena's overall
   dimensions to generate a rectangular layout.
2. Place the two calibration points. These are the known positions the
   transmitter will be put at during calibration.
3. Press **Verify** to check the geometry is consistent, then save.

Saving sets `valid_settings = true` in `config.toml`. Until then the program
warns at startup that the settings are uninitialised and treats the arena size
as zero.

## Calibration

Calibration turns raw tick counts into centimetres, and has to be redone whenever
receivers move.

1. Put the transmitter on **calibration point 1** and start run 1. The panel
   collects readings and shows a live histogram per receiver.
2. Move the transmitter to **calibration point 2** and run it again.
3. Save. The modal tick value from each run is paired with the known distance
   from that receiver to each point, giving a slope and intercept per receiver:

```
slope     = (dist_1 - dist_2) / (tick_1 - tick_2)
intercept = dist_1 - slope * tick_1
```

The histograms are the useful diagnostic here — a receiver with a broad or
double-peaked distribution is picking up reflections rather than the direct
pulse, and its calibration will be poor.

## Position

The tracking view. The arena plot shows the receivers, the current position, and
a fading trail of the last 50 fixes; six mini-plots around it show each
receiver's recent distance history.

Two solutions are drawn at once — **LM** and **CEP** (see
[Architecture](architecture.md)). Comparing them is the quickest way to judge
whether a fix is trustworthy: when they disagree, at least one receiver is
feeding bad data.

Indicators to watch:

- **positions/sec** — the achieved update rate.
- **Per-receiver LEDs** — which receivers passed the sanity filter for the
  current reading.
- **Insufficient receivers** — fewer than three passed, so the displayed position
  is a stale fallback rather than a new fix.

The panel can poll in either mode: **continuous** (the ground station streams
over UDP) or **pulse** (one TCP request per fix). Continuous is faster; pulse is
easier to reason about when debugging.

Barriers are drawn on the arena and flash when they trigger.

## Barriers

An interactive editor for the virtual zones that fire events when the
transmitter enters or leaves them.

- **Polygon** — click to place vertices, close the shape to finish.
- **Circle** — click the centre, drag out the radius.
- **Line** — click both endpoints; `thickness` sets how close counts as crossing.

For each barrier you set a name, colour and opacity, and choose:

| Setting | Options | Meaning |
|---------|---------|---------|
| `trigger_when` | `inside` / `outside` | which condition is the active one |
| `trigger_mode` | `event` / `continuous` | fire once on transition, or on every update while it holds |
| `callback_name` | any string | passed through in the event, for consumers to dispatch on |

You can also import an image — a court or field diagram — as a backdrop, position
and rotate it, and trace its outline into a polygon barrier automatically.

Barriers are saved to `barriers.toml` in the configuration directory, and images
are copied into `resources/` beside it.

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
