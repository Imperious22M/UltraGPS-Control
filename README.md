# UltraGPS Control

Operator interface for the St. Mary's University **UltraGPS** indoor positioning
system, developed as part of graduate research.

## What it does

`ultragps-control` runs on any machine on the same network as the ground-station
host. It:

- Polls the [`UltraGpsXY`](https://github.com/Imperious22M/UltraGPS-Ground)
  ground station (TCP `9000` for commands, UDP `9001` for the stream) for raw
  per-receiver tick counts.
- Converts them into a **2D position** by multilateration, running two solvers
  side by side — an OLS-seeded Levenberg-Marquardt fit and a CEP best-subset
  search — so their disagreement flags an unreliable fix. The position panel
  also shows how many receivers passed the sanity filter.
- Displays the result live on an arena plot with per-receiver distance traces.
- **Calibrates** the receivers on the Calibration panel. The **origin tool**
  moves (0, 0) — picked, typed, offset, or measured by standing the transmitter
  on the mark and confirming. **Offsets** edits every receiver's slope and
  intercept, with a one-click reset of the slopes to the default 0.272.
  **System Origin Calibration** measures each receiver's distance with the
  transmitter on (0, 0) and turns it into a new intercept — the distance the
  arena map gives minus the distance measured — and shows the current and new
  intercepts side by side before asking to save them.
- Lays out the arena (**Setup**): 3 to 6 active receivers (of the base
  station's six channels; dormant ones keep their calibration), placed by
  clicking or dragging them on the arena (or typed), each with a facing, or
  built from a W × H rectangle or tape-measure distances. An optional
  **coverage estimate** shades how many receivers hear each spot, under a
  sensor model whose reception angle, distances and timing are edited in a
  **Sensor settings…** pop-up and saved in the configuration directory
  (`sensor.toml`).
- Exposes the base station on a **Base Station** panel: trigger rate (`F`),
  sampling window (`H`), baud rate (`B`, read-only), burst-delay sweep (`D`)
  and default (`E`), reset transmitter (`R`), reset settings (`M`), and test
  pulses (`P`, `S`, live `C`), each explained in terms of range, update rate
  and calibration.
- Draws **virtual barriers** that fire events when the transmitter crosses
  them.
- **Re-broadcasts** every position over TCP and UDP — as a coordinate, as an
  NMEA `$GPGGA` sentence, and as barrier events — so other programs on the
  network can consume the feed.

The window adapts to the screen: controls, text and plots scale with the window
size, from a 1024×600 laptop screen up to a 4K monitor.

It is a PyQt6 + Matplotlib application. The protocol, the positioning maths, the
calibration and coverage models and the barrier geometry live in five separate
libraries under `libs/`, none of which depend on the GUI.

The ground-station program must be running before this one is started. If it
is not — or another program holds its only client slot — UltraGPS Control says
so, shows how to start it, and offers **Retry** / **Quit** instead of crashing.
Once running, a link indicator at the bottom of every panel shows the
connection, and a lost ground station is reconnected automatically in the
background.

See the [full documentation](https://imperious22m.github.io/UltraGPS-Control/)
for architecture, configuration, and usage.

## Quick start

You don't need to build anything to run the program — each release ships a
prebuilt Debian package. Download the latest `.deb` from the
**[Releases page](https://github.com/Imperious22M/UltraGPS-Control/releases/latest)**
and install it:

```bash
sudo apt install ./ultragps-control_*_all.deb
```

Then run it, pointing it at the ground-station host:

```bash
ultragps-control --ip 192.168.1.100
```

### Options

| Option | Default | Effect |
|--------|---------|--------|
| `--ip HOST` | `127.0.0.1` | Address (or host name) of the computer running `UltraGpsXY` |
| `--ground-tcp-port PORT` | `9000` | Its TCP command port (`UltraGpsXY -t`) |
| `--ground-udp-port PORT` | `9001` | Its UDP stream port (`UltraGpsXY -u`) |
| `--config PATH` | search path | Use this `config.toml` (see [Configuration](#configuration)) |
| `-v`, `--verbose` | off | Log debug messages |
| `-h`, `--help` | — | Show usage |

### Exit status

| Status | When |
|-------:|------|
| `0` | The window was closed normally |
| `1` | It could not start: no ground station (or it was busy) and you chose **Quit** or pressed Ctrl+C, a position-feed port (`8000`–`8004`) is already in use — usually by another UltraGPS Control — or `config.toml` could not be read |
| `130` / `143` | Stopped by Ctrl+C (`SIGINT`) / `SIGTERM` once running (128 + the signal number); the window closes cleanly first |

## Requirements

- Python 3.11 or later (for `tomllib`)
- PyQt6, Matplotlib, NumPy, SciPy, openpyxl

On Ubuntu 24.04 / Debian 12 and later these are all available as system packages,
which is what the `.deb` depends on — no pip needed for an installed copy.

## Development checkout

```bash
git clone https://github.com/Imperious22M/UltraGPS-Control.git
cd UltraGPS-Control
./install.sh          # creates .venv, installs deps + the five local libraries
./run.sh              # or: ./run.sh --ip 192.168.1.100
```

`install.sh` installs the libraries as editable installs, so changes under
`libs/` take effect with no reinstall. `run.sh` calls `.venv/bin/python` directly
— there is no `activate` step.

The program will not start without a ground station. To try it on a computer
with no UltraGPS hardware, run the small stand-in ground station from the
[Developer's Guide](https://imperious22m.github.io/UltraGPS-Control/developer-guide/#running-without-the-hardware)
first.

## Using the build script

`build.sh` is the single entry point for packaging and the docs:

```bash
./build.sh <command>
```

| Command | What it does |
|---------|--------------|
| `deb` | Build the Debian package into `./build/` |
| `clean` | Remove `./build/` and `./site/` |
| `docs` | Preview the documentation locally with live reload |
| `docs-build` | Render the static documentation site into `./site/` |

### Building the Debian package

```bash
sudo apt-get install -y debhelper dh-python pybuild-plugin-pyproject \
                        python3-all python3-setuptools
./build.sh deb        # → ./build/ultragps-control_<version>_all.deb
sudo apt install ./build/ultragps-control_*_all.deb
```

> **Note** The `deb` target stages the tree into a temporary directory under
> `/tmp` and builds there, so the working tree is left untouched.

A Debian package is built against the **system** Python and Debian's own
`python3-*` modules. pybuild's `build` module ships in
`/usr/lib/python3/dist-packages`, which only Debian's `/usr/bin/python3` has on
`sys.path` — any other interpreter found first on `PATH` fails with
`No module named build`.

`build.sh` does not rearrange your environment — it builds with the Python it is
called with, so **run `./build.sh deb` from a system shell, not from the
development virtualenv**. It refuses to start inside one rather than failing
halfway through:

```
error: ./build.sh deb is running inside a virtualenv:
    /path/to/UltraGPS-Control/.venv
```

If `deactivate` is not defined — the venv is on your `PATH` from a shell profile
rather than a sourced `activate` — run it from a clean environment:

```bash
env -u VIRTUAL_ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    ./build.sh deb
```

The same guard rejects any other non-Debian interpreter, not just virtualenvs —
only `/usr/bin/python3` can import `build` from `/usr/lib/python3/dist-packages`:

```
error: ./build.sh deb needs the system Python, but python3 here is
    /opt/hostedtoolcache/Python/3.12.14/x64/bin/python3
```

This is why `.github/workflows/release.yml` leads with `/usr/bin` in its build
step — GitHub's runners put a hosted tool-cache Python ahead of `/usr/bin`:

```yaml
run: |
  export PATH="/usr/bin:$PATH"
  bash build.sh deb
```

### Building the documentation

The `docs` commands need MkDocs, which `install.sh` does **not** install:

```bash
pip install -r docs/requirements.txt
./build.sh docs         # live preview on http://127.0.0.1:8000
./build.sh docs-build   # render into ./site/
```

The site builds with `strict: true`, so a broken internal link fails the build.

## Configuration

Settings live in a directory holding `config.toml`, `barriers.toml`,
`sensor.toml` and `resources/`. It is searched for in this order, first hit wins:

1. `~/.config/ultragps-control/` — your own settings
2. `./config/` — the checkout's defaults
3. `/etc/ultragps-control/` — installed by the Debian package

They travel together as a unit: `resources/` holds the image overlays that
`barriers.toml` refers to, so a copy of the directory is never split up. It ships
with three example PNGs — red, green and blue circles — which you can place on
the arena with **Import** in the barrier editor.

`sensor.toml` holds the receiver model of the Setup panel's **Sensor
settings…** pop-up, which the coverage estimate uses. It is written when the pop-up's settings are
applied; until then, the HC-SR04 datasheet limits (2–400 cm within ±15°) are
used.

If none exists, a default is created in `/etc` (or in `~/.config` when `/etc`
isn't writable). When the active directory is read-only, the first save copies
the whole directory — `resources/` included — into `~/.config/ultragps-control/`
and continues from there, so an installed package's defaults are never modified.
`--config <path>` bypasses the search.

Every save keeps the keys it does not know about, so hand-added settings survive.
Moving the arena's (0, 0) rewrites `config.toml` and `barriers.toml` together, so
the barriers stay on the same spot of the floor; `sensor.toml` holds no
coordinates and is left as it is. Each receiver's distance to (0, 0)
(`origin_distance`, used by the System Origin Calibration) is derived from its
position and kept up to date in `config.toml` at startup and whenever the arena
changes.

See [Configuration](https://imperious22m.github.io/UltraGPS-Control/configuration/)
for the file formats.

## Releasing

The `VERSION` file is the single source of truth. Bump it, commit, then tag:

```bash
git checkout release
git tag v1.0
git push origin v1.0
```

GitHub Actions builds the `.deb` and attaches it to a new GitHub Release. The
workflow fails if the tag doesn't match `VERSION`.

## Developer's Guide

See the **[Developer's Guide](https://imperious22m.github.io/UltraGPS-Control/developer-guide/)**
in the documentation for the architecture, threading and scaling rules, running
without the hardware, and packaging notes.
