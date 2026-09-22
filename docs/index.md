# UltraGPS Control

Operator interface for the St. Mary's University **UltraGPS** indoor positioning
system, developed as a result of graduate research.

`ultragps-control` runs on any computer on the same network as the ground-station
host. It asks the ground station for raw per-receiver tick counts, converts them
into a 2D position by multilateration, and draws the result live on an arena plot.
Beyond tracking, it is also the tool used to set the arena up: placing receivers,
calibrating them, and drawing the virtual barriers that fire events when the
transmitter crosses them.

The position it computes does not stay inside the GUI. Every solved position is
re-broadcast over TCP and UDP — as a raw coordinate, as an NMEA sentence, and as
barrier-crossing events — so other programs can consume the feed without knowing
anything about the positioning hardware.

The [`UltraGpsXY`](https://imperious22m.github.io/UltraGPS-Ground/) ground-station
program must be running before this program is started.

## Documentation

- **[Architecture](architecture.md)** — how the program is structured, and how
  data flows from the Arduino through the solver to the network clients.
- **[Installation](installation.md)** — installing the Debian package or
  setting up a development checkout.
- **[Configuration](configuration.md)** — where the settings files live, the
  order they are searched in, and what is in them.
- **[Usage](usage.md)** — running the program, its command-line options, and a
  tour of the four panels.

## At a glance

| | |
|---|---|
| Command | `ultragps-control` |
| Language | Python 3.11+ (PyQt6 + Matplotlib) |
| Upstream ground station | TCP `9000` (commands), UDP `9001` (stream) |
| Downstream position feed | UDP `8001` |
| Downstream NMEA feed | UDP `8002` |
| Downstream barrier events | UDP `8003`, TCP `8004` |
| Downstream command port | TCP `8000` |
| Units | centimetres, origin at arena centre |
| Platform | Linux (packaged for Ubuntu/Debian, architecture-independent) |
