#!/usr/bin/env python3
"""Trajectory of a supersonic projectile from shock arrival times.

A supersonic round drags a conical shock. The sound a node hears does NOT come from the shooter:
it radiates off the bullet's own Mach cone, up to ~68 deg away from the trajectory for M855 at
these speeds. Solving for the shooter by treating the crack as a point source at the muzzle is
wrong by that angle.

The shock reaches a node at

    t(P) = t0 + a/V + m * sqrt(V^2 - c^2)/(V*c)

Derivation, because this coefficient was wrong for a long time and nothing caught it: the bullet
reaches along-track x at x/V, sound then covers sqrt((a-x)^2 + m^2)/c. Minimising over the emission
point x gives a-x = c*m/sqrt(V^2-c^2), and substituting back collapses to the form above. The
sanity check that should have been here from the start is that a shock front cannot arrive LATER
than plain sound from closest approach, m/c -- the previous coefficient implied a front moving at
205 m/s and solve() could not see it, because solve() reused the same k and the round-trip agreed
with itself.

where `a` is the along-track distance to P's closest approach and `m` is the miss distance. With
V known from the round, a 2D trajectory has three unknowns -- bearing, perpendicular offset, and
t0 -- and t0 cancels in TDoA. So N nodes give N-1 equations for 2 unknowns: three nodes exactly
determine it and four begin to over-determine it.

⚠️OBSERVABILITY. When every node lies on the SAME side of the trajectory, shifting the track
sideways adds the same delay to all of them and cancels exactly in TDoA. The perpendicular offset
is then UNOBSERVABLE: every parallel track fits the arrivals identically, at zero residual.
Measured, moving a track +/-6 m past three same-side nodes changed the TDoAs by 0.000 ms; past
straddling nodes it changed them by up to 117 ms.

This is why `solve()` returns an observability verdict and refuses to quote an offset when the
nodes do not straddle. A solver that returns a confident number in a degenerate configuration is
how this project twice produced a fit that disagreed with the operator's own eyes.

⚠️A SIGMA IS A VARIANCE AND NOT A BIAS, and the two gates that follow from that are separate.
`sigmas` lets a receiver that states a worse clock vote less; it does NOT remove a systematic
offset. An uncalibrated capture path MOVES the fit rather than widening it, and no weight
recovers that. Measured, from dama-gotchi's acoustic_latency_calibration.json (the same file
nodeclass.py:546 cites): myasshurts 13_122_000 ns = 13.122 ms = 4.51 m at 343.42 m/s, and
financialdistress 293_499_000 ns = 293.499 ms. Two of the three phones that detect are in that
file at all; fancyantsy is not in it. So a receiver may be weighted on its clock and must STILL
be refused on an unmeasured path delay. That refusal is an ADMISSION question, it does not live
here, and a sigma argument is not permission to skip it.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np


def sound_speed(temp_c: float) -> float:
    """Metres per second. 0.606 per degC is ~183 us over 35 m -- not negligible at these budgets."""
    return 331.3 + 0.606 * float(temp_c)


def mach_angle_deg(v_mps: float, c: float) -> Optional[float]:
    """Cone half-angle. None when subsonic: there is no shock and no crack to hear."""
    m = float(v_mps) / float(c)
    if m <= 1.0:
        return None
    return math.degrees(math.asin(1.0 / m))


def sigma_weights(sigmas: Optional[Sequence[float]], n: int):
    """(sigma_s, relative_weight) from per-receiver ARRIVAL sigmas in SECONDS, or (None, None).

    Shared with point.py so the two solvers cannot disagree about what a stated sigma means.

    ⚠️NORMALISED TO THE SMALLEST STATED SIGMA, NOT THE MEAN, AND THAT IS LOAD-BEARING. x/x is
    exactly 1.0 for every finite non-zero float, so equal sigmas give a weight vector of exactly
    1.0 and `None` is returned in its place -- which is what lets the caller run the ORIGINAL
    unweighted arithmetic and be bit-identical to the day before this argument existed. The mean
    of N equal floats is not exactly that float (three copies of 0.1 sum to 0.30000000000000004),
    so a mean-normalised weight would be 1.0 +/- an ulp and the equal-sigma result would drift.

    ⚠️ABSENT IS NOT ZERO AND IT IS NOT A CLASS DEFAULT. A partially-stated vector RAISES rather
    than being filled in: filling it is precisely how nodeclass's worst-tier class constant came
    to override what a detection said about itself. A caller that means "use the class figure for
    this receiver" has to write the figure down.
    """
    if sigmas is None:
        return None, None
    if len(sigmas) != n:
        raise ValueError("sigmas must have one entry per receiver: got %d for %d receivers"
                         % (len(sigmas), n))
    missing = [i for i, v in enumerate(sigmas) if v is None]
    if missing:
        raise ValueError(
            "sigma stated for %d of %d receivers, absent at index %s: this solver will not "
            "invent one. Absent is not zero and it is not a class figure -- state every sigma, "
            "or pass sigmas=None and weight nothing" % (n - len(missing), n, missing))
    s = np.asarray(sigmas, float)
    if not (np.all(np.isfinite(s)) and np.all(s > 0.0)):
        raise ValueError("every sigma must be finite and > 0 seconds: got %r" % (s.tolist(),))
    w = s.min() / s
    return s, (None if bool(np.all(w == 1.0)) else w)


# JUDGEMENT, not a measurement: a receiver counts toward DETERMINACY when its stated sigma is
# within 10x the best stated sigma in the group. Some floor is unavoidable -- a weight is
# continuous and determinacy is an integer -- and the alternatives were worse. Kish's effective
# count alone cannot carry the test: four 106 us receivers plus the corpus's own 3.23 s maximum
# score 4.000000002, so `n_eff - 1 > 3` is TRUE on float noise and the residual of a system whose
# fifth receiver contributes 3.3e-05 of a vote reads as evidence. Both the floor and every
# relative weight are published, so this judgement can be second-guessed from the result alone.
WEIGHT_FLOOR: float = 0.1


def effective_n(w, n: int) -> float:
    """Kish effective count for relative weights `w` (None meaning all equal).

    (sum q)^2 / sum(q^2) over q = w^2. Equal weights give exactly `n`; one receiver carrying all
    the weight gives 1. Continuous and threshold-free, which is why it is REPORTED -- but see
    WEIGHT_FLOOR for why the determinacy boolean cannot be read off it.
    """
    if w is None:
        return float(n)
    q = np.asarray(w, float) ** 2
    return float(q.sum() ** 2 / (q * q).sum())


def counting_n(w, n: int) -> int:
    """How many receivers carry at least WEIGHT_FLOOR of a vote. Exactly `n` for equal sigmas."""
    if w is None:
        return int(n)
    return int(np.count_nonzero(np.asarray(w, float) >= WEIGHT_FLOOR))


def _axes(bearing_rad: float):
    u = np.array([math.sin(bearing_rad), math.cos(bearing_rad)])      # along-track
    n = np.array([math.cos(bearing_rad), -math.sin(bearing_rad)])     # right-hand normal
    return u, n


def shock_time(P, bearing_rad: float, offset_m: float, v_mps: float, c: float) -> float:
    """Arrival of the shock at P, relative to t0. Signed offset is measured along the normal."""
    u, n = _axes(bearing_rad)
    w = np.asarray(P, float)[:2] - n * float(offset_m)
    a = float(np.dot(w, u))
    m = abs(float(np.dot(w, n)))
    k = math.sqrt(v_mps * v_mps - c * c) / (v_mps * c)
    return a / v_mps + m * k


def straddles(positions, bearing_rad: float, offset_m: float) -> bool:
    """Do the nodes sit on BOTH sides of the track? If not, the offset cannot be recovered."""
    _, n = _axes(bearing_rad)
    s = [float(np.dot(np.asarray(P, float)[:2] - n * offset_m, n)) for P in positions]
    return any(x > 0 for x in s) and any(x < 0 for x in s)


def solve(positions: Sequence, arrivals: Sequence[float], v_mps: float = 900.0,
          temp_c: float = 20.0, bearing_step_deg: float = 0.25,
          offset_range_m: float = 150.0, offset_step_m: float = 0.25,
          sigmas: Optional[Sequence[float]] = None) -> Dict:
    """Fit bearing and offset to shock arrival times.

    `positions` are 2D east/north metres in a common local frame; `arrivals` are absolute seconds
    on a shared clock (PPS-disciplined per node -- the radio never carries time).

    `sigmas` is the OPTIONAL per-receiver arrival sigma in SECONDS -- one entry per receiver or
    None for all of them, never a mixture. Given, the fit is a weighted least squares and
    `chi2`/`chi2_reduced` report the residual against the uncertainties that produced it.

    Returns `offset_m: None` and `offset_observable: False` when the nodes do not straddle the
    fitted track, because in that geometry any offset fits equally well.
    """
    if len(positions) != len(arrivals) or len(positions) < 3:
        raise ValueError("need >= 3 nodes with matching arrival times")
    c = sound_speed(temp_c)
    if v_mps <= c:
        raise ValueError("v %.1f is subsonic at c %.1f: no shock exists" % (v_mps, c))
    P = [np.asarray(p, float)[:2] for p in positions]
    t = np.asarray(arrivals, float)
    sig, wrel = sigma_weights(sigmas, len(P))
    # ⚠️THE REFERENCE RECEIVER IS THE BEST-STATED ONE, NOT LISTED-FIRST. This form differences
    # against one receiver instead of marginalising t0 out (point.py:_residual does the latter),
    # so the reference's clock error enters EVERY equation and no per-equation weight can remove
    # it. Referencing on the worst clock in the group is how a 3.23 s phone sigma would poison a
    # fit that the weights then report as clean. argmin returns 0 on ties and sigma_weights
    # returns None for equal sigmas, so an unweighted call still differences against index 0.
    ref = 0 if wrel is None else int(np.argmin(sig))
    idx = [i for i in range(len(P)) if i != ref]
    dt_obs = t[idx] - t[ref]
    # Per-EQUATION sigma: a difference of two receivers has variance sigma_i^2 + sigma_ref^2.
    # ⚠️DIAGONAL ONLY. The true covariance of the differenced system also carries sigma_ref^2 in
    # every off-diagonal, because every equation shares the reference. Dropping it is an
    # approximation, and it is taken deliberately: the exact GLS weight is (I + J)^-1 even when
    # the sigmas are all equal, so an exact solver could not reproduce today's numbers and
    # bit-identity at equal sigmas is the property this change is judged on. The approximation
    # under-weights the reference's contribution to the correlated part; the clean fix is to
    # marginalise t0 the way point.py does, which is a change to the unweighted answer too.
    wp = None
    if wrel is not None:
        rho = sig / sig.min()
        rho_pair = np.sqrt((rho[idx] ** 2 + rho[ref] ** 2) / 2.0)
        wp = 1.0 / rho_pair
    # Vectorised over offset: the along-track term is INDEPENDENT of offset (the normal is
    # orthogonal to the track), so only the miss term moves. Scalar nesting took 8 minutes for
    # a handful of solves; this is the same arithmetic.
    k = math.sqrt(v_mps * v_mps - c * c) / (v_mps * c)
    offs = np.arange(-offset_range_m, offset_range_m, offset_step_m)
    best = None
    for bd in np.arange(0.0, 360.0, bearing_step_deg):
        br = math.radians(bd)
        u, n = _axes(br)
        a = np.array([float(np.dot(p, u)) for p in P])          # along-track, fixed vs offset
        q = np.array([float(np.dot(p, n)) for p in P])          # signed distance to the track
        tt = a[:, None] / v_mps + np.abs(q[:, None] - offs[None, :]) * k
        d = tt[idx, :] - tt[ref, :] - dt_obs[:, None]
        if wp is not None:
            d = d * wp[:, None]
        r = np.sum(d ** 2, axis=0)
        i = int(np.argmin(r))
        if best is None or r[i] < best[0]:
            best = (float(r[i]), br, float(offs[i]))
    r, br, off = best
    n_eq = len(P) - 1
    n_unk = 2                                                   # bearing and offset; t0 cancels
    obs = straddles(P, br, off)
    rms_ms = math.sqrt(r / n_eq) * 1000.0
    # r is already sum((d/rho_pair)^2) and rho_pair = sigma_pair/(sqrt(2)*sigma_min), so dividing
    # by 2*sigma_min^2 recovers sum((d/sigma_pair)^2) -- the same expression on both branches.
    chi2 = None if sig is None else float(r / (2.0 * float(sig.min()) ** 2))
    dof = n_eq - n_unk
    n_eff_eq = effective_n(wp, n_eq)
    n_cnt_eq = counting_n(wp, n_eq)
    return {
        "bearing_deg": math.degrees(br) % 360.0,
        "offset_m": off if obs else None,
        "offset_observable": obs,
        "rms_residual_ms": rms_ms,
        # Counted over EQUATIONS here, not receivers: this form differences against `ref`, so a
        # receiver contributes exactly one equation and the reference contributes to all of them.
        "residual_is_meaningful": n_eq > n_unk and n_cnt_eq > n_unk,
        "n_nodes": len(P), "n_equations": n_eq, "n_unknowns": n_unk,
        "residual_dof": dof,
        "chi2": chi2,
        "chi2_reduced": None if (chi2 is None or dof <= 0) else chi2 / dof,
        "n_effective_equations": n_eff_eq,
        "n_counting_equations": n_cnt_eq,
        "weight_floor": WEIGHT_FLOOR,
        "reference_node_index": ref,
        "sigma_s": None if sig is None else sig.tolist(),
        "equation_weights": None if wp is None else wp.tolist(),
        "sound_speed_mps": c,
        "mach_angle_deg": mach_angle_deg(v_mps, c),
        "note": None if obs else
                "nodes all lie on one side of the fitted track: offset is UNOBSERVABLE "
                "(every parallel track fits identically). Bearing is still valid.",
    }


def miss_distance_m(P, bearing_rad: float, offset_m: float) -> float:
    _, n = _axes(bearing_rad)
    return abs(float(np.dot(np.asarray(P, float)[:2] - n * offset_m, n)))
