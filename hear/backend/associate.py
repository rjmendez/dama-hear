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

⚠️AN ARRAY WIDER THAN THE CADENCE LOSES EVERY ROUND BUT THE FIRST. A rejected candidate is
terminal: it is not returned to the pool, so it can never seed a group of its own. Once the window
(d/c + margin) exceeds the round spacing, round 2 lands inside round 1's scan, every node of it is
`duplicate_node_in_group`, and the round is gone. Run on a 128.1 m array (window 0.403 s) with
three rounds 85 ms apart that all four nodes heard: 1 event, 8 rejections, rounds 2 and 3 vanish.
Conservation still holds -- they are reported, not dropped -- but they are never solved. Re-seeding
rejected candidates would satisfy conservation too and is the open alternative; it is not what this
does. tests/test_associate.py TestWideArray pins the loss, so the choice cannot change by accident.

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
    "pairwise_dt_exceeds_geometry", "too_few_nodes", "unusable_arrival"})

# ---------------------------------------------------------------------------------------------
# ARRIVAL QUALITY. A detection can be perfectly real and still not carry a usable arrival TIME,
# and until now there was no door to refuse one at -- every detection that named a known node was
# admitted, and any defect in its timestamp went straight into the solve.
#
# Two producers already know when their own timestamp is not a measurement and had nowhere to say
# so:
#
#   onset_found   The node gate walks back from the envelope peak to a constant fraction of it.
#                 When the envelope never falls that far the walk reaches the clamp and returns
#                 the CLAMP EDGE, which is numerically indistinguishable from a genuine slow
#                 rise -- 25 ms early, 8.6 m of range at 343 m/s.
#
#                 ⚠️THIS IS NOW A BACKSTOP, NOT THE MITIGATION. When this gate was written the
#                 case was 42 of the 228 reference events (18.4%) -- retriggers sitting inside
#                 the previous round's decay tail, where the envelope never falls to 20% of the
#                 NEW peak because the old one is still ringing. detect.onset_index_checked now
#                 refers the fraction to the LOCAL TROUGH instead of to zero, which takes that
#                 count to ZERO on the same corpus: those events are timed rather than refused,
#                 which is strictly better than discarding 18.4% of them.
#
#                 It still fires, and must stay: the trough reference is deliberately NOT applied
#                 when the window's minimum sits at its left edge, because that means the rise
#                 predates the window and referring to it would report a confidently LATE onset.
#                 Those are still clamp edges, and they are still not measurements.
#
#   utc_trusted   The phone's audio path. False means the HAL supplied no AudioTimestamp or the
#                 GPS anchor was stale, so the stamp still carries the input-buffer and HAL
#                 latency -- tens of milliseconds, constant per handset.
#
# ⚠️THE TIMESTAMP IS NOT MOVED. Rewriting arrival times that are already in a shipped pipeline
# would change every historical answer silently, which is worse than the defect. This refuses the
# detection instead, by the same discipline as nodeclass.require_arrival() and survey's load-time
# raises: a measurement that is known to be wrong is refused at the door, not corrected in place
# and not quietly averaged in.
#
# ⚠️ABSENT MEANS USABLE. A producer that does not publish these fields is not thereby suspect --
# most of them predate the fields. Only an EXPLICIT false is a refusal, so this is a strict no-op
# on every detection recorded before the producers started emitting them.
_QUALITY_FLAGS = ("onset_found", "utc_trusted")


def arrival_is_usable(d: Dict) -> bool:
    """True unless a producer has explicitly said its own timestamp is not a measurement."""
    return all(d.get(k, True) is not False for k in _QUALITY_FLAGS)


def _unusable_reason(d: Dict) -> str:
    bad = [k for k in _QUALITY_FLAGS if d.get(k, True) is False]
    detail = {
        "onset_found": "the constant-fraction onset was never crossed even against the local "
                       "trough, so the timestamp is the clamp edge and not a measurement "
                       "(~25 ms early, ~8.6 m)",
        "utc_trusted": "no HAL audio timestamp or no fresh GPS anchor, so the stamp still "
                       "carries the input-buffer and HAL latency",
    }
    return "node %d at %.6f s: %s" % (
        int(d["node_id"]), float(d["t_utc_s"]),
        "; ".join("%s=false -- %s" % (k, detail[k]) for k in bad))


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
    Refuses to treat a repeated (node_id, seq) as a duplicate once it is a window away: seq wraps.
    """
    c = SW.sound_speed(temp_c)
    diameter_m = float(survey.diameter_m())
    # One definition of the policy, so max_window_s() cannot drift away from what the scan uses.
    window_s = float(max_window_s(survey, temp_c, margin_s) if window_s is None else window_s)

    rejected: List[Dict] = []
    duplicates: List[Dict] = []

    known = []
    for d in detections:
        if int(d["node_id"]) not in survey:
            rejected.append(_row(d, "unknown_node", "node %d at %.6f s is not in the survey"
                                 % (int(d["node_id"]), float(d["t_utc_s"]))))
        elif not arrival_is_usable(d):
            rejected.append(_row(d, "unusable_arrival", _unusable_reason(d)))
        else:
            known.append(d)
    known.sort(key=lambda d: (float(d["t_utc_s"]), int(d["node_id"]), int(d["seq"])))

    pool: List[Dict] = []
    first_seen: Dict = {}
    for d in known:
        key = (int(d["node_id"]), int(d["seq"]))
        t = float(d["t_utc_s"])
        prior = first_seen.get(key)
        # ⚠️seq is an 8-bit field that wraps (hear/wire.py:113) and a rebooted node restarts its
        # counter, so an unbounded (node_id, seq) dedupe silently eats genuine later events. A
        # repeat is the same frame only if it could still have joined the same group -- one window.
        # `known` is sorted by time, so `prior` is the most recent accepted copy.
        if prior is not None and t - prior <= window_s:
            duplicates.append(_row(d, "duplicate_seq",
                                   "node %d seq %d already accepted at %.6f s; this copy %.6f s"
                                   % (key[0], key[1], prior, t)))
        else:
            first_seen[key] = t
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
