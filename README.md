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
  search — so their disagreement flags an unreliable fix.
- Displays the result live on an arena plot with per-receiver distance traces.
- Provides panels for **arena setup**, **two-point calibration**, and drawing
  **virtual barriers** that fire events when the transmitter crosses them.
- **Re-broadcasts** every position over TCP and UDP — as a coordinate, as an
  NMEA `$GPGGA` sentence, and as barrier events — so other programs on the
  network can consume the feed.

It is a PyQt6 + Matplotlib application. The protocol, the positioning maths and
the barrier geometry live in five separate libraries under `libs/`, none of which
depend on the GUI.

The ground-station program must be running before this one is started.

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

## Requirements

- Python 3.11 or later (for `tomllib`)
- PyQt6, Matplotlib, NumPy, SciPy

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
`python3-*` modules — never the development virtualenv, which has no `build`
module and would fail with `No module named build`. `build.sh` drops any
activated virtualenv from the packaging environment itself, reporting

```
>> Ignoring the active virtualenv; packaging uses the system Python.
```

when it does, so `./build.sh deb` is safe to run with `.venv` on your `PATH`.

### Building the documentation

The `docs` commands need MkDocs, which `install.sh` does **not** install:

```bash
pip install -r docs/requirements.txt
./build.sh docs         # live preview on http://127.0.0.1:8000
./build.sh docs-build   # render into ./site/
```

The site builds with `strict: true`, so a broken internal link fails the build.

## Configuration

Settings live in a directory holding `config.toml`, `barriers.toml` and
`resources/`. It is searched for in this order, first hit wins:

1. `~/.config/ultragps-control/` — your own settings
2. `./config/` — the checkout's defaults
3. `/etc/ultragps-control/` — installed by the Debian package

The three travel together as a unit: `resources/` holds the image overlays that
`barriers.toml` refers to, so a copy of the directory is never split up. It ships
with three example PNGs — red, green and blue circles — which you can place on
the arena with **Import** in the barrier editor.

If none exists, a default is created in `/etc` (or in `~/.config` when `/etc`
isn't writable). When the active directory is read-only, the first save copies
the whole directory — `resources/` included — into `~/.config/ultragps-control/`
and continues from there, so an installed package's defaults are never modified.
`--config <path>` bypasses the search.

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
in the documentation for the architecture, threading rules, and packaging notes.
