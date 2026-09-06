#!/usr/bin/env python3
"""Where to put the nodes. Placement is the binding constraint, so it gets a tool.

The field session measured this and it is the most expensive lesson in the repo: shifting a track
+/-6 m past three SAME-SIDE nodes changed the TDoAs by 0.000 ms, and past straddling nodes by up
to 117 ms. Timing quality did not matter. Geometry did. A network can be built, synced to 30 ns
and deployed, and still be unable in principle to answer the question it was built for.

So this answers two questions BEFORE anyone digs a post hole:

  1. Point source (a blast, a firework, a backfire): how well can this node set locate one, and
     WHERE. Reported as `dop` -- metres of position error per metre of range-difference error.
     Multiply by c * sigma_t for a real number: at 1 us of timing error that scale is 0.34 m.

  2. Supersonic round: is the track's perpendicular offset observable at all? Reuses
     `shockwave.shock_time` rather than restating the cone maths, so this cannot drift from
     the solver it is advising.

⚠️DOP AND REDUNDANCY ARE DIFFERENT QUESTIONS AND BOTH MATTER. Three nodes give two equations for
two unknowns: the fit is exact, the residual is ~0 by construction, and DOP is still finite and
may look excellent. It says how precisely you locate IF nothing is wrong, and nothing about
whether you would notice if something were. `dof` is the second number. Four nodes is where a
residual begins to carry information.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import shockwave as SW

# Position error per metre of range-difference error, above which the geometry is not worth
# deploying. 10 means 1 us of timing error (0.34 m of range) becomes 3.4 m of position error.
DOP_USABLE = 10.0


def _unit_rows(nodes: np.ndarray, source: np.ndarray) -> Optional[np.ndarray]:
    """Unit vectors from each node toward the source. None if the source sits on a node."""
    d = source[None, :] - nodes
    r = np.linalg.norm(d, axis=1)
    if float(r.min()) < 1e-6:
        return None
    return d / r[:, None]


def dop(nodes: Sequence, source: Sequence) -> Dict:
    """Geometric dilution of precision for point-source TDoA at `source`.

    Formulated by marginalising the unknown emission time rather than differencing against a
    chosen reference node, so the answer does not depend on which node you call node 0 -- it is
    a property of the geometry, not of the bookkeeping. The projector (I - 11'/N) IS that
    marginalisation: it removes the direction in measurement space that a common time shift moves.
    """
    P = np.asarray(nodes, float)[:, :2]
    s = np.asarray(source, float)[:2]
    n = len(P)
    if n < 3:
        raise ValueError("need >= 3 nodes")
    G = _unit_rows(P, s)
    dof = (n - 1) - 2
    if G is None:
        return {"dop": float("inf"), "dof": dof, "redundant": dof > 0, "singular": True}
    M = np.eye(n) - np.ones((n, n)) / n
    F = G.T @ M @ G                                   # Fisher information, sigma_r = 1
    try:
        if abs(float(np.linalg.det(F))) < 1e-12:
            raise np.linalg.LinAlgError
        val = float(np.sqrt(np.trace(np.linalg.inv(F))))
    except np.linalg.LinAlgError:
        val = float("inf")                            # collinear nodes, or source on the baseline
    return {"dop": val, "dof": dof, "redundant": dof > 0, "singular": not math.isfinite(val)}


def dop_grid(nodes: Sequence, bounds: Tuple[float, float, float, float],
             step: float = 10.0) -> Dict:
    """DOP sampled over a box. `bounds` is (x_min, y_min, x_max, y_max) in local metres."""
    x0, y0, x1, y1 = bounds
    xs = np.arange(x0, x1 + step * 0.5, step)
    ys = np.arange(y0, y1 + step * 0.5, step)
    g = np.empty((len(ys), len(xs)), float)
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            g[j, i] = dop(nodes, (x, y))["dop"]
    finite = g[np.isfinite(g)]
    return {
        "grid": g, "xs": xs, "ys": ys,
        "median": float(np.median(finite)) if finite.size else float("inf"),
        "usable_frac": float(np.mean(g <= DOP_USABLE)),
    }


def offset_sensitivity(nodes: Sequence, bearing_deg: float, offset_m: float = 0.0,
                       v_mps: float = 900.0, temp_c: float = 20.0,
                       delta_m: float = 6.0) -> Dict:
    """Can this node set see a supersonic track move sideways?

    Shifts the track by `delta_m` and reports the largest change in any TDoA. The field number to
    compare against: same-side nodes gave 0.000 ms for a +/-6 m shift, straddling nodes up to
    117 ms. Uses `shockwave.shock_time`, so it asks the solver's own model.
    """
    P = [np.asarray(p, float)[:2] for p in nodes]
    if len(P) < 3:
        raise ValueError("need >= 3 nodes")
    c = SW.sound_speed(temp_c)
    br = math.radians(bearing_deg)

    def tdoa(off):
        t = np.array([SW.shock_time(p, br, off, v_mps, c) for p in P])
        return t[1:] - t[0]

    swing_ms = float(np.abs(tdoa(offset_m + delta_m) - tdoa(offset_m)).max() * 1000.0)
    return {
        "straddles": SW.straddles(P, br, offset_m),
        "max_tdoa_swing_ms": swing_ms,
        "delta_m": delta_m,
        "observable": swing_ms > 0.05,               # a swing the timebase can actually resolve
        "bearing_deg": bearing_deg,
    }


def observable_span(nodes: Sequence, bearing_deg: float) -> Dict:
    """The band of track offsets this layout can actually measure, at this bearing.

    A track's offset is observable only where the nodes straddle it, so the observable offsets are
    exactly the open interval between the extreme node projections onto the track normal. Outside
    that band every parallel track fits identically -- the field's 0.000 ms result. The width of
    that band, not the timing budget, is what a layout is worth.
    """
    P = [np.asarray(p, float)[:2] for p in nodes]
    _, n = SW._axes(math.radians(float(bearing_deg)))
    q = [float(np.dot(p, n)) for p in P]
    return {"bearing_deg": float(bearing_deg), "span_m": float(max(q) - min(q)),
            "offset_lo": float(min(q)), "offset_hi": float(max(q))}


def worst_bearing(nodes: Sequence, step_deg: float = 5.0, **kw) -> Dict:
    """The bearing whose observable band is narrowest -- the direction the array is thinnest in.

    Judged on span, not on a single sampled track. Sweeping bearing at a fixed offset would sweep
    tracks through the coordinate origin and make the verdict depend on where the survey put
    (0,0); sweeping through the centroid only ever tests tracks that pass THROUGH the array, which
    always straddle. Neither is the field case: the round goes past, not through.
    """
    worst = None
    for b in np.arange(0.0, 180.0, step_deg):
        sp = observable_span(nodes, float(b))
        if worst is None or sp["span_m"] < worst["span_m"]:
            worst = sp
    mid = 0.5 * (worst["offset_lo"] + worst["offset_hi"])
    r = offset_sensitivity(nodes, worst["bearing_deg"], offset_m=mid, **kw)
    return dict(worst, max_tdoa_swing_ms=r["max_tdoa_swing_ms"], observable=r["observable"])


def best_addition(nodes: Sequence, candidates: Sequence,
                  bounds: Tuple[float, float, float, float], step: float = 20.0) -> List[Dict]:
    """Rank candidate sites for the NEXT node by what they actually buy.

    Reports the median DOP with each candidate added, and whether it lifts the blindest bearing
    above the resolvable floor -- which is usually the one that matters and is not the same as
    improving DOP.
    """
    base_med = dop_grid(nodes, bounds, step)["median"]
    base_worst = worst_bearing(nodes)["span_m"]
    out = []
    for cand in candidates:
        trial = list(nodes) + [cand]
        g = dop_grid(trial, bounds, step)
        w = worst_bearing(trial)
        out.append({
            "position": tuple(float(v) for v in np.asarray(cand, float)[:2]),
            "median_dop": g["median"],
            "dop_gain": base_med - g["median"],
            "usable_frac": g["usable_frac"],
            "worst_span_m": w["span_m"],
            "worst_span_gain_m": w["span_m"] - base_worst,
            "dof": (len(trial) - 1) - 2,
        })
    out.sort(key=lambda r: r["median_dop"])
    return out


_RAMP = " .:-=+*#%@"


def render(g: Dict, nodes: Sequence, width: int = 60) -> str:
    """ASCII map. Dark is good: ' ' is well-conditioned, '@' is degenerate. Nodes marked 'N'."""
    grid, xs, ys = g["grid"], g["xs"], g["ys"]
    step = max(1, len(xs) // width)
    sub = grid[::step, ::step]
    sx, sy = xs[::step], ys[::step]
    lo, hi = 1.0, DOP_USABLE * 2.0
    marks = {}
    for p in nodes:
        p = np.asarray(p, float)
        i = int(np.argmin(np.abs(sx - p[0])))
        j = int(np.argmin(np.abs(sy - p[1])))
        marks[(j, i)] = "N"
    lines = []
    for j in range(sub.shape[0] - 1, -1, -1):          # north at the top
        row = []
        for i in range(sub.shape[1]):
            if (j, i) in marks:
                row.append("N")
                continue
            v = sub[j, i]
            if not math.isfinite(v):
                row.append("@")
            else:
                f = (min(max(v, lo), hi) - lo) / (hi - lo)
                row.append(_RAMP[min(len(_RAMP) - 1, int(f * len(_RAMP)))])
        lines.append("".join(row))
    return "\n".join(lines)


def _parse_points(s: str) -> List[Tuple[float, float]]:
    out = []
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        x, y = part.split(",")
        out.append((float(x), float(y)))
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Node placement: DOP map and offset observability.")
    ap.add_argument("--nodes", required=True, help='local metres, "x,y;x,y;..."')
    ap.add_argument("--candidates", default="", help='candidate sites for one more node')
    ap.add_argument("--margin", type=float, default=100.0, help="box margin around the nodes (m)")
    ap.add_argument("--step", type=float, default=5.0, help="grid step (m)")
    ap.add_argument("--speed", type=float, default=900.0, help="projectile speed (m/s)")
    ap.add_argument("--temp", type=float, default=20.0, help="air temperature (degC)")
    a = ap.parse_args(argv)

    nodes = _parse_points(a.nodes)
    if len(nodes) < 3:
        print("need >= 3 nodes"); return 2
    P = np.asarray(nodes, float)
    b = (float(P[:, 0].min() - a.margin), float(P[:, 1].min() - a.margin),
         float(P[:, 0].max() + a.margin), float(P[:, 1].max() + a.margin))
    g = dop_grid(nodes, b, a.step)
    dof = (len(nodes) - 1) - 2

    print("nodes      %d    dof %d    %s" % (
        len(nodes), dof,
        "residual carries information" if dof > 0 else
        "RESIDUAL IS ZERO BY CONSTRUCTION -- it cannot tell you the fit is wrong"))
    mm_per_us = g["median"] * SW.sound_speed(a.temp) * 1e-6 * 1000.0
    print("median DOP %.2f   -> %.2f mm of position error per us of timing error"
          % (g["median"], mm_per_us))
    print("           timing is not your limit at this geometry; node survey error is")
    print("usable     %.0f%% of the box at DOP <= %.0f" % (100 * g["usable_frac"], DOP_USABLE))
    w = worst_bearing(nodes, v_mps=a.speed, temp_c=a.temp)
    print("thinnest bearing %.0f deg: offset observable only for tracks in a %.1f m band"
          % (w["bearing_deg"], w["span_m"]))
    print()
    print(render(g, nodes))
    print("\n' '=good  '@'=degenerate  'N'=node   north up")

    if a.candidates:
        print("\ncandidates for the next node, best first:")
        for r in best_addition(nodes, _parse_points(a.candidates), b, max(a.step, 20.0)):
            print("  (%7.1f,%7.1f)  median DOP %6.2f (%+.2f)  thinnest band %6.1f m (%+.1f)"
                  % (r["position"][0], r["position"][1], r["median_dop"], -r["dop_gain"],
                     r["worst_span_m"], r["worst_span_gain_m"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
