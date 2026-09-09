#!/usr/bin/env python3
"""Range from the CRACK-BLAST interval -- two arrival times on ONE microphone.

A supersonic round gives a node two arrivals: the shock off its Mach cone, then the muzzle blast
travelling from the firing point at c.  Their separation encodes range.  Unlike every bearing
method it needs no inter-microphone phase at all -- two times on one channel -- which is why it
survives on this project's hardware, where three independent arrays have failed to close a
bearing.  A suppressor attenuates the blast but cannot remove it; it cannot touch the shock.

MODEL (constant Mach).  Shooter at S, bullet along unit u, mic at M, R = |M-S|, theta the angle
between u and (M-S).  A shock made at along-track x reaches the mic at x/V + sqrt((x_m-x)^2+m^2)/c;
minimising over x puts the radiating point at x = x_m - m/sqrt(M^2-1) and gives

    t_shock = (x_m + m*sqrt(M^2-1)) / (M c),      t_blast = R/c

    dt = t_blast - t_shock = (R/c) * (1 - sin(theta + mu)),      mu = asin(1/M)

The minimisation is only valid while that radiating point is ahead of the muzzle, x >= 0, i.e.
theta <= 90 - mu.  Past that the mic is outside the Mach cone from the muzzle: there is NO crack,
only a blast, and an interval measured there is not this quantity.

WHAT IT BUYS, AND WHAT IT DOES NOT.
  * dt is largest ON the trajectory, so with NO angle knowledge at all the interval still gives a
    rigorous LOWER BOUND on range:  R >= c*dt / (1 - 1/M).   `range_lower_bound`.
  * Turning dt into a range needs theta, and dlnR/dtheta is ~3 %/deg at the small angles where
    this method is sharpest.  An angle is a bearing.  So the interval does NOT remove the need for
    a bearing -- it changes what the bearing has to be good for, from sub-millisecond phase across
    an array to a couple of degrees of shock arrival angle.  `range_uncertainty` prices that.
  * theta is the angle to the TRAJECTORY, not to the shooter.  Feeding it a bearing to the muzzle
    blast is a different angle and gives a different (wrong) range.

MEASURED 2026-09-05, robot ESP array, 6 mics on 3 boards:
  surveyed string 20:04:45, R = 36.26 m known from RTK on both ends
      dt = 53.78 ms (median of 5 rounds x 3 boards); same-board replicate sigma <= 0.14 ms
      angle-free lower bound 28.6-34.3 m (true 36.26 m -- the bound holds every time)
      with theta = 6.62 deg from the surveyed flag line: R = 35.8-39.0 m, -1.2 to +7.6 %
  full-auto burst 21:05:33
      dt = 34.77 ms (7 rounds x 3 boards), sd across rounds 1.09 ms
      angle-free lower bound 17.0-19.9 m -- the shooter was tens of metres out, not beside the robot
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, Optional, Sequence, Tuple

import numpy as np

from .consistency import physically_possible
from .shockwave import mach_angle_deg, sound_speed

__all__ = [
    "sound_speed", "mach_angle_deg", "cone_limit_deg", "crack_blast_interval",
    "max_interval", "range_from_interval", "range_lower_bound", "theta_from_interval",
    "range_uncertainty", "band_power", "crack_onset", "second_arrival",
    "rise_reference", "interval_consistent", "IntervalCheck",
    "CRLB_MB_SW_M_PER_S", "MAX_USEFUL_SLACK", "position_error_floor_m", "crlb_geometry_factor",
]


# ---------------------------------------------------------------- the CRLB floor
#
# ⚠️THIS REPLACES THE PASS/FAIL GATE AS THE THING TO REPORT. `interval_consistent`'s |dt_i - dt_j|
# <= 2d/c bound is a real physical inequality and is kept, but it is not a result: it scales with
# separation, so past ~0.35 m of spacing at 2 ms of onset scatter it cannot fail, and the three
# 2026-09-05 phones (10.1-20.0 m, bound 58.7-115.9 ms) passed every pair on every burst including
# pairs whose implied ranges differed by a factor of two. A check that cannot fail is not
# evidence.
#
# The field's answer is not a tighter bound, it is a CRLB. Lindgren et al. (2010, EURASIP JASP
# 690732) publish, for their muzzle-blast/shock-wave difference model, the position-error floor
#
#     RMSE(x) >= 1430 * sigma_e   [m],    sigma_e = per-microphone detection-time error [s]
#
# which converts an onset sigma straight into metres of position error, with no threshold to tune
# and no way to pass by construction.
#
# ⚠️1430 IS THEIR ARRAY'S NUMBER, NOT A UNIVERSAL CONSTANT, AND IT MUST BE RE-DIMENSIONED BEFORE
# IT IS BELIEVED HERE. It carries units of m/s, and 1430 / 345.238 = 4.14: it is c multiplied by a
# geometric dilution of ~4.1 for THEIR sensor layout and THEIR source geometry. Our own layouts
# are far worse conditioned -- `placement.dop_grid` over a 120 m box gives a median DOP of 35-200
# for three nodes around the nyquist-mach pair -- so 1430 is a floor we are nowhere near, not a
# prediction of what we would achieve. `crlb_geometry_factor()` exposes the 4.14 so a caller can
# substitute a DOP measured for its own geometry instead of importing Lindgren's.

#: Lindgren et al. (2010) MB-SW position-error floor, metres per second of detection-time error.
CRLB_MB_SW_M_PER_S: float = 1430.0

#: (2d/c) / tol above which the cross-mic bound cannot realistically fail and so tests nothing.
#: JUDGEMENT. Sized on the measured cases it must separate: the ESP intra-board 38.1 mm pair at a
#: 1.06 ms tolerance scores 0.21, the 2026-09-05 phones at 10.1-20.0 m score 55-109.
MAX_USEFUL_SLACK: float = 3.0


def position_error_floor_m(sigma_e_s: float, k_m_per_s: float = CRLB_MB_SW_M_PER_S) -> float:
    """RMSE(x) >= k * sigma_e. The CRLB position-error floor a given onset sigma cannot beat.

    Read it as a floor and never as an estimate: it is what a perfectly-conditioned estimator on
    Lindgren's geometry would achieve, so any real answer is worse. Pass `k_m_per_s` to substitute
    a factor derived from the layout at hand -- see `crlb_geometry_factor`.
    """
    s = float(sigma_e_s)
    if s < 0.0:
        raise ValueError("sigma_e must be >= 0 s (got %r)" % sigma_e_s)
    if k_m_per_s <= 0.0:
        raise ValueError("k must be > 0 m/s (got %r)" % k_m_per_s)
    return float(k_m_per_s) * s


def crlb_geometry_factor(k_m_per_s: float = CRLB_MB_SW_M_PER_S, c: float = 345.238) -> float:
    """k / c: the dilution the published floor bakes in, in metres of position per metre of range.

    1430 / 345.238 = 4.14. Quoting it this way is the whole point -- it is the number that has to
    be replaced by this array's own DOP before the floor means anything about this array.
    """
    if c <= 0.0:
        raise ValueError("c must be > 0 m/s (got %r)" % c)
    return float(k_m_per_s) / float(c)


# ---------------------------------------------------------------- geometry


def _mach(v_mps: Optional[float], c: float, mach: Optional[float]) -> float:
    if (mach is None) == (v_mps is None):
        raise ValueError("give exactly one of v_mps or mach")
    m = float(mach) if mach is not None else float(v_mps) / float(c)
    if m <= 1.0:
        raise ValueError(f"subsonic (M={m:.3f}): there is no shock and no crack-blast interval")
    return m


def cone_limit_deg(v_mps: float = None, c: float = 345.238, mach: float = None) -> float:
    """Largest theta that still hears a crack: 90 - mu.  Beyond it the mic is outside the Mach
    cone from the muzzle and hears the blast alone."""
    return 90.0 - mach_angle_deg(_mach(v_mps, c, mach) * c, c)


def crack_blast_interval(range_m: float, theta_deg: float, v_mps: float = None,
                         c: float = 345.238, mach: float = None) -> Optional[float]:
    """Seconds.  None when theta is outside the Mach cone -- no crack exists to measure from."""
    m = _mach(v_mps, c, mach)
    mu = math.asin(1.0 / m)
    th = math.radians(float(theta_deg))
    if th > math.pi / 2.0 - mu + 1e-12:
        return None
    return (float(range_m) / float(c)) * (1.0 - math.sin(th + mu))


def max_interval(range_m: float, v_mps: float = None, c: float = 345.238,
                 mach: float = None) -> float:
    """The interval at theta=0.  A measured interval ABOVE this for a known range falsifies the
    pairing -- the two arrivals are not crack and blast from that shot."""
    m = _mach(v_mps, c, mach)
    return (float(range_m) / float(c)) * (1.0 - 1.0 / m)


def range_from_interval(dt_s: float, theta_deg: float, v_mps: float = None,
                        c: float = 345.238, mach: float = None) -> float:
    m = _mach(v_mps, c, mach)
    mu = math.asin(1.0 / m)
    th = math.radians(float(theta_deg))
    s = 1.0 - math.sin(th + mu)
    if s <= 1e-9:
        raise ValueError("theta is at the cone limit: dt -> 0 and range is unobservable here")
    return float(c) * float(dt_s) / s


def range_lower_bound(dt_s: float, v_mps: float = None, c: float = 345.238,
                      mach: float = None) -> float:
    """Range cannot be smaller than this, whatever the geometry.  Needs no angle."""
    m = _mach(v_mps, c, mach)
    return float(c) * float(dt_s) / (1.0 - 1.0 / m)


def theta_from_interval(dt_s: float, range_m: float, v_mps: float = None,
                        c: float = 345.238, mach: float = None) -> Optional[float]:
    """Inverse for a KNOWN range -- the validation direction.  None when the interval is
    impossible for that range (i.e. above max_interval)."""
    m = _mach(v_mps, c, mach)
    mu = math.asin(1.0 / m)
    s = 1.0 - float(c) * float(dt_s) / float(range_m)
    if not -1.0 <= s <= 1.0:
        return None
    th = math.asin(s) - mu
    return None if th < -1e-9 else math.degrees(th)


def range_uncertainty(dt_s: float, theta_deg: float, sigma_dt_s: float, sigma_theta_deg: float,
                      v_mps: float = 900.0, c: float = 345.238, sigma_v_mps: float = 40.0,
                      sigma_c_frac: float = 0.015) -> Dict[str, float]:
    """Fractional (1-sigma) range error, term by term.  Every entry is d(ln R), so it reads as a
    fraction of range directly.  c appears TWICE -- in the path and in the Mach angle -- which is
    why its coefficient is ~1.7 and not 1."""
    m = _mach(v_mps, c, None)
    mu = math.asin(1.0 / m)
    th = math.radians(float(theta_deg))
    s = 1.0 - math.sin(th + mu)
    k = math.cos(th + mu) / s                       # d(ln R)/d(theta) and /d(mu), per radian
    t_dt = abs(sigma_dt_s / dt_s)
    t_th = abs(k) * math.radians(sigma_theta_deg)
    t_M = abs(k / (m * math.sqrt(m * m - 1.0))) * (sigma_v_mps / c)
    t_c = abs(1.0 + k / math.sqrt(m * m - 1.0)) * sigma_c_frac
    return dict(interval=t_dt, theta=t_th, mach=t_M, sound_speed=t_c,
                total=math.sqrt(t_dt**2 + t_th**2 + t_M**2 + t_c**2),
                d_lnR_d_theta_per_deg=k * math.pi / 180.0,
                d_lnR_d_lnc=1.0 + k / math.sqrt(m * m - 1.0))


# ---------------------------------------------------------------- detection


def band_power(x, fs: float, band: Tuple[float, float], frame_s: float = 0.004,
               hop_s: float = 0.0005) -> Tuple[np.ndarray, np.ndarray]:
    """Framed band power.  numpy only -- no IIR, so nothing rings for 1/BW after an impulse and
    turns the filter's own decay into a train of fake arrivals.

    Returns (t, p) with t the frame CENTRE.  A frame is a window of the signal, so an arrival can
    only raise frames that overlap it: energy is localised to +/- frame_s/2 and never further.
    Compare arrivals measured with the SAME frame_s wherever the difference matters.
    """
    x = np.asarray(x, dtype=np.float64)
    n = max(8, int(round(frame_s * fs)))
    h = max(1, int(round(hop_s * fs)))
    if len(x) < n:
        raise ValueError("signal shorter than one frame")
    idx = np.arange(0, len(x) - n + 1, h)
    w = np.hanning(n)
    F = np.fft.rfft(x[idx[:, None] + np.arange(n)] * w, axis=1)
    f = np.fft.rfftfreq(n, 1.0 / fs)
    keep = (f >= band[0]) & (f <= band[1])
    if not keep.any():
        raise ValueError(f"band {band} has no bin at frame_s={frame_s} (resolution {fs/n:.0f} Hz)")
    p = (np.abs(F[:, keep]) ** 2).sum(axis=1) / (n * (w ** 2).sum())
    return (idx + n / 2.0) / fs, p + 1e-12


def crack_onset(x, fs: float, search_s: Tuple[float, float], band=(2000.0, 10000.0),
                frame_s: float = 0.001, hop_s: float = 0.00025,
                floor_s: Tuple[float, float] = (0.0, 0.0), frac: float = 0.2) -> Dict[str, float]:
    """Shock arrival: the HF peak inside `search_s`, backtracked to `frac` of its height above the
    pre-event floor.  The shock rise is tens of microseconds, so this is the sharp one; clipping
    changes its amplitude and not its time."""
    t, p = band_power(x, fs, band, frame_s, hop_s)
    m = (t >= search_s[0]) & (t <= search_s[1])
    if not m.any():
        raise ValueError("empty search window")
    i = int(np.where(m)[0][int(np.argmax(p[m]))])
    fm = (t >= floor_s[0]) & (t <= floor_s[1]) if floor_s[1] > floor_s[0] else (t < search_s[0])
    floor = float(np.median(p[fm])) if fm.any() else float(np.median(p[:max(4, i // 2)]))
    thr = floor + frac * (p[i] - floor)
    j = i
    while j > 0 and p[j] > thr:
        j -= 1
    a, b = p[j], p[j + 1]
    f = 0.0 if b <= a else (thr - a) / (b - a)
    return dict(t_s=float(t[j] + f * (t[j + 1] - t[j])), t_peak_s=float(t[i]),
                peak=float(p[i]), floor=float(floor))


def second_arrival(x, fs: float, t_crack_s: float, band=(100.0, 1000.0), frame_s: float = 0.008,
                   hop_s: float = 0.0005, trough_s: Tuple[float, float] = (0.030, 0.050),
                   search_s: Tuple[float, float] = (0.045, 0.100),
                   frac: float = 0.5) -> Optional[Dict[str, float]]:
    """Muzzle blast: the LF maximum in `search_s` after the crack, timed by its `frac` rise above
    the reverberation TROUGH that precedes it.

    Referred to the trough and not to an absolute threshold, because at these ranges a suppressed
    blast lands INSIDE the reverberant tail of its own crack -- a level threshold finds the crack's
    tail, and a decay-line fit finds the far end of the window (the fitted line keeps descending
    after the real signal has flattened onto the noise floor, so its residual grows with lag).
    """
    t, p = band_power(x, fs, band, frame_s, hop_s)
    lag = t - float(t_crack_s)
    tm = (lag >= trough_s[0]) & (lag <= trough_s[1])
    sm = (lag >= search_s[0]) & (lag <= search_s[1])
    if not (tm.any() and sm.any()):
        return None
    it = int(np.where(tm)[0][int(np.argmin(p[tm]))])
    ip = int(np.where(sm)[0][int(np.argmax(p[sm]))])
    if ip <= it:
        return None
    trough, peak = float(p[it]), float(p[ip])
    thr = trough + frac * (peak - trough)
    j = ip
    while j > it and p[j] > thr:
        j -= 1
    a, b = p[j], p[j + 1]
    f = 0.0 if b <= a else (thr - a) / (b - a)
    t_on = float(t[j] + f * (t[j + 1] - t[j]))
    return dict(dt_s=t_on - float(t_crack_s), dt_peak_s=float(t[ip]) - float(t_crack_s),
                rise_db=10.0 * math.log10(peak / trough), trough_lag_s=float(lag[it]),
                frame_s=frame_s)


# Measured null of `rise_db` on shot-free audio; see rise_reference.__doc__ for provenance.
# A threshold is read off this, never guessed.
NULL_PERCENTILES = {50: 11.3, 90: 16.4, 95: 18.0, 99: 20.6, 99.9: 25.7}
NULL_FALSE_ALARM = {12: 0.41, 16: 0.11, 18: 0.048, 20: 0.019, 22: 0.008}


def suggested_threshold_db(max_false_alarm: float = 0.01) -> float:
    """Smallest documented threshold whose measured false-alarm rate is <= `max_false_alarm`.

    Refuses rather than extrapolating past the measured curve: a threshold nobody measured is
    exactly the mistake the old p99~12 figure was.
    """
    ok = [t for t, fa in sorted(NULL_FALSE_ALARM.items()) if fa <= max_false_alarm]
    if not ok:
        raise ValueError("no measured threshold reaches FA <= %.3f; tightest measured is %.3f at %d dB"
                         % (max_false_alarm, min(NULL_FALSE_ALARM.values()),
                            max(NULL_FALSE_ALARM, key=lambda k: -NULL_FALSE_ALARM[k])))
    return float(min(ok))


def rise_reference(x, fs: float, anchors_s: Iterable[float], **kw) -> np.ndarray:
    """The `rise_db` of `second_arrival` evaluated at SHOT-FREE anchor times in the same recording.

    Needed because `second_arrival` always returns the maximum of its search window, so `rise_db`
    is not zero when there is no arrival: band power over ~100 frames of noise routinely peaks
    ~10 dB above its own trough.  This is the false-alarm curve, and the operating threshold is
    read off it rather than guessed.

    ⚠️THE CURVE BELOW REPLACES A WRONG ONE. This docstring previously quoted p50~5 / p90~8 /
    p99~12 dB, which was measured against the PRE-EVENT FLOOR while `rise_db` is referred to the
    REVERBERATION TROUGH -- a different, always-smaller denominator. Anyone thresholding at the
    old 12 dB was running at ~41% false alarms, not 1%.

    MEASURED, and reproduced on two independent instruments:
        robot ESP array, 48 kHz : p50 11.47, p90 16.65, p99 19.79 dB
        phone clips,     48 kHz : p50 11.19, p90 16.21, p99 21.31 dB  (1660 anchors, 140 clips)
    Agreement across different mics, hosts and sample paths says this is a property of the
    STATISTIC, not of one dataset. `NULL_PERCENTILES` carries it in code, and a test asserts the
    documented numbers are the ones this function actually produces -- the drift that made the old
    curve wrong was invisible precisely because nothing checked it.

    Operating points off the measured curve:
        12 dB -> ~41% false alarm     18 dB -> ~4.8%
        16 dB -> ~11%                 20 dB -> ~1.9%      22 dB -> ~0.8%

    So the surveyed string's 20-26 dB is roughly p99, NOT "far clear", and the full-auto burst's
    6-15 dB is below the median of noise. What carries both detections is LAG CONCENTRATION --
    every real trace lands in a narrow interval where the null is spread over the whole search
    window -- plus the cross-mic bound. Never single-trace rise_db.
    """
    out = []
    for a in anchors_s:
        r = second_arrival(x, fs, float(a), **kw)
        if r is not None:
            out.append(r["rise_db"])
    return np.asarray(out, dtype=float)


class IntervalCheck(dict):
    """dict with a truthy `valid`, so `if check_array_intervals(...):` reads correctly.

    ⚠️`valid` ALONE IS NOT A RESULT. Read `discriminating` beside it: False means the 2d/c bound
    was too loose to fail on this geometry, so `valid` is True by construction and carries no
    information. `usable_as_gate` is the conjunction, and `position_floor_m` is the quantity that
    replaces the verdict when there is a sigma to compute it from.
    """
    def __bool__(self) -> bool:
        return bool(self.get("valid"))

    @property
    def usable_as_gate(self) -> bool:
        """True only when the check both passed AND could have failed."""
        return bool(self.get("valid")) and bool(self.get("discriminating"))


def interval_consistent(intervals_s: Dict[int, float], mic_positions, c: float = 345.238,
                        tol_s: Optional[float] = None,
                        sigma_s: Optional[float] = None) -> IntervalCheck:
    """Gate the crack-blast interval ACROSS microphones.

    dt_i = t_blast_i - t_crack_i, so any constant per-channel clock offset cancels -- which is
    what makes this usable on the ESP ring, whose per-board offsets are tens of milliseconds and
    drift.  What does NOT cancel is geometry: dt_i - dt_j = tau_blast(i,j) - tau_crack(i,j), a
    difference of two arrival-time differences, each bounded by d(i,j)/c.  So

        |dt_i - dt_j| <= 2 * d(i,j) / c

    is a free physical bound on a quantity the ring offsets cannot corrupt.  It is the only
    cross-board check on this array that needs neither the offsets nor a survey.

    Note additivity has NOTHING to say here: dt is one number per mic, so every loop of pairwise
    differences closes by construction.  The bound is the whole test.

    A tolerance is MANDATORY -- pass `tol_s`, or `sigma_s` (per-mic 1-sigma) and get 3*sqrt(2)*
    sigma.  Without one this is not a test but a trap: two mics on one ESP board are 38 mm apart,
    so their bound is 0.22 ms, below any onset noise this detector achieves, and every event
    "fails".  Measure sigma from same-board replicates -- their true intervals cannot differ by
    more than that same 0.22 ms, so their observed scatter IS the noise, with no ground truth.

    ⚠️AND THE OPPOSITE TRAP, MEASURED. The bound scales with separation, so far apart it cannot
    fail. On the three 2026-09-05 phones (10.132 / 13.012 / 20.005 m) it is 58.7 / 75.4 / 115.9 ms
    against an onset scatter near 2 ms: every pair passed on every burst, including pairs whose
    implied ranges differed by a factor of two (dt 41.2 ms vs 91.6 ms on one shot). A check that
    cannot fail is not evidence the picks are right. For the bound to discriminate you need 2d/c
    comparable to the scatter -- d <~ 0.35 m at 2 ms -- which is a CO-LOCATED pair, i.e. exactly
    what a node array is and exactly what surveyed flags tens of metres apart are not.

    ⚠️SO DO NOT REPORT `valid`. `discriminating` says whether the bound could have failed at all,
    `tightest_bound_slack` says by how far, and `position_floor_m` (given `sigma_s`) is the
    CRLB-derived quantity that replaces the verdict -- see `position_error_floor_m`.
    """
    if (tol_s is None) == (sigma_s is None):
        raise ValueError("pass exactly one of tol_s or sigma_s: a bound with no tolerance is "
                         "not a test (38 mm pairs bound at 0.22 ms and always fail)")
    if tol_s is None:
        tol_s = 3.0 * math.sqrt(2.0) * float(sigma_s)
    P = np.asarray(mic_positions, dtype=float)
    keys = sorted(intervals_s)
    pairs, bad = [], []
    slacks = []
    for a in range(len(keys)):
        for b in range(a + 1, len(keys)):
            i, j = keys[a], keys[b]
            d = float(np.linalg.norm(P[i] - P[j]))
            ddt = float(intervals_s[i] - intervals_s[j])
            ok = physically_possible(ddt / 2.0, d, c, tol_s=tol_s / 2.0)
            # How much room the bound has over the tolerance. > 1 means a wrong pick has to be
            # bigger than the tolerance by this factor before the bound notices it at all.
            slack = float("inf") if tol_s <= 0.0 else (2.0 * d / c) / float(tol_s)
            slacks.append(slack)
            pairs.append(dict(pair=(i, j), spacing_m=d, ddt_s=ddt, max_ddt_s=2.0 * d / c,
                              within_bound=bool(ok), bound_slack=slack))
            if not ok:
                bad.append(f"|dt{i}-dt{j}| = {abs(ddt)*1e3:.2f} ms > 2d/c = {2*d/c*1e3:.2f} ms")
    v = np.array([intervals_s[k] for k in keys], dtype=float)
    tightest = min(slacks) if slacks else float("inf")
    discriminating = bool(tightest <= MAX_USEFUL_SLACK)
    floor_m = (position_error_floor_m(float(sigma_s)) if sigma_s is not None else None)
    return IntervalCheck(
        valid=not bad, reasons=bad, pairs=pairs,
        interval_s=float(np.median(v)), spread_s=float(v.max() - v.min()),
        n_mics=len(keys),
        # ⚠️the three fields that stop `valid` from being read as a result on its own.
        discriminating=discriminating,
        tightest_bound_slack=tightest,
        position_floor_m=floor_m,
        note=(None if discriminating else
              "the tightest pair's bound is %.0fx its tolerance (%.2f ms over %.2f ms), so no "
              "onset error this detector produces can fail it: `valid` here is true by "
              "construction. Report position_floor_m = %s instead -- pass sigma_s to get it."
              % (tightest, tightest * tol_s * 1e3, tol_s * 1e3,
                 "%.2f m" % floor_m if floor_m is not None else "(needs sigma_s)")))
