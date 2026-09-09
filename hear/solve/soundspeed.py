#!/usr/bin/env python3
"""Recovering c -- and therefore air temperature -- from acoustic arrivals, or refusing to.

Every range this project quotes is a time multiplied by c, and c was never measured. On
2026-09-05 no thermometer was read all session, so `c = 345.238 m/s` (23 C, from the
operator's recollection) is a free parameter propagated into every bearing, every TDoA
bound and every shockwave fit. This module is about getting it from the acoustics instead,
and -- more often -- about saying honestly that a given data set cannot.

    c(T) = 331.3 + 0.606 T    (m/s, C)

0.606 m/s per degC is 0.176% of c. That single number sets every budget here: **a 1 degC
temperature costs 0.176% of whatever propagation time you measure**, so recovering T to
+/-1 degC over a 36 m path (105 ms) needs that path timed to +/-185 us.

## The two ways to get c, and what each one costs

**1. Absolute.** Time the flight over a surveyed range: c = R / (t_arrive - t_emit).
Needs BOTH ends. A surveyed range and a good arrival clock are not enough -- without an
emission observable (a trigger sensor, a muzzle-flash frame, or a receiver co-located with
the source) there is no t_emit and the inversion does not exist. `absolute_budget()` states
what timing precision the range demands before anyone goes looking for one.

**2. Relative, over a known baseline.** Put a clock-synced receiver AT the source. Then
the delay to a second surveyed receiver IS the propagation time over the surveyed
separation, and c = d / tau with no emission time anywhere. `speed_from_baseline()`.

## ⚠️Three receivers and one unknown source CANNOT determine c

N receivers on a shared clock give N-1 independent delays per event. An event with an
unknown source costs `dim` unknowns (2 on the ground, 3 in the air) plus the one shared
unknown c. Three ground receivers: 2 equations, 3 unknowns. Under-determined by exactly
one, for any number of events and any number of bursts -- extra rounds from the same spot
repeat the same two equations, and a new firing point brings its own two unknowns with it.

This is not a conditioning problem that better data fixes; `determines_speed()` counts it.
MEASURED on the three surveyed phones of 2026-09-05: refitting a gated burst at c = 320
and at c = 360 m/s (a 12.5% span, -19 C to +47 C) moved the fitted source by **0.73 m** and
left the delay residual at **0.00 ms** at both ends. Reading c off that geometry would need
the source position surveyed to ~6 cm.

## What a delay alone does give you

|tau| <= d/c is one-sided in c as well as in tau: a delay measured over a surveyed pair
means `c <= d/|tau|`. It needs no source position and no emission time, so it survives when
everything else fails -- see `speed_upper_bound()`. It is weak (the best of the 2026-09-05
phone pairs gives c <= 369.5 m/s, T <= 63 C) but it is a measurement, not an assumption.

`apparent_speed()` is the other assumption-free reading: 1/|s| for the slowness projected
onto the receivers' own plane. Elevation and wavefront curvature both inflate it, so it is
an upper bound too, approached only by a wave travelling in that plane.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .shockwave import sound_speed  # noqa: F401  (re-exported: one definition of c per repo)

DC_DT = 0.606          # m/s per degC
C0 = 331.3             # m/s at 0 C


def temperature_c(c: float) -> float:
    """Inverse of `sound_speed`: dry-air temperature in degC implied by c in m/s."""
    return (float(c) - C0) / DC_DT


def temperature_sigma_c(c: float, sigma_c: float) -> float:
    """degC uncertainty from a m/s uncertainty. Linear: sigma_T = sigma_c / 0.606."""
    return abs(float(sigma_c)) / DC_DT


def fractional_speed_error(delta_t_c: float) -> float:
    """Fraction of c that a temperature error of `delta_t_c` degC is worth, at ~20 C.

    0.176% per degC. Quoted so that a timing budget can be argued in per-cent rather than
    in degrees, which is where the sign of an error is easy to lose.
    """
    return abs(float(delta_t_c)) * DC_DT / sound_speed(20.0)


# ── the two inversions ──────────────────────────────────────────────────────────────────────────

def speed_from_baseline(tau_s: float, d_m: float) -> float:
    """c from a delay measured over a KNOWN separation with the source at one receiver.

    This is the whole relative method: if a clock-synced node sits at the source, the delay
    to a second surveyed node is the propagation time over the surveyed separation. No
    emission time, no absolute clock, no source solve.
    """
    tau = abs(float(tau_s))
    if tau <= 0.0:
        raise ValueError("delay must be > 0 s (got %r): a zero delay carries no speed" % tau_s)
    if d_m <= 0.0:
        raise ValueError("separation must be > 0 m (got %r)" % d_m)
    return float(d_m) / tau


def speed_upper_bound(tau_s: float, d_m: float) -> float:
    """c <= d/|tau| -- the plane-wave bound read as a bound on the SPEED, not on the delay.

    `consistency.physically_possible` asks whether a delay is possible at an assumed c.
    Turned around, the same inequality is the only statement about c that a single surveyed
    pair can make without knowing where the source was. One-sided and usually weak, but it
    costs nothing and it is falsifiable.
    """
    if d_m <= 0.0:
        raise ValueError("separation must be > 0 m (got %r)" % d_m)
    tau = abs(float(tau_s))
    if tau <= 0.0:
        return math.inf
    return float(d_m) / tau


def apparent_speed(positions, arrivals) -> float:
    """1/|s| for the slowness projected onto the receivers' own plane. An UPPER bound on c.

    A wavefront in a homogeneous medium has slowness magnitude 1/c whatever emitted it --
    muzzle blast, Mach cone or echo. Receivers measure only the projection of that vector
    into the subspace they span, so the recovered magnitude is |s| cos(angle out of the
    plane) <= 1/c, i.e. the apparent speed is >= c. Wavefront curvature over a finite
    aperture pushes it the same way. Equality only for a wave travelling in the plane.
    """
    P = np.asarray(positions, dtype=float)
    t = np.asarray(arrivals, dtype=float).reshape(-1)
    if P.ndim != 2 or P.shape[0] < 3 or P.shape[0] != t.shape[0]:
        raise ValueError("need >= 3 positions and one arrival each, got %r / %r"
                         % (P.shape, t.shape))
    s = np.linalg.pinv(P[1:] - P[0]) @ (t[1:] - t[0])
    n = float(np.linalg.norm(s))
    return math.inf if n == 0.0 else 1.0 / n


# ── budgets: what precision the question actually demands ───────────────────────────────────────

@dataclass(frozen=True)
class Budget:
    """What a given geometry costs in timing, and whether a clock can pay it."""
    path_m: float
    c_mps: float
    path_time_s: float
    target_dT_c: float
    required_sigma_s: float
    available_sigma_s: Optional[float] = None

    @property
    def feasible(self) -> Optional[bool]:
        if self.available_sigma_s is None:
            return None
        return self.available_sigma_s <= self.required_sigma_s

    @property
    def shortfall(self) -> Optional[float]:
        """How many times too coarse the available clock is. > 1 means it cannot pay."""
        if self.available_sigma_s is None:
            return None
        return self.available_sigma_s / self.required_sigma_s

    def achievable_dT_c(self) -> Optional[float]:
        """The temperature precision the available clock DOES buy over this path."""
        if self.available_sigma_s is None:
            return None
        return self.target_dT_c * self.shortfall

    def summary(self) -> str:
        head = ("path %.2f m = %.3f ms at c = %.3f m/s; +/-%.2f degC needs the path timed to "
                "+/-%.0f us" % (self.path_m, self.path_time_s * 1e3, self.c_mps,
                                self.target_dT_c, self.required_sigma_s * 1e6))
        if self.available_sigma_s is None:
            return head
        return head + ("; the clock offered is +/-%.0f us -- %.0fx too coarse, worth +/-%.0f degC"
                       % (self.available_sigma_s * 1e6, self.shortfall, self.achievable_dT_c()))


def separation_for_temperature(target_dT_c: float, sigma_d_m: float,
                               sigma_tau_s: float = 0.0, c: Optional[float] = None,
                               temp_c: float = 20.0) -> Dict[str, float]:
    """How much RANGE DIFFERENCE a known-source pair needs to recover T to `target_dT_c`.

    This is the inversion the dama-gotchi phone chirp makes available, and it is the only one on
    this fleet that needs no emission time and no per-handset audio offset: a phone at a SURVEYED
    point emits, two clock-synced nodes at surveyed points hear it, and

        c = (d_2 - d_1) / (t_2 - t_1)

    -- the emission instant and every constant in the phone's playback path cancel in the
    difference. With tau = Delta_d / c,

        sigma_c / c = sqrt(sigma_Delta_d^2 + c^2 sigma_tau^2) / Delta_d

    so the required separation is c * sqrt(...) / (0.606 * dT). `sigma_d_m` is the error in
    Delta_d, i.e. the two NODE positions differenced -- a source far out on the baseline extension
    contributes only second-order, which is where to stand.

    ⚠️THE SURVEY TERM DOES NOT AVERAGE DOWN OVER CHIRPS and the timing term does. Measured on
    survey.json, the existing pair is the binding case: sigma_m 0.717 and 0.521 m give
    sigma_Delta_d = 0.89 m against a maximum Delta_d of 16.873 m -- 5.3%, worth 18 m/s, worth
    30 degC. The pair cannot measure air temperature to any useful precision until it is
    re-surveyed, whatever the clock does.
    """
    c = sound_speed(temp_c) if c is None else float(c)
    if target_dT_c <= 0.0:
        raise ValueError("target dT must be > 0 degC (got %r)" % target_dT_c)
    if sigma_d_m < 0.0 or sigma_tau_s < 0.0:
        raise ValueError("sigmas must be >= 0")
    num = math.hypot(float(sigma_d_m), c * float(sigma_tau_s))
    need = c * num / (DC_DT * float(target_dT_c))
    return {
        "required_separation_m": need,
        "required_delay_s": need / c,
        "survey_only_separation_m": c * float(sigma_d_m) / (DC_DT * float(target_dT_c)),
        "target_dT_c": float(target_dT_c),
        "sigma_c_mps": DC_DT * float(target_dT_c),
        "c_mps": c,
    }


def temperature_from_separation(separation_m: float, sigma_d_m: float,
                                sigma_tau_s: float = 0.0, c: Optional[float] = None,
                                temp_c: float = 20.0) -> Dict[str, float]:
    """The inverse of `separation_for_temperature`: what dT a given range difference buys.

    Reported term by term because the two error sources behave differently over repeated chirps --
    the timing term averages as 1/sqrt(N), the survey term does not move at all until someone
    re-surveys the nodes.
    """
    c = sound_speed(temp_c) if c is None else float(c)
    d = float(separation_m)
    if d <= 0.0:
        raise ValueError("separation must be > 0 m (got %r)" % separation_m)
    f_survey = float(sigma_d_m) / d
    f_timing = c * float(sigma_tau_s) / d
    frac = math.hypot(f_survey, f_timing)
    return {
        "separation_m": d,
        "sigma_c_mps": frac * c,
        "dT_c": frac * c / DC_DT,
        "dT_c_survey_term": f_survey * c / DC_DT,
        "dT_c_timing_term": f_timing * c / DC_DT,
        "c_mps": c,
    }


def absolute_budget(path_m: float, target_dT_c: float = 1.0, c: Optional[float] = None,
                    temp_c: float = 20.0, available_sigma_s: Optional[float] = None) -> Budget:
    """Timing precision a path must be measured to for a target temperature precision.

    sigma_t / t = sigma_c / c, and sigma_c = 0.606 * dT, so sigma_t = t * 0.606 * dT / c.
    """
    c = sound_speed(temp_c) if c is None else float(c)
    if path_m <= 0.0:
        raise ValueError("path must be > 0 m (got %r)" % path_m)
    if target_dT_c <= 0.0:
        raise ValueError("target dT must be > 0 degC (got %r)" % target_dT_c)
    t = float(path_m) / c
    return Budget(float(path_m), c, t, float(target_dT_c), t * DC_DT * float(target_dT_c) / c,
                  None if available_sigma_s is None else float(available_sigma_s))


# ── the counting argument ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Determinacy:
    equations: int
    unknowns: int
    determined: bool
    reason: str

    @property
    def deficit(self) -> int:
        return self.unknowns - self.equations


def determines_speed(n_receivers: int, n_events: int = 1, dim: int = 2,
                     n_known_sources: int = 0, shared_clock: bool = True,
                     n_unknown_clock_offsets: int = 0) -> Determinacy:
    """Can this configuration determine c at all? Counts equations against unknowns.

    An `event` here is one SOURCE POSITION, not one round: twenty rounds fired from the
    same spot repeat the same delays and count once. Each event gives `n_receivers - 1`
    independent delays (a common emission time always cancels). Each event with an UNKNOWN
    source costs `dim` unknowns; an event whose source is surveyed costs none. c is one unknown shared by every event. Unsynchronised receivers
    cost one constant offset each -- `n_unknown_clock_offsets` covers the uncalibrated
    per-device microphone latency that phone clips ship with (`mic_bias_ns=0 UNCALIBRATED`).

    ⚠️This is necessary, not sufficient: it counts degrees of freedom and says nothing about
    conditioning, geometry or noise. It exists to catch the case that no amount of data can
    fix -- three receivers and an unknown source, which is 2 equations against 3 unknowns
    however many rounds are fired.
    """
    if n_receivers < 2:
        raise ValueError("need >= 2 receivers (got %d): one receiver has no delay" % n_receivers)
    if n_events < 1:
        raise ValueError("need >= 1 event (got %d)" % n_events)
    if n_known_sources > n_events:
        raise ValueError("cannot have more known sources (%d) than events (%d)"
                         % (n_known_sources, n_events))
    eq = n_events * (n_receivers - 1)
    unk = 1 + (n_events - n_known_sources) * int(dim) + int(n_unknown_clock_offsets)
    if not shared_clock:
        unk += n_receivers - 1
    ok = eq >= unk
    if ok:
        reason = ("%d equations vs %d unknowns: c is determined" % (eq, unk))
    else:
        reason = ("%d equations vs %d unknowns -- under-determined by %d. Every c in the "
                  "plausible range has a source that fits the delays exactly; more events at "
                  "new firing points bring their own unknowns and do not help. Survey a source "
                  "position, or put a synced receiver ON one." % (eq, unk, unk - eq))
    return Determinacy(eq, unk, ok, reason)


# ── the honest top-level call ───────────────────────────────────────────────────────────────────

@dataclass
class SpeedVerdict:
    """c and T, or a refusal. `temp_c` is None whenever `valid` is False."""
    valid: bool
    reasons: List[str]
    c_mps: Optional[float] = None
    c_sigma_mps: Optional[float] = None
    temp_c: Optional[float] = None
    temp_sigma_c: Optional[float] = None
    c_upper_mps: Optional[float] = None
    temp_upper_c: Optional[float] = None
    note: Optional[str] = None

    def require_temperature(self) -> Tuple[float, float]:
        if not self.valid or self.temp_c is None:
            raise SoundSpeedUnrecoverable("; ".join(self.reasons) or "no usable inversion")
        return self.temp_c, self.temp_sigma_c

    def summary(self) -> str:
        if self.valid:
            return "c = %.2f +/- %.2f m/s  ->  T = %.1f +/- %.1f degC" % (
                self.c_mps, self.c_sigma_mps or 0.0, self.temp_c, self.temp_sigma_c or 0.0)
        head = "REFUSED [" + "; ".join(self.reasons) + "]"
        if self.c_upper_mps is not None:
            head += "  bound only: c <= %.1f m/s, T <= %.1f degC" % (self.c_upper_mps,
                                                                    self.temp_upper_c)
        return head


class SoundSpeedUnrecoverable(Exception):
    """Raised by SpeedVerdict.require_temperature() when no inversion is supported."""


def recover_from_baseline(tau_s: float, d_m: float, sigma_tau_s: float,
                          sigma_d_m: float = 0.0) -> SpeedVerdict:
    """c and T from a delay over a surveyed separation with the source at one receiver.

    Both error terms are propagated: sigma_c/c = sqrt((sigma_d/d)^2 + (sigma_tau/tau)^2). The
    timing term dominates at every geometry this project has -- an RTK separation is good to
    ~2 cm out of 10 m (0.2%), a 110 us phone sync out of a 29 ms delay is 0.37%.
    """
    reasons: List[str] = []
    if abs(tau_s) <= 0.0:
        reasons.append("delay is zero: the source is equidistant, this pair carries no speed")
    if d_m <= 0.0:
        reasons.append("separation must be > 0 m")
    if sigma_tau_s < 0 or sigma_d_m < 0:
        reasons.append("sigmas must be >= 0")
    if reasons:
        return SpeedVerdict(False, reasons)
    c = speed_from_baseline(tau_s, d_m)
    frac = math.hypot(sigma_d_m / float(d_m), float(sigma_tau_s) / abs(float(tau_s)))
    sc = c * frac
    return SpeedVerdict(True, [], c_mps=c, c_sigma_mps=sc,
                        temp_c=temperature_c(c), temp_sigma_c=temperature_sigma_c(c, sc))


def recover_from_delays(taus: Dict[Tuple[int, int], float], receiver_positions,
                        source_position=None, sigma_tau_s: float = 0.0,
                        n_unknown_clock_offsets: int = 0) -> SpeedVerdict:
    """c from a set of pairwise delays. REFUSES when the configuration cannot determine it.

    With `source_position` given, every pair contributes an independent estimate
    c = (d_j - d_i) / tau(i,j) and the spread across pairs is a real consistency check --
    it is the same test the delays' own ratio makes, and it needs no assumed c.
    Without it, three receivers are under-determined and the call returns the plane-wave
    UPPER bound instead of a number.
    """
    P = np.asarray(receiver_positions, dtype=float)
    if P.ndim == 1:
        P = P[:, None]
    n = P.shape[0]
    pairs = [(i, j) for i, j in itertools.combinations(range(n), 2)
             if (i, j) in taus or (j, i) in taus]
    if not pairs:
        return SpeedVerdict(False, ["no delays given"])

    def tau(i, j):
        return float(taus[(i, j)]) if (i, j) in taus else -float(taus[(j, i)])

    ub = min(speed_upper_bound(tau(i, j), float(np.linalg.norm(P[j] - P[i]))) for i, j in pairs)
    if source_position is None:
        det = determines_speed(n, 1, dim=P.shape[1],
                               n_unknown_clock_offsets=n_unknown_clock_offsets)
        if not det.determined:
            return SpeedVerdict(False, [det.reason], c_upper_mps=ub,
                                temp_upper_c=temperature_c(ub),
                                note="the plane-wave bound is the only statement about c that "
                                     "survives an unknown source")
    if source_position is None:
        return SpeedVerdict(False, ["a source position is required to invert delays for c"],
                            c_upper_mps=ub, temp_upper_c=temperature_c(ub))

    S = np.asarray(source_position, dtype=float).reshape(-1)
    if S.shape[0] != P.shape[1]:
        raise ValueError("source has %d components, receivers have %d" % (S.shape[0], P.shape[1]))
    d = np.linalg.norm(P - S, axis=1)
    est, wt = [], []
    for i, j in pairs:
        t = tau(i, j)
        dd = d[j] - d[i]
        if abs(t) <= 0.0:
            continue
        est.append(dd / t)
        wt.append(abs(t))
    if not est:
        return SpeedVerdict(False, ["every delay is zero at this source: no speed information"],
                            c_upper_mps=ub, temp_upper_c=temperature_c(ub))
    est = np.asarray(est, float)
    wt = np.asarray(wt, float)
    c = float(np.sum(est * wt) / np.sum(wt))
    reasons = []
    if len(est) > 1 and float(np.ptp(est)) > 0.05 * abs(c):
        reasons.append("pairs disagree on c by %.1f m/s (%.1f%%): the assumed source position or "
                       "at least one delay is wrong" % (float(np.ptp(est)), 100 * np.ptp(est) / abs(c)))
    if not (200.0 < c < 500.0):
        reasons.append("c = %.1f m/s is not a speed of sound in air" % c)
    if reasons:
        return SpeedVerdict(False, reasons, c_upper_mps=ub, temp_upper_c=temperature_c(ub))
    sigma = (float(np.std(est, ddof=1)) if len(est) > 1 else
             c * float(sigma_tau_s) / max(abs(tau(*pairs[0])), 1e-12))
    return SpeedVerdict(True, [], c_mps=c, c_sigma_mps=sigma, temp_c=temperature_c(c),
                        temp_sigma_c=temperature_sigma_c(c, sigma))
