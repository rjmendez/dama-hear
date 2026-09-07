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

⚠️DOP IGNORES WHETHER A NODE CAN HEAR THE EVENT. It improves with baseline extent, so this will
happily rank a node 2 km away above one in the middle of the array -- geometrically true, useless
if the shot never clears that node's threshold. Read the ranking as "best geometry among sites that
all detect"; detectability is a separate question this tool does not answer.

⚠️DOP AND REDUNDANCY ARE DIFFERENT QUESTIONS AND BOTH MATTER. Three nodes give two equations for
two unknowns: the fit is exact, the residual is ~0 by construction, and DOP is still finite and
may look excellent. It says how precisely you locate IF nothing is wrong, and nothing about
whether you would notice if something were. `dof` is the second number. Four nodes is where a
residual begins to carry information.

⚠️THE SOLVERS ARE 2D. shockwave.py slices [:2] at lines 66, 76, 96 and 137; this module at 85, 86,
132, 160 and 201. docs/node-hardware.md:32 and hear/node/telemetry.py:10 both promise "the 3D
geometry" and are wrong until someone decides otherwise. The 3D block below -- `coplanarity`,
`dop3`, `height_sensitivity`, `vertical_observability`, `plan_3d` -- answers "could this site do
3D at all?" before anyone buys two more nodes. It does not make the solvers 3D.

⚠️COPLANAR NODES CANNOT OBSERVE ELEVATION. It is the measured same-side trap rotated into the
vertical: raising the source adds the same delay to every node in the plane and cancels in TDoA,
exactly as shifting a track past same-side nodes moved the TDoAs by 0.000 ms
(docs/findings-2026-09-05.md:44-46). Worse, a horizontal coplanar array has a mirror twin -- a
source above the plane and its reflection below fit IDENTICALLY at any timing quality -- so
`mirror_ambiguous` is reported separately from `observable`: an off-centre source gives a small
nonzero swing, meaning the array can weakly see the MAGNITUDE of elevation and never the SIGN.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import shockwave as SW

# Position error per metre of range-difference error, above which the geometry is not worth
# deploying. 10 means 1 us of timing error (0.34 m of range) becomes 3.4 m of position error.
DOP_USABLE = 10.0

# s1/s0 below which dop() is effectively singular. JUDGEMENT, not measured.
COLLINEAR_LINEARITY: float = 0.02
# RMS distance to the best-fit plane. JUDGEMENT, tied to survey error -- the repo has no field
# data on vertical geometry; the 2026-09-05 session recorded no node heights.
COPLANAR_RMS_M: float = 0.5
# The floor a verdict of "observable" has to clear, horizontally and vertically. Both
# offset_sensitivity() and height_sensitivity() read it from here: the two are meant to be the
# same test in two directions, and a second copy of the number is how that quietly stops being true.
RESOLVABLE_SWING_MS: float = 0.05


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
        "observable": swing_ms > RESOLVABLE_SWING_MS,   # a swing the timebase can resolve
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


# ---------------------------------------------------------------------------------------------
# 3D / vertical observability. Advisory only: the solvers above and in shockwave.py are still 2D.
# ---------------------------------------------------------------------------------------------

def _as3(pts: Sequence) -> np.ndarray:
    """(N,3) float. 2-vectors are lifted with up=0; anything longer is sliced to three."""
    P = np.asarray(pts, float)
    if P.ndim != 2 or P.shape[1] < 2:
        raise ValueError("nodes must be (N,2) or (N,3)")
    if P.shape[1] == 2:
        return np.hstack([P, np.zeros((len(P), 1))])
    return P[:, :3]


def _pt3(p: Sequence) -> np.ndarray:
    q = np.asarray(p, float).ravel()
    if q.size == 2:
        return np.array([q[0], q[1], 0.0])
    return q[:3].astype(float)


def linearity(nodes: Sequence) -> float:
    """s1/s0 of the centred HORIZONTAL positions. 0.0 exactly collinear, 1.0 isotropic.

    ⚠️hear/backend/survey.py and hear/solve/point.py both import this and compare against
    COLLINEAR_LINEARITY. It is a hard numeric contract -- three modules must agree to the last
    digit -- so it is computed here and nowhere else. Refuses fewer than 3 nodes.
    """
    P = np.asarray(nodes, float)[:, :2]
    if len(P) < 3:
        raise ValueError("need >= 3 nodes")
    s = np.linalg.svd(P - P.mean(axis=0), compute_uv=False)
    return 0.0 if s[0] <= 0 else float(s[1] / s[0])


def coplanarity(nodes: Sequence) -> Dict:
    """How far the nodes are from lying in one plane, and which plane that is.

    ⚠️THREE NODES ARE COPLANAR BY CONSTRUCTION -- three points define a plane, so s[2] is 0
    exactly and `planarity_rms_m` is 0.0 whatever their heights. The number only carries
    information from four nodes up. Refuses fewer than 3. 2-vectors are read as up=0.
    """
    P = _as3(nodes)
    n = len(P)
    if n < 3:
        raise ValueError("need >= 3 nodes")
    C = P - P.mean(axis=0)
    s, Vt = np.linalg.svd(C, full_matrices=False)[1:]
    nrm = Vt[2] / np.linalg.norm(Vt[2])
    if nrm[2] < 0:                                    # one representative per plane, not two
        nrm = -nrm
    rms = float(s[2] / math.sqrt(n))
    return {
        "planarity_rms_m": rms,
        "plane_normal": (float(nrm[0]), float(nrm[1]), float(nrm[2])),
        "coplanar": rms < COPLANAR_RMS_M,
        "near_horizontal": abs(float(nrm[2])) > 0.9,
        "vertical_spread_m": float(P[:, 2].max() - P[:, 2].min()),
        "n_nodes": n,
    }


def dop3(nodes: Sequence, source: Sequence) -> Dict:
    """Point-source TDoA dilution of precision in 3D, split into horizontal and VERTICAL.

    Marginalised the same way dop() is, so the answer does not depend on which node is called
    first. NOT an extension of dop(): 3D has three unknowns, so `dof` is (n-1)-3 and a layout
    that is redundant in 2D can be exactly determined or singular here. Refuses fewer than 4
    nodes. Returns inf rather than raising when the Fisher matrix is singular -- a coplanar
    horizontal array with an in-plane source is exactly that case.
    """
    P = _as3(nodes)
    s = _pt3(source)
    n = len(P)
    if n < 4:
        raise ValueError("need >= 4 nodes")
    dof = (n - 1) - 3
    G = _unit_rows(P, s)
    if G is None:
        return {"hdop": float("inf"), "vdop": float("inf"), "pdop": float("inf"),
                "dof": dof, "redundant": dof > 0, "singular": True}
    M = np.eye(n) - np.ones((n, n)) / n
    F = G.T @ M @ G
    try:
        if abs(float(np.linalg.det(F))) < 1e-12:
            raise np.linalg.LinAlgError
        C = np.linalg.inv(F)
        h = float(np.sqrt(C[0, 0] + C[1, 1]))
        v = float(np.sqrt(C[2, 2]))
        p = float(np.sqrt(np.trace(C)))
    except (np.linalg.LinAlgError, ValueError):
        h = v = p = float("inf")
    return {"hdop": h, "vdop": v, "pdop": p, "dof": dof, "redundant": dof > 0,
            "singular": not math.isfinite(p)}


def height_sensitivity(nodes: Sequence, source: Sequence, delta_m: float = 6.0,
                       temp_c: float = 20.0) -> Dict:
    """offset_sensitivity() rotated into the vertical: raise the source and watch the TDoAs.

    A POINT source on straight-line ranges, not the Mach cone -- elevation observability is a
    property of the node geometry, and the cone would only add a second unknown to the question.
    `delta_m` defaults to 6.0 to match the field's measured +/-6 m shift
    (docs/findings-2026-09-05.md:44-46), so the vertical verdict and the horizontal one are the
    same test in two directions.
    """
    P = _as3(nodes)
    if len(P) < 3:
        raise ValueError("need >= 3 nodes")
    s = _pt3(source)
    c = SW.sound_speed(temp_c)

    def tdoa(src):
        t = np.linalg.norm(src[None, :] - P, axis=1) / c
        return t[1:] - t[0]

    swing_ms = float(np.abs(tdoa(s + np.array([0.0, 0.0, float(delta_m)])) - tdoa(s)).max()
                     * 1000.0)
    return {
        "max_tdoa_swing_ms": swing_ms,
        "delta_m": float(delta_m),
        "source": (float(s[0]), float(s[1]), float(s[2])),
        "observable": swing_ms > RESOLVABLE_SWING_MS,
        "sound_speed_mps": c,
    }


def vertical_observability(nodes: Sequence, source: Optional[Sequence] = None,
                           delta_m: float = 6.0, temp_c: float = 20.0) -> Dict:
    """Can this layout see how high the source was? The plain-English verdict.

    `observable` is MEASURED by perturbation, never inferred from a threshold on planarity --
    COPLANAR_RMS_M is a judgement and would not survive being used as evidence.
    `mirror_ambiguous` is a separate failure and is reported separately: a horizontal coplanar
    array with an off-centre source has a small nonzero swing, so it can weakly see the
    magnitude of elevation and can never see the sign. Source defaults to the centroid at up=0.
    """
    P = _as3(nodes)
    if source is None:
        source = (float(P[:, 0].mean()), float(P[:, 1].mean()), 0.0)
    cp = coplanarity(P)
    hs = height_sensitivity(P, source, delta_m=delta_m, temp_c=temp_c)
    mirror = bool(cp["coplanar"] and cp["near_horizontal"])
    note = None
    if not hs["observable"]:
        note = ("raising the source %.1f m moved every TDoA by %.4f ms (floor %.2f ms): "
                "elevation is UNOBSERVABLE at this geometry"
                % (hs["delta_m"], hs["max_tdoa_swing_ms"], RESOLVABLE_SWING_MS))
        if mirror:
            note += "; nodes are coplanar and horizontal, so the mirrored source fits identically"
    out = dict(cp, **hs)                              # hs already carries `observable`
    out["mirror_ambiguous"] = mirror
    out["note"] = note
    return out


def node_counts(model: str = "trajectory", dims: int = 2) -> Dict:
    """Fact 2 as data, and the only machine-readable copy of it.

    t0 cancels in TDoA, so N nodes give N-1 equations. A residual at `exact_n` is ~0 BY
    CONSTRUCTION and proves nothing; `meaningful_n` is where it starts carrying information
    (docs/findings-2026-09-05.md:48-50). Refuses an unknown model or dimension rather than
    guessing an unknown count.
    """
    table = {("trajectory", 2): 2, ("trajectory", 3): 4, ("point", 2): 2, ("point", 3): 3}
    key = (str(model), int(dims))
    if key not in table:
        raise ValueError("unknown model/dims %r: expected trajectory|point and 2|3" % (key,))
    u = table[key]
    return {"model": key[0], "dims": key[1], "unknowns": u,
            "exact_n": u + 1, "meaningful_n": u + 2}


def plan_3d(nodes: Sequence, source: Optional[Sequence] = None, temp_c: float = 20.0) -> Dict:
    """One call for "can this site do 3D at all?", with what 3D would cost in nodes."""
    P = _as3(nodes)
    if source is None:
        source = (float(P[:, 0].mean()), float(P[:, 1].mean()), 0.0)
    return {
        "vertical": vertical_observability(P, source, temp_c=temp_c),
        "dop3": dop3(P, source) if len(P) >= 4 else None,
        "counts": {
            "trajectory_3d": node_counts("trajectory", 3),
            "point_3d": node_counts("point", 3),
            "trajectory_2d": node_counts("trajectory", 2),
            "point_2d": node_counts("point", 2),
        },
        "n_nodes": len(P),
        "linearity": linearity(P),
    }


_RAMP = " .:-=+*#%"          # '@' is reserved for non-finite


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


def _parse_points(s: str) -> List[Tuple[float, ...]]:
    """"x,y;..." or "x,y,z;...". ⚠️Every point must have the SAME arity: np.asarray on a ragged
    list silently builds an object array and every downstream slice then lies."""
    out: List[Tuple[float, ...]] = []
    arity = None
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        vals = tuple(float(v) for v in part.split(","))
        if len(vals) not in (2, 3):
            raise ValueError("point %r needs 2 or 3 comma-separated numbers" % part)
        if arity is None:
            arity = len(vals)
        elif len(vals) != arity:
            raise ValueError("mixed 2D and 3D node coordinates")
        out.append(vals)
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Node placement: DOP map and offset observability.")
    ap.add_argument("--nodes", required=True, help='local metres, "x,y;..." or "x,y,z;..."')
    ap.add_argument("--source", default="", help='probe point for the 3D block, "x,y,z"')
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
    print("usable     %.0f%% of the box at DOP <= %.0f  (geometry only -- a node must still hear it)"
          % (100 * g["usable_frac"], DOP_USABLE))
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

    if len(nodes[0]) == 3:
        src = _parse_points(a.source)[0] if a.source else None
        pl = plan_3d(nodes, src, temp_c=a.temp)
        v, d3, ct = pl["vertical"], pl["dop3"], pl["counts"]
        print("\n3D / vertical (advisory -- the solvers themselves are 2D)")
        print("vertical spread %.1f m   planarity RMS %.2f m   %s"
              % (v["vertical_spread_m"], v["planarity_rms_m"],
                 "COPLANAR" if v["coplanar"] else "has relief"))
        print("VDOP       %s" % ("n<4" if d3 is None else
                                 "%.2f   HDOP %.2f   dof %d" % (d3["vdop"], d3["hdop"], d3["dof"])))
        print("raising the source %.0f m swings TDoA by %.3f ms -> elevation %s"
              % (v["delta_m"], v["max_tdoa_swing_ms"],
                 "observable" if v["observable"] else "UNOBSERVABLE"))
        if v["mirror_ambiguous"]:
            print("           mirror ambiguous: above and below the plane fit identically")
        print("3D costs: trajectory exact at %d nodes, meaningful at %d;"
              "  point exact at %d, meaningful at %d"
              % (ct["trajectory_3d"]["exact_n"], ct["trajectory_3d"]["meaningful_n"],
                 ct["point_3d"]["exact_n"], ct["point_3d"]["meaningful_n"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
