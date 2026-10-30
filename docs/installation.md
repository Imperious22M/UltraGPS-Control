# Installation

There are two ways to get UltraGPS Control onto a machine: install the prebuilt
Debian package from a release, or set up a development checkout.

## Install from a release (recommended)

Each tagged version is published on the
[Releases page](https://github.com/Imperious22M/UltraGPS-Control/releases) with
the `.deb` package attached. Download the latest
`ultragps-control_<version>_all.deb` and install it:

```bash
sudo apt install ./ultragps-control_*_all.deb
```

Using `apt install ./file.deb` rather than `dpkg -i` lets apt pull in the
runtime dependencies (`python3-pyqt6`, `python3-matplotlib`, `python3-numpy`,
`python3-scipy`, `python3-openpyxl`) in the same step. If you use `dpkg -i` instead, follow it with
`sudo apt-get install -f` to resolve them.

Then run it from a terminal or from your desktop's application menu:

```bash
ultragps-control            # see `ultragps-control --help` for options
```

The ground-station program (`UltraGpsXY`) must be running first; if it is not,
the program says so and offers Retry / Quit. See [Usage](usage.md) for the
options and the startup check.

The package is architecture-independent (`all`), since it ships pure Python.

### What the package installs

| Path | Contents |
|------|----------|
| `/usr/bin/ultragps-control` | launcher |
| `/usr/lib/python3/dist-packages/ultragps_*` | the five libraries |
| `/usr/share/ultragps-control/` | application modules |
| `/etc/ultragps-control/` | default `config.toml` and `barriers.toml` |
| `/usr/share/applications/` | desktop menu entry |

The application modules are installed to a private directory rather than to
`dist-packages` because module names like `graphics` and `windows` are too
generic to sit on the global Python path. The launcher adds that directory to
`sys.path` before importing them.

!!! note "Your settings are never overwritten"
    Files under `/etc` are registered as conffiles, so local edits survive an
    upgrade. In practice the program copies them into
    `~/.config/ultragps-control/` the first time it saves anything, and works
    from there afterwards. See [Configuration](configuration.md).

## Requirements

- Python 3.11 or later (for `tomllib`)
- PyQt6, Matplotlib, NumPy, SciPy, openpyxl

On Ubuntu 24.04 / Debian 12 and later these are all available as system
packages, which is what the `.deb` depends on.

Of the five libraries, `ultragps_position` needs NumPy and SciPy, and
`ultragps_calibration` needs NumPy (for choosing calibration points) and
`ultragps_client`; the others use the standard library only. openpyxl is used
only for the spreadsheet copy of the position log: without it the log is
written as CSV alone.

## Development checkout

Clone the repository and run the installer, which creates a virtual environment
and installs the third-party dependencies plus the five local libraries as
editable installs:

```bash
git clone https://github.com/Imperious22M/UltraGPS-Control.git
cd UltraGPS-Control
./install.sh
./run.sh
```

`run.sh` invokes `.venv/bin/python graphics.py` directly, so there is no
`activate` step, and passes the program's options on:

```bash
./run.sh --ip 192.168.1.100
./run.sh --ip 192.168.1.100 --ground-tcp-port 9100 --ground-udp-port 9101 -v
```

To try the program on a computer without the UltraGPS hardware, start the
stand-in ground station from the
[Developer's Guide](developer-guide.md#running-without-the-hardware) first.

Because the libraries are installed editable, changes under `libs/` take effect
immediately with no reinstall.

## Building the Debian package locally

`build.sh` is the single entry point for packaging, cleaning, and previewing the
docs. Building a `.deb` needs the Debian packaging tools:

```bash
sudo apt-get install -y debhelper dh-python pybuild-plugin-pyproject \
                        python3-all python3-setuptools
./build.sh deb            # → ./build/ultragps-control_<version>_all.deb
sudo apt install ./build/ultragps-control_*_all.deb
```

| Command | What it does |
|---------|--------------|
| `./build.sh deb` | Build the Debian package into `./build/` |
| `./build.sh clean` | Remove `./build/` and `./site/` |
| `./build.sh docs` | Preview the documentation with live reload |
| `./build.sh docs-build` | Render the static documentation into `./site/` |

!!! note "Packaging builds in an isolated directory"
    The `deb` target stages the source tree into a temporary directory under
    `/tmp`, builds there, and copies the finished `.deb` back into `./build/`.
    The working tree is left untouched, and `debian/changelog` is generated from
    the `VERSION` file rather than being committed.
