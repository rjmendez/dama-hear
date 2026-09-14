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
import time
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


# ── fleet temperature averaging & TDoA variance propagation ───────────────────────────────────

@dataclass(frozen=True)
class FleetTemperatureEstimate:
    """Aggregated temperature statistics across network nodes."""
    mean_temp_c: float
    variance_temp_c2: float
    sigma_temp_c: float
    node_count: int
    source: str


@dataclass(frozen=True)
class EffectiveSoundSpeed:
    """Effective acoustic wave speed and uncertainty bounds."""
    c_mps: float
    temp_c: float
    variance_c: float          # (m/s)^2
    sigma_c_mps: float         # m/s
    is_local: bool
    source: str

    def range_variance_m2(self, tau_s: float, sigma_tau_s: float = 0.0) -> float:
        """Propagate sound speed and timing uncertainty into range variance sigma_d^2.

        d = c * tau
        sigma_d^2 = tau^2 * sigma_c^2 + c^2 * sigma_tau^2
        """
        tau = float(tau_s)
        st = float(sigma_tau_s)
        return (tau * tau) * self.variance_c + (self.c_mps * self.c_mps) * (st * st)

    def range_sigma_m(self, tau_s: float, sigma_tau_s: float = 0.0) -> float:
        return math.sqrt(self.range_variance_m2(tau_s, sigma_tau_s))


class FleetTemperatureProvider:
    """Aggregates node temperature telemetry and computes effective speed of sound c and variance.

    Formula: c = 331.3 + 0.606 * T
    Variances: sigma_c = 0.606 * sigma_T,  sigma_c^2 = (0.606)^2 * sigma_T^2

    Handles local sensor nodes, bare mic nodes (missing local sensors using fleet-wide average),
    and fleet-wide default fallbacks when no sensor reports exist.
    """
    def __init__(self, fallback_temp_c: float = 20.0,
                 fallback_sigma_temp_c: float = 5.0,
                 default_sensor_sigma_c: float = 0.5,
                 max_age_s: float = 600.0,
                 min_variance_floor: float = 0.01):
        self.fallback_temp_c = float(fallback_temp_c)
        self.fallback_sigma_temp_c = float(fallback_sigma_temp_c)
        self.default_sensor_sigma_c = float(default_sensor_sigma_c)
        self.max_age_s = float(max_age_s)
        self.min_variance_floor = float(min_variance_floor)
        self._readings: Dict[str, Tuple[float, float, float]] = {}  # node_id -> (temp_c, ts_s, sigma_T)

    def update_node_temperature(self, node_id: str, temp_c: float,
                                timestamp_s: Optional[float] = None,
                                sensor_sigma_temp_c: Optional[float] = None) -> bool:
        """Record or update a node's temperature reading (in degC). Return True if accepted."""
        try:
            t = float(temp_c)
        except (TypeError, ValueError):
            return False
        if not (-50.0 <= t <= 60.0):
            return False
        ts = time.time() if timestamp_s is None else float(timestamp_s)
        sig = (self.default_sensor_sigma_c
               if sensor_sigma_temp_c is None or sensor_sigma_temp_c <= 0
               else float(sensor_sigma_temp_c))
        self._readings[str(node_id)] = (t, ts, sig)
        return True

    def update_node_telemetry(self, node_id: str, telemetry: dict,
                              timestamp_s: Optional[float] = None) -> bool:
        """Extract temperature from node telemetry frame/payload dictionary."""
        if not isinstance(telemetry, dict):
            return False
        t_val = telemetry.get("temp_c")
        if t_val is None:
            env = telemetry.get("env")
            if isinstance(env, dict):
                t_val = env.get("temp_c")
        if t_val is None:
            c_val = telemetry.get("sound_speed_mps")
            if c_val is not None:
                try:
                    c_f = float(c_val)
                    if 310.0 <= c_f <= 370.0:
                        t_val = temperature_c(c_f)
                except (TypeError, ValueError):
                    pass
        if t_val is None:
            return False
        ts = timestamp_s
        if ts is None:
            ts_ms = telemetry.get("ts_utc_ms")
            if ts_ms is not None:
                ts = float(ts_ms) / 1000.0
        sig = telemetry.get("temp_sigma_c")
        return self.update_node_temperature(node_id, t_val, ts, sig)

    def get_fleet_temperature(self, now_s: Optional[float] = None) -> FleetTemperatureEstimate:
        """Aggregate temperature statistics across fresh network node readings."""
        now = time.time() if now_s is None else float(now_s)
        fresh = [(t, ts, sig) for (t, ts, sig) in self._readings.values()
                 if (now - ts) <= self.max_age_s]

        if not fresh:
            var_t = self.fallback_sigma_temp_c ** 2
            return FleetTemperatureEstimate(
                mean_temp_c=self.fallback_temp_c,
                variance_temp_c2=var_t,
                sigma_temp_c=self.fallback_sigma_temp_c,
                node_count=0,
                source="fallback_default"
            )

        temps = [f[0] for f in fresh]
        sigmas = [f[2] for f in fresh]
        n = len(temps)
        mean_t = float(np.mean(temps))

        if n == 1:
            var_t = (sigmas[0] ** 2) + self.min_variance_floor
            source = "measured_n1"
        else:
            sample_var = float(np.var(temps, ddof=1))
            sensor_var_mean = float(np.mean([s ** 2 for s in sigmas])) / n
            var_t = max(sample_var, sensor_var_mean) + self.min_variance_floor
            source = f"measured_n{n}"

        sigma_t = math.sqrt(var_t)
        return FleetTemperatureEstimate(
            mean_temp_c=mean_t,
            variance_temp_c2=var_t,
            sigma_temp_c=sigma_t,
            node_count=n,
            source=source
        )

    def get_effective_sound_speed(self, node_id: Optional[str] = None,
                                  now_s: Optional[float] = None) -> EffectiveSoundSpeed:
        """Compute effective speed of sound c and variance for a node or fleet default."""
        now = time.time() if now_s is None else float(now_s)

        if node_id is not None and str(node_id) in self._readings:
            t, ts, sig_t = self._readings[str(node_id)]
            if (now - ts) <= self.max_age_s:
                c = sound_speed(t)
                var_t = (sig_t ** 2) + self.min_variance_floor
                sig_c = DC_DT * math.sqrt(var_t)
                var_c = sig_c ** 2
                return EffectiveSoundSpeed(
                    c_mps=c,
                    temp_c=t,
                    variance_c=var_c,
                    sigma_c_mps=sig_c,
                    is_local=True,
                    source=f"local_{node_id}"
                )

        fleet_est = self.get_fleet_temperature(now)
        c = sound_speed(fleet_est.mean_temp_c)
        sig_c = DC_DT * fleet_est.sigma_temp_c
        var_c = sig_c ** 2
        return EffectiveSoundSpeed(
            c_mps=c,
            temp_c=fleet_est.mean_temp_c,
            variance_c=var_c,
            sigma_c_mps=sig_c,
            is_local=False,
            source=fleet_est.source
        )


def propagate_tdoa_variance(tau_s: float, sigma_tau_s: float,
                           c_mps: float = 343.0,
                           sigma_c_mps: float = 0.0) -> Dict[str, float]:
    """Propagate timing delay uncertainty and sound speed uncertainty into range/TDoA variance.

    d = c * tau
    sigma_d^2 = tau^2 * sigma_c^2 + c^2 * sigma_tau^2
    """
    tau = float(tau_s)
    st = float(sigma_tau_s)
    c = float(c_mps)
    sc = float(sigma_c_mps)

    var_d = (tau * tau) * (sc * sc) + (c * c) * (st * st)
    sigma_d = math.sqrt(var_d)

    var_tau_total = st * st + ((tau * sc / c) ** 2 if c > 0 else 0.0)
    sigma_tau_total = math.sqrt(var_tau_total)

    return {
        "range_diff_m": c * tau,
        "range_diff_var_m2": var_d,
        "range_diff_sigma_m": sigma_d,
        "tau_sigma_effective_s": sigma_tau_total,
        "c_mps": c,
        "sigma_c_mps": sc,
    }


def propagate_pairwise_tdoa_uncertainty(
    node_a_id: str, node_b_id: str, tau_s: float, sigma_tau_s: float,
    baseline_distance_m: Optional[float] = None,
    provider: Optional[FleetTemperatureProvider] = None,
    now_s: Optional[float] = None
) -> Dict[str, float]:
    """Propagate temperature and timing uncertainties across a pairwise baseline (Node A <-> Node B)."""
    if provider is None:
        provider = FleetTemperatureProvider()

    eff_a = provider.get_effective_sound_speed(node_a_id, now_s)
    eff_b = provider.get_effective_sound_speed(node_b_id, now_s)

    c_eff = 0.5 * (eff_a.c_mps + eff_b.c_mps)
    var_c_eff = 0.25 * (eff_a.variance_c + eff_b.variance_c)
    sigma_c_eff = math.sqrt(var_c_eff)

    res = propagate_tdoa_variance(tau_s, sigma_tau_s, c_eff, sigma_c_eff)
    res.update({
        "node_a_id": node_a_id,
        "node_b_id": node_b_id,
        "c_node_a_mps": eff_a.c_mps,
        "c_node_b_mps": eff_b.c_mps,
        "source_a": eff_a.source,
        "source_b": eff_b.source,
    })

    if baseline_distance_m is not None and baseline_distance_m > 0:
        d_base = float(baseline_distance_m)
        max_tau_bound = d_base / c_eff if c_eff > 0 else 0.0
        speed_bound_uncertainty_s = (d_base / (c_eff * c_eff)) * sigma_c_eff if c_eff > 0 else 0.0
        res["baseline_distance_m"] = d_base
        res["max_tdoa_bound_s"] = max_tau_bound
        res["max_tdoa_sigma_s"] = speed_bound_uncertainty_s

    return res


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
    variance_c_m2s2: Optional[float] = None
    variance_temp_c2: Optional[float] = None

    def require_temperature(self) -> Tuple[float, float]:
        if not self.valid or self.temp_c is None:
            raise SoundSpeedUnrecoverable("; ".join(self.reasons) or "no usable inversion")
        return self.temp_c, self.temp_sigma_c or 0.0

    def tdoa_variance_m2(self, tau_s: float, sigma_tau_s: float = 0.0) -> float:
        if self.c_mps is None or self.c_sigma_mps is None:
            raise ValueError("SpeedVerdict has no valid c_mps/c_sigma_mps")
        sc = self.c_sigma_mps
        c = self.c_mps
        st = float(sigma_tau_s)
        return (tau_s * tau_s) * (sc * sc) + (c * c) * (st * st)

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
    tc = temperature_c(c)
    st = temperature_sigma_c(c, sc)
    return SpeedVerdict(True, [], c_mps=c, c_sigma_mps=sc,
                        temp_c=tc, temp_sigma_c=st,
                        variance_c_m2s2=sc * sc, variance_temp_c2=st * st)


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
    tc = temperature_c(c)
    st = temperature_sigma_c(c, sigma)
    return SpeedVerdict(True, [], c_mps=c, c_sigma_mps=sigma, temp_c=tc,
                        temp_sigma_c=st,
                        variance_c_m2s2=sigma * sigma, variance_temp_c2=st * st)
