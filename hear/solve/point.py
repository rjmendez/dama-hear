#!/usr/bin/env python3
"""Position of a STATIONARY source from arrival times. A muzzle blast, a firework, a backfire.

The sound leaves one point at one instant and spreads spherically, so a node at P hears it at

    t(P) = t0 + |s - P| / c

Four unknowns collapse to two: t0 cancels in TDoA, so N nodes give N-1 equations for the 2D
position. Three nodes determine it exactly and the residual is ~0 BY CONSTRUCTION; four is where
a residual starts carrying information (docs/findings-2026-09-05.md:46-48).

⚠️DO NOT APPLY THIS TO A SUPERSONIC CRACK. The crack radiates off the bullet's Mach cone, not
from the muzzle. For M855 at ~900 m/s and 23 degC the cone half-angle is 22.6 deg, so the crack
arrives 67.4 deg off the trajectory (docs/findings-2026-09-05.md:42-43). Fitting a point source
to it returns a confident position that is wrong by that angle. The caller must gate on a source
class; `source_class` is a required argument for that reason and CONE_CLASSES is refused
outright. What the array actually heard on 2026-09-05 was 92 cracks and 0 blast-only events
(docs/findings-2026-09-05.md:36) -- the gate is not hypothetical. Cost of ignoring it, run in
tests/test_point.py on a 400 m array: 145 m off at rms 299 ms with four nodes, and 302 m off at
rms 0.00000 ms with three, where nothing in the result says so.

⚠️COLLINEAR NODES CANNOT PLACE A SOURCE. Reflect the source across the line the nodes sit on and
every range is unchanged, so both fit identically at zero residual. That is the same degeneracy
as the measured "offset is unobservable from same-side nodes" (+/-6 m moved the TDoAs by
0.000 ms, docs/findings-2026-09-05.md:44-46), rotated. `solve` returns a verdict there and
refuses to quote a coordinate.

The solver is 2D: positions may be 2- or 3-vectors and are sliced [:2], matching
hear/solve/shockwave.py:88.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy.optimize import least_squares

from . import placement as PL
from . import shockwave as SW

# Model selection lives here and only here; hear/backend/pipeline.py imports these rather than
# keeping its own list. JUDGEMENT: the vocabulary is ours, the split between the two is physics.
POINT_CLASSES: frozenset = frozenset({"point", "blast", "muzzle", "firework", "backfire", "impact"})
CONE_CLASSES: frozenset = frozenset({"crack", "shock", "supersonic"})


def is_point_source(source_class: str) -> bool:
    """True for a spherical radiator, False for a Mach cone. Refuses to guess at anything else."""
    if source_class in POINT_CLASSES:
        return True
    if source_class in CONE_CLASSES:
        return False
    raise ValueError("unknown source class %r" % (source_class,))


def _residual(s: np.ndarray, P: np.ndarray, t: np.ndarray, c: float) -> np.ndarray:
    """TDoA residual with t0 marginalised out, not differenced against node 0.

    Subtracting the mean IS the marginalisation -- it removes the direction a common time shift
    moves, exactly as placement.dop()'s (I - 11'/N) does -- so the answer cannot depend on which
    node the caller listed first.
    """
    r = t - np.linalg.norm(s[None, :] - P, axis=1) / c
    return r - r.mean()


def _grid_seed(P: np.ndarray, t: np.ndarray, c: float,
               margin_m: float, step_m: float) -> np.ndarray:
    """Coarse global scan. The TDoA cost is non-convex and a single Gauss-Newton from the centroid
    lands in a local minimum for sources outside the array -- shockwave.solve made the same call
    (shockwave.py:91-95)."""
    lo, hi = P.min(axis=0) - margin_m, P.max(axis=0) + margin_m
    xs = np.arange(lo[0], hi[0] + step_m * 0.5, step_m)
    ys = np.arange(lo[1], hi[1] + step_m * 0.5, step_m)
    gx, gy = np.meshgrid(xs, ys)
    S = np.column_stack([gx.ravel(), gy.ravel()])
    d = np.linalg.norm(S[:, None, :] - P[None, :, :], axis=2) / c
    r = t[None, :] - d
    r = r - r.mean(axis=1, keepdims=True)
    return S[int(np.argmin((r * r).sum(axis=1)))]


def solve(positions: Sequence, arrivals: Sequence[float], source_class: str,
          temp_c: float = 20.0, search_margin_m: float = 500.0,
          grid_step_m: float = 10.0) -> Dict:
    """Fit a stationary source position to arrival times.

    `positions` are east/north metres in the same local frame as hear/solve/shockwave.solve;
    3-vectors are sliced [:2]. `arrivals` are absolute seconds on a shared clock.

    Raises on a caller error -- too few nodes, mismatched lengths, non-finite input, or a source
    class that radiates off a cone. Returns `east_m: None` and `position_observable: False` with
    an UNOBSERVABLE note when the nodes are collinear, because the fit has a mirror twin across
    the node line that matches it to the last digit.
    """
    if len(positions) != len(arrivals) or len(positions) < 3:
        raise ValueError("need >= 3 nodes with matching arrival times")
    if source_class in CONE_CLASSES:
        raise ValueError("source class %r radiates off a Mach cone, not from a point: "
                         "use shockwave.solve" % (source_class,))
    if source_class not in POINT_CLASSES:
        raise ValueError("unknown source class %r" % (source_class,))
    P = np.asarray([np.asarray(p, float)[:2] for p in positions], float)
    t = np.asarray(arrivals, float)
    # Recentre before anything numerical touches it. `arrivals` are absolute epoch seconds
    # (~1.76e9), where float64 spacing is 2.4e-7 s. least_squares' 2-point Jacobian perturbs a
    # position by ~1.5e-8*|x|, worth ~4e-9 s of range -- entirely lost in that rounding, so the
    # Jacobian comes back EXACTLY zero and the optimiser exits on its first iteration with the
    # raw grid cell. Measured: 0.001 m error at t0=1e3, 6.58 m at real UTC, snapped to the grid.
    # shockwave.solve is immune only because it differences t[1:] - t[0] before it does anything.
    t_ref = float(t[0])
    t = t - t_ref
    if not (np.all(np.isfinite(P)) and np.all(np.isfinite(t))):
        raise ValueError("non-finite node position or arrival time")

    c = SW.sound_speed(temp_c)
    n_eq = len(P) - 1
    seed = _grid_seed(P, t, c, search_margin_m, grid_step_m)
    s = least_squares(_residual, seed, args=(P, t, c)).x
    res = _residual(s, P, t, c)
    rms_ms = math.sqrt(float(res @ res) / n_eq) * 1000.0
    t0 = t_ref + float(np.mean(t - np.linalg.norm(s[None, :] - P, axis=1) / c))
    lin = PL.linearity(P)
    obs = lin >= PL.COLLINEAR_LINEARITY
    return {
        "east_m": float(s[0]) if obs else None,
        "north_m": float(s[1]) if obs else None,
        "position_observable": obs,
        "t0_utc_s": t0 if obs else None,
        "range_m": float(np.linalg.norm(s - P.mean(axis=0))) if obs else None,
        "rms_residual_ms": rms_ms,
        "residual_is_meaningful": n_eq > 2,
        "n_nodes": len(P), "n_equations": n_eq,
        "sound_speed_mps": c,
        "dop": PL.dop(P, s)["dop"],
        "linearity": lin,
        "source_class": source_class,
        "note": None if obs else
                "nodes are collinear (linearity %.4f): position is UNOBSERVABLE -- the source "
                "and its mirror reflected across the node line fit identically at zero "
                "residual." % lin,
    }
