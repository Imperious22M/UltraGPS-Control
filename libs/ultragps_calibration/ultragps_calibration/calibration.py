"""
UltraGPS Calibration Library — core module
==========================================
Standalone two-point histogram calibration library for the UltraGPS system.

Drives the UltraGPS hardware via an ``UltraGPSClient`` instance to collect
tick readings at two known calibration points, then computes per-receiver
linear calibration offsets (slope + intercept) and writes them back to
``config.toml``.

The calibration formula for each receiver is:

.. code-block:: text

    slope     = (dist_1 - dist_2) / (tick_1 - tick_2)
    intercept = dist_1 - slope * tick_1

where ``tick_1`` / ``tick_2`` are the modal tick values from run 1 / run 2,
and ``dist_1`` / ``dist_2`` are the pre-computed Euclidean distances from that
receiver to calibration point 1 and 2 respectively (stored in ``cal_distances``
in ``config.toml``).  The resulting parameters map raw ticks to centimetres via:

.. code-block:: text

    distance_cm = tick * slope + intercept

Two usage modes
---------------

**Step-by-step** (designed for GUI integration):

    from ultragps_calibration import UltraGPSCalibration
    from ultragps_client import UltraGPSClient

    client = UltraGPSClient(host="192.168.1.100")
    client.connect()

    cal = UltraGPSCalibration("path/to/config.toml", client)

    # GUI shows run-1 dialog, then:
    cal.clear_run_data(1)
    cal.start_run(1, min_reads=5,
                  on_reading=lambda run, n: gui_update_histograms(run),
                  on_complete=lambda run: gui_handle_run_complete(run))

    # When run 1 finishes, GUI shows run-2 dialog, then:
    cal.clear_run_data(2)
    cal.start_run(2, min_reads=5,
                  on_reading=lambda run, n: gui_update_histograms(run),
                  on_complete=lambda run: gui_handle_run_complete(run))

    # When run 2 finishes:
    offsets = cal.calculate_and_save_offsets()

    # Query histogram data at any time for display:
    data = cal.get_histogram_data(receiver_id=0, run_num=1)
    modal_tick = cal.get_most_frequent(receiver_id=0, run_num=1)

**Full automated** (headless / scripted use):

    offsets = cal.run_full_calibration(
        min_reads=5,
        on_between_runs=lambda: input("Move transmitter to point 2, then press Enter") or True,
        on_reading=lambda run, n: print(f"Run {run}: {n} reads collected"),
    )

.. note::

    **GUI callers**: ``on_reading`` and ``on_complete`` are invoked from the
    background collection thread, **not** the GUI main thread.  Schedule any
    GUI updates through your framework's thread-safe mechanism — for Tkinter
    use ``root.after(0, callback)`` inside the callback.
"""

from __future__ import annotations

import os
import threading
import tomllib
from collections.abc import Callable
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_config(config_path: str) -> dict:
    """Load and return config.toml as a Python dict.

    Args:
        config_path: Absolute or relative path to ``config.toml``.

    Raises:
        FileNotFoundError: If the file does not exist.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
    """
    path = os.path.abspath(config_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _write_config(config_path: str, config: dict) -> None:
    """Write a config dict back to config.toml using the project's TOML layout.

    Preserves all fields; only the values in the dict are updated on disk.

    Args:
        config_path: Absolute or relative path to ``config.toml``.
        config:      Full configuration dict (as returned by :func:`_load_config`
                     and modified in-place).
    """
    path = os.path.abspath(config_path)
    with open(path, "w") as f:
        # Top-level scalar values
        for key in [
            "valid_settings", "cal_state", "calibration_reads",
            "number_of_receivers", "serial_port", "units",
        ]:
            if key in config:
                value = config[key]
                if isinstance(value, str):
                    f.write(f"{key} = '{value}'\n")
                elif isinstance(value, bool):
                    f.write(f"{key} = {str(value).lower()}\n")
                else:
                    f.write(f"{key} = {value}\n")

        # Calibration point arrays
        for key in ["cal_point_1", "cal_point_2"]:
            if key in config:
                f.write(f"{key} = {config[key]}\n")

        f.write("\n")

        # Receivers array
        for receiver in config.get("receivers", []):
            f.write("[[receivers]]\n")
            if "cal_distances" in receiver:
                f.write(f"cal_distances = {receiver['cal_distances']}\n")
            if "id" in receiver:
                f.write(f"id = {receiver['id']}\n")
            if "position" in receiver:
                f.write(f"position = {receiver['position']}\n")
            f.write("\n")
            if "offset" in receiver:
                f.write("    [receivers.offset]\n")
                f.write(f"    intercept = {receiver['offset']['intercept']}\n")
                f.write(f"    slope = {receiver['offset']['slope']}\n")
            f.write("\n")


# ─────────────────────────────────────────────────────────────────────────────
# Main library class
# ─────────────────────────────────────────────────────────────────────────────

class UltraGPSCalibration:
    """Standalone two-point histogram calibration library for the UltraGPS system.

    Manages tick-count histograms across two calibration runs, computes
    per-receiver linear offsets, and writes them back to ``config.toml``.

    Each instance represents one calibration session.  Clear histogram data
    between sessions with :meth:`clear_run_data`.

    Args:
        config_path: Path to the UltraGPS ``config.toml`` file.  Used both to
                     read calibration geometry (``cal_distances``,
                     ``cal_point_*``, ``calibration_reads``) and to write the
                     computed offsets back to disk.
        client:      A **connected** ``UltraGPSClient`` instance.  The library
                     calls ``client.pulse()`` to collect tick readings from the
                     hardware.
    """

    RECEIVER_COUNT: int = 6
    """Number of receivers expected per pulse reading."""

    HISTOGRAM_UPDATE_INTERVAL: int = 5
    """How many reads between successive ``on_reading`` callback invocations."""

    def __init__(self, config_path: str, client) -> None:
        self._config_path: str = os.path.abspath(config_path)
        self._client = client

        # Histogram store: run_num → receiver_id → {tick_value: count}
        self._histogram: dict[int, dict[int, dict[float, int]]] = {
            1: {i: {} for i in range(self.RECEIVER_COUNT)},
            2: {i: {} for i in range(self.RECEIVER_COUNT)},
        }

        self._running: bool = False
        self._current_run: int = 0
        self._lock: threading.Lock = threading.Lock()
        self._cal_thread: Optional[threading.Thread] = None

    # ─── Histogram data management ────────────────────────────────────────

    def clear_run_data(self, run_num: int) -> None:
        """Clear all collected histogram data for a run.

        Call this before starting a new data-collection run to discard any
        readings left over from a previous session.

        Args:
            run_num: 1 or 2 — which run's data to clear.
        """
        if run_num not in (1, 2):
            raise ValueError(f"run_num must be 1 or 2, got {run_num}")
        with self._lock:
            self._histogram[run_num] = {i: {} for i in range(self.RECEIVER_COUNT)}

    def add_reading(self, receiver_id: int, tick_value: float, run_num: int) -> None:
        """Record one tick reading into the histogram for a receiver and run.

        The collection thread calls this automatically during :meth:`start_run`.
        Callers may also call it directly when managing data collection outside
        this library.

        Args:
            receiver_id: Receiver index (0–5).
            tick_value:  Tick count from :meth:`UltraGPSClient.pulse`.
            run_num:     1 or 2 — which calibration run this reading belongs to.
        """
        with self._lock:
            bucket = self._histogram[run_num][receiver_id]
            bucket[tick_value] = bucket.get(tick_value, 0) + 1

    def get_most_frequent(self, receiver_id: int, run_num: int) -> Optional[float]:
        """Return the modal tick value for a receiver in a given run.

        This is the value used as ``tick_1`` / ``tick_2`` in the calibration
        formula when :meth:`calculate_offsets` is called.

        Args:
            receiver_id: Receiver index (0–5).
            run_num:     1 or 2.

        Returns:
            The most-frequent tick value, or ``None`` if no data has been
            collected.
        """
        with self._lock:
            data = dict(self._histogram[run_num][receiver_id])
        if not data:
            return None
        return max(data, key=data.__getitem__)

    def get_max_count(self, receiver_id: int, run_num: int) -> int:
        """Return the highest count seen for any single tick value.

        Used internally to check whether a receiver has met the ``min_reads``
        threshold.  Also useful for GUI progress indicators.

        Args:
            receiver_id: Receiver index (0–5).
            run_num:     1 or 2.

        Returns:
            Maximum count across all tick values, or 0 if no data exists.
        """
        with self._lock:
            data = dict(self._histogram[run_num][receiver_id])
        return max(data.values(), default=0)

    def get_histogram_data(self, receiver_id: int, run_num: int) -> dict[float, int]:
        """Return a snapshot of the full histogram for a receiver and run.

        Returns a copy so the caller can safely iterate without holding the
        internal lock.

        Args:
            receiver_id: Receiver index (0–5).
            run_num:     1 or 2.

        Returns:
            ``{tick_value: count}`` dict.
        """
        with self._lock:
            return dict(self._histogram[run_num][receiver_id])

    # ─── Config access ────────────────────────────────────────────────────

    @property
    def cal_point_1(self) -> list[float]:
        """Calibration point 1 coordinates ``[x, y]`` (cm), from config.toml."""
        return list(_load_config(self._config_path).get("cal_point_1", [0.0, 0.0]))

    @property
    def cal_point_2(self) -> list[float]:
        """Calibration point 2 coordinates ``[x, y]`` (cm), from config.toml."""
        return list(_load_config(self._config_path).get("cal_point_2", [0.0, 0.0]))

    @property
    def calibration_reads(self) -> int:
        """Default minimum reads per receiver, from ``calibration_reads`` in config.toml."""
        return int(_load_config(self._config_path).get("calibration_reads", 5))

    def get_cal_distances(self, receiver_id: int) -> Optional[list[float]]:
        """Return the two known calibration distances for a receiver.

        These are the pre-computed Euclidean distances from the receiver to
        calibration point 1 and calibration point 2 respectively, stored in
        config.toml as ``cal_distances``.

        Args:
            receiver_id: Receiver index (0–5).

        Returns:
            ``[dist_to_point_1, dist_to_point_2]``, or ``None`` if the
            receiver is not found in the config.
        """
        config = _load_config(self._config_path)
        for r in config.get("receivers", []):
            if r.get("id") == receiver_id:
                return list(r.get("cal_distances", []))
        return None

    # ─── Step-by-step API (for GUI integration) ───────────────────────────

    def start_run(
        self,
        run_num: int,
        min_reads: int,
        on_reading: Optional[Callable[[int, int], None]] = None,
        on_complete: Optional[Callable[[int], None]] = None,
        on_error: Optional[Callable[[int, str], None]] = None,
        max_consecutive_failures: int = 10,
    ) -> None:
        """Start a calibration data-collection run in a background thread.

        The thread calls :meth:`UltraGPSClient.pulse` repeatedly, accumulates
        tick counts via :meth:`add_reading`, and stops once every receiver has
        at least *min_reads* counts on its modal tick value.

        Args:
            run_num:     1 or 2 — which calibration run to collect.
            min_reads:   Minimum number of modal-value hits per receiver before
                         the run completes.
            on_reading:  Optional callback invoked every
                         :attr:`HISTOGRAM_UPDATE_INTERVAL` reads, plus once
                         more after the final read.  Signature::

                             on_reading(run_num: int, read_count: int) -> None

                         Designed for triggering histogram redraws in a GUI.

                         .. note::
                             Called from the background thread — schedule GUI
                             updates via ``root.after(0, ...)`` in Tkinter.
            on_complete: Optional callback invoked once when the run finishes
                         naturally (i.e. not when stopped via
                         :meth:`stop_run`).  Signature::

                             on_complete(run_num: int) -> None

                         .. note::
                             Called from the background thread — schedule GUI
                             updates via ``root.after(0, ...)`` in Tkinter.

        Raises:
            ValueError:  If *run_num* is not 1 or 2.
            RuntimeError: If a run is already in progress.
        """
        if run_num not in (1, 2):
            raise ValueError(f"run_num must be 1 or 2, got {run_num}")

        with self._lock:
            if self._running:
                raise RuntimeError("A calibration run is already in progress")
            self._running = True
            self._current_run = run_num

        self._cal_thread = threading.Thread(
            target=self._collection_loop,
            args=(run_num, min_reads, on_reading, on_complete, on_error, max_consecutive_failures),
            daemon=True,
            name=f"ultragps_cal_run_{run_num}",
        )
        self._cal_thread.start()

    def stop_run(self) -> None:
        """Request the current collection run to stop early.

        The background thread will finish its current pulse call and exit
        cleanly.  The ``on_complete`` callback is **not** called when a run
        is stopped this way.
        """
        with self._lock:
            self._running = False

    # ─── Offset calculation and saving ────────────────────────────────────

    def calculate_offsets(self) -> dict[int, dict[str, float]]:
        """Calculate per-receiver slope and intercept from collected histogram data.

        For each receiver, the modal tick value from run 1 and run 2 is paired
        with the pre-stored known distances to derive the linear map:

        .. code-block:: text

            slope     = (dist_1 - dist_2) / (tick_1 - tick_2)
            intercept = dist_1 - slope * tick_1

        Receivers with missing data or identical tick values for both runs are
        skipped with a printed warning.

        Returns:
            ``{receiver_id: {'slope': float, 'intercept': float}}`` for every
            receiver that produced valid results.
        """
        offsets: dict[int, dict[str, float]] = {}
        config = _load_config(self._config_path)

        for recv_id in range(self.RECEIVER_COUNT):
            # Look up known distances from config
            cal_distances: Optional[list[float]] = None
            for r in config.get("receivers", []):
                if r.get("id") == recv_id:
                    cal_distances = list(r.get("cal_distances", []))
                    break

            if not cal_distances or len(cal_distances) < 2:
                print(
                    f"[calibration] Receiver {recv_id + 1}: "
                    "cal_distances missing or incomplete — skipped"
                )
                continue

            known_dist_1, known_dist_2 = cal_distances[0], cal_distances[1]

            tick_1 = self.get_most_frequent(recv_id, 1)
            tick_2 = self.get_most_frequent(recv_id, 2)

            if tick_1 is None or tick_2 is None:
                print(
                    f"[calibration] Receiver {recv_id + 1}: "
                    "no histogram data for one or both runs — skipped"
                )
                continue

            if tick_1 == tick_2:
                print(
                    f"[calibration] Receiver {recv_id + 1}: "
                    f"identical tick values for both runs ({tick_1}) — skipped"
                )
                continue

            slope = (known_dist_1 - known_dist_2) / (tick_1 - tick_2)
            intercept = known_dist_1 - slope * tick_1

            offsets[recv_id] = {"slope": slope, "intercept": intercept}
            print(
                f"[calibration] Receiver {recv_id + 1}: "
                f"slope={slope:.4f}, intercept={intercept:.4f}"
            )

        return offsets

    def save_offsets(self, offsets: dict[int, dict[str, float]]) -> None:
        """Write calibration offsets to config.toml.

        Reads the current config, updates only the ``[receivers.offset]`` block
        for each receiver present in *offsets*, and writes the full config back
        to disk.  All other config values are preserved.

        Args:
            offsets: ``{receiver_id: {'slope': float, 'intercept': float}}``
                     as returned by :meth:`calculate_offsets`.
        """
        config = _load_config(self._config_path)
        for r in config.get("receivers", []):
            recv_id = r.get("id")
            if recv_id in offsets:
                if "offset" not in r:
                    r["offset"] = {}
                r["offset"]["slope"]     = float(offsets[recv_id]["slope"])
                r["offset"]["intercept"] = float(offsets[recv_id]["intercept"])
        _write_config(self._config_path, config)

    def calculate_and_save_offsets(self) -> dict[int, dict[str, float]]:
        """Calculate offsets from collected data and write them to config.toml.

        Convenience wrapper around :meth:`calculate_offsets` +
        :meth:`save_offsets`.

        Returns:
            The calculated ``{receiver_id: {'slope': float, 'intercept': float}}``
            dict (same as :meth:`calculate_offsets`).
        """
        offsets = self.calculate_offsets()
        self.save_offsets(offsets)
        return offsets

    # ─── Full automated calibration ───────────────────────────────────────

    def run_full_calibration(
        self,
        min_reads: Optional[int] = None,
        on_between_runs: Optional[Callable[[], bool]] = None,
        on_reading: Optional[Callable[[int, int], None]] = None,
    ) -> dict[int, dict[str, float]]:
        """Run the complete two-point calibration sequence synchronously.

        Collects data for run 1, optionally waits for the caller to confirm
        the transmitter has been moved, then collects run 2.  Calculates and
        saves offsets when both runs are complete.

        This method blocks until calibration is finished or aborted.  For
        GUI-driven flows where each run is started in response to a user
        action use :meth:`start_run` directly instead.

        Args:
            min_reads:       Minimum reads per receiver per run.  Defaults to
                             the value of ``calibration_reads`` in config.toml.
            on_between_runs: Called synchronously on the calling thread after
                             run 1 completes.  Should return ``True`` to
                             proceed to run 2, or ``False`` to abort.  If
                             ``None`` the library proceeds automatically.

                             Typical usage::

                                 def prompt():
                                     ans = input("Move transmitter to point 2. Ready? [y/n] ")
                                     return ans.strip().lower() == "y"

                                 cal.run_full_calibration(on_between_runs=prompt)

            on_reading:      Forwarded to :meth:`start_run` for both runs.
                             Invoked every :attr:`HISTOGRAM_UPDATE_INTERVAL`
                             reads from the background thread.

                             Signature: ``on_reading(run_num: int, read_count: int) -> None``

        Returns:
            Calculated and saved offsets ``{receiver_id: {'slope': float,
            'intercept': float}}``, or an empty dict if calibration was
            aborted via *on_between_runs*.
        """
        if min_reads is None:
            min_reads = self.calibration_reads

        # ── Run 1 ─────────────────────────────────────────────────────────
        self.clear_run_data(1)
        run1_done = threading.Event()

        def _on_complete_1(run_num: int) -> None:
            run1_done.set()

        self.start_run(1, min_reads, on_reading=on_reading, on_complete=_on_complete_1)
        run1_done.wait()

        # ── Between-runs gate ─────────────────────────────────────────────
        if on_between_runs is not None:
            if not on_between_runs():
                return {}

        # ── Run 2 ─────────────────────────────────────────────────────────
        self.clear_run_data(2)
        run2_done = threading.Event()

        def _on_complete_2(run_num: int) -> None:
            run2_done.set()

        self.start_run(2, min_reads, on_reading=on_reading, on_complete=_on_complete_2)
        run2_done.wait()

        # ── Calculate and persist offsets ─────────────────────────────────
        return self.calculate_and_save_offsets()

    # ─── Properties ───────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """``True`` while a data-collection run is active in the background."""
        with self._lock:
            return self._running

    @property
    def current_run(self) -> int:
        """Run number (1 or 2) currently being collected, or 0 if idle."""
        with self._lock:
            return self._current_run

    # ─── Internal ─────────────────────────────────────────────────────────

    def _collection_loop(
        self,
        run_num: int,
        min_reads: int,
        on_reading: Optional[Callable[[int, int], None]],
        on_complete: Optional[Callable[[int], None]],
        on_error: Optional[Callable[[int, str], None]],
        max_consecutive_failures: int,
    ) -> None:
        """Background thread: collect pulse readings until the threshold is met.

        Issues one discard pulse first to flush any stale network buffer, then
        loops calling ``client.pulse()`` until every receiver has at least
        *min_reads* counts on its modal tick value.
        """
        # Discard one reading to flush the network buffer (mirrors GraphicsModule)
        try:
            self._client.pulse()
        except Exception as exc:
            print(f"[calibration] Warning: flush pulse failed: {exc}")

        read_count = 0
        consecutive_failures = 0
        completed_naturally = False

        while True:
            with self._lock:
                still_running = self._running
            if not still_running:
                break  # Externally stopped via stop_run() — skip on_complete

            try:
                ticks = self._client.pulse()
            except Exception as exc:
                print(f"[calibration] Error during pulse: {exc}")
                if on_error is not None:
                    try:
                        on_error(run_num, str(exc))
                    except Exception:
                        pass
                break

            if ticks is not None and len(ticks) == self.RECEIVER_COUNT:
                consecutive_failures = 0
                for recv_id in range(self.RECEIVER_COUNT):
                    self.add_reading(recv_id, float(ticks[recv_id]), run_num)
                read_count += 1
            else:
                consecutive_failures += 1
                if consecutive_failures >= max_consecutive_failures:
                    print(
                        f"[calibration] Run {run_num}: {max_consecutive_failures} "
                        "consecutive failures — aborting"
                    )
                    if on_error is not None:
                        try:
                            on_error(
                                run_num,
                                f"Server not responding ({max_consecutive_failures} consecutive timeouts)",
                            )
                        except Exception:
                            pass
                    break

            # Periodic reading callback
            if read_count > 0 and read_count % self.HISTOGRAM_UPDATE_INTERVAL == 0:
                if on_reading is not None:
                    try:
                        on_reading(run_num, read_count)
                    except Exception as exc:
                        print(f"[calibration] on_reading callback error: {exc}")

            # Check whether all receivers have met the threshold
            all_met = all(
                self.get_max_count(i, run_num) >= min_reads
                for i in range(self.RECEIVER_COUNT)
            )
            if all_met:
                completed_naturally = True
                break

        # Final reading callback to flush any unseen reads
        if completed_naturally and on_reading is not None and read_count > 0:
            try:
                on_reading(run_num, read_count)
            except Exception as exc:
                print(f"[calibration] on_reading callback error: {exc}")

        with self._lock:
            self._running = False
            self._current_run = 0

        if completed_naturally and on_complete is not None:
            try:
                on_complete(run_num)
            except Exception as exc:
                print(f"[calibration] on_complete callback error: {exc}")
