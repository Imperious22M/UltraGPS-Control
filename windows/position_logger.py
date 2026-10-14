"""Position data logger.

Writes every position sample (input ticks, calculated distances and output
positions) to a CSV file and an Excel spreadsheet inside a ``log`` folder.
Each logging session produces one pair of files named after the wall-clock
time it started; rows carry a monotonic timestamp in seconds since the
session began.
"""

import csv
import os
import time
from datetime import datetime

NUM_RECEIVERS = 6

HEADER = (
    ["timestamp_s", "mode"]
    + [f"ticks_{i + 1}" for i in range(NUM_RECEIVERS)]
    + [f"distance_{i + 1}_cm" for i in range(NUM_RECEIVERS)]
    + ["lm_x", "lm_y", "lm_success", "cep_x", "cep_y", "cep_success",
       "sane_receivers"]
)


def default_log_dir() -> str:
    """Return the ``log`` folder the logger writes to.

    Uses ``log`` beside the application when that location is writable (a
    source checkout), otherwise ``$XDG_DATA_HOME/ultragps-control/log`` (an
    installed package, where the application directory is read-only).
    """
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.access(app_dir, os.W_OK):
        return os.path.join(app_dir, "log")
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(data_home, "ultragps-control", "log")


def _padded(values, count: int = NUM_RECEIVERS) -> list:
    values = [] if values is None else [float(v) for v in values]
    values = values[:count]
    return values + [None] * (count - len(values))


class PositionLogger:
    """Logs position samples to ``<log_dir>/positions_<start time>.csv/.xlsx``.

    The CSV is written and flushed row by row so it survives a crash.  The
    spreadsheet is streamed through openpyxl's write-only mode and saved when
    the logger is closed; if openpyxl is not installed only the CSV is made.
    """

    def __init__(self, log_dir: str | None = None):
        self.log_dir = log_dir or default_log_dir()
        os.makedirs(self.log_dir, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(self.log_dir, f"positions_{stamp}")
        suffix = 1
        while os.path.exists(base + ".csv"):
            base = os.path.join(self.log_dir, f"positions_{stamp}_{suffix}")
            suffix += 1
        self.csv_path = base + ".csv"
        self.xlsx_path = base + ".xlsx"

        self._start = time.monotonic()
        self._last_timestamp = -1.0

        self._csv_file = open(self.csv_path, "w", newline="")
        self._csv = csv.writer(self._csv_file)
        self._csv.writerow(HEADER)
        self._csv_file.flush()

        self._workbook = None
        self._sheet = None
        try:
            from openpyxl import Workbook
            self._workbook = Workbook(write_only=True)
            self._sheet = self._workbook.create_sheet("positions")
            self._sheet.append(HEADER)
        except ImportError:
            print("openpyxl is not installed, logging to CSV only")
            self.xlsx_path = None

        print(f"Logging positions to {self.csv_path}")

    def log(self, ticks, result: dict, continuous: bool) -> None:
        """Append one sample: the raw ticks and the ``get_position_full`` result."""
        # time.monotonic() never goes backwards, but two samples can share a
        # clock tick; nudge the later one so timestamps strictly increase.
        timestamp = time.monotonic() - self._start
        if timestamp <= self._last_timestamp:
            timestamp = self._last_timestamp + 1e-6
        self._last_timestamp = timestamp

        lm_pos = result.get("lm_position")
        cep_pos = result.get("cep_position")
        row = (
            [round(timestamp, 6), "continuous" if continuous else "pulse"]
            + [None if t is None else int(t) for t in _padded(ticks)]
            + _padded(result.get("distances"))
            + _padded(lm_pos, 2)
            + [bool(result.get("lm_success", False))]
            + _padded(cep_pos, 2)
            + [bool(result.get("cep_success", False))]
            + [" ".join(str(i + 1) for i in result.get("sane_indices", []))]
        )

        self._csv.writerow(row)
        self._csv_file.flush()
        if self._sheet is not None:
            self._sheet.append(row)

    def close(self) -> None:
        """Close the CSV and save the spreadsheet."""
        if self._csv_file is not None:
            self._csv_file.close()
            self._csv_file = None
        if self._workbook is not None:
            try:
                self._workbook.save(self.xlsx_path)
                print(f"Saved position spreadsheet {self.xlsx_path}")
            except Exception as exc:
                print(f"Could not save {self.xlsx_path}: {exc}")
            self._workbook = None
            self._sheet = None
