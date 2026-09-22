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

## Repository layout

```
graphics.py            entry point
SettingsModule.py      config discovery and config.toml read/write
bin/ultragps-control   launcher used by the Debian package
build.sh               packaging and docs
install.sh / run.sh    development environment
VERSION                single source of truth for the package version
config/                default config.toml and barriers.toml
debian/                Debian packaging
docs/                  this documentation
libs/                  the five libraries
windows/               PyQt6 panels
```

## Where to make a change

| Change | Where it belongs |
|--------|------------------|
| A new panel or widget | `windows/` |
| Protocol or wire format | `libs/ultragps_client`, `libs/ultragps_server` |
| Positioning maths | `libs/ultragps_position` |
| Barrier geometry or persistence | `libs/ultragps_barrier` |
| Calibration procedure | `libs/ultragps_calibration` |
| A new setting | `SettingsModule.py` — and see the warning below |

The dependency direction is one-way: libraries never import from `windows/` or
`SettingsModule.py`. Keeping the maths and the protocol out of the GUI is what
makes them testable without a running Qt application.

!!! warning "config.toml has three independent parsers"
    `SettingsModule` (read/write, with a hand-rolled TOML writer because
    `tomllib` is read-only), `ultragps_position.load_config` (read-only), and
    `UltraGPSCalibration._write_config` (read/write) each parse the file
    separately. A schema change has to be made in all three.

## Threading rules

Two rules cover almost everything:

1. **Only the main thread touches widgets.** `NetworkThread` emits Qt signals;
   Qt's queued connections deliver them on the GUI thread. Never call into a
   widget or a Matplotlib canvas from a worker.
2. **Whatever a panel starts, it stops.** Threads and timers begun in
   `on_panel_show()` must be shut down in `on_panel_hide()`, which runs both on
   panel switches and on window close.

Redraws are decoupled from data: a 20 Hz `QTimer` repaints from cached values, so
the redraw cost does not scale with the position rate.

## Adding a panel

1. Write a `QWidget` subclass in `windows/`, with `on_panel_show()` /
   `on_panel_hide()` if it owns any background work.
2. Register it in `UltraGPSMainWindow._init_panels()` under a short name.
3. Add a button to `MainMenuPanel._build()` that calls `show_panel(<name>)`.

Panels receive the shared `SettingsModule`, `UltraGPSClient`, position library
and calibration objects from the main window — construct nothing that already
exists there.

## Adding a library

1. Create `libs/ultragps_<name>/` with a `pyproject.toml` and the package inside.
2. Add `-e libs/ultragps_<name>` to `requirements.txt`.
3. Add the import to the sanity check at the end of `install.sh`.
4. Add the path to `.vscode/settings.json` so the editor resolves it.
5. Add the source root to `[tool.setuptools.packages.find] where` in the root
   `pyproject.toml`, or the Debian package will not ship it.

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
(`python3-pyqt6`, `python3-matplotlib`, `python3-numpy`, `python3-scipy`) rather
than by pip, which is why the package is `Architecture: all` and needs Ubuntu
24.04 / Debian 12 or newer.

`build.sh deb` stages the tree into `/tmp`, builds there, and copies the result
back into `./build/` — the working tree is never touched, and `debian/changelog`
is generated rather than committed.
