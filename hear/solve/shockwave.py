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
          offset_range_m: float = 150.0, offset_step_m: float = 0.25) -> Dict:
    """Fit bearing and offset to shock arrival times.

    `positions` are 2D east/north metres in a common local frame; `arrivals` are absolute seconds
    on a shared clock (PPS-disciplined per node -- the radio never carries time).

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
    dt_obs = t[1:] - t[0]
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
        t = a[:, None] / v_mps + np.abs(q[:, None] - offs[None, :]) * k
        pred = t[1:, :] - t[0, :]
        r = np.sum((pred - dt_obs[:, None]) ** 2, axis=0)
        i = int(np.argmin(r))
        if best is None or r[i] < best[0]:
            best = (float(r[i]), br, float(offs[i]))
    r, br, off = best
    n_eq = len(P) - 1
    obs = straddles(P, br, off)
    rms_ms = math.sqrt(r / n_eq) * 1000.0
    return {
        "bearing_deg": math.degrees(br) % 360.0,
        "offset_m": off if obs else None,
        "offset_observable": obs,
        "rms_residual_ms": rms_ms,
        "residual_is_meaningful": n_eq > 2,
        "n_nodes": len(P), "n_equations": n_eq,
        "sound_speed_mps": c,
        "mach_angle_deg": mach_angle_deg(v_mps, c),
        "note": None if obs else
                "nodes all lie on one side of the fitted track: offset is UNOBSERVABLE "
                "(every parallel track fits identically). Bearing is still valid.",
    }


def miss_distance_m(P, bearing_rad: float, offset_m: float) -> float:
    _, n = _axes(bearing_rad)
    return abs(float(np.dot(np.asarray(P, float)[:2] - n * offset_m, n)))
