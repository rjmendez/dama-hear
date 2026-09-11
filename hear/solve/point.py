#!/usr/bin/env python3
"""Position of a STATIONARY source from arrival times. A muzzle blast, a firework, a backfire.

The sound leaves one point at one instant and spreads spherically, so a node at P hears it at

    t(P) = t0 + |s - P| / c

Four unknowns collapse to three: t0 cancels in TDoA, so N nodes give N-1 equations for the 3D
position. FOUR nodes determine it exactly and the residual is ~0 BY CONSTRUCTION; five is where a
residual starts carrying information. (In the 2D era those numbers were three and four --
docs/findings-2026-09-05.md:46-48 -- and adding the third unknown moved both up by one.)

⚠️DO NOT APPLY THIS TO A SUPERSONIC CRACK. The crack radiates off the bullet's Mach cone, not
from the muzzle. For M855 at ~900 m/s and 23 degC the cone half-angle is 22.6 deg, so the crack
arrives 67.4 deg off the trajectory (docs/findings-2026-09-05.md:42-43). Fitting a point source
to it returns a confident position that is wrong by that angle. The caller must gate on a source
class; `source_class` is a required argument for that reason and CONE_CLASSES is refused
outright. What the array actually heard on 2026-09-05 was 92 cracks and 0 blast-only events
(docs/findings-2026-09-05.md:36) -- the gate is not hypothetical. Cost of ignoring it, run in
tests/test_point.py on a 400 m array: 134 m off at rms 188 ms with four nodes, and 145 m off at
rms 0.00001 ms with three, where nothing in the result says so.

⚠️COLLINEAR NODES CANNOT PLACE A SOURCE. Reflect the source across the line the nodes sit on and
every range is unchanged, so both fit identically at zero residual. That is the same degeneracy
as the measured "offset is unobservable from same-side nodes" (+/-6 m moved the TDoAs by
0.000 ms, docs/findings-2026-09-05.md:44-46), rotated. `solve` returns a verdict there and
refuses to quote a coordinate.

⚠️COPLANAR NODES CANNOT PLACE A SOURCE IN HEIGHT. Reflect the source through the plane the nodes
lie in and every range is unchanged, so both fit identically -- the same degeneracy as the
collinear case, one dimension up. Every node at ground level IS that case, and it is the normal
case for this project rather than an exotic one: nyquist and mach sit on one house. `up_m` is
still returned, with `up_observable: False`, `up_mirror_m` giving the twin, and -- when the node
plane is near-horizontal, so that "the other solution is underground" is a real statement rather
than a coordinate accident -- `up_preferred_reason` naming which one survives physical sense.
Callers that need height must break the plane, not the timing.

DIMENSION. Positions are (east, north, up) metres in one local frame; 2-vectors are read as up=0.
Use hear.geodesy to get there from lat/lon/height, and pass the ELLIPSOID height -- hMSL carries
the geoid undulation, which is not a distance. This module no longer slices [:2] anywhere.

UNCERTAINTY. `sigmas` is optional and, given, makes the fit a weighted least squares: a receiver
that states a worse clock votes less, and `chi2_reduced` puts the residual on a scale where it
can be compared against the uncertainties that produced it instead of read as raw milliseconds.
Omitted, every arithmetic operation below is the one that was there before the argument existed.

⚠️chi2 HAS A NUMERICAL FLOOR AND PPS IS UNDER IT. This solver leaves ~5.07e-08 s rms (17 um of
range) on arrivals that are EXACT by construction -- that is least_squares converging, not the
data disagreeing, and it is unchanged from before this module took a sigma. Squared against a
stated sigma it stops being negligible: chi2 is 6.8e-07 at the corpus's 106 us phone median and
4.81 at a 40 ns PPS figure, on a fit with nothing wrong with it. Below about 88 ns of stated
sigma the chi-square is measuring this module and not the array. Pinned in
tests/test_point.py::test_chi2_has_a_numerical_floor so the number cannot rot quietly.

⚠️A SIGMA IS A VARIANCE AND NOT A BIAS -- see shockwave.py's header for the measured numbers.
Weighting widens a receiver's allowance; it does not move a systematic capture-path offset, which
displaces the fit rather than inflating its residual. A receiver may be weighted on its clock and
must STILL be refused on an unmeasured path delay. That is an ADMISSION gate and it is not here.
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


def _residual(s: np.ndarray, P: np.ndarray, t: np.ndarray, c: float,
              w: Optional[np.ndarray] = None) -> np.ndarray:
    """TDoA residual with t0 marginalised out, not differenced against node 0.

    Subtracting the mean IS the marginalisation -- it removes the direction a common time shift
    moves, exactly as placement.dop()'s (I - 11'/N) does -- so the answer cannot depend on which
    node the caller listed first.

    `w` is the relative weight vector from shockwave.sigma_weights (1 at the best-stated receiver,
    smaller elsewhere), or None. Weighted, the marginalised t0 is the WEIGHTED mean -- profiling
    t0 out of sum q_i (r_i - t0)^2 gives sum(q r)/sum(q), and using the plain mean there would
    leave a t0 the cost does not actually want. `w is None` takes the original expression
    untouched rather than multiplying by a vector of ones, so the unweighted answer is identical
    by construction and not by the grace of ddot rounding the same way as a pairwise sum.
    """
    r = t - np.linalg.norm(s[None, :] - P, axis=1) / c
    if w is None:
        return r - r.mean()
    q = w * w
    return w * (r - float(q @ r) / float(q.sum()))


def _jacobian(s: np.ndarray, P: np.ndarray, t: np.ndarray, c: float,
              w: Optional[np.ndarray] = None) -> np.ndarray:
    """Exact derivative of _residual. Supplied rather than finite-differenced.

    least_squares' 2-point Jacobian perturbs a coordinate by ~1.5e-8*|x|, which at a few hundred
    metres of range is worth a few nanoseconds -- and the third unknown made that fragile in a way
    two never were. With a coplanar array the cost barely changes as the source moves off the node
    plane, so the true gradient in that direction is already tiny; differencing it against float64
    noise returned something indistinguishable from zero and the optimiser stopped ON the seed
    plane, reporting rms 0.019 ms where the true source scored exactly 0.0.

    The derivative itself is elementary -- d|s-P|/ds is the unit vector from P to s -- and the
    mean-subtraction in _residual is linear, so it carries straight through to the Jacobian.
    """
    d = s[None, :] - P
    n = np.linalg.norm(d, axis=1)
    J = -d / (n[:, None] * c)
    if w is None:
        return J - J.mean(axis=0, keepdims=True)
    q = w * w
    return w[:, None] * (J - (q @ J)[None, :] / float(q.sum()))


def _grid_seed(P: np.ndarray, t: np.ndarray, c: float,
               margin_m: float, step_m: float, z_levels: int = 5,
               w: Optional[np.ndarray] = None) -> np.ndarray:
    """Coarse global scan. The TDoA cost is non-convex and a single Gauss-Newton from the centroid
    lands in a local minimum for sources outside the array -- shockwave.solve made the same call
    (shockwave.py:91-95).

    The horizontal grid is the expensive axis and keeps its full resolution. Height gets only a
    few levels on purpose: with near-coplanar nodes the cost surface is almost flat in z, so a
    fine z grid buys nothing a Gauss-Newton step will not find, and cubing the grid to discover
    that would be a waste. The levels span the node vertical extent plus the same margin, so a
    source above or below the array is still inside the search.
    """
    lo, hi = P.min(axis=0) - margin_m, P.max(axis=0) + margin_m
    xs = np.arange(lo[0], hi[0] + step_m * 0.5, step_m)
    ys = np.arange(lo[1], hi[1] + step_m * 0.5, step_m)
    zs = (np.linspace(lo[2], hi[2], z_levels) if hi[2] > lo[2]
          else np.array([float(P[:, 2].mean())]))
    gx, gy, gz = np.meshgrid(xs, ys, zs, indexing="ij")
    S = np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()])
    d = np.linalg.norm(S[:, None, :] - P[None, :, :], axis=2) / c
    r = t[None, :] - d
    if w is None:
        r = r - r.mean(axis=1, keepdims=True)
        return S[int(np.argmin((r * r).sum(axis=1)))]
    # The seed has to score cells with the same cost the refine minimises, or the coarse scan
    # hands the optimiser a cell that is not the weighted minimum at all.
    q = w * w
    f = (r - (r @ q)[:, None] / float(q.sum())) * w
    return S[int(np.argmin((f * f).sum(axis=1)))]


def _mirror_through_plane(s: np.ndarray, P: np.ndarray, normal: Sequence[float]) -> np.ndarray:
    """The twin solution: reflect the source through the node plane.

    When the nodes are coplanar this point has IDENTICAL ranges to every node, so it fits the
    arrival times to the last digit. It is not a worse answer that the optimiser missed; it is the
    same answer, and the data cannot choose between them.
    """
    n = np.asarray(normal, float)
    n = n / np.linalg.norm(n)
    return s - 2.0 * float((s - P.mean(axis=0)) @ n) * n


def _report(s, P, t, c, n_eq, n_unk, rms_ms, t0, at_bound, source_class,
            search_margin_m, fixed_up_m, res=None, sig=None, w=None):
    """Everything the solver says about a fit. One definition, shared by the 3-unknown and
    the declared-height paths, so the two cannot disagree about what a result means.
    """
    lin = PL.linearity(P)
    obs = lin >= PL.COLLINEAR_LINEARITY

    # CHI-SQUARE, not milliseconds. `res` is already (r - weighted mean)/rho with rho normalised
    # to the smallest stated sigma, so dividing by that sigma turns it into (r - t0)/sigma_i on
    # both the weighted and the equal-sigma branch -- one expression, no case analysis.
    chi2 = chi2_red = n_eff = None
    n_cnt = len(P)
    dof = n_eq - n_unk
    if sig is not None and res is not None:
        ref = float(np.min(sig))
        chi2 = float(res @ res) / (ref * ref)
        chi2_red = chi2 / dof if dof > 0 else None
        n_eff = SW.effective_n(w, len(P))
        n_cnt = SW.counting_n(w, len(P))
    # ⚠️THE POINT OF THE WHOLE CHANGE. An exactly-determined fit has a residual of ~0 BY
    # CONSTRUCTION -- it is not evidence, and reading it as such is how a 3-node fit looked
    # clean. n_eq > n_unk was already the right test on the RAW count; what it could not see is
    # a receiver whose sigma is so large that it contributes nothing, which removes an equation
    # without removing a node. n_cnt - 1 is that same count over the receivers still carrying a
    # vote, and it equals n_eq exactly whenever the sigmas are equal or absent.
    meaningful = n_eq > n_unk and n_cnt - 1 > n_unk

    # ⚠️A HUGE SIGMA DOES NOT WIDEN THIS RESULT, IT REMOVES THAT RECEIVER FROM IT. The weight is
    # 1/rho, so sigma -> inf drives both the residual entry and its Jacobian row to zero: the
    # receiver neither pulls the fit nor inflates chi2. That is correct weighted least squares
    # and it is also how a 5-receiver event quietly becomes a 4-receiver one. It is reported
    # rather than refused -- refusing is an admission decision and this module does not make any.
    wnote = None
    degen = None
    if n_eff is not None:
        degen = bool(n_cnt < n_unk + 1)
        if n_cnt < len(P):
            j = int(np.argmax(np.asarray(sig, float)))
            wnote = ("%d of %d receivers carry less than %.2g of a vote and do not count toward "
                     "determinacy (%.2f effective receivers): the worst is index %d at sigma "
                     "%.6g s against a best of %.6g s, relative weight %.3g. A sigma that large "
                     "does not widen this fit, it removes that receiver from it%s"
                     % (len(P) - n_cnt, len(P), SW.WEIGHT_FLOOR, n_eff, j, float(sig[j]),
                        float(np.min(sig)), 1.0 if w is None else float(w[j]),
                        " -- and the position is now under-determined." if degen else "."))

    # Height observability is a SEPARATE question from horizontal observability, and for this
    # project it is usually the one that bites: nodes sitting on the ground are coplanar, and a
    # coplanar array cannot tell a source above the plane from its reflection below it. Both fit
    # to the last digit, so this is not something a better optimiser or a longer capture fixes.
    cop = PL.coplanarity(P)
    d3 = (PL.dop3(P, s) if len(P) >= 4
          else {"hdop": float("inf"), "vdop": float("inf"), "pdop": float("inf"),
                "dof": 0, "singular": True})
    # A DECLARED height is not an observed one. Saying up_observable True here because the number
    # happens to be exact would be the worst possible lie: it is exact because it was assumed.
    up_obs = obs and not cop["coplanar"] and fixed_up_m is None
    mirror = (_mirror_through_plane(s, P, cop["plane_normal"])
              if (obs and fixed_up_m is None) else None)

    # When the node plane is near-horizontal, "the other one is below the array" is a physical
    # statement and not a coordinate accident, so it is worth naming. This is a PREFERENCE from
    # outside the data, never a measurement: up_observable stays False either way.
    up_pref = None
    if obs and not up_obs and mirror is not None and cop["near_horizontal"]:
        hi = s if s[2] >= mirror[2] else mirror
        up_pref = ("the node plane is within 26 deg of horizontal, so the twin at up=%.1f m is "
                   "below it -- preferring up=%.1f m is an assumption that the source is above "
                   "the array, not a measurement" % (min(s[2], mirror[2]), hi[2]))

    return {
        "east_m": float(s[0]) if obs else None,
        "north_m": float(s[1]) if obs else None,
        "up_m": float(s[2]) if obs else None,
        "position_observable": obs,
        "up_observable": up_obs,
        "up_mirror_m": float(mirror[2]) if (mirror is not None and not up_obs) else None,
        "up_preferred_reason": up_pref,
        "t0_utc_s": t0 if obs else None,
        "range_m": float(np.linalg.norm(s - P.mean(axis=0))) if obs else None,
        "ground_range_m": float(np.linalg.norm(s[:2] - P.mean(axis=0)[:2])) if obs else None,
        # ⚠️WEIGHTED milliseconds when sigmas are stated: the entries are divided by the
        # per-receiver sigma relative to the best one, so this is "ms at the best receiver's
        # precision" and not a plain time. chi2_reduced is the figure to compare across events.
        "rms_residual_ms": rms_ms,
        "at_search_bound": at_bound,
        # Three unknowns now, so four nodes give an exact fit whose residual is ~0 by
        # construction. Five is where it starts carrying information -- one more than before.
        "residual_is_meaningful": meaningful,
        "residual_dof": dof,
        "chi2": chi2,
        "chi2_reduced": chi2_red,
        "n_effective_nodes": n_eff,
        "n_counting_nodes": n_cnt,
        "weight_floor": SW.WEIGHT_FLOOR,
        "weights_degenerate": degen,
        "weight_note": wnote,
        "sigma_s": None if sig is None else np.asarray(sig, float).tolist(),
        "relative_weights": (None if sig is None else
                             ([1.0] * len(P) if w is None else np.asarray(w, float).tolist())),
        "n_nodes": len(P), "n_equations": n_eq, "n_unknowns": n_unk,
        "up_assumed_m": float(fixed_up_m) if fixed_up_m is not None else None,
        "sound_speed_mps": c,
        "dop": PL.dop(P, s)["dop"],
        # hdop/vdop reported separately because they are not interchangeable: the vertical is the
        # weak axis of a ground-based array by construction, and a single combined figure hides
        # exactly the component this project keeps getting wrong.
        "hdop": d3["hdop"], "vdop": d3["vdop"], "pdop": d3["pdop"],
        "dop_dof": d3["dof"], "dop_singular": d3["singular"],
        "linearity": lin,
        "planarity_rms_m": cop["planarity_rms_m"],
        "vertical_spread_m": cop["vertical_spread_m"],
        "source_class": source_class,
        "note": (
            "source height was DECLARED as %.2f m, not measured: two unknowns, so three nodes "
            "suffice. Every reported coordinate is conditional on that height being right -- if "
            "the source is 10 m up and this said 0, the horizontal answer absorbs the error."
            % fixed_up_m if (fixed_up_m is not None and obs and not at_bound) else
            "solution is pinned at the edge of the searched region (search_margin_m=%.0f): the "
            "source is outside it, or a point-source model does not fit this data. The "
            "coordinates are the box edge, not a measurement." % search_margin_m
            if at_bound else
            "nodes are collinear (linearity %.4f): position is UNOBSERVABLE -- the source and "
            "its mirror reflected across the node line fit identically at zero residual." % lin
            if not obs else
            "nodes are coplanar (planarity rms %.3f m over %.1f m of vertical spread): HEIGHT is "
            "unobservable -- up=%.1f m and up=%.1f m fit identically. East/north are unaffected. "
            "Breaking the plane, not improving the timing, is what fixes this."
            % (cop["planarity_rms_m"], cop["vertical_spread_m"], s[2], mirror[2])
            if not up_obs else None),
    }


def solve(positions: Sequence, arrivals: Sequence[float], source_class: str,
          temp_c: float = 20.0, search_margin_m: float = 500.0,
          grid_step_m: float = 10.0, fixed_up_m: Optional[float] = None,
          sigmas: Optional[Sequence[float]] = None) -> Dict:
    """Fit a stationary source position to arrival times.

    `positions` are east/north/up metres in one local frame; 2-vectors are read as up=0.
    `arrivals` are absolute seconds on a shared clock.

    `sigmas` is the OPTIONAL per-receiver arrival sigma in SECONDS -- one entry per receiver or
    None for all of them, never a mixture, and never invented for a receiver that states none.

    Raises on a caller error -- too few nodes, mismatched lengths, non-finite input, or a source
    class that radiates off a cone. Returns `east_m: None` and `position_observable: False` with
    an UNOBSERVABLE note when the nodes are collinear, because the fit has a mirror twin across
    the node line that matches it to the last digit.
    """
    # fixed_up_m DECLARES the source height instead of solving for it: two unknowns, so three
    # nodes suffice again. This exists because going 3D raised the minimum node count from three
    # to four, and a lot of real arrays sit at three -- two PPS nodes plus a phone, say. Assuming
    # the source is on the ground is often true and always a CHOICE; the point is that it is now
    # written in the call and echoed in the result, rather than being smuggled in by a solver that
    # silently ignores the third axis. That was the old behaviour and it is what this whole change
    # set exists to remove.
    n_unk = 2 if fixed_up_m is not None else 3
    min_nodes = n_unk + 1

    # Two distinct faults, reported distinctly. Folding them into one message made a length
    # mismatch of 4 positions against 3 arrivals say "need >= 4 nodes ... got 4", which sends the
    # reader to count nodes they already have enough of.
    if len(positions) != len(arrivals):
        raise ValueError("positions and arrivals must be the same length: got %d and %d"
                         % (len(positions), len(arrivals)))
    sig, w = SW.sigma_weights(sigmas, len(positions))
    if len(positions) < min_nodes:
        raise ValueError(
            "need >= %d nodes for a %dD fit: t0 cancels, so N nodes give N-1 equations for %d "
            "unknowns; got %d%s"
            % (min_nodes, n_unk, n_unk, len(positions),
               "" if fixed_up_m is not None else
               ". Pass fixed_up_m to declare the source height and solve in 2D instead"))
    if source_class in CONE_CLASSES:
        raise ValueError("source class %r radiates off a Mach cone, not from a point: "
                         "use shockwave.solve" % (source_class,))
    if source_class not in POINT_CLASSES:
        raise ValueError("unknown source class %r" % (source_class,))
    P = PL._as3(positions)
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
    seed = _grid_seed(P, t, c, search_margin_m, grid_step_m, w=w)

    # BOUND THE REFINE TO THE REGION THAT WAS SEARCHED. Unbounded, the third unknown gave the
    # optimiser a nearly-flat direction to slide along whenever the nodes are coplanar -- the
    # cost barely changes as the source moves perpendicular to the node plane -- and inconsistent
    # arrivals (a Mach cone fed in as a blast) sent it to 33 km below ground at a confident-looking
    # 138 ms. That is not a fit; it is a runaway, and returning coordinates from outside the box
    # the grid seed scanned is indefensible whatever the residual says.
    lo = P.min(axis=0) - search_margin_m
    hi = P.max(axis=0) + search_margin_m
    # THE NODE PLANE IS A STATIONARY POINT OF THE COST, BY SYMMETRY. Every range depends on the
    # out-of-plane offset only through its square, so the gradient in that direction is EXACTLY
    # zero on the plane -- look at _jacobian: with s and every P in one plane, that column is all
    # zeros before the mean is even subtracted. A seed that lands on the plane therefore cannot
    # leave it, and the optimiser returns the plane while reporting convergence. Measured on a
    # 100 m square lifted 7.5 m over a ground-level source: it stopped at rms 0.019 ms and 2.2 m
    # of horizontal error where the true source scores exactly 0.0.
    #
    # So refine from the seed AND from seeds nudged either side of the node plane, and keep the
    # best. On a coplanar array the two sides are mirror twins and score identically, which is the
    # ambiguity `up_observable` reports; on a broken plane they are not, and this is what finds
    # the real one.
    # With the height declared there are genuinely two unknowns, so the optimiser is given two.
    # Equal bounds were tried first and least_squares rejects them ("each lower bound must be
    # strictly less than each upper"); an epsilon-wide window would have worked numerically and
    # lied about the dimensionality. These wrappers reuse _residual and _jacobian unchanged, so
    # the 2D and 3D paths cannot drift apart -- the Jacobian simply loses its third column.
    if fixed_up_m is not None:
        u0 = float(fixed_up_m)
        _lift = lambda s2: np.array([s2[0], s2[1], u0])
        # ⚠️CLIPPED, exactly as the 3D branch below clips its seeds. `_grid_seed` builds its axes
        # with np.arange(lo, hi + step/2, step), which OVERSHOOTS `hi` by up to step/2 -- on a
        # 17 m array with the default 500 m margin and 10 m step that is 3.4 m past the east
        # bound. When the coarse minimum happens to land in that last cell, least_squares raises
        # "Initial guess is outside of provided bounds" and the whole event is refused. It is
        # data-dependent, so it fires on some temperatures/geometries and not others: found by
        # tools/hear_tdoa.py at --temp-c 0 on a layout that solves cleanly at 20.
        fit = least_squares(lambda s2, *a: _residual(_lift(s2), *a),
                            np.clip(seed[:2], lo[:2], hi[:2]),
                            jac=lambda s2, *a: _jacobian(_lift(s2), *a)[:, :2],
                            args=(P, t, c, w), bounds=(lo[:2], hi[:2]),
                            xtol=1e-14, ftol=1e-14, gtol=1e-14)
        s = _lift(fit.x)
        _chk = slice(0, 2)
        at_bound = bool(np.any(np.isclose(s[:2], lo[:2], atol=1e-6))
                        or np.any(np.isclose(s[:2], hi[:2], atol=1e-6)))
        res = _residual(s, P, t, c, w)
        rms_ms = math.sqrt(float(res @ res) / n_eq) * 1000.0
        t0 = t_ref + float(np.mean(t - np.linalg.norm(s[None, :] - P, axis=1) / c))
        return _report(s, P, t, c, n_eq, n_unk, rms_ms, t0, at_bound, source_class,
                       search_margin_m, fixed_up_m, res, sig, w)
    nrm = np.asarray(PL.coplanarity(P)["plane_normal"], float)
    span = float(np.linalg.norm(P.max(axis=0) - P.min(axis=0)))
    nudge = max(0.05 * span, 1.0)
    cands = [seed, seed + nudge * nrm, seed - nudge * nrm]
    best = None
    for s0 in cands:
        f = least_squares(_residual, np.clip(s0, lo, hi), jac=_jacobian, args=(P, t, c, w),
                          bounds=(lo, hi), xtol=1e-14, ftol=1e-14, gtol=1e-14)
        if best is None or f.cost < best.cost:
            best = f
    fit = best
    s = fit.x
    # Pinned means the best fit is outside the searched region: either the source really is, and
    # the caller should widen search_margin_m, or the model is wrong for this data. Either way it
    # is not a position, and saying so beats quoting the edge of a box as a measurement.
    _chk = slice(0, 2) if fixed_up_m is not None else slice(0, 3)
    at_bound = bool(np.any(np.isclose(s[_chk], lo[_chk], atol=1e-6))
                    or np.any(np.isclose(s[_chk], hi[_chk], atol=1e-6)))
    res = _residual(s, P, t, c, w)
    rms_ms = math.sqrt(float(res @ res) / n_eq) * 1000.0
    t0 = t_ref + float(np.mean(t - np.linalg.norm(s[None, :] - P, axis=1) / c))
    return _report(s, P, t, c, n_eq, n_unk, rms_ms, t0, at_bound, source_class,
                   search_margin_m, fixed_up_m, res, sig, w)
