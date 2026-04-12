"""
UltraGPS Position Library — core module
========================================
Standalone 2D-position computation library for the UltraGPS system.

Parses the distance-tick messages produced by the UltraGPS-Ground server,
applies per-receiver calibration from a config.toml file, and solves for
transmitter position using two methods:

  • OLS-seeded Levenberg-Marquardt multilateration  (``get_position``)
  • CEP-based receiver-subset selection              (``get_position_cep``)

Quick-start
-----------
    from ultragps_position import UltraGPSPositionLib

    lib = UltraGPSPositionLib("path/to/config.toml")

    # Both methods take a plain list/array of integer tick values — one per
    # receiver in id order.  Use parse_message() to convert a raw server
    # string (TCP or UDP) into the required tick list.

    ticks = UltraGPSPositionLib.parse_message("N: 1234, 2345, 3456, 4567, 5678, 6789")
    result = lib.get_position(ticks)
    if result["success"]:
        x, y = result["position"]
        print(f"Position: ({x:.1f}, {y:.1f}) cm")

    # CEP subset selection — more robust, slightly slower
    result = lib.get_position_cep(ticks)
    if result["success"]:
        x, y = result["position"]
        print(f"CEP position: ({x:.1f}, {y:.1f}) cm  CEP radius: {result['cep']:.1f} cm")

Message format reference
------------------------
Raw server messages carry a two-character marker prefix followed by
comma-space-separated integer tick counts, one per receiver in id order:

    "N: <tick0>, <tick1>, <tick2>, <tick3>, <tick4>, <tick5>"   (TCP poll)
    "C: <tick0>, <tick1>, ..."                                   (UDP stream)

Use ``UltraGPSPositionLib.parse_message(raw)`` to convert either format into a
``list[int]`` suitable for ``get_position`` / ``get_position_cep``.

Each tick is converted to centimetres inside the library via:

    distance_i = tick_i * slope_i + intercept_i
"""

from __future__ import annotations

import os
import tomllib
from itertools import combinations
from typing import Optional

import numpy as np
from scipy.optimize import least_squares


# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_config(config_path: str) -> dict:
    """Load and return the UltraGPS config.toml as a Python dict.

    Args:
        config_path: Absolute or relative path to config.toml.

    Returns:
        Parsed TOML dictionary.

    Raises:
        FileNotFoundError: If the file does not exist.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
    """
    path = os.path.abspath(config_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "rb") as fh:
        return tomllib.load(fh)


# ─────────────────────────────────────────────────────────────────────────────
# Main library class
# ─────────────────────────────────────────────────────────────────────────────

class UltraGPSPositionLib:
    """Standalone 2D-position computation library for the UltraGPS system.

    All positioning state (differential filter history, last-good position
    fallback) is stored on the instance, so each ``UltraGPSPositionLib`` object
    represents one independent tracking session.  Call ``reset_state()`` to
    start fresh without recreating the object.

    Args:
        config_path:      Path to the config.toml file that contains receiver
                          positions and per-receiver calibration offsets.
        max_differential: Maximum allowed jump in distance (cm) between two
                          consecutive readings before a receiver is flagged as
                          noisy.  Default: 30.
    """

    def __init__(self, config_path: str, max_differential: float = 30.0):
        self._config_path: str = os.path.abspath(config_path)
        self.max_differential: float = max_differential

        # ── Geometry / calibration (populated by _init_from_config) ──────────
        self._receiver_count: int = 0
        self._receiver_coords: np.ndarray = np.empty((0, 2))
        self._offsets: dict[int, dict] = {}
        self._max_receiver_dist: float = 0.0
        self.units: str = "cm"

        self._init_from_config()

        # ── Differential-filter state ────────────────────────────────────────
        self._last_distances:     Optional[np.ndarray] = None
        self._last_sane_indices:  Optional[np.ndarray] = None

        # ── OLS+LM last-good fallback ─────────────────────────────────────────
        self._lm_last_good_pos: Optional[np.ndarray] = None

        # ── CEP last-good fallback ────────────────────────────────────────────
        self._cep_last_good_pos:     Optional[np.ndarray] = None
        self._cep_last_good_cep:     float                = float("inf")
        self._cep_last_good_indices: Optional[list]       = None
        self._cep_last_good_cov:     Optional[np.ndarray] = None

    def _init_from_config(self) -> None:
        """(Re-)load receiver geometry and calibration from the config file."""
        config = load_config(self._config_path)

        # Sort receivers by id so index == id throughout
        receivers = sorted(config["receivers"], key=lambda r: r["id"])
        self._receiver_count = len(receivers)

        # (N, 2) float array  —  receiver positions in cm
        self._receiver_coords = np.array(
            [r["position"] for r in receivers], dtype=float
        )

        # Per-receiver calibration parameters  {id: {slope, intercept}}
        self._offsets = {}
        for r in receivers:
            rid = int(r["id"])
            off = r.get("offset", {})
            self._offsets[rid] = {
                "slope":     float(off.get("slope",     1.0)),
                "intercept": float(off.get("intercept", 0.0)),
            }

        # Maximum pairwise receiver distance — used as arena-bounds gate
        self._max_receiver_dist = self._calc_max_receiver_dist()
        self.units = str(config.get("units", "cm"))

    # ─── Public API ───────────────────────────────────────────────────────────

    def get_position(self, ticks: list | np.ndarray) -> dict:
        """Compute 2D position from a tick array using OLS-seeded
        Levenberg-Marquardt multilateration.

        The full pipeline is:
            1. Convert tick counts to distances via calibration offsets.
            2. Filter receivers by arena-bounds and differential.
            3. Compute OLS linear estimate.
            4. Refine with Levenberg-Marquardt non-linear least squares.

        If fewer than 3 receivers pass the sanity filter the last successful
        position is returned with ``success=False``.

        Args:
            ticks: Integer (or float) tick values from the UltraGPS receivers,
                one per receiver in id order.  Use ``parse_message()`` to
                convert a raw server string into this array.

        Returns:
            dict with keys:

            ``'position'``
                ``np.ndarray([x, y])`` in cm, or ``None`` if no good position
                has ever been computed.
            ``'distances'``
                ``tuple[float]`` — per-receiver calibrated distances (cm).
            ``'sane_indices'``
                ``list[int]`` — receiver ids that passed the sanity filter.
            ``'residual_rms'``
                ``float`` — RMS of solver residuals (cm), or ``None``.
            ``'success'``
                ``bool`` — ``True`` when the solver converged with ≥ 3 sane
                receivers.
        """
        distances = self._ticks_to_distances(ticks)
        sane_idx  = self._filter_receivers(distances)

        if len(sane_idx) < 3:
            return {
                "position":     self._lm_last_good_pos,
                "distances":    distances,
                "sane_indices": list(sane_idx),
                "residual_rms": None,
                "success":      False,
            }

        position, result = self._multilaterate(distances, sane_idx)

        if result is not None and result.success:
            self._lm_last_good_pos = position.copy()

        rms = (
            float(np.sqrt(np.mean(result.fun ** 2)))
            if result is not None
            else None
        )

        return {
            "position":     position,
            "distances":    distances,
            "sane_indices": list(sane_idx),
            "residual_rms": rms,
            "success":      result is not None and result.success,
        }

    def get_position_cep(
        self,
        ticks: list | np.ndarray,
        max_subsets: int = 15,
    ) -> dict:
        """Compute 2D position from a tick array using CEP-based
        receiver-subset selection.

        Tries up to *max_subsets* combinations of the sane receivers and
        returns the one with the lowest Circular Error Probable (CEP).
        The resulting position is validated against the receiver bounding box;
        out-of-bounds results fall back to the last valid CEP position.

        Args:
            ticks: Integer (or float) tick values from the UltraGPS receivers,
                one per receiver in id order.  Use ``parse_message()`` to
                convert a raw server string into this array.
            max_subsets: Maximum number of receiver subsets to evaluate.
                         More subsets improve robustness at the cost of
                         latency.  Default: 15.

        Returns:
            dict with keys:

            ``'position'``
                ``np.ndarray([x, y])`` in cm, or ``None``.
            ``'distances'``
                ``tuple[float]`` — per-receiver calibrated distances (cm).
            ``'sane_indices'``
                ``list[int]`` — receiver ids that passed the sanity filter.
            ``'best_indices'``
                ``list[int]`` — the winning receiver subset.
            ``'cep'``
                ``float`` — CEP radius (cm) of the winning subset.
            ``'cov'``
                ``np.ndarray`` (2×2) — position covariance matrix, or ``None``.
            ``'success'``
                ``bool`` — ``True`` when a valid in-bounds position was found.
        """
        distances = self._ticks_to_distances(ticks)
        sane_idx  = self._filter_receivers(distances)

        if len(sane_idx) < 3:
            return {
                "position":     self._cep_last_good_pos,
                "distances":    distances,
                "sane_indices": list(sane_idx),
                "best_indices": self._cep_last_good_indices,
                "cep":          self._cep_last_good_cep,
                "cov":          self._cep_last_good_cov,
                "success":      False,
            }

        best_pos, best_cep, best_idx, best_cov = self._find_best_subset(
            distances, list(sane_idx), max_subsets
        )

        best_pos, invalid = self._validate_position(best_pos)

        if not invalid and best_pos is not None:
            self._cep_last_good_pos     = best_pos.copy()
            self._cep_last_good_cep     = best_cep
            self._cep_last_good_indices = best_idx
            self._cep_last_good_cov     = best_cov

        return {
            "position":     best_pos,
            "distances":    distances,
            "sane_indices": list(sane_idx),
            "best_indices": best_idx,
            "cep":          best_cep,
            "cov":          best_cov,
            "success":      not invalid and best_pos is not None,
        }

    def reset_state(self) -> None:
        """Clear all history so the next call starts fresh.

        Resets the differential filter, last-good OLS+LM position, and all
        CEP fallback state.  The receiver geometry and calibration offsets
        loaded from config.toml are preserved.
        """
        self._last_distances        = None
        self._last_sane_indices     = None
        self._lm_last_good_pos      = None
        self._cep_last_good_pos     = None
        self._cep_last_good_cep     = float("inf")
        self._cep_last_good_indices = None
        self._cep_last_good_cov     = None

    def reload(self, config_path: str | None = None) -> None:
        """Reload receiver geometry and calibration from config, then reset state.

        Call this whenever the arena map / calibration data changes on disk so
        that subsequent ``get_position*`` calls use the updated settings.

        Args:
            config_path: Path to the config.toml to load.  If ``None``,
                         reloads from the path given at construction time.
        """
        if config_path is not None:
            self._config_path = os.path.abspath(config_path)
        self._init_from_config()
        self.reset_state()

    def get_position_full(
        self,
        ticks: list | np.ndarray,
        max_subsets: int = 15,
    ) -> dict:
        """Compute both OLS+LM and CEP positions in a single filter pass.

        Runs the receiver sanity filter exactly once so the differential
        history is updated consistently, then computes both the
        Levenberg-Marquardt position and the CEP best-subset position from
        the same filtered set.

        Args:
            ticks:       Integer tick values, one per receiver in id order.
            max_subsets: Maximum receiver subsets to try for CEP.

        Returns:
            dict with keys:

            ``'lm_position'``   — ``np.ndarray([x, y])`` or ``None``
            ``'lm_rms'``        — float RMS residual (cm) or ``None``
            ``'lm_success'``    — bool
            ``'cep_position'``  — ``np.ndarray([x, y])`` or ``None``
            ``'cep'``           — float CEP radius (cm)
            ``'best_indices'``  — list[int] winning receiver subset
            ``'cov'``           — 2×2 covariance array or ``None``
            ``'cep_success'``   — bool (in-bounds valid solution found)
            ``'distances'``     — tuple of calibrated distances (cm)
            ``'sane_indices'``  — list[int] receivers that passed the filter
        """
        distances = self._ticks_to_distances(ticks)
        sane_idx  = self._filter_receivers(distances)

        # ── OLS + Levenberg-Marquardt ─────────────────────────────────────────
        lm_pos     = self._lm_last_good_pos
        lm_rms     = None
        lm_success = False

        if len(sane_idx) >= 3:
            position, result = self._multilaterate(distances, sane_idx)
            lm_rms     = float(np.sqrt(np.mean(result.fun ** 2))) if result is not None else None
            lm_success = result is not None and result.success
            if lm_success:
                self._lm_last_good_pos = position.copy()
                lm_pos = position
            else:
                lm_pos = position  # still use even if not converged

        # ── CEP subset selection ──────────────────────────────────────────────
        cep_pos     = self._cep_last_good_pos
        cep_val     = self._cep_last_good_cep
        cep_idx     = self._cep_last_good_indices
        cep_cov     = self._cep_last_good_cov
        cep_success = False

        if len(sane_idx) >= 3:
            best_pos, best_cep, best_idx, best_cov = self._find_best_subset(
                distances, list(sane_idx), max_subsets
            )
            best_pos, invalid = self._validate_position(best_pos)

            if not invalid and best_pos is not None:
                self._cep_last_good_pos     = best_pos.copy()
                self._cep_last_good_cep     = best_cep
                self._cep_last_good_indices = best_idx
                self._cep_last_good_cov     = best_cov

            cep_pos     = best_pos
            cep_val     = best_cep
            cep_idx     = best_idx
            cep_cov     = best_cov
            cep_success = not invalid and best_pos is not None

        return {
            "lm_position":  lm_pos,
            "lm_rms":       lm_rms,
            "lm_success":   lm_success,
            "cep_position": cep_pos,
            "cep":          cep_val,
            "best_indices": cep_idx,
            "cov":          cep_cov,
            "cep_success":  cep_success,
            "distances":    distances,
            "sane_indices": list(sane_idx),
        }

    # ─── Properties ──────────────────────────────────────────────────────────

    @property
    def receiver_count(self) -> int:
        """Number of receivers loaded from config."""
        return self._receiver_count

    @property
    def receiver_coords(self) -> np.ndarray:
        """Copy of the (N, 2) receiver-position array (cm)."""
        return self._receiver_coords.copy()

    # ─── Message parsing (static utilities) ──────────────────────────────────

    @staticmethod
    def parse_message(raw: str) -> list[int]:
        """Convert a raw UltraGPS server string into a list of integer ticks.

        Strips the two-character protocol prefix (``"N: "`` for TCP poll
        responses, ``"C: "`` for UDP continuous-stream messages) then splits
        on ``", "`` and casts each value to ``int``.

        Args:
            raw: Raw string straight off the wire, e.g.
                 ``"N: 1234, 2345, 3456, 4567, 5678, 6789"``  (TCP)
                 ``"C: 1234, 2345, ..."``                      (UDP)

        Returns:
            ``list[int]`` of tick values in receiver-id order, or ``[]`` if
            the string cannot be parsed.
        """
        s = raw.strip()
        if len(s) >= 3 and s[0].isupper() and s[1] == ":" and s[2] == " ":
            s = s[3:]
        try:
            return [int(float(v)) for v in s.split(", ")]
        except ValueError:
            return []

    # ─── Calibration / distance conversion ───────────────────────────────────

    def _ticks_to_distances(self, ticks: list | np.ndarray) -> tuple:
        """Convert an array of integer tick values to calibrated distances (cm).

        Formula per receiver:  ``distance = tick * slope + intercept``

        Args:
            ticks: Sequence of numeric tick values, one per receiver in id
                   order.  May be a ``list``, ``tuple``, or ``np.ndarray``.

        Returns:
            ``tuple[float]`` of length ``receiver_count``.  Filled with
            ``0.0`` if *ticks* is shorter than expected.
        """
        try:
            tick_arr = [float(v) for v in ticks]
        except (TypeError, ValueError):
            return tuple(0.0 for _ in range(self._receiver_count))

        if len(tick_arr) < self._receiver_count:
            return tuple(0.0 for _ in range(self._receiver_count))

        distances = []
        for i in range(self._receiver_count):
            off = self._offsets.get(i, {"slope": 1.0, "intercept": 0.0})
            distances.append(tick_arr[i] * off["slope"] + off["intercept"])

        return tuple(distances)

    # ─── Receiver sanity filter ───────────────────────────────────────────────

    def _calc_max_receiver_dist(self) -> float:
        """Maximum pairwise Euclidean distance between any two receivers."""
        coords = self._receiver_coords
        n      = self._receiver_count
        max_d  = 0.0
        for i in range(n):
            for j in range(i + 1, n):
                d = float(np.linalg.norm(coords[i] - coords[j]))
                if d > max_d:
                    max_d = d
        return max_d

    def _filter_receivers(self, distances: tuple) -> np.ndarray:
        """Return indices of receivers that pass both sanity checks:

        1. **Bounds check** — reported distance ≤ maximum pairwise receiver
           distance (a reading larger than this cannot be physically valid
           within the arena).
        2. **Differential check** — absolute change from the previous reading
           is below ``max_differential`` (cm).  This check is skipped on the
           first call when no prior data exists.

        Updates ``_last_distances`` and ``_last_sane_indices`` in-place.
        """
        arr           = np.array(distances, dtype=float)
        within_bounds = arr <= self._max_receiver_dist

        if self._last_distances is None:
            self._last_distances    = arr.copy()
            sane                    = np.where(within_bounds)[0]
            self._last_sane_indices = sane
            return sane

        diff      = np.abs(arr - self._last_distances)
        sane_mask = within_bounds & (diff < self.max_differential)
        sane      = np.where(sane_mask)[0]

        self._last_distances    = arr.copy()
        self._last_sane_indices = sane
        return sane

    # ─── Multilateration core ─────────────────────────────────────────────────

    def _multilaterate(
        self,
        distances: tuple,
        indices: np.ndarray,
    ) -> tuple:
        """Run OLS initial estimate followed by Levenberg-Marquardt refinement."""
        d_arr   = np.array(distances, dtype=float)
        initial = self._ols(d_arr, indices)
        return self._nlls(d_arr, initial, indices)

    def _ols(self, distances: np.ndarray, indices: np.ndarray) -> np.ndarray:
        """Ordinary Least Squares linearisation of the multilateration system."""
        coords = self._receiver_coords[indices]
        dists  = distances[indices]

        x1, y1 = coords[0]
        d1_sq  = dists[0] ** 2

        A, b = [], []
        for i in range(1, len(dists)):
            xi, yi = coords[i]
            A.append([2.0 * (x1 - xi), 2.0 * (y1 - yi)])
            b.append(
                dists[i] ** 2 - d1_sq
                - (xi ** 2 + yi ** 2)
                + (x1 ** 2 + y1 ** 2)
            )

        return np.linalg.lstsq(np.array(A), np.array(b), rcond=None)[0]

    def _nlls(
        self,
        distances: np.ndarray,
        initial: np.ndarray,
        indices: np.ndarray,
    ) -> tuple:
        """Levenberg-Marquardt non-linear least squares refinement."""
        coords = self._receiver_coords[indices]
        dists  = distances[indices]

        def residuals(pos: np.ndarray) -> np.ndarray:
            return np.linalg.norm(coords - pos, axis=1) - dists

        result = least_squares(residuals, initial, method="lm")
        return result.x, result

    # ─── CEP subset selection ─────────────────────────────────────────────────

    def _compute_cep(
        self,
        distances: tuple,
        indices: list,
    ) -> tuple:
        """Solve for position and compute the Circular Error Probable (CEP)."""
        d_arr    = np.array(distances, dtype=float)
        idx      = np.array(indices)
        initial  = self._ols(d_arr, idx)
        position, result = self._nlls(d_arr, initial, idx)

        if result is None or len(indices) <= 2:
            return position, float("inf"), np.eye(2) * 1000.0

        J   = result.jac
        fun = result.fun
        try:
            W   = np.diag(1.0 / (np.abs(fun) + 0.1))
            cov = np.linalg.inv(J.T @ W @ J)
            sx  = float(np.sqrt(abs(cov[0, 0])))
            sy  = float(np.sqrt(abs(cov[1, 1])))
            cep = 0.59 * np.sqrt(sx ** 2 + sy ** 2) * np.sqrt(2.0)
        except np.linalg.LinAlgError:
            cov = np.eye(2) * 1000.0
            cep = float("inf")

        return position, cep, cov

    def _find_best_subset(
        self,
        distances: tuple,
        sane_list: list,
        max_subsets: int,
    ) -> tuple:
        """Try receiver subsets and return the one with the lowest CEP."""
        all_combos: list[list] = [sane_list]

        for k in range(3, len(sane_list)):
            for subset in combinations(sane_list, k):
                all_combos.append(list(subset))
                if len(all_combos) >= max_subsets:
                    break
            if len(all_combos) >= max_subsets:
                break

        best_pos = None
        best_cep = float("inf")
        best_idx = None
        best_cov = None

        for combo in all_combos:
            if len(combo) < 3:
                continue
            pos, cep, cov = self._compute_cep(distances, combo)
            if cep < best_cep:
                best_cep = cep
                best_pos = pos
                best_idx = combo
                best_cov = cov

        return best_pos, best_cep, best_idx, best_cov

    # ─── Position validation ──────────────────────────────────────────────────

    def _validate_position(
        self,
        position: Optional[np.ndarray],
    ) -> tuple:
        """Check that *position* lies within the receiver bounding box."""
        if position is None:
            return self._cep_last_good_pos, True

        x, y  = float(position[0]), float(position[1])
        x_min = float(self._receiver_coords[:, 0].min())
        x_max = float(self._receiver_coords[:, 0].max())
        y_min = float(self._receiver_coords[:, 1].min())
        y_max = float(self._receiver_coords[:, 1].max())

        if x_min <= x <= x_max and y_min <= y <= y_max:
            return position, False

        fallback = self._cep_last_good_pos
        return (fallback if fallback is not None else position), True
