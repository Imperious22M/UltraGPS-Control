"""
UltraGPS Calibration Library — calibration points
=================================================
Where to put the 2 to ``MAX_POINTS`` calibration points: scoring a point set,
searching for the best one, checking the points a user picked, and a map of
the arena that shows where the next point is best placed.  Pure numpy — no
GUI code, no hardware, no file I/O.

Ported from the UltraGPS simulator (``engine/cal_points.py`` and the point
checks of its calibration window), where each set was also calibrated many
times on the simulated sensors to confirm that the score predicts the error.

Why the choice matters
----------------------
Calibration fits ``distance = slope * tick + intercept`` for each receiver
from one modal tick per calibration point.  Each modal tick is off by a little
(tick rounding plus noise).  With points at distances ``d_j`` from a receiver,
the least-squares fit's error is

.. code-block:: text

    slope error     ∝ 1 / sqrt(Sxx)                     Sxx = Σ (d_j - d̄)²
    intercept error ∝ sqrt(1/n + d̄² / Sxx)
    distance error at D ∝ sqrt(1/n + (D - d̄)² / Sxx)

so a receiver wants its calibration points at very different distances from
it (large Sxx), centred near the distances it will actually measure.  Six
receivers pull in different directions, so a point set is scored by the RMS
distance error it would cause over the whole arena, for all receivers:

.. code-block:: text

    score = sqrt(mean over receivers and arena positions of 1/n + (D - d̄)² / Sxx)

i.e. the calibration-induced distance error per unit of per-reading error.
That single number combines slope and intercept error in the units that
matter (cm of distance).  Lower is better.  It depends only on geometry, so
the ranking holds for any noise level.  The arena positions are a grid
``EVAL_STEP_CM`` apart over the receivers' bounding box; the mean over them
is taken in closed form, ``mean (D - d̄)² = var(D) + (mean(D) - d̄)²``.

In the simulator (1 cm range noise, its default arena) the old pair of points
near the centre, (0, 76) and (-76, 0), scores 4.82 and calibrates to a 3.1 cm
distance error over the arena; the best two points score 1.17 (1.7 cm), the
best three 0.86 (1.2 cm) and the best four 0.68 (1.1 cm).  The old pair sits
at almost the same distance from some receivers (292 and 276 cm from one), so
a one-tick difference becomes a large slope error.  Points spread far apart,
about 30 cm inside the receivers' box and heard by every receiver are best.

The room
--------
The simulator knows its room's walls and obstacles and traces each receiver's
line of sight.  Here two optional callables stand in for them, so the caller
decides what "in the arena" and "heard" mean (the arena outline the user
drew, a receiver's beam and range model):

``inside(points)``
    ``(M, 2)`` array → ``(M,)`` bool: can the transmitter be there?  Points
    where it is False are never candidates, are left off the quality map and
    are refused by ``check_points``.
``hears(points)``
    ``(M, 2)`` array → ``(M, R)`` bool: does receiver ``r`` (column ``r``,
    in the order of ``coords``) hear the transmitter's direct sound from
    there?  Every point–receiver pair that is not heard costs
    ``BLOCKED_PENALTY``: a receiver calibrated on an echo, or on nothing, is
    wrong everywhere, so a set every receiver hears wins whenever there is
    one.

Without them every point of the receivers' box is in the arena and heard by
every receiver, and ``search`` returns exactly the simulator's sets.

Search
------
Candidates are a grid over the receivers' bounding box, shrunk by ``margin``
cm on every side, keeping only points within the HC-SR04's 400 cm of every
receiver (otherwise a receiver can't be calibrated there) and, given
``inside``, only points in the arena.  The margin keeps points away from the
receiver walls: a point level with a wall of receivers is far off their
beams, where readings are weak and late.  No two points of a set are closer
than ``MIN_SEPARATION_CM``: the same spot read twice repeats its range walk
and adds nothing.  2 points: every pair.  3 and 4 points: greedy from the
best smaller set, then exchange passes (swap one point for any candidate
while that improves the score), then a refinement on a finer grid.

Checks
------
``check_points`` says why a set of points can't be calibrated on (problems:
fewer than 2 or more than ``MAX_POINTS`` points, a point outside the arena,
two points closer than ``MIN_SEPARATION_CM``, a receiver at the same distance
from every point) and what will go wrong if it is (warnings: a receiver
beyond ``MAX_RANGE_CM``, a receiver that can't hear a point, a receiver whose
distances to the points span less than ``SPREAD_CM``, a point less than
``margin`` cm inside the receivers' box).

Quality map
-----------
``quality_map`` gives every point ``p`` of a grid the score of the best set
that would hold it: with other points already chosen, the score of "those
points + p"; with none, the score of the best pair "p + q".  ``goodness``
turns it into 0..1 for shading (1 = the best place on the map).

Usage
-----
    import numpy as np
    from ultragps_calibration import points

    coords = np.array([r["position"] for r in sorted(cfg["receivers"], key=lambda r: r["id"])])

    best = points.search(coords, max_points=4)          # {2: (2, 2), 3: (3, 2), 4: (4, 2)}
    print(points.arena_score(best[4], coords))           # lower is better

    check = points.check_points(best[4], coords)
    if check.problems:
        print("Can't calibrate:", "; ".join(check.problems))

    # Where should P2 go, given P1?
    xs, ys, Z = points.quality_map(coords, fixed=[best[4][0]])
    shade = points.goodness(Z)                           # Z[j, i] at (xs[i], ys[j])

All coordinates and distances are in centimetres.  Receivers are labelled
R1..Rn (1-indexed, in the order of ``coords``) and points P1..Pn.  Points and
receiver positions are ``(N, 2)`` array-likes (a single ``(x, y)`` is one
point); any other shape raises ``ValueError``.  Every function copies its
inputs and keeps no state, so it may run on a worker thread while the GUI
edits its own arrays.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Optional

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MAX_RANGE_CM      = 400.0   # HC-SR04 datasheet range: no calibration beyond it
MIN_SEPARATION_CM = 20.0    # points closer than this are one point read twice
BLOCKED_PENALTY   = 1000.0  # per point a receiver can't hear: worse than any heard set
SPREAD_CM         = 10.0    # a receiver whose points' distances span less gets a poor slope
DEFAULT_MARGIN_CM = 30.0    # keep points this far inside the receivers' box
EVAL_STEP_CM      = 20.0    # grid step of the arena positions the score averages over
MIN_POINTS        = 2       # a straight line needs two points
MAX_POINTS        = 4       # most points the calibration library collects at

SCORE_TIP = (
    "Arena score of the points: the RMS distance error their calibration causes "
    "over the whole arena and every receiver, per cm of error in a single reading "
    "(sqrt(1/n + (D − d̄)²/Sxx)). Lower is better. It depends only on geometry: a "
    "receiver wants its points at very different distances from it, centred on "
    "the distances it will measure."
)

InsideFn = Callable[[np.ndarray], np.ndarray]   # (M, 2) points -> (M,) bool
HearsFn  = Callable[[np.ndarray], np.ndarray]   # (M, 2) points -> (M, R) bool

_TOL         = 1e-6        # cm: a separation this short of the minimum still counts
_FLAT_SXX    = 1e-9        # cm²: a receiver's Sxx at or below this fits no slope
_PAIR_BUDGET = 1.5e7       # quality_map: (point, partner, receiver) terms before partners thin out
_MAX_STRIDE  = 16          # quality_map: sparsest partner grid, in map steps
_CHUNK       = 1 << 17     # quality_map: elements per working array (fits in cache)
_MAX_CELLS   = 1_000_000   # quality_map: grid cells before the step is coarsened
_MAX_EVAL    = 250_000     # arena positions before the evaluation step is coarsened


# ─────────────────────────────────────────────────────────────────────────────
# Geometry
# ─────────────────────────────────────────────────────────────────────────────

def grid(coords, step: float) -> np.ndarray:
    """Points on a ``step`` grid over the receivers' bounding box.

    The grid starts at the box's lower-left corner; the last line in x or y
    may fall short of the far edge by less than ``step``.

    Args:
        coords: ``(R, 2)`` receiver positions (cm).
        step:   Grid spacing (cm).

    Returns:
        ``(N, 2)`` array in x-major order (every y of the first x, then the
        next x, ...).
    """
    xs, ys = _axes(_coords(coords), step)
    X, Y = np.meshgrid(xs, ys, indexing="ij")
    return np.column_stack([X.ravel(), Y.ravel()])


def margin_box(coords, margin: float = DEFAULT_MARGIN_CM) -> tuple[float, float, float, float]:
    """The receivers' bounding box shrunk by ``margin`` cm on every side.

    Returns:
        ``(x0, y0, x1, y1)`` (cm).  The box is empty (``x0 > x1`` or
        ``y0 > y1``) when the receivers span less than ``2 * margin``.
    """
    c = _coords(coords)
    (x0, y0), (x1, y1) = c.min(axis=0) + margin, c.max(axis=0) - margin
    return float(x0), float(y0), float(x1), float(y1)


def candidates(coords, step: float, margin: float = 0.0,
               inside: Optional[InsideFn] = None) -> np.ndarray:
    """Grid points where a calibration point may go.

    Points of a ``step`` grid (see ``grid``) ``margin`` cm inside the
    receivers' box and within ``MAX_RANGE_CM`` of every receiver; given
    ``inside``, only those in the arena.

    Returns:
        ``(N, 2)`` array, in ``grid`` order.
    """
    coords = _coords(coords)
    if ((coords.max(axis=0) - coords.min(axis=0)) > 2 * MAX_RANGE_CM).any():
        return np.empty((0, 2))           # no point is in range of both ends
    pts = grid(coords, step)
    (x0, y0), (x1, y1) = coords.min(axis=0) + margin, coords.max(axis=0) - margin
    pts = pts[(pts[:, 0] >= x0) & (pts[:, 0] <= x1) & (pts[:, 1] >= y0) & (pts[:, 1] <= y1)]
    far = _distances(pts, coords).max(axis=1)
    pts = pts[far <= MAX_RANGE_CM].reshape(-1, 2)
    if inside is not None and len(pts):
        pts = pts[_inside(inside, pts)]
    return pts


def too_close(points) -> list[tuple[int, int, float]]:
    """``(i, j, distance)`` of every two points closer than ``MIN_SEPARATION_CM``.

    ``i < j`` are 0-based indices into ``points``.
    """
    P = _points(points)
    return [(i, j, float(np.hypot(*(P[i] - P[j])))) for i, j in combinations(range(len(P)), 2)
            if np.hypot(*(P[i] - P[j])) < MIN_SEPARATION_CM - _TOL]


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def score(dists: np.ndarray, eval_d: np.ndarray) -> np.ndarray:
    """Score point sets: the calibration-induced distance error per unit of
    per-reading error, RMS over the arena and the receivers (lower is better).

    For each receiver, with the set's distances ``d_j`` to it (``n`` points,
    mean ``d̄``, ``Sxx = Σ (d_j - d̄)²``) and the arena's distances ``D`` to it,
    the mean over the arena of ``1/n + (D - d̄)² / Sxx`` is
    ``1/n + (var(D) + (mean(D) - d̄)²) / Sxx``.  The score is the square root
    of its mean over the receivers.  A receiver with ``Sxx = 0`` (the same
    distance from every point: no slope) makes the score ``inf``.

    Args:
        dists:  ``(..., n, R)`` distance from each of ``n`` calibration points
                to each receiver.
        eval_d: ``(E, R)`` distance from each arena position to each receiver.

    Returns:
        ``(...)`` scores.
    """
    dists, eval_d = np.asarray(dists, dtype=float), np.asarray(eval_d, dtype=float)
    n    = dists.shape[-2]
    dbar = dists.mean(axis=-2)                                   # (..., R)
    sxx  = ((dists - dbar[..., None, :]) ** 2).sum(axis=-2)      # (..., R)
    with np.errstate(divide="ignore", invalid="ignore"):
        inv = np.where(sxx > _FLAT_SXX, 1.0 / sxx, np.inf)
        # mean over E of (D - d̄)² = var(D) + (mean(D) - d̄)²
        m, v = eval_d.mean(axis=0), eval_d.var(axis=0)           # (R,)
        per_rx = 1.0 / n + (v + (m - dbar) ** 2) * inv           # (..., R)
    return np.sqrt(per_rx.mean(axis=-1))


def arena_score(points, coords, eval_step: float = EVAL_STEP_CM) -> float:
    """Score of one point set over the arena (see ``score``); lower is better.

    Args:
        points:    ``(n, 2)`` calibration points (cm).
        coords:    ``(R, 2)`` receiver positions (cm).
        eval_step: Spacing (cm) of the arena positions averaged over.

    Returns:
        The score; ``inf`` with fewer than 2 points or when a receiver is the
        same distance from every point.
    """
    coords = _coords(coords)
    pts = _points(points)
    if len(pts) < MIN_POINTS:
        return math.inf
    return float(score(_distances(pts, coords), _eval_distances(coords, eval_step)))


def receiver_errors(points, coords) -> list[tuple[float, float]]:
    """Per receiver: ``(relative slope error, intercept error)``, per unit of
    per-reading distance error.

    ``1 / sqrt(Sxx)`` and ``sqrt(1/n + d̄² / Sxx)`` for that receiver's
    distances to the points; divide two sets' values to compare them (the
    simulator prints "slope err ×, intercept err ×" against the set in use).
    Both are ``inf`` for a receiver at the same distance from every point.

    Returns:
        One ``(slope_err, intercept_err)`` tuple per receiver, in ``coords``
        order.
    """
    coords = _coords(coords)
    pts = _points(points)
    if not len(pts):
        return [(math.inf, math.inf)] * len(coords)
    d    = _distances(pts, coords)
    dbar = d.mean(axis=0)
    sxx  = ((d - dbar) ** 2).sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        return [(float(1 / np.sqrt(s)), float(np.sqrt(1 / len(pts) + db ** 2 / s)))
                for s, db in zip(sxx, dbar)]


# ─────────────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────────────

def search(coords, max_points: int, margin: float = DEFAULT_MARGIN_CM, step: float = 20.0,
           fine: float = 5.0, inside: Optional[InsideFn] = None,
           hears: Optional[HearsFn] = None) -> dict[int, np.ndarray]:
    """Best point set for each size 2..``max_points``.

    Every pair of ``step`` grid candidates is scored and the best kept; each
    larger set adds the best candidate to the previous one; exchange passes
    then swap one point at a time for any candidate while that lowers the
    score, and the same passes on a ``fine`` grid finish each set.  No two
    points of a set are closer than ``MIN_SEPARATION_CM`` (on the default
    20 cm grid no two candidates are).  A set costs
    ``BLOCKED_PENALTY`` extra for every point a receiver can't hear (given
    ``hears``), so a set every receiver hears wins whenever there is one.
    Takes a fraction of a second for six receivers.

    Args:
        coords:     ``(R, 2)`` receiver positions (cm).
        max_points: Largest set wanted, ``MIN_POINTS``..``MAX_POINTS``.
        margin:     Keep points this far (cm) inside the receivers' box.
        step:       Coarse grid spacing (cm).
        fine:       Refinement grid spacing (cm).
        inside:     Optional arena test (see the module docstring).
        hears:      Optional direct-sound test (see the module docstring).

    Returns:
        ``{n: (n, 2) array}`` for every ``n`` in 2..``max_points``.

    Raises:
        ValueError: ``max_points`` out of range, or not enough points qualify
                    (receivers too far apart or in a line, or an arena that
                    leaves too little room for ``max_points`` points
                    ``MIN_SEPARATION_CM`` apart); also bad receivers or an
                    ``inside``/``hears`` answer of the wrong shape.
    """
    coords = _coords(coords)
    if not MIN_POINTS <= max_points <= MAX_POINTS:
        raise ValueError(f"max_points must be {MIN_POINTS} to {MAX_POINTS}, not {max_points}")
    cand = candidates(coords, step, margin, inside)
    rule = (f"a point must be within {MAX_RANGE_CM:.0f} cm of every receiver and {margin:g} cm inside "
            f"their box{', inside the arena' if inside is not None else ''}")
    if len(cand) < max(max_points, 2):
        found = ("no point qualifies" if not len(cand) else
                 f"only {len(cand)} point{' qualifies' if len(cand) == 1 else 's qualify'} "
                 f"for {max_points}")
        raise ValueError(f"{found}: {rule}. Are the receivers too far apart, or in a line?")

    def cramped(n: int) -> ValueError:
        return ValueError(f"no {n} points that qualify are {MIN_SEPARATION_CM:g} cm or more apart: "
                          f"{rule}. Is the arena too small?")

    eval_d = _eval_distances(coords, EVAL_STEP_CM)

    def pool(pts: np.ndarray) -> tuple:
        """The points, their distances to the receivers and their hearing penalty."""
        return pts, _distances(pts, coords), _penalty(pts, hears, len(coords))

    def cost(p: tuple, idx: np.ndarray) -> np.ndarray:
        return score(p[1][idx], eval_d) + p[2][idx].sum(axis=-1)

    def exchange(idx: list[int], p: tuple) -> list[int]:
        """Swap one point at a time for the pool point that lowers the cost most, until none does."""
        idx = list(idx)
        cur = float(cost(p, np.array(idx)))
        improved = True
        while improved:
            improved = False
            for slot in range(len(idx)):
                trial = np.repeat(np.array(idx)[None, :], len(p[0]), axis=0)
                trial[:, slot] = np.arange(len(p[0]))
                sc = cost(p, trial)
                sc[_crowded(p[0], p[0][[k for n, k in enumerate(idx) if n != slot]])] = np.inf
                j  = int(np.argmin(sc))
                if sc[j] < cur - 1e-12:
                    idx[slot], cur, improved = j, float(sc[j]), True
        return idx

    # 2 points: every pair far enough apart (every pair on a step of
    # MIN_SEPARATION_CM or more: distinct grid points are at least a step apart)
    coarse = pool(cand)
    pairs  = np.array(list(combinations(range(len(cand)), 2)))
    pairs  = pairs[np.hypot(*(cand[pairs[:, 0]] - cand[pairs[:, 1]]).T) >= MIN_SEPARATION_CM - _TOL]
    if not len(pairs):
        raise cramped(2)
    s      = np.concatenate([cost(coarse, pairs[i:i + 20000]) for i in range(0, len(pairs), 20000)])
    best   = {2: list(pairs[int(np.argmin(s))])}

    for n in range(3, max_points + 1):
        base  = best[n - 1]
        crowded = _crowded(cand, cand[base])
        if crowded.all():
            raise cramped(n)
        trial = np.array([base + [j] for j in range(len(cand))])
        sc    = cost(coarse, trial)
        sc[crowded] = np.inf
        best[n] = exchange(base + [int(np.argmin(sc))], coarse)
    best[2] = exchange(best[2], coarse)

    # Fine refinement: let each point move on a finer grid.  A fine grid
    # that is not a divisor of ``step`` need not hold the coarse points: when
    # it misses the arena, or the points it starts from end up too close
    # together, the coarse set stands (never the case with the defaults)
    fpool = pool(candidates(coords, fine, margin, inside))
    out   = {}
    for n, idx in best.items():
        if len(fpool[0]) < n:
            out[n] = cand[idx]
            continue
        near   = [int(np.argmin(np.linalg.norm(fpool[0] - q, axis=1))) for q in cand[idx]]
        found  = fpool[0][exchange(near, fpool)]
        out[n] = cand[idx] if too_close(found) else found
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Checks
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PointCheck:
    """What ``check_points`` found.

    Attributes:
        problems: Why the points can't be calibrated on — refuse to calibrate.
        warnings: What will go wrong if they are — calibrate, but say so.
        rows:     Per point, in order, ``(problems, warnings)`` about that
                  point alone (for its table row: red, amber or plain).
    """

    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rows: list[tuple[list[str], list[str]]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing stops a calibration on the points."""
        return not self.problems


def check_points(points, coords, inside: Optional[InsideFn] = None,
                 hears: Optional[HearsFn] = None, margin: float = DEFAULT_MARGIN_CM,
                 names: Optional[Sequence[str]] = None) -> PointCheck:
    """Check calibration points before a run.

    Problems (refuse to calibrate):

    - fewer than ``MIN_POINTS`` or more than ``MAX_POINTS`` points;
    - a point that is not a finite position, or is outside the arena
      (``inside`` False);
    - two points closer than ``MIN_SEPARATION_CM`` (the same spot read twice);
    - a receiver at the same distance from every point: no slope can be
      fitted (``Sxx = 0``, as in ``score``).

    Warnings (calibrate, but expect a worse fit), for points in the arena:

    - a receiver farther than ``MAX_RANGE_CM`` (the HC-SR04's range);
    - a receiver that can't hear a point (``hears`` False): it would
      calibrate on an echo or read nothing there (pairs already out of range
      are not repeated);
    - a receiver whose distances to the points span less than ``SPREAD_CM``:
      a poor slope;
    - a point less than ``margin`` cm inside the receivers' box (outside the
      box with ``margin`` 0): the receivers along that edge hear it far off
      their beams, weak and late.

    Args:
        points: ``(n, 2)`` calibration points (cm), P1..Pn in order.
        coords: ``(R, 2)`` receiver positions (cm).
        inside: Optional arena test (see the module docstring).
        hears:  Optional direct-sound test (see the module docstring).
        margin: The margin (cm) the search keeps inside the receivers' box.
        names:  Receiver labels, one per row of ``coords``; default
                ``R1``..``Rn``.

    Returns:
        A ``PointCheck``; ``rows`` has one entry per point.

    Raises:
        ValueError: Bad receivers, points that are not ``(x, y)`` pairs, a
                    ``names`` of the wrong length, or an ``inside``/``hears``
                    answer of the wrong shape.
    """
    coords = _coords(coords)
    pts = _points(points)
    n, R = len(pts), len(coords)
    rx = [f"R{r + 1}" for r in range(R)] if names is None else [str(x) for x in names]
    if len(rx) != R:
        raise ValueError(f"{len(rx)} receiver names for {R} receivers")
    rows: list[tuple[list[str], list[str]]] = [([], []) for _ in range(n)]
    problems: list[str] = []
    warnings: list[str] = []

    if n < MIN_POINTS:
        problems.append(f"{'No' if not n else 'Only 1'} calibration point{'' if n else 's'}: "
                        f"calibration needs at least {MIN_POINTS}")
    elif n > MAX_POINTS:
        problems.append(f"{n} calibration points: calibration takes at most {MAX_POINTS}")

    # Points the transmitter can't be at
    finite = np.isfinite(pts).all(axis=1)
    reachable = finite.copy()
    if inside is not None and finite.any():
        reachable[finite] = _inside(inside, pts[finite])
    for k in range(n):
        if not finite[k]:
            rows[k][0].append("not a valid position")
        elif not reachable[k]:
            rows[k][0].append("outside the arena")
    problems += [f"P{k + 1} is {bad[0]}" for k, (bad, _) in enumerate(rows) if bad]

    close = too_close(pts)
    for i, j, d in close:
        problems.append(f"P{i + 1} and P{j + 1} are only {d:.1f} cm apart "
                        f"(keep them {MIN_SEPARATION_CM:g} cm or more apart)")
        rows[i][0].append(f"{d:.1f} cm from P{j + 1}")
        rows[j][0].append(f"{d:.1f} cm from P{i + 1}")

    dist = _distances(pts, coords)                                         # (point, receiver)
    flat = np.zeros(R, dtype=bool)
    if n >= MIN_POINTS:
        flat = ((dist - dist.mean(axis=0)) ** 2).sum(axis=0) <= _FLAT_SXX  # as in the score
    if flat.any() and not close:
        who = [rx[r] for r in np.flatnonzero(flat)]
        problems.append(f"{_join(who)} {_is(who)} the same distance from every point: "
                        f"no slope can be fitted")

    # What a point the transmitter can be at will get wrong
    far = (dist > MAX_RANGE_CM) & reachable[:, None]
    for k in np.flatnonzero(far.any(axis=1)):
        who = [rx[r] for r in np.flatnonzero(far[k])]
        cms = [f"{dist[k, r]:.0f}" for r in np.flatnonzero(far[k])]
        rows[k][1].append(f"{_join(who)} {_is(who)} {_join(cms)} cm away, beyond the HC-SR04's "
                          f"{MAX_RANGE_CM:.0f} cm range")
    beyond = [f"P{k + 1}" for k in np.flatnonzero(far.any(axis=1))]
    if beyond:
        warnings.append(f"{_join(beyond)} {_is(beyond)} beyond a receiver's {MAX_RANGE_CM:.0f} cm range")

    if hears is not None and reachable.any():
        deaf = np.zeros((n, R), dtype=bool)
        deaf[reachable] = ~_heard(hears, pts[reachable], R)
        deaf &= ~far                                       # out of range: said already
        for k in np.flatnonzero(deaf.any(axis=1)):
            who = [rx[r] for r in np.flatnonzero(deaf[k])]
            rows[k][1].append(f"{_join(who)} can't hear it: {'it' if len(who) == 1 else 'they'} "
                              f"would calibrate on an echo or read nothing")
        pairs = [f"P{k + 1}–{rx[r]}" for k, r in zip(*np.nonzero(deaf))]
        if pairs:
            warnings.append(f"No direct sound {', '.join(pairs)}: those receivers would calibrate "
                            f"on an echo or read nothing")

    if n >= MIN_POINTS and not close:
        spread = np.ptp(dist, axis=0)
        narrow = [rx[r] for r in range(R) if spread[r] < SPREAD_CM and not flat[r]]
        if narrow:
            warnings.append(f"{_join(narrow)}: the points' distances differ by under {SPREAD_CM:g} cm, "
                            f"a poor slope")

    x0, y0, x1, y1 = margin_box(coords, margin)
    boxed = (pts[:, 0] >= x0) & (pts[:, 0] <= x1) & (pts[:, 1] >= y0) & (pts[:, 1] <= y1)
    edge = [k for k in range(n) if reachable[k] and not boxed[k]]
    where = (f"less than {margin:g} cm inside the receivers' box" if margin > 0
             else "outside the receivers' box")
    for k in edge:
        rows[k][1].append(f"{where}: weak, late readings far off the receivers' beams")
    if edge:
        who = [f"P{k + 1}" for k in edge]
        warnings.append(f"{_join(who)} {_is(who)} {where}: the receivers along that edge hear "
                        f"{'it' if len(who) == 1 else 'them'} far off their beams, weak and late")

    return PointCheck(problems, warnings, rows)


# ─────────────────────────────────────────────────────────────────────────────
# Quality map
# ─────────────────────────────────────────────────────────────────────────────

def quality_map(coords, fixed=(), step: float = 5.0, margin: float = DEFAULT_MARGIN_CM,
                inside: Optional[InsideFn] = None, hears: Optional[HearsFn] = None,
                eval_step: float = EVAL_STEP_CM,
                partner_step: Optional[float] = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """How good a place each grid point ``p`` is for a calibration point.

    With one or more ``fixed`` points (the points already chosen):

    .. code-block:: text

        Z(p) = score(fixed + [p]) + BLOCKED_PENALTY × (unheard pairs of p and of the fixed points)

    exactly ``arena_score(fixed + [p], coords)`` plus the penalty.  With no
    fixed point, the score of the best pair that holds ``p``:

    .. code-block:: text

        Z(p) = min over partners q of  score({p, q}) + penalty(p) + penalty(q)

    where a partner is any point that could itself be on the map, at least
    ``MIN_SEPARATION_CM`` from ``p``.  Every pair of a fine map would be
    slow (the simulator's 2.5 × 3.7 m arena has about 2400 candidates on a
    5 cm map: 6 million pairs), so the partners are the points on every k-th
    line of the map's grid (``partner_step``, or the smallest k that keeps
    the work under a fixed budget: k = 1, every point, on small maps) plus
    every point on an edge, of the candidate region or between points that
    pay different hearing penalties, where the best partner often is; each
    point's best partner is then refined over the full grid round it.  The
    result is never below the minimum over every partner on the grid, and
    at most 0.7 % above it on the 5 and 2.5 cm maps of the 150 random
    layouts tried, with and without walls and hearing models (``goodness``
    within 0.011).  ``partner_step=step`` gives the exact minimum, at up to
    ten times the cost.

    Lower is better.  ``nan`` marks a point that is not a candidate: outside
    the receivers' box shrunk by ``margin``, outside the arena (``inside``
    False), farther than ``MAX_RANGE_CM`` from a receiver, or within
    ``MIN_SEPARATION_CM`` of a fixed point.  ``inf`` marks a candidate that
    no set can use: some receiver would be the same distance from every
    point (or, with no fixed point, there is no partner).  ``goodness``
    turns the map into 0..1 for shading.

    Args:
        coords:       ``(R, 2)`` receiver positions (cm).
        fixed:        ``(k, 2)`` points already chosen (cm); may be empty
                      (or ``None``).
        step:         Map grid spacing (cm).  Raised when the receivers'
                      box would need more than a million cells (a receiver
                      typed metres away).
        margin:       Keep points this far (cm) inside the receivers' box.
        inside:       Optional arena test (see the module docstring).
        hears:        Optional direct-sound test (see the module docstring).
        eval_step:    Spacing (cm) of the arena positions the score averages
                      over (``arena_score`` uses the same default).
        partner_step: Partner grid spacing (cm), rounded to a whole number of
                      map steps (1 to 16); ``None`` picks it from the work
                      budget.

    Returns:
        ``(xs, ys, Z)``: ascending grid lines over the receivers' bounding box
        (from its lower-left corner, ``s`` apart: ``step`` unless it had to
        be raised) and ``Z`` with ``Z[j, i]`` at ``(xs[i], ys[j])``.  For
        ``imshow`` use ``origin="lower"`` and ``extent=(xs[0] - s/2,
        xs[-1] + s/2, ys[0] - s/2, ys[-1] + s/2)``.

    Raises:
        ValueError: No receivers, a receiver or fixed point that is not a
                    finite position, points that are not ``(x, y)`` pairs,
                    a step that is not positive, or an ``inside`` or
                    ``hears`` answer of the wrong shape.

    A 5 cm map of a 4 × 4 m arena with six receivers takes about 0.05 s with
    no fixed point and a few milliseconds with one, plus the time ``inside``
    and ``hears`` take on its points.
    """
    coords = _coords(coords)
    R = len(coords)
    fixed = _points(fixed, "fixed points")
    if not np.isfinite(fixed).all():
        raise ValueError("fixed points must be finite numbers")
    step = _fit_step(coords, _positive(step, "step"), _MAX_CELLS)
    xs, ys = _axes(coords, step)
    Z = np.full((len(ys), len(xs)), np.nan)

    # Candidates: in the margin box and within range of the outermost
    # receivers (both separable in x and y), then the exact tests
    x0, y0, x1, y1 = margin_box(coords, margin)
    lo, hi = coords.max(axis=0) - MAX_RANGE_CM - _TOL, coords.min(axis=0) + MAX_RANGE_CM + _TOL
    ix = np.flatnonzero((xs >= x0) & (xs <= x1) & (xs >= lo[0]) & (xs <= hi[0]))
    jy = np.flatnonzero((ys >= y0) & (ys <= y1) & (ys >= lo[1]) & (ys <= hi[1]))
    J, I = (a.ravel() for a in np.meshgrid(jy, ix, indexing="ij"))
    P = np.column_stack([xs[I], ys[J]])
    d = _distances(P, coords)
    keep = d.max(axis=1) <= MAX_RANGE_CM
    if len(fixed):
        keep &= ~_crowded(P, fixed)
    if inside is not None and keep.any():
        keep[keep] = _inside(inside, P[keep])
    P, d, J, I = P[keep], d[keep], J[keep], I[keep]
    if not len(P):
        return xs, ys, Z

    pen = _penalty(P, hears, R)
    eval_d = _eval_distances(coords, eval_step)
    if len(fixed):
        Z[J, I] = (_scores_with(d, _distances(fixed, coords), eval_d) + pen
                   + _penalty(fixed, hears, R).sum())
    else:
        part, k = _partners(J, I, Z.shape, pen, R, step, partner_step)
        Z[J, I] = _best_pair_scores(P, d, J, I, Z.shape, pen, eval_d, part, k) + pen
    return xs, ys, Z


def goodness(Z, worst: float = 2.0) -> np.ndarray:
    """Turn a quality map into 0..1 for shading: 1 = the best point of the map.

    The score is proportional to the calibration error, so each point is
    rated by its error in multiples of the best point's, and the rating
    falls linearly from 1 there to 0 at ``worst`` times the best error:

    .. code-block:: text

        ratio    = (Z - F) / (Z_best - F)
        goodness = clip((worst - ratio) / (worst - 1), 0, 1)

    With the default ``worst = 2``: 1 at the best point, 0.5 where the error
    would be 1.5 times the best, 0 where it would be twice the best or more.
    ``F`` is the hearing penalty every point of the map pays
    (``BLOCKED_PENALTY`` × the best point's unheard pairs, 0 when it has
    none), so the geometry still shades when no point is heard by every
    receiver; a point with more unheard pairs than the best one gets 0.
    ``nan`` and ``inf`` give 0.  Every score is above 0; a map whose best
    value is not (no map from ``quality_map``) rates its best points 1 and
    the rest 0.

    Args:
        Z:     A map from ``quality_map`` (any shape).
        worst: Error ratio (> 1) that rates 0.

    Returns:
        Array of ``Z``'s shape with values in [0, 1].
    """
    worst = float(worst)
    if not worst > 1:
        raise ValueError(f"worst must be above 1, not {worst!r}")
    Z = np.asarray(Z, dtype=float)
    out = np.zeros(Z.shape)
    ok = np.isfinite(Z)
    if not ok.any():
        return out
    best = float(Z[ok].min())
    if not best > 0:                                   # no ratio to the best: only the best rates
        out[ok] = Z[ok] == best
        return out
    floor = BLOCKED_PENALTY * math.floor(best / BLOCKED_PENALTY)
    if not best - floor > 0:
        floor = 0.0
    ratio = (Z[ok] - floor) / (best - floor)
    out[ok] = (worst - ratio) / (worst - 1.0)
    return np.clip(out, 0.0, 1.0)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _points(points, what: str = "points") -> np.ndarray:
    """``points`` as a new float ``(N, 2)`` array.

    A single ``(x, y)`` is one point; ``None`` or an empty sequence is none.
    The array is a copy, so a caller changing its own array while a call runs
    (a drag on the GUI thread, the work on another) can't mix two states.

    Raises:
        ValueError: ``points`` is not ``(x, y)`` pairs (e.g. ``(x, y, z)``
                    rows, which a reshape would silently re-pair).
    """
    arr = np.array([] if points is None else points, dtype=float)
    if arr.size == 0:
        return arr.reshape(0, 2)
    if arr.ndim == 1 and arr.shape[0] == 2:
        return arr[None, :]
    if arr.ndim != 2 or arr.shape[1] != 2:
        raise ValueError(f"{what} must be (x, y) pairs in an (N, 2) array, not shape {arr.shape}")
    return arr


def _coords(coords) -> np.ndarray:
    """Receiver positions as a new float ``(R, 2)`` array (at least one receiver)."""
    c = _points(coords, "coords")
    if not len(c):
        raise ValueError("no receivers: calibration points need receiver positions")
    if not np.isfinite(c).all():
        raise ValueError("receiver positions must be finite numbers")
    return c


def _positive(value: float, name: str) -> float:
    """``value`` as a float, which must be finite and above 0."""
    value = float(value)
    if not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a positive number of cm, not {value!r}")
    return value


def _axes(coords: np.ndarray, step: float) -> tuple[np.ndarray, np.ndarray]:
    """Grid lines ``step`` apart over the receivers' box, from its lower-left corner."""
    step = _positive(step, "step")
    (x0, y0), (x1, y1) = coords.min(axis=0), coords.max(axis=0)
    xs = np.arange(x0, x1 + 1e-9, step)
    ys = np.arange(y0, y1 + 1e-9, step)
    return xs, ys


def _fit_step(coords: np.ndarray, step: float, max_cells: int) -> float:
    """``step``, coarsened until a grid over the receivers' box has at most ``max_cells`` cells."""
    w, h = coords.max(axis=0) - coords.min(axis=0)
    while (math.floor(w / step) + 1) * (math.floor(h / step) + 1) > max_cells:
        step *= 1.25
    return step


def _distances(points: np.ndarray, coords: np.ndarray) -> np.ndarray:
    """``(M, R)`` distance from each point to each receiver."""
    return np.linalg.norm(points[:, None, :] - coords[None, :, :], axis=2)


def _eval_distances(coords: np.ndarray, eval_step: float = EVAL_STEP_CM) -> np.ndarray:
    """``(E, R)`` distances from the arena positions the score averages over."""
    step = _fit_step(coords, _positive(eval_step, "eval_step"), _MAX_EVAL)
    return _distances(grid(coords, step), coords)


def _crowded(pool: np.ndarray, chosen: np.ndarray) -> np.ndarray:
    """Which ``pool`` points are closer than ``MIN_SEPARATION_CM`` to one of ``chosen``."""
    if not len(chosen):
        return np.zeros(len(pool), dtype=bool)
    d = np.linalg.norm(pool[:, None, :] - np.asarray(chosen)[None, :, :], axis=2)
    return (d < MIN_SEPARATION_CM - _TOL).any(axis=1)


def _inside(inside: InsideFn, points: np.ndarray) -> np.ndarray:
    """The caller's arena test on ``points``, as an ``(M,)`` bool array."""
    out = np.asarray(inside(points), dtype=bool)
    if out.size != len(points):
        raise ValueError(f"inside() gave {out.size} answers for {len(points)} points")
    return out.reshape(len(points))


def _heard(hears: HearsFn, points: np.ndarray, receivers: int) -> np.ndarray:
    """The caller's direct-sound test on ``points``, as an ``(M, R)`` bool array.

    The shape must be exact: a transposed answer, or one for another set of
    receivers, would otherwise be re-paired without a word.
    """
    if not len(points):
        return np.ones((0, receivers), dtype=bool)
    out = np.asarray(hears(points), dtype=bool)
    if out.shape != (len(points), receivers):
        raise ValueError(f"hears() gave shape {out.shape} for {len(points)} points and {receivers} receivers: "
                         f"it must be (points, receivers), for the receivers in coords")
    return out


def _penalty(points: np.ndarray, hears: Optional[HearsFn], receivers: int) -> np.ndarray:
    """``BLOCKED_PENALTY`` × the receivers that can't hear each point (0 without ``hears``)."""
    if hears is None:
        return np.zeros(len(points))
    return BLOCKED_PENALTY * (~_heard(hears, points, receivers)).sum(axis=1)


def _scores_with(d: np.ndarray, d_fixed: np.ndarray, eval_d: np.ndarray) -> np.ndarray:
    """``score`` of the fixed points plus each point in turn.

    Args:
        d:       ``(M, R)`` each point's distances to the receivers.
        d_fixed: ``(k, R)`` the fixed points' distances.
        eval_d:  ``(E, R)`` the arena positions' distances.
    """
    out = np.empty(len(d))
    c = max(1, _CHUNK // ((len(d_fixed) + 1) * d.shape[1]))
    for s in range(0, len(d), c):
        part = d[s:s + c]
        sets = np.concatenate([np.broadcast_to(d_fixed, (len(part),) + d_fixed.shape),
                               part[:, None, :]], axis=1)                  # (c, k + 1, R)
        out[s:s + c] = score(sets, eval_d)
    return out


def _pair_scores(da: np.ndarray, db: np.ndarray, m: np.ndarray, v: np.ndarray) -> np.ndarray:
    """``score`` of the 2-point sets {a, b}, in closed form.

    For two points ``d̄ = (a + b) / 2`` and ``Sxx = (a - b)² / 2``, so each
    receiver's term is ``1/2 + (var(D) + (mean(D) - d̄)²) / Sxx``.

    Args:
        da, db: Receiver-first distance arrays that broadcast together, e.g.
                ``(R, A, 1)`` and ``(R, 1, B)`` for every pair of two lists,
                or ``(R, A, 1)`` and ``(R, A, W)`` for ``W`` partners each.
        m, v:   ``(R, 1, 1)`` mean and variance of the arena's distances.

    Returns:
        The broadcast shape without its first axis.
    """
    sxx = da - db
    sxx *= sxx
    sxx *= 0.5
    dev = da + db
    dev *= -0.5
    dev += m                               # mean(D) - d̄
    dev *= dev
    dev += v
    flat = sxx <= _FLAT_SXX
    with np.errstate(divide="ignore", invalid="ignore"):
        dev /= sxx
    np.putmask(dev, flat, np.inf)
    out = dev.sum(axis=0)
    out *= 1.0 / len(dev)
    out += 0.5
    return np.sqrt(out, out=out)


def _partners(J: np.ndarray, I: np.ndarray, shape: tuple, pen: np.ndarray, receivers: int,
              step: float, partner_step: Optional[float]) -> tuple[np.ndarray, int]:
    """The map points that serve as partners, and the stride ``k`` they were taken at.

    Partners are the points on every k-th grid row and column plus every
    point on an edge: of the candidate region, or between points that pay
    different hearing penalties.  The best partner is as far from ``p`` as
    the region allows, or as far as the receivers still hear, so it is
    often on one of those edges; a region every receiver hears can be
    narrower than k lines and hold no point of the sub-grid at all.  ``k``
    is ``partner_step`` in whole steps (at most ``_MAX_STRIDE``), or the
    smallest stride whose work fits the budget (the cheapest one when none
    does).  The work counts a term for every (point, partner, receiver) and
    about four for every term of the refinement round each point's best
    partner, which gathers its distances point by point.

    Args:
        J, I:         ``(M,)`` the map points' grid rows and columns.
        shape:        ``(rows, columns)`` of the grid.
        pen:          ``(M,)`` the map points' hearing penalties.
        receivers:    Number of receivers.
        step:         Map grid spacing (cm).
        partner_step: The caller's partner spacing (cm), or ``None``.

    Returns:
        ``(indices into the map's points, k)``.
    """
    rows, cols = shape
    level = np.full((rows + 2, cols + 2), np.nan)                # padded: off the grid is outside
    level[J + 1, I + 1] = pen
    here = level[J + 1, I + 1]
    edge = ~((level[J, I + 1] == here) & (level[J + 2, I + 1] == here)     # nan (outside) is never equal
             & (level[J + 1, I] == here) & (level[J + 1, I + 2] == here))

    def chosen(k: int) -> np.ndarray:
        return edge | ((J % k == 0) & (I % k == 0))

    if partner_step is not None:
        k = min(_MAX_STRIDE, max(1, int(round(_positive(partner_step, "partner_step") / step))))
        return np.flatnonzero(chosen(k)), k
    cheapest, least = 1, math.inf
    for k in range(1, _MAX_STRIDE + 1):
        window = (2 * k - 1) ** 2 if k > 1 else 0
        work = len(J) * receivers * (np.count_nonzero(chosen(k)) + 4 * window)
        if work <= _PAIR_BUDGET:
            return np.flatnonzero(chosen(k)), k
        if work < least:
            cheapest, least = k, work
    return np.flatnonzero(chosen(cheapest)), cheapest


def _best_pair_scores(P: np.ndarray, d: np.ndarray, J: np.ndarray, I: np.ndarray, shape: tuple,
                      pen: np.ndarray, eval_d: np.ndarray, part: np.ndarray, k: int) -> np.ndarray:
    """For each map point, ``min over partners q of score({p, q}) + penalty(q)``.

    Partners closer than ``MIN_SEPARATION_CM`` don't count.  They are seldom
    the best (a receiver sees both points at nearly the same distance), so
    instead of measuring every pair the rows whose best partner is too close
    are masked and searched again.

    Args:
        P, d:   ``(M, 2)`` the map's candidate points and ``(M, R)`` their
                distances to the receivers.
        J, I:   ``(M,)`` their grid rows and columns.
        shape:  ``(rows, columns)`` of the grid.
        pen:    ``(M,)`` their hearing penalties.
        eval_d: ``(E, R)`` the arena positions' distances.
        part:   Indices of the points that serve as partners (``_partners``).
        k:      The stride they were taken at; with k > 1 each point's best
                partner is then refined over every point within k - 1 grid
                lines of it.
    """
    M, R = d.shape
    rows, cols = shape
    m = eval_d.mean(axis=0)[:, None, None]
    v = eval_d.var(axis=0)[:, None, None]
    dT = np.ascontiguousarray(d.T)                                  # (R, M)
    dB = dT[:, part][:, None, :]                                    # (R, 1, B)
    PB, penB = P[part], pen[part]
    window = 0
    if k > 1:
        cell = np.full(rows * cols, -1)
        cell[J * cols + I] = np.arange(M)
        off = np.arange(1 - k, k)
        WJ, WI = (a.ravel() for a in np.meshgrid(off, off, indexing="ij"))
        window = len(WJ)

    out = np.empty(M)
    every = np.arange(len(part))
    c = max(1, _CHUNK // (R * max(len(part), window)))           # rows per block: either pass fits
    for s in range(0, M, c):
        e  = min(s + c, M)
        da = dT[:, s:e, None]                                       # (R, c, 1)
        sc = _pair_scores(da, dB, m, v)                             # (c, B)
        if penB.any():
            sc += penB
        j = _argmin_apart(sc, P[s:e], PB, every)
        best = sc[np.arange(e - s), j]
        if k > 1:
            # Refine: every grid point within k - 1 lines of the best partner
            q  = part[j]
            jj = J[q, None] + WJ
            ii = I[q, None] + WI
            ok = (jj >= 0) & (jj < rows) & (ii >= 0) & (ii < cols)
            w  = np.full(jj.shape, -1)
            w[ok] = cell[jj[ok] * cols + ii[ok]]
            ok = w >= 0
            w  = np.where(ok, w, 0)
            near = _pair_scores(da, dT[:, w], m, v)                 # (c, W)
            near += pen[w]
            near[~ok] = np.inf
            j = _argmin_apart(near, P[s:e], P, w)
            best = np.minimum(best, near[np.arange(e - s), j])
        out[s:e] = best
    return out


def _argmin_apart(sc: np.ndarray, A: np.ndarray, B: np.ndarray, cols: np.ndarray) -> np.ndarray:
    """Per row of ``sc``, the column of the lowest score whose point is at
    least ``MIN_SEPARATION_CM`` from the row's point.

    Args:
        sc:   ``(c, W)`` scores, changed in place: a column found too close
              is set to ``inf``.
        A:    ``(c, 2)`` the rows' points.
        B:    The candidate points, indexed by ``cols``.
        cols: Index of each column's point in ``B``: ``(W,)`` for the same
              columns in every row, ``(c, W)`` for each row its own.

    Returns:
        ``(c,)`` column indices (any column of a row with no finite score).
    """
    j = sc.argmin(axis=1)
    todo = np.arange(len(sc))
    while len(todo):
        q = cols[j[todo]] if cols.ndim == 1 else cols[todo, j[todo]]
        close = np.hypot(*(A[todo] - B[q]).T) < MIN_SEPARATION_CM - _TOL
        close &= np.isfinite(sc[todo, j[todo]])
        todo = todo[close]
        sc[todo, j[todo]] = np.inf
        j[todo] = sc[todo].argmin(axis=1)
    return j


def _join(items: Sequence[str]) -> str:
    """``"A"``, ``"A and B"``, ``"A, B and C"``."""
    items = list(items)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _is(items: Sequence) -> str:
    """The verb for a list of one or more things."""
    return "is" if len(items) == 1 else "are"


__all__ = [
    "MAX_RANGE_CM", "MIN_SEPARATION_CM", "BLOCKED_PENALTY", "SPREAD_CM", "DEFAULT_MARGIN_CM",
    "EVAL_STEP_CM", "MIN_POINTS", "MAX_POINTS", "SCORE_TIP", "InsideFn", "HearsFn",
    "grid", "margin_box", "candidates", "too_close", "score", "arena_score", "receiver_errors",
    "search", "PointCheck", "check_points", "quality_map", "goodness",
]
