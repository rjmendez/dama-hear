#!/usr/bin/env python3
"""Array validity gate — can this array's delays be believed at all?

Three independent arrays on this project have produced confident bearings that were fiction:
the ESP intra-board pairs (scatter 22-78 us against a 110 us full scale), the ESP cross-board
set (ring offsets 11-88 ms against a 1.5-2.1 ms physical bound), and the Kinect 4-mic bar.
Nothing caught any of them. A GCC-PHAT peak-picker always returns a number, the median of a
handful of pairs always looks plausible, and a bearing computed from contradictory delays is
indistinguishable from a real one unless you check the delays against each other.

The checks here are the ones that cost nothing:

**Additivity.** Arrival times are one number per mic, so pairwise delays live in a space of
dimension N-1, not C(N,2). Every closed loop must sum to zero: tau(i,k) == tau(i,j) + tau(j,k).
That is an identity in the *measurement*, not in the geometry, so it needs NO ground truth and no
trusted survey -- it gates every event, not the handful with a known answer.

MEASURED over 205 unclipped Kinect impulses from 2026-09-05, at 30 us (half a sample at 16 kHz),
per-pair bands already clamped to each pair's spatial Nyquist:

    worst loop over the field basis {012,123,013}   4 / 205   2.0%   (the published number)
    worst loop over this module's basis {012,013,023}  5 / 205   2.4%
    worst loop over ALL FOUR triangles              0 / 205   0.0%
    worst PAIR off the best-fit arrival times      31 / 205  15.1%   (closure_residual)
    the full verdict -- closure AND the bound       17 / 205   8.3%
    the full verdict with the band NOT clamped      0 / 205   0.0%

The spread across the first three lines is why this module gates on `closure_residual` and not on
a maximum over triangles: a loop residual is a sum of up to three pair errors, so the same data
scores 2-3x worse as a loop than as a pair, and WHICH loops you pick changes the answer. A
tolerance of half a correlator sample is a statement about one pair, so it belongs on one pair.

⚠️What additivity CANNOT see, because it costs nothing:
  * a wrong geometry. The identity never mentions mic positions. Perfectly additive delays with
    a mismeasured baseline give a perfectly consistent wrong angle.
  * a per-mic constant timing offset. An error field of the form (o_j - o_i) is exactly additive
    and cancels in every loop. A clock skew between channels is INVISIBLE here.
  * a coherent peak-pick error that happens to be self-consistent (all pairs one period of the
    same tone off).
  Additivity proves the pairs agree with each other. It does not prove they are right.

**Spatial Nyquist, per PAIR.** c/(2d). Above it a pair's cross-correlation has more than one
candidate peak inside the physical lag window and GCC picks whichever is taller. Kinect pairs:
57 mm -> 3023 Hz, 114 mm -> 1512 Hz, 167 mm -> 1036 Hz; the ESP intra-board 38.2 mm pair ->
4501 Hz. Running every pair over 200-7000 Hz -- which is what produced three wrong Kinect
bearings -- runs the widest pair nearly 7x above its limit. This was already written down for
the ESP boards and was still missed on the Kinect, so it is a gate here rather than a comment.

**The plane-wave bound.** |tau| <= d/c. On that same Kinect set it is the single most productive
check: **117 of 205 impulses (57%) report a delay that no direction of arrival can produce**, and
it removes 14 of the 31 events that survive the consistency check. A wave cannot cross d metres
of baseline faster than sound; a delay past the bound is not a bad measurement, it is not a
measurement. ⚠️the check only bites if the correlator is allowed to SEARCH past the bound -- the
field script searched +/-1.15 d/c plus two samples, which is what made these 117 visible instead
of silently clamped to a wrong lag at the edge.

**The verdict REFUSES.** `check_array()` returns an `ArrayVerdict` whose direction is `None`
when the array is inconsistent. It does not fall back to the median of contradictory pairs,
because that is precisely the failure mode: the median of six pairs that disagree is a number
with an error bar that looks fine and means nothing.

⚠️SELECTION EFFECT, measured. A fixed absolute tolerance is easier to pass near broadside. Of the
205 impulses, 19% have |direction cosine| > 0.7 (near endfire); of the 17 that pass, NONE do --
every survivor lies 58-117 deg off the array axis. Near endfire the true delay sits at the edge of
the physical lag window, so any peak-pick error crosses the bound rather than merely widening the
residual. A gated event set is therefore NOT an unbiased sample of directions, and a bearing
histogram built from it will lean broadside. What would falsify this: gating a set of synthetic
endfire arrivals at realistic SNR and seeing the same pass rate as broadside ones.

Sign convention throughout: `taus[(i, j)]` is the arrival at mic j MINUS the arrival at mic i,
in seconds. Either key order is accepted; (j, i) is read as the negation of (i, j).
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .shockwave import sound_speed  # noqa: F401  (re-exported: one definition of c per repo)

Pair = Tuple[int, int]

# Half a sample is the honest floor for a parabolic-interpolated integer-lag correlator: the
# field gate used 30 us at 16 kHz. Callers pass fs and get this, or state a tolerance outright.
SUBSAMPLE_TOL_FRACTION = 0.5


class ArrayInconsistent(Exception):
    """Raised by ArrayVerdict.require_direction() when the array failed its own checks."""


# ── the three primitives ────────────────────────────────────────────────────────────────────────

def spatial_nyquist(d: float, c: float) -> float:
    """Highest frequency a pair d metres apart can carry unambiguously, Hz. = c/(2d).

    Above it, half a wavelength fits inside the baseline more than once, so the correlation has
    several peaks within the physical lag window +/- d/c and the tallest one is not necessarily
    the true one. This is a property of the PAIR, not of the array: on a 4-mic bar the closest
    pair may be good to 3 kHz while the widest is aliased above 1 kHz.
    """
    d = float(d)
    if d <= 0.0:
        raise ValueError("spacing must be > 0 m (got %r): a zero baseline has no Nyquist" % d)
    return float(c) / (2.0 * d)


def clamp_band(band: Tuple[float, float], d: float, c: float,
               margin: float = 1.0) -> Optional[Tuple[float, float]]:
    """Trim an analysis band to a pair's spatial Nyquist. None when the whole band is above it.

    `margin` < 1 keeps a guard below the limit (0.95 is what the field script used). Returning
    None is deliberate: a pair whose usable band is empty must be DROPPED from the solve, not
    run anyway. Silently clamping such a pair to an empty band would leave GCC correlating
    nothing and still returning a lag.
    """
    lo, hi = float(band[0]), float(band[1])
    if not (hi > lo >= 0.0):
        raise ValueError("band must be 0 <= lo < hi (got %r)" % (band,))
    limit = margin * spatial_nyquist(d, c)
    if lo >= limit:
        return None
    return (lo, min(hi, limit))


def physically_possible(tau: float, d: float, c: float, tol_s: float = 0.0) -> bool:
    """|tau| <= d/c. The bound a plane wave cannot exceed, whatever the direction of arrival.

    `tol_s` allows for the correlator's own quantisation (half a sample) so that a true endfire
    arrival, which sits exactly on the bound, is not rejected for a rounding.
    """
    if d <= 0.0:
        raise ValueError("spacing must be > 0 m (got %r)" % d)
    return abs(float(tau)) <= float(d) / float(c) + float(tol_s)


# ── geometry helpers ────────────────────────────────────────────────────────────────────────────

def _positions(mic_positions) -> np.ndarray:
    """Mic positions as (N, D) metres. A sequence of scalars is read as coordinates on a line."""
    P = np.asarray(mic_positions, dtype=float)
    if P.ndim == 1:
        P = P[:, None]
    if P.ndim != 2 or P.shape[0] < 2:
        raise ValueError("need >= 2 mic positions, got shape %r" % (P.shape,))
    return P


def array_axis(mic_positions, tol_m: float = 1e-3) -> Tuple[np.ndarray, float, bool]:
    """Best-fit line through the mics: (unit axis, max perpendicular offset in m, collinear?).

    The axis is oriented from the first mic toward the last so that the sign of a recovered
    direction cosine is reproducible across runs.
    """
    P = _positions(mic_positions)
    Q = P - P.mean(axis=0)
    if P.shape[1] == 1:
        e = np.array([1.0])
        off = 0.0
    else:
        _, _, Vt = np.linalg.svd(Q, full_matrices=False)
        e = Vt[0]
        perp = Q - np.outer(Q @ e, e)
        off = float(np.max(np.linalg.norm(perp, axis=1)))
    if float(np.dot(P[-1] - P[0], e)) < 0.0:
        e = -e
    return e, off, off <= float(tol_m)


def _axis_coords(mic_positions) -> Tuple[np.ndarray, np.ndarray, float, bool]:
    P = _positions(mic_positions)
    e, off, collinear = array_axis(mic_positions)
    s = (P - P[0]) @ e
    return P, s, off, collinear


def spacing(mic_positions, i: int, j: int) -> float:
    """Straight-line distance between two mics, metres."""
    P = _positions(mic_positions)
    return float(np.linalg.norm(P[j] - P[i]))


def plane_wave_taus(mic_positions, arrival_direction, c: float) -> Dict[Pair, float]:
    """Delays a plane wave arriving FROM `arrival_direction` produces. For tests and forward checks.

    `arrival_direction` points from the array toward the source (it is normalised here). With
    the far-field range R, t_i = (R - a.p_i)/c, so tau(i,j) = -a.(p_j - p_i)/c.
    """
    P = _positions(mic_positions)
    a = np.asarray(arrival_direction, dtype=float).reshape(-1)
    if a.shape[0] != P.shape[1]:
        raise ValueError("direction has %d components, positions have %d"
                         % (a.shape[0], P.shape[1]))
    n = float(np.linalg.norm(a))
    if n == 0.0:
        raise ValueError("arrival direction must be non-zero")
    a = a / n
    return {(i, j): float(-np.dot(a, P[j] - P[i]) / c)
            for i, j in itertools.combinations(range(P.shape[0]), 2)}


# ── additivity ──────────────────────────────────────────────────────────────────────────────────

def _tau(taus: Dict[Pair, float], i: int, j: int) -> float:
    if (i, j) in taus:
        return float(taus[(i, j)])
    if (j, i) in taus:
        return -float(taus[(j, i)])
    raise KeyError("no delay for pair (%d,%d)" % (i, j))


def independent_triangles(n: int) -> List[Tuple[int, int, int]]:
    """A basis of the cycle space of the complete graph on n mics: C(n-1, 2) triangles.

    All C(n,3) triangles are checkable, but only C(n,2) - (n-1) of them are independent -- the
    rest are sums of these. For 4 mics that is 3 independent triangles out of 4, which is what
    the field script checked. The basis is the star spanning tree rooted at mic 0: every non-tree
    edge (i,j) closes exactly one loop 0 -> i -> j -> 0.
    """
    return [(0, i, j) for i, j in itertools.combinations(range(1, n), 2)]


def additivity_residuals(taus: Dict[Pair, float], mic_positions,
                         triangles: Optional[Sequence[Tuple[int, int, int]]] = None
                         ) -> Dict[Tuple[int, int, int], float]:
    """Signed loop closure for each triangle, seconds: tau(i,k) - (tau(i,j) + tau(j,k)).

    Defaults to an independent basis. Pass `itertools.combinations(range(n), 3)` to see all of
    them; the extra ones are linear combinations of the basis and add no information, but they
    do make it obvious which mic is the odd one out.
    """
    P = _positions(mic_positions)
    n = P.shape[0]
    if n < 3:
        raise ValueError("additivity needs >= 3 mics, got %d (2 mics have no loop to close)" % n)
    tri = list(independent_triangles(n) if triangles is None else triangles)
    missing = sorted({(min(a, b), max(a, b))
                      for i, j, k in tri for a, b in ((i, j), (j, k), (i, k))
                      if (a, b) not in taus and (b, a) not in taus})
    if missing:
        raise ValueError("delays missing for pairs %r: additivity needs the closed loop" % missing)
    return {(i, j, k): _tau(taus, i, k) - (_tau(taus, i, j) + _tau(taus, j, k)) for i, j, k in tri}


def additivity_residual(taus: Dict[Pair, float], mic_positions,
                        triangles: Optional[Sequence[Tuple[int, int, int]]] = None) -> float:
    """Worst absolute loop closure over the independent triangles, in SECONDS.

    This is the whole gate in one number and it needs no ground truth. Compare it against half a
    correlator sample: 30 us at 16 kHz, 10 us at 48 kHz. On the Kinect bar 98% of live-fire
    impulses fail it.
    """
    return max(abs(v) for v in additivity_residuals(taus, mic_positions, triangles).values())


def arrival_times(taus: Dict[Pair, float], mic_positions) -> np.ndarray:
    """Least-squares arrival times (seconds, relative to mic 0) implied by the pairwise delays.

    The whole content of additivity is that N(N-1)/2 delays are generated by N-1 free numbers.
    Recovering those numbers directly is the basis-free way to say it, and it uses every pair
    instead of a spanning tree.
    """
    P = _positions(mic_positions)
    n = P.shape[0]
    keys = [(i, j) for i, j in itertools.combinations(range(n), 2)
            if (i, j) in taus or (j, i) in taus]
    if not keys:
        raise ValueError("no delays given")
    A = np.zeros((len(keys) + 1, n))
    b = np.zeros(len(keys) + 1)
    for r, (i, j) in enumerate(keys):
        A[r, j] = 1.0
        A[r, i] = -1.0
        b[r] = _tau(taus, i, j)
    A[-1, 0] = 1.0                              # gauge: t_0 = 0, the delays fix only differences
    t, *_ = np.linalg.lstsq(A, b, rcond=None)
    return t


def closure_residual(taus: Dict[Pair, float], mic_positions) -> float:
    """Worst |tau(i,j) - (t_j - t_i)| over all pairs, seconds. Basis-INDEPENDENT.

    ⚠️`additivity_residual` is a maximum over a chosen basis of triangles and therefore depends
    on which basis you chose: measured over 205 Kinect impulses, the field script's basis passed
    4 events at 30 us, the library's default basis passed 5, and requiring all four triangles
    passed 0. Loop residuals are not independent -- for 4 mics the fourth triangle is
    r(0,2,3) = r(0,1,3) - r(0,1,2) + r(1,2,3), so it can be three times the largest of them.

    This number has no such freedom: it asks whether ANY set of arrival times explains the
    measured delays, which is the question the gate actually means to ask. It is the residual of
    an L2 fit, so it is a (tight in practice) upper bound on the best possible L-infinity fit.
    """
    P = _positions(mic_positions)
    t = arrival_times(taus, P)
    worst = 0.0
    for i, j in itertools.combinations(range(P.shape[0]), 2):
        try:
            m = _tau(taus, i, j)
        except KeyError:
            continue
        worst = max(worst, abs(m - (t[j] - t[i])))
    return float(worst)


# ── the verdict ─────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PairCheck:
    pair: Pair
    spacing_m: float
    tau_s: float
    max_tau_s: float                     # d/c, the plane-wave bound
    within_bound: bool
    nyquist_hz: float
    band_hz: Optional[Tuple[float, float]]
    band_within_nyquist: bool
    closure_s: float                     # tau minus the fitted (t_j - t_i): this pair's own share
    axis_cos: Optional[float]            # per-pair direction cosine, collinear arrays only


@dataclass
class ArrayVerdict:
    """What the array measurement is worth. `valid` False means: do not use this event.

    `axis_cos` / `cone_angle_deg` are None whenever `valid` is False. That is the point of the
    object -- there is no "best effort" bearing on this path.
    """
    valid: bool
    reasons: List[str]
    n_mics: int
    sound_speed_mps: float
    worst_additivity_s: float
    worst_closure_s: float
    additivity_tol_s: float
    triangles_s: Dict[Tuple[int, int, int], float]
    pairs: List[PairCheck]
    collinear: bool
    off_axis_m: float
    plane_wave_residual_s: Optional[float] = None
    plane_wave_tol_s: Optional[float] = None
    axis_cos: Optional[float] = None
    axis_cos_spread: Optional[float] = None
    cone_angle_deg: Optional[float] = None
    note: Optional[str] = None

    def require_direction(self) -> float:
        """The direction cosine, or ArrayInconsistent. Use this where a bearing is mandatory."""
        if not self.valid or self.axis_cos is None:
            raise ArrayInconsistent("; ".join(self.reasons) or "array failed its validity gate")
        return self.axis_cos

    def summary(self) -> str:
        head = "VALID" if self.valid else "REFUSED"
        return "%s  worst closure %.1f us / worst loop %.1f us (tol %.1f us)%s" % (
            head, self.worst_closure_s * 1e6, self.worst_additivity_s * 1e6,
            self.additivity_tol_s * 1e6,
            "" if self.valid else "  [" + "; ".join(self.reasons) + "]")


def check_array(taus: Dict[Pair, float], mic_positions, *,
                temp_c: Optional[float] = None, c: Optional[float] = None,
                fs: Optional[float] = None, tol_s: Optional[float] = None,
                bands: Optional[object] = None, nyquist_margin: float = 1.0,
                plane_wave_tol_s: Optional[float] = None) -> ArrayVerdict:
    """Run every free check on one array measurement and return a verdict that can refuse.

    `taus`     pairwise delays, seconds, tau(i,j) = arrival at j minus arrival at i.
    `bands`    the analysis band actually used: one (lo, hi) for every pair, or a dict keyed by
               pair. Omit it only if you genuinely do not know what band the correlator ran in --
               and then the Nyquist check cannot run, which is how the Kinect bearings happened.
    `tol_s`    additivity tolerance. Derived as half a sample from `fs` when not given; one of
               the two is required, because a residual without a tolerance is not a test.
    `plane_wave_tol_s` opt-in. Gates the extra check that arrival times are AFFINE in position
               along the array -- which additivity does not imply. It is off by default because
               it trusts the survey, and the Kinect offsets are marked "APPROXIMATE, NEVER
               MEASURED"; failing an event on a geometry nobody measured would be blaming the
               audio for the tape measure.
    """
    if c is None:
        c = sound_speed(20.0 if temp_c is None else temp_c)
    c = float(c)
    if tol_s is None:
        if fs is None:
            raise ValueError("give tol_s, or fs to derive half a sample from: a residual "
                             "without a tolerance is not a test")
        tol_s = SUBSAMPLE_TOL_FRACTION / float(fs)
    tol_s = float(tol_s)

    P, s, off_axis, collinear = _axis_coords(mic_positions)
    n = P.shape[0]
    reasons: List[str] = []

    tri = additivity_residuals(taus, P)
    worst = max(abs(v) for v in tri.values())
    t_fit = arrival_times(taus, P)
    worst_closure = closure_residual(taus, P)
    # ⚠️gated on the basis-free closure, NOT on the triangle maximum: see closure_residual().
    if worst_closure > tol_s:
        bad = max(tri.items(), key=lambda kv: abs(kv[1]))[0]
        reasons.append("consistency: no arrival times explain these delays -- worst pair off by "
                       "%.1f us over tol %.1f us (worst loop %.1f us at triangle %s); at least "
                       "one pair's correlation peak is wrong"
                       % (worst_closure * 1e6, tol_s * 1e6, worst * 1e6, bad))

    def band_for(pair: Pair) -> Optional[Tuple[float, float]]:
        if bands is None:
            return None
        if isinstance(bands, dict):
            if pair in bands:
                return tuple(bands[pair])          # type: ignore[return-value]
            rev = (pair[1], pair[0])
            return tuple(bands[rev]) if rev in bands else None  # type: ignore[return-value]
        return tuple(bands)                        # type: ignore[return-value]

    pairs: List[PairCheck] = []
    cosines: List[float] = []
    for i, j in itertools.combinations(range(n), 2):
        try:
            t = _tau(taus, i, j)
        except KeyError:
            continue
        d = float(np.linalg.norm(P[j] - P[i]))
        bound = d / c
        ok_bound = physically_possible(t, d, c, tol_s)
        if not ok_bound:
            reasons.append("pair (%d,%d): |tau| %.1f us exceeds the plane-wave bound %.1f us "
                           "(d = %.4f m) -- not a slow measurement, an impossible one"
                           % (i, j, abs(t) * 1e6, bound * 1e6, d))
        nyq = spatial_nyquist(d, c)
        band = band_for((i, j))
        ok_band = True
        if band is not None:
            limit = nyquist_margin * nyq
            if band[1] > limit:
                ok_band = False
                reasons.append("pair (%d,%d): analysis band %.0f-%.0f Hz runs above the pair's "
                               "spatial Nyquist %.0f Hz (d = %.4f m) -- GCC can pick the wrong "
                               "peak" % (i, j, band[0], band[1], nyq, d))
        ac = None
        if collinear and abs(s[j] - s[i]) > 0.0:
            ac = -c * t / (s[j] - s[i])
            cosines.append(ac)
        pairs.append(PairCheck((i, j), d, t, bound, ok_bound, nyq, band, ok_band,
                               float(t - (t_fit[j] - t_fit[i])), ac))

    pw_res: Optional[float] = None
    axis_cos: Optional[float] = None
    spread: Optional[float] = None
    note: Optional[str] = None
    if collinear and n >= 2:
        # Is the fitted arrival time AFFINE in position along the array? A plane wave says yes.
        A = np.column_stack([s, np.ones(n)])
        coef, *_ = np.linalg.lstsq(A, t_fit, rcond=None)
        pw_res = float(np.max(np.abs(A @ coef - t_fit)))
        axis_cos = float(-coef[0] * c)
        spread = float(np.std(cosines)) if cosines else None
        if plane_wave_tol_s is not None and pw_res > plane_wave_tol_s:
            reasons.append("plane wave: arrival times are not affine in mic position (max %.1f us "
                           "over tol %.1f us) -- either the wave is not planar here or the "
                           "surveyed spacing is wrong" % (pw_res * 1e6, plane_wave_tol_s * 1e6))
        if abs(axis_cos) > 1.0 + 1e-9:
            reasons.append("direction cosine %.3f is outside [-1, 1]: no direction of arrival "
                           "produces these delays" % axis_cos)
        note = ("collinear array: only the angle from the array axis is observable. The source "
                "lies on a cone about the axis; azimuth needs a second, non-parallel baseline.")
    else:
        note = ("non-collinear array (max %.4f m off the best-fit line): additivity and the "
                "plane-wave bound still apply, but this module does not solve a 2D/3D "
                "direction -- use a full DoA solver on the gated delays." % off_axis)

    valid = not reasons
    if not valid:
        axis_cos = None
        spread = None
    cone = (math.degrees(math.acos(max(-1.0, min(1.0, axis_cos))))
            if (valid and axis_cos is not None) else None)
    return ArrayVerdict(
        valid=valid, reasons=reasons, n_mics=n, sound_speed_mps=c,
        worst_additivity_s=worst, worst_closure_s=worst_closure,
        additivity_tol_s=tol_s, triangles_s=tri, pairs=pairs,
        collinear=collinear, off_axis_m=off_axis,
        plane_wave_residual_s=pw_res, plane_wave_tol_s=plane_wave_tol_s,
        axis_cos=axis_cos, axis_cos_spread=spread, cone_angle_deg=cone, note=note,
    )
