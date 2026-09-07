#!/usr/bin/env python3
"""Group per-node detections into events, bounded by the array's own geometry.

The window is COMPUTED -- array diameter / c plus a stated margin -- and it only bounds the scan.
The actual gate is pairwise: two nodes cannot disagree about one event by more than their own
separation allows. Stating that the other way round is how a window becomes the whole algorithm.

⚠️A FIXED WINDOW MANUFACTURES PHANTOMS. Measured round spacings are 85 ms at 700 rpm
(docs/validation-full-captures.md:35), ~328 ms burst cadence (docs/findings-2026-09-05.md:67-68)
and 522 ms for 19 rounds over 9.4 s (docs/validation-full-captures.md:15). A 600 ms guess contains
all three, so rounds merge into one event that fits at residual 0.00 and is hundreds of metres
wrong. 600 ms is never computed from anything; d/c is.

⚠️EARLIEST WINS, WITH NO REPLACEMENT. One detection per node per group. 60% of raw detections
fired within 60 ms of the previous one, inside a single blast's own decay
(docs/findings-2026-09-05.md:11), so a node latched onto a reflection must not displace the direct
arrival that already joined.

Nothing is dropped silently: n_input == sum(event n_nodes) + len(rejected) + len(duplicates), and
every rejection names its reason and carries the numbers that produced it.

What this refuses to do: it does not classify, does not solve, and does not say whether a residual
is meaningful -- that depends on the model and the dimension and belongs to the solver. It also
does not resolve the genuinely ambiguous case, a node that missed a round on an array whose d/c
exceeds the round cadence; it rejects that detection and reports why.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np

from ..solve import shockwave as SW

# Slop beyond pure propagation. The gate reports the argmax over a 25 ms guard
# (hear/node/detect.py:22 GUARD_S), plus a few metres of survey error and temperature spread.
# JUDGEMENT, not measured. It must stay well under the tightest measured round spacing -- 85 ms at
# 700 rpm (docs/validation-full-captures.md:35) -- or consecutive rounds merge. 30 ms leaves 55 ms.
MARGIN_S: float = 0.030

REASONS: frozenset = frozenset({
    "unknown_node", "duplicate_seq", "duplicate_node_in_group",
    "pairwise_dt_exceeds_geometry", "too_few_nodes"})


def _xyz(p) -> np.ndarray:
    """3-vector from a survey position. 2-vectors are treated as up=0.

    Distances here are propagation paths, not the solvers' 2D projection, so nothing is sliced:
    the 3D separation is the larger bound and admitting is the conservative direction.
    """
    v = np.asarray(p, float).ravel()
    return v if v.size == 3 else np.array([v[0], v[1], 0.0])


def _row(det: Dict, reason: str, detail: str, seed: Optional[Dict] = None) -> Dict:
    return {
        "node_id": int(det["node_id"]), "seq": int(det["seq"]),
        "t_utc_s": float(det["t_utc_s"]),
        "reason": reason, "detail": detail,
        "seed_node_id": None if seed is None else int(seed["node_id"]),
        "seed_t_utc_s": None if seed is None else float(seed["t_utc_s"]),
    }


def max_window_s(survey, temp_c: float = 20.0, margin_s: float = MARGIN_S) -> float:
    """Widest defensible spread of one event across the array, in seconds.

    diameter / c + margin. NEVER a constant: on a 10 m array this is 59 ms and on a 300 m array
    904 ms, and a single number cannot be right for both.
    """
    return float(survey.diameter_m()) / SW.sound_speed(temp_c) + float(margin_s)


def associate(detections: Sequence[Dict], survey, temp_c: float = 20.0,
              margin_s: float = MARGIN_S, min_nodes: int = 3,
              window_s: Optional[float] = None) -> Dict:
    """Group detections into events. Reads only `node_id`, `seq` and `t_utc_s`; carries the rest.

    `survey` is duck-typed: `node_id in survey`, `survey.position(node_id)`, `survey.diameter_m()`.
    `window_s` None means computed. An explicit value exists so a test can prove what a too-wide
    one costs; production passes None.

    Refuses to drop anything: every input detection ends in exactly one of `events`, `rejected` or
    `duplicates`. Refuses to admit a second detection from a node already in a group, and refuses
    any candidate whose separation-in-time from a member exceeds their separation-in-space over c.
    """
    c = SW.sound_speed(temp_c)
    diameter_m = float(survey.diameter_m())
    if window_s is None:
        window_s = diameter_m / c + float(margin_s)
    window_s = float(window_s)

    rejected: List[Dict] = []
    duplicates: List[Dict] = []

    known = []
    for d in detections:
        if int(d["node_id"]) not in survey:
            rejected.append(_row(d, "unknown_node", "node %d at %.6f s is not in the survey"
                                 % (int(d["node_id"]), float(d["t_utc_s"]))))
        else:
            known.append(d)
    known.sort(key=lambda d: (float(d["t_utc_s"]), int(d["node_id"]), int(d["seq"])))

    pool: List[Dict] = []
    first_seen: Dict = {}
    for d in known:
        key = (int(d["node_id"]), int(d["seq"]))
        if key in first_seen:
            duplicates.append(_row(d, "duplicate_seq",
                                   "node %d seq %d already accepted at %.6f s; this copy %.6f s"
                                   % (key[0], key[1], first_seen[key], float(d["t_utc_s"]))))
        else:
            first_seen[key] = float(d["t_utc_s"])
            pool.append(d)

    pos: Dict[int, np.ndarray] = {}

    def _p(node_id: int) -> np.ndarray:
        if node_id not in pos:
            pos[node_id] = _xyz(survey.position(node_id))
        return pos[node_id]

    events: List[Dict] = []
    used = [False] * len(pool)
    for i, seed in enumerate(pool):
        if used[i]:
            continue
        used[i] = True
        group = [seed]
        members = {int(seed["node_id"])}
        limit = float(seed["t_utc_s"]) + window_s
        for j in range(i + 1, len(pool)):
            if used[j]:
                continue
            cand = pool[j]
            t_c = float(cand["t_utc_s"])
            if t_c > limit:
                break
            nid = int(cand["node_id"])
            if nid in members:
                held = next(m for m in group if int(m["node_id"]) == nid)
                used[j] = True
                rejected.append(_row(cand, "duplicate_node_in_group",
                                     "node %d already in event at %.6f s, %.1f ms earlier: "
                                     "earliest wins, no replacement"
                                     % (nid, float(held["t_utc_s"]),
                                        (t_c - float(held["t_utc_s"])) * 1e3), seed))
                continue
            bad = None
            for m in group:
                dt = abs(t_c - float(m["t_utc_s"]))
                d_m = float(np.linalg.norm(_p(nid) - _p(int(m["node_id"]))))
                bound = d_m / c + float(margin_s)
                if dt > bound:
                    bad = (int(m["node_id"]), dt, bound, d_m)
                    break
            if bad is not None:
                used[j] = True
                rejected.append(_row(cand, "pairwise_dt_exceeds_geometry",
                                     "node %d vs node %d: dt %.1f ms > %.1f ms "
                                     "(d %.1f m / c %.1f + %.0f ms)"
                                     % (nid, bad[0], bad[1] * 1e3, bad[2] * 1e3, bad[3], c,
                                        float(margin_s) * 1e3), seed))
                continue
            used[j] = True
            members.add(nid)
            group.append(cand)

        if len(group) < min_nodes:
            for m in group:
                rejected.append(_row(m, "too_few_nodes",
                                     "group of %d around node %d at %.6f s: min_nodes %d"
                                     % (len(group), int(seed["node_id"]),
                                        float(seed["t_utc_s"]), min_nodes), seed))
            continue
        arrivals = [float(m["t_utc_s"]) for m in group]
        events.append({
            "event_id": len(events),
            "t0_utc_s": arrivals[0],
            "node_ids": [int(m["node_id"]) for m in group],
            "arrivals": arrivals,
            "detections": list(group),
            "n_nodes": len(group),
            "n_equations": len(group) - 1,      # t0 cancels in TDoA
            "span_s": arrivals[-1] - arrivals[0],
        })

    return {
        "events": events,
        "rejected": rejected,
        "duplicates": duplicates,
        "window_s": window_s,
        "margin_s": float(margin_s),
        "sound_speed_mps": c,
        "diameter_m": diameter_m,
        "n_input": len(detections),
    }
