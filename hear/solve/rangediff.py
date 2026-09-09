#!/usr/bin/env python3
"""What TWO nodes can say about where a sound came from, and the proof that it is not a position.

⚠️A TWO-SENSOR ARRAY CANNOT PRODUCE A 2D FIX. NOT "not yet", not "not at this SNR" -- never.
Two sensors give exactly ONE TDoA, so the position Fisher information is a rank-1 outer product

    J = (1/sigma_tau^2) * g g^T ,      g = grad_s [ (|s-P2| - |s-P1|) / c ] = (u2 - u1)/c

singular for any geometry, any baseline, any SNR (Fisher information for TDoA localisation,
Uni Stuttgart ISS; the n >= d+1 multilateration requirement, Calhoun et al. arXiv:2108.07377
SS II.A/V.C). One direction is observable -- along g -- and the two orthogonal to it carry ZERO
information, so their variance is infinite. There is no GDOP, no CEP and no covariance to quote.
`fisher()` computes the thing and reports its rank rather than asserting the result.

THE MECHANISM behind the field's geometry-dominance result: |u2 - u1| = 2 sin(theta/2), theta
being the angle the two nodes subtend AT THE SOURCE, so theta -> 0 (source roughly same-side of
both) drives the entire gradient to zero and theta -> 180 deg (source straddled) maximises it.
`subtended_angle_deg()` is that number and `fisher()['gradient_norm_per_m']` matches the closed
form to float precision. INFERENCE, not a reproduction: the field's 0.000 ms / 117 ms pair was a
TRACK shifted +/-6 m past three nodes under the cone model, and this is a point source under two.
What is measured HERE is the same shape at the same nodes -- a source at (200, 200) m subtends
2.32 deg and gets |g| = 1.172e-4 /m, one at (-8.3, 0.5) m subtends 158.4 deg and gets 5.691e-3,
a factor of 48.6 from geometry alone with the timing untouched.

WHAT THE PAIR DOES GIVE, EXACTLY. One range difference, Delta = c*tau = |s-P2| - |s-P1|, bounded
by |Delta| <= B (the baseline; equality only on the baseline extension). The source lies on one
sheet of a hyperboloid of revolution about the baseline: semi-transverse axis |Delta|/2, focal
half-distance B/2. Its ASYMPTOTIC cone about the baseline has half-angle exactly

    theta_inf = arccos(-Delta / B)      measured from the p1 -> p2 direction, so 0..180 deg

which is a DIRECTION, ambiguous around the whole cone, and only asymptotic -- at finite range the
sheet is inside its own asymptote and `direction_at_range()` says by how much. Nothing here
returns east/north/up, and `cue()` is tested to carry no key a fix consumer reads.

⚠️THE DIRECTION IS SURVEY-LIMITED, NOT TIMING-LIMITED, AND BY TWO ORDERS OF MAGNITUDE. Measured
on survey.json (nyquist sigma 0.717 m, mach sigma 0.521 m + 1.0 m assumed on a NOMINAL 3.0 m
storey height), B = 16.873 m:

    term                         theta=30 deg   theta=60 deg   theta=90 deg
    timing, sigma_e = 19.3 us       0.064 deg      0.037 deg      0.032 deg
    timing, sigma_e = 336.9 us      1.117 deg      0.645 deg      0.559 deg
    baseline LENGTH (0.890 m)       5.235 deg      1.745 deg      0.000 deg
    baseline AXIS (0.997 m perp)    3.38 deg       3.38 deg       3.38 deg
    sound speed, 1 degC             0.175 deg      0.058 deg      0.000 deg

So at the onset picker's own median sigma the node survey is 50-160x the timing term, and the
2.5 m of unmeasured mach height alone is worth more than every clock in the fleet. `cue()`
reports all four terms separately for that reason: a single number would hide which one to fix.
Clock sync is not third on this list, it is fifth.

⚠️ENDFIRE IS DEGENERATE. sin(theta_inf) divides every sigma above, so as |Delta| -> B the
direction uncertainty diverges: the sheet degenerates to a ray along the baseline. `cue()`
returns infinite sigmas there rather than a small-looking number, which is the same selection
effect consistency.py measures from the other side (of 205 Kinect impulses, every one that passed
the gate lay 58-117 deg off the array axis, and none of the 19% near endfire did).

⚠️2 mics have no loop to close, so `consistency.check_array` raises on them and the ONE check
that IS defined for a pair -- the plane-wave bound |tau| <= d/c -- never runs on this path.
`cue()` calls `consistency.physically_possible` directly and refuses past the bound.

WHAT WOULD BUY A FIX: more receivers, off the baseline. See `hear/solve/placement.py`; the
measured requirement is in docs/localisation-2026-09-08.md.
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Sequence, Tuple

import numpy as np

from .consistency import physically_possible
from .shockwave import sound_speed
from .soundspeed import DC_DT

#: Keys a consumer reads as "this is a position". Nothing this module emits may use one.
#: `hear/backend/pipeline.to_dama_event` copies exactly these through to the fleet payload when
#: present, so the guard is against a real wire contract and not against a style preference.
FIX_KEYS: frozenset = frozenset({
    "east_m", "north_m", "up_m", "range_m", "ground_range_m", "position_observable",
    "dop", "hdop", "vdop", "pdop", "cep_m", "confidence", "t0_utc_s"})

#: |Delta| within this fraction of the baseline counts as endfire: sin(theta) is then small
#: enough that the linearised sigmas below are meaningless. JUDGEMENT, not measured.
ENDFIRE_FRACTION: float = 0.999


def _pts(p1, p2) -> Tuple[np.ndarray, np.ndarray]:
    a = np.asarray(p1, float).ravel()
    b = np.asarray(p2, float).ravel()
    if a.shape != b.shape or a.size not in (2, 3):
        raise ValueError("two node positions of the same arity (2 or 3), got %r and %r"
                         % (a.shape, b.shape))
    if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
        raise ValueError("non-finite node position")
    if float(np.linalg.norm(b - a)) <= 0.0:
        raise ValueError("the two nodes are at the same point: there is no baseline")
    return a, b


def baseline_m(p1, p2) -> float:
    """Straight-line separation, metres. Also the largest range difference the pair can produce."""
    a, b = _pts(p1, p2)
    return float(np.linalg.norm(b - a))


def fisher(p1, p2, source, c: float = 345.238, sigma_tau_s: float = 1.0) -> Dict:
    """The position Fisher information of a TWO-node TDoA measurement, and its rank.

    Returned rather than asserted so the singularity is checkable on any geometry a caller cares
    about. `rank` is 1 for every non-degenerate input; `null_directions` are the displacement
    directions along which the TDoA does not change AT ALL -- moving the source there is free.
    """
    a, b = _pts(p1, p2)
    s = np.asarray(source, float).ravel()
    if s.shape != a.shape:
        raise ValueError("source has %d components, nodes have %d" % (s.size, a.size))
    r1, r2 = float(np.linalg.norm(s - a)), float(np.linalg.norm(s - b))
    if min(r1, r2) < 1e-9:
        raise ValueError("source sits on a node: the range gradient is undefined there")
    g = ((s - b) / r2 - (s - a) / r1) / float(c)
    J = np.outer(g, g) / (float(sigma_tau_s) ** 2)
    w, V = np.linalg.eigh(J)
    order = np.argsort(w)[::-1]
    w, V = w[order], V[:, order]
    tol = max(1e-12, 1e-9 * float(abs(w[0])))
    rank = int(np.sum(w > tol))
    return {
        "fisher": J,
        "rank": rank,
        "eigenvalues": [float(v) for v in w],
        "observable_direction": tuple(float(v) for v in V[:, 0]),
        "null_directions": [tuple(float(v) for v in V[:, k]) for k in range(1, len(w))
                            if w[k] <= tol],
        "gradient_norm_per_m": float(np.linalg.norm(g)),
        "subtended_angle_deg": subtended_angle_deg(a, b, s),
        "note": ("the position Fisher matrix of a 2-node TDoA is a rank-1 outer product: %d of %d "
                 "directions carry zero information, so no covariance, GDOP or CEP exists"
                 % (len(w) - rank, len(w))),
    }


def subtended_angle_deg(p1, p2, source) -> float:
    """The angle the two nodes subtend AT the source, degrees.

    |u2 - u1| = 2 sin(theta/2), so this is the single number that decides how much of the TDoA
    gradient survives: 0 deg (source same-side of both) kills it entirely, 180 deg (source between
    them) maximises it. It is the field's 0.000 ms / 117 ms result in one quantity.
    """
    a, b = _pts(p1, p2)
    s = np.asarray(source, float).ravel()
    v1, v2 = a - s, b - s
    n1, n2 = float(np.linalg.norm(v1)), float(np.linalg.norm(v2))
    if min(n1, n2) < 1e-9:
        raise ValueError("source sits on a node")
    return math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(v1, v2)) / (n1 * n2)))))


def range_difference_m(tau_s: float, c: float = 345.238) -> float:
    """Delta = c*tau = |s-P2| - |s-P1|. The only quantity a pair measures."""
    return float(c) * float(tau_s)


def asymptote_angle_deg(delta_m: float, baseline_m_: float) -> float:
    """arccos(-Delta/B): the asymptotic cone angle measured FROM THE p1 -> p2 DIRECTION, 0..180.

    Signed, because the sign of Delta is the only thing that says which end of the baseline the
    source is toward: taking |Delta| folds 175 deg onto 5 deg and throws that away. |cos| of this
    angle is |Delta|/B, which is what the sigma terms below use.

    EXACT as a property of the asymptote -- it is not a far-field approximation of the angle, it
    is the angle the sheet approaches. What IS approximate is using it as the direction to a
    source at finite range; `direction_at_range` prices that.
    """
    B = float(baseline_m_)
    if B <= 0.0:
        raise ValueError("baseline must be > 0 m")
    x = -float(delta_m) / B
    if abs(x) > 1.0 + 1e-12:
        raise ValueError("|Delta| = %.4f m exceeds the baseline %.4f m: no source produces this "
                         "range difference" % (abs(float(delta_m)), B))
    return math.degrees(math.acos(max(-1.0, min(1.0, x))))


def direction_at_range(delta_m: float, p1, p2, range_m: float) -> float:
    """The angle from the baseline axis to a point ON the sheet at `range_m` from the midpoint.

    The asymptote is the range -> infinity limit; at finite range the sheet lies inside it. This
    solves the exact locus so a caller can see the cost of the asymptotic reading at the range it
    actually cares about, instead of assuming far field.
    """
    a, b = _pts(p1, p2)
    B = float(np.linalg.norm(b - a))
    R = float(range_m)
    if R <= B / 2.0:
        raise ValueError("range %.3f m is inside the baseline half-length %.3f m" % (R, B / 2.0))
    d = float(delta_m)
    mid = 0.5 * (a + b)
    u = (b - a) / B
    # any unit vector orthogonal to the axis; the sheet is a surface of revolution about u
    w = np.eye(len(u))[int(np.argmin(np.abs(u)))]
    w = w - np.dot(w, u) * u
    w = w / np.linalg.norm(w)

    def f(phi):
        s = mid + R * (math.cos(phi) * u + math.sin(phi) * w)
        return (np.linalg.norm(s - b) - np.linalg.norm(s - a)) - d

    lo, hi = 1e-9, math.pi - 1e-9
    if f(lo) * f(hi) > 0.0:
        raise ValueError("no point of the sheet lies at range %.3f m for Delta = %.4f m" % (R, d))
    for _ in range(200):
        m = 0.5 * (lo + hi)
        if f(lo) * f(m) <= 0.0:
            hi = m
        else:
            lo = m
    return math.degrees(0.5 * (lo + hi))


def _survey_terms(p1, p2, sigma_pos_m, sigma_up_m) -> Tuple[float, float]:
    """(sigma along the baseline, sigma perpendicular to it), metres, from per-node survey sigmas.

    Two nodes, independent errors, so the pair sigma is the quadrature sum. The split matters
    because the two enter the direction differently: the along component scales the baseline
    LENGTH (and so vanishes at broadside), the perpendicular component rotates the AXIS (and so
    never vanishes).
    """
    a, b = _pts(p1, p2)
    u = (b - a) / np.linalg.norm(b - a)
    sh = math.hypot(float(sigma_pos_m[0]), float(sigma_pos_m[1]))
    if len(u) == 3:
        su = math.hypot(float(sigma_up_m[0]), float(sigma_up_m[1]))
        h2 = float(u[0] ** 2 + u[1] ** 2)
        along = math.sqrt(h2 * sh ** 2 + float(u[2] ** 2) * su ** 2)
        perp = math.sqrt((1.0 - h2) * sh ** 2 + (1.0 - float(u[2] ** 2)) * su ** 2)
    else:
        along = sh
        perp = sh
    return along, perp


def cue(p1, p2, tau_s: float, *, sigma_tau_s: float, sigma_pos_m: Sequence[float],
        sigma_up_m: Optional[Sequence[float]] = None, temp_c: float = 20.0,
        c: Optional[float] = None, sigma_temp_c: float = 1.0,
        tol_s: float = 0.0) -> Dict:
    """The honest product of a two-node solve: a range difference and the cone it implies.

    `tau_s`         arrival at p2 minus arrival at p1, seconds.
    `sigma_tau_s`   1-sigma of that delay. MANDATORY -- a delay with no sigma is not a
                    measurement, and `burstassoc.associate_burst` returns a peak-to-peak spread,
                    not a sigma, so the caller has to say which it is passing.
    `sigma_pos_m`   (sigma_p1, sigma_p2) horizontal survey sigma per node, metres. MANDATORY,
                    because on this fleet it is the term that dominates -- see the module
                    docstring's table.
    `sigma_up_m`    (sigma_p1, sigma_p2) vertical survey sigma. Required for 3D positions.

    Returns a dict that carries NO key from `FIX_KEYS`. There is no position in it to mistake.
    """
    a, b = _pts(p1, p2)
    if sigma_tau_s is None or float(sigma_tau_s) <= 0.0:
        raise ValueError("sigma_tau_s must be > 0 s: a delay with no uncertainty is an assertion")
    if len(a) == 3 and sigma_up_m is None:
        raise ValueError("3D node positions need sigma_up_m: the mach height is a NOMINAL storey "
                         "(+/-1.0 m assumed, survey.json) and dropping it understates the axis")
    cc = sound_speed(temp_c) if c is None else float(c)
    B = float(np.linalg.norm(b - a))

    if not physically_possible(tau_s, B, cc, tol_s=float(tol_s)):
        return {
            "usable": False,
            "reason": ("|tau| = %.3f ms exceeds the plane-wave bound d/c = %.3f ms (B = %.4f m): "
                       "not a slow measurement, an impossible one"
                       % (abs(float(tau_s)) * 1e3, B / cc * 1e3, B)),
            "baseline_m": B, "max_range_difference_m": B,
            "range_difference_m": range_difference_m(tau_s, cc),
            "sound_speed_mps": cc,
        }

    delta = range_difference_m(tau_s, cc)
    q = min(1.0, abs(delta) / B)                 # |cos(theta)|
    endfire = q >= ENDFIRE_FRACTION
    th = math.radians(asymptote_angle_deg(delta, B))
    sin_t = math.sin(th)

    along, perp = _survey_terms(a, b, sigma_pos_m, sigma_up_m or (0.0, 0.0))
    sigma_delta = cc * float(sigma_tau_s)
    frac_c = abs(float(sigma_temp_c)) * DC_DT / cc

    if endfire:
        terms = {k: float("inf") for k in
                 ("timing_deg", "baseline_length_deg", "sound_speed_deg")}
        terms["axis_orientation_deg"] = math.degrees(perp / B)
        total = float("inf")
    else:
        terms = {
            "timing_deg": math.degrees(sigma_delta / (B * sin_t)),
            "baseline_length_deg": math.degrees(q * along / (B * sin_t)),
            "sound_speed_deg": math.degrees(q * frac_c / sin_t),
            "axis_orientation_deg": math.degrees(perp / B),
        }
        total = math.sqrt(sum(v * v for v in terms.values()))

    axis = (b - a) / B
    out = {
        "usable": True,
        "reason": "",
        # ── the measurement ──────────────────────────────────────────────────────────────────
        "range_difference_m": delta,
        "range_difference_sigma_m": sigma_delta,
        "max_range_difference_m": B,
        "baseline_m": B,
        "tau_s": float(tau_s),
        "sound_speed_mps": cc,
        # ── the locus, named so it cannot be read as a point ─────────────────────────────────
        "locus": "hyperboloid_sheet" if len(a) == 3 else "hyperbola_branch",
        "locus_semi_transverse_m": abs(delta) / 2.0,
        "locus_focal_half_m": B / 2.0,
        "fix": False,
        "observable_dof": 1,
        "unobservable_dof": len(a) - 1,
        "mirror_ambiguous": True,
        # ── the direction, and every term of its uncertainty separately ──────────────────────
        "asymptote_angle_deg": math.degrees(th),
        "asymptote_angle_sigma_deg": total,
        "direction_sigma_terms_deg": terms,
        "endfire_degenerate": endfire,
        "axis_unit": tuple(float(v) for v in axis),
        "axis_bearing_deg": math.degrees(math.atan2(float(axis[0]), float(axis[1]))) % 360.0,
        "axis_elevation_deg": (math.degrees(math.asin(max(-1.0, min(1.0, float(axis[2])))))
                               if len(axis) == 3 else None),
        "survey_sigma_along_m": along,
        "survey_sigma_perp_m": perp,
        "note": (
            "endfire: |Delta| is within %.1f%% of the baseline, so the sheet degenerates toward a "
            "ray along the axis and every linearised direction sigma diverges. The range "
            "difference is still a measurement; the direction is not."
            % (100.0 * (1.0 - ENDFIRE_FRACTION)) if endfire else
            "TWO NODES, so this is a LOCUS and not a position: the Fisher information is rank 1 "
            "and %d of %d directions carry none. The source lies on the hyperboloid sheet with "
            "this range difference, anywhere around the cone. Largest term in the direction "
            "sigma: %s."
            % (len(a) - 1, len(a),
               max(terms, key=lambda k: terms[k]) if terms else "n/a")),
    }
    leaked = FIX_KEYS & set(out)
    if leaked:
        raise AssertionError("range-difference cue leaked position keys %r" % sorted(leaked))
    return out
