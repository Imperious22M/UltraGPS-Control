# Developer's Guide

## Getting set up

```bash
git clone https://github.com/Imperious22M/UltraGPS-Control.git
cd UltraGPS-Control
./install.sh          # creates .venv and installs everything
./run.sh
```

`install.sh` installs the third-party dependencies and the five libraries under
`libs/` as **editable** installs, so edits inside `libs/` take effect without a
reinstall. It finishes by importing every module to prove the environment works.

The program does not start without a ground station: see
[Running without the hardware](#running-without-the-hardware).

## Repository layout

```
graphics.py            entry point: startup check, feed server, main window
SettingsModule.py      config discovery, config.toml read/write, origin shift
bin/ultragps-control   launcher used by the Debian package
build.sh               packaging and docs
install.sh / run.sh    development environment
VERSION                single source of truth for the package version
config/                default config.toml, barriers.toml and resources/
debian/                Debian packaging
docs/                  this documentation
libs/                  the five libraries
windows/               PyQt6 panels and their shared helpers
```

[Architecture](architecture.md#code-layout) lists what each module under
`windows/` and `libs/` holds.

## Where to make a change

| Change | Where it belongs |
|--------|------------------|
| A new panel | `windows/`, registered in `windows/main_window.py` |
| A widget or dialog more than one panel needs | `windows/widgets.py` |
| How the arena is drawn (outline, receivers, heat maps, picking) | `windows/arena_view.py` |
| Scaling with the window size | `windows/ui_scale.py` |
| Protocol or wire format | `libs/ultragps_client`, `libs/ultragps_server` |
| Positioning maths | `libs/ultragps_position` (`core.py`) |
| Receiver limits and coverage maps | `libs/ultragps_position/ultragps_position/coverage.py` |
| Barrier geometry or persistence | `libs/ultragps_barrier` |
| Calibration procedure | `libs/ultragps_calibration` (`calibration.py`) |
| Calibration-point scoring, search and checks | `libs/ultragps_calibration/ultragps_calibration/points.py` |
| A new setting | `SettingsModule.py` — and see the warning below |

The dependency direction is one-way: libraries never import from `windows/` or
`SettingsModule.py`, and none imports Qt or Matplotlib. Keeping the maths and the
protocol out of the GUI is what makes them testable without a running Qt
application. The UltraGPS Simulator carries its own copies of
`ultragps_position` and `ultragps_calibration`, so keep their public API
backward compatible.

!!! warning "config.toml has one writer and three readers"
    Everything that writes `config.toml` goes through
    `ultragps_calibration.write_config` (`tomllib` is read-only), which keeps
    every key it does not know, so a new key survives every save. Three places
    read the file: `SettingsModule`, `ultragps_position.load_config` and
    `UltraGPSCalibration`. A key that changes what the positioning or the
    calibration does has to be read in each place that needs it; add it to
    `CONFIG_KEY_ORDER` in `calibration.py` to give it a fixed place in the file.
    After saving, a panel calls `main_window.notify_config_changed()` so that
    the libraries and the other panels reload.

## Threading rules

Four rules cover almost everything:

1. **Only the main thread touches widgets.** `NetworkThread` emits Qt signals;
   Qt's queued connections deliver them on the GUI thread. Never call into a
   widget or a Matplotlib canvas from a worker.
2. **Never wait on the network on the main thread.** Run every client command,
   measurement or long computation with `widgets.run_in_thread(fn, ...,
   on_result=..., on_error=...)`: the callbacks run on the GUI thread. Disable
   the buttons while it runs, show `client.last_error` when it fails, and
   follow `main_window.link_changed`. Once connected, client commands return
   `None` at once while the link is down, so loops must back off rather than
   spin.
3. **Whatever a panel starts, it stops.** Threads and timers begun in
   `on_panel_show()` must be shut down in `on_panel_hide()`, which runs both on
   panel switches and on window close.
4. **No exception escapes a slot.** PyQt6 aborts the whole program on one.
   A slot that does I/O, saves the configuration or parses user input catches
   the error and reports it in the panel.

Redraws are decoupled from data: a 20 Hz `QTimer` repaints from cached values,
and only when something changed, so the redraw cost does not scale with the
position rate.

## Scaling rules

The panels follow the window size (see
[Architecture](architecture.md#adaptive-graphics)). In new GUI code:

- style widgets with `ui_scale.style(widget, css)` rather than
  `setStyleSheet`, so that every `px` in the CSS scales;
- set fonts and sizes with `ui_scale.font`, `ui_scale.fixed_width`,
  `ui_scale.min_width`, `ui_scale.margins`, `ui_scale.spacing` and friends;
- put Matplotlib figures in a `ui_scale.ScalingCanvas`, and side panels that
  can overflow a small window in `widgets.scroll_area`;
- use the `widgets` dialogs (`info`, `confirm`, `ask_point` …): no
  `setFixedSize`, and no large `setMinimumSize`.

Check a new panel at 1024 × 600 and at 3840 × 2160.

## Adding a panel

1. Write a `QWidget` subclass in `windows/`, with `on_panel_show()` /
   `on_panel_hide()` if it owns any background work.
2. Register it in `UltraGPSMainWindow._init_panels()` under a short name.
3. Add an entry — label, panel name, button colour, text colour — to
   `MainMenuPanel.BUTTONS`.

Panels receive the shared `SettingsModule`, `UltraGPSClient`, position library
and calibration objects from the main window — construct nothing that already
exists there. The main window also offers `show_panel(name)`,
`notify_config_changed()` (call it after every save that changes receivers,
facings, offsets, calibration points or the origin) and the signals
`config_changed` and `link_changed(bool)`; connect to them in the constructor.

## Adding a library

1. Create `libs/ultragps_<name>/` with a `pyproject.toml` and the package inside.
2. Add `-e libs/ultragps_<name>` to `requirements.txt`.
3. Add the import to the sanity check at the end of `install.sh`.
4. Add the path to `.vscode/settings.json` so the editor resolves it.
5. Add the source root to `[tool.setuptools.packages.find] where` in the root
   `pyproject.toml`, or the Debian package will not ship it.

## Running without the hardware

UltraGPS Control only talks to the ground-station program, so it runs on any
computer that can reach one. Against the real system, start `UltraGpsXY` (from
the UltraGPS-Ground checkout) on the computer with the base station plugged in,
then point UltraGPS Control at it:

```bash
./build/UltraGpsXY -s /dev/ttyACM0 -t 9000 -u 9001     # ground-station computer
./run.sh --ip 192.168.1.100                            # here
```

With no hardware at all, any program that speaks the
[ground-station protocol](architecture.md#upstream-talking-to-the-ground-station)
can stand in for `UltraGpsXY`. This one answers every command and streams one
fixed reading; with the shipped `config.toml` it puts the transmitter near
(0, 0):

```python
"""A minimal stand-in for UltraGpsXY: no base station, one fixed reading."""
import socket
import threading
import time

TCP_PORT, UDP_PORT = 9000, 9001
TICKS = "499, 393, 527, 527, 392, 525"      # R1..R6; near (0, 0) with the shipped config
REPLIES = {"P": "N: " + TICKS, "S": "N: " + TICKS, "R": "N: R", "E": "E: 2000",
           "M": "M: OK", "D": "D: 2000, 450, 18/18", "F": "F: 10, 6249, 80",
           "H": "H: 80, 5000", "B": "B: 38400"}

udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.bind(("", UDP_PORT))
peers, streaming = set(), threading.Event()


def register():                       # any datagram registers its sender for the stream
    while True:
        peers.add(udp.recvfrom(64)[1])


def stream():                         # continuous mode: one reading per 100 ms (10 Hz)
    while True:
        time.sleep(0.1)
        if streaming.is_set():
            for peer in list(peers):
                udp.sendto(("C: " + TICKS).encode(), peer)


threading.Thread(target=register, daemon=True).start()
threading.Thread(target=stream, daemon=True).start()
server = socket.create_server(("", TCP_PORT))
while True:
    conn, _ = server.accept()         # one client at a time
    with conn:
        while data := conn.recv(64):  # one command per segment, e.g. b"P\n" or b"H 0\n"
            letter = data.decode(errors="replace").strip()[:1]
            streaming.clear()         # any command ends continuous mode ...
            if letter == "C":
                streaming.set()       # ... and C (no reply) starts it
            elif letter in REPLIES:
                conn.sendall(REPLIES[letter].encode())    # bare reply, no newline
```

Save it outside the repository (say as `~/standin_ground.py`), run it with
`.venv/bin/python ~/standin_ground.py`, then `./run.sh` in another terminal.
Stop it with Ctrl+C. It does not remember settings changes, and a second client
waits until the first one leaves instead of being turned away.

When trying changes, run against a copy of the configuration —
`./run.sh --config /tmp/arena/config.toml`, with `barriers.toml` and
`resources/` copied beside it — so that the checkout's `config/` is not
rewritten: the program saves the configuration as soon as it starts, and every
panel saves to the active configuration directory. `QT_QPA_PLATFORM=offscreen`
runs the GUI without a display, for scripted checks.

## Documentation

The site is MkDocs with the built-in `readthedocs` theme, built in `strict` mode
so a broken internal link fails the build.

```bash
pip install -r docs/requirements.txt
./build.sh docs          # live preview on http://127.0.0.1:8000
./build.sh docs-build    # render into ./site
```

Pushing a change under `docs/` or `mkdocs.yml` to the `release` branch triggers
`.github/workflows/docs.yml`, which runs `mkdocs gh-deploy` and publishes to the
`gh-pages` branch. New pages must be added to the `nav:` list in `mkdocs.yml`.

## Cutting a release

A release is a `.deb` built and attached to a GitHub Release automatically.

1. Bump the `VERSION` file. It is the only place the version is written —
   `pyproject.toml` reads it, and `build.sh` generates `debian/changelog` from it.
2. Test the package locally:

   ```bash
   ./build.sh deb
   sudo apt install ./build/ultragps-control_*_all.deb
   ```

3. Commit, make sure `release` points at the commit you want to ship, then tag
   and push:

   ```bash
   git checkout release
   git tag v1.0
   git push origin v1.0
   ```

`.github/workflows/release.yml` takes over: it checks the tag matches `VERSION`
(failing loudly if not), builds the `.deb`, and publishes it under the Releases
tab with generated notes.

## Packaging notes

The package is built with `debhelper` and `dh-python`, driven by the root
`pyproject.toml`.

The five `ultragps_*` libraries install normally into `dist-packages`. The
application itself does not: `graphics`, `SettingsModule` and `windows` are far
too generic to occupy the global Python path, so they are installed to
`/usr/share/ultragps-control/`, and `/usr/bin/ultragps-control` prepends that
directory to `sys.path` before importing them.

Runtime dependencies are satisfied by Debian's own packages
(`python3-pyqt6`, `python3-matplotlib`, `python3-numpy`, `python3-scipy`,
`python3-openpyxl`) rather than by pip, which is why the package is `Architecture: all` and needs Ubuntu
24.04 / Debian 12 or newer.

`build.sh deb` stages the tree into `/tmp`, builds there, and copies the result
back into `./build/` — the working tree is never touched, and `debian/changelog`
is generated rather than committed.
