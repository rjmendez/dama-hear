#!/usr/bin/env python3
"""How many admissible EPISODES each pair of nodes shares, against what chance alone would give.

    python3 tools/hear_pair_census.py --pool /pool/corpus --survey survey.json --json

WHY THIS EXISTS SEPARATELY FROM `associate`. It measures nothing new: the admissibility test is
`hear.backend.associate.associate` itself, unchanged, with `min_nodes=2` so a two-node coincidence
is an event rather than a rejection. What this adds is a BEFORE/AFTER instrument -- one number per
pair, computed the same way on both sides of a change to the pool -- because "we recovered 233
rows" is an ingest statistic and says nothing about whether the array can hear anything more.

⚠️A PAIR COUNT ALONE IS NOT EVIDENCE OF COINCIDENCE. Two nodes that each detect often enough will
share admissible windows by chance: rankine and mach are 20.28 m apart, so any two arrivals within
~89 ms are admissible, and a node firing every few seconds hits that window regularly. The chance
column is therefore MEASURED, not assumed: one node's arrival times are circularly shifted by a
random offset (which preserves its own count and burst structure and destroys only its phase
against the other node) and the census re-run, N times. The report gives the null DISTRIBUTION --
median, p95, max -- not a single number, because the question "is 26 more than chance" has no
answer from one draw.

⚠️THE SHIFT PRESERVES EACH NODE'S OWN CADENCE, WHICH IS THE POINT. Drawing fake arrival times
from a uniform distribution would destroy the bursts, and a burst-free null is easy to beat: it
would call every pair significant. Circular shift keeps every inter-arrival interval a node
actually had.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import sys
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hear import pool as P                                        # noqa: E402
from hear.backend import associate as A                           # noqa: E402
from hear.backend import survey as SV                             # noqa: E402


def arrivals(pl: "P.Pool", names: Dict[str, int], t0: Optional[float] = None,
             t1: Optional[float] = None) -> List[Dict]:
    """Every anchored node arrival the pool holds, as `associate` input.

    ⚠️`seq` IS A FRESH INDEX, NOT THE CARD'S `sample`. associate() refuses a repeated
    (node_id, seq) as a duplicate, and `sample` restarts at every boot -- so the card's own
    counter would make two arrivals from two different boots look like one arrival counted twice,
    which is a silent LOSS in exactly the direction this census is trying to measure.
    """
    out: List[Dict] = []
    per_node: Dict[str, int] = {}
    for r in pl.records(source="node", anchored_only=True):
        nid = names.get(r.node_id)
        if nid is None or r.ts_utc_s is None:
            continue
        if (t0 is not None and r.ts_utc_s < t0) or (t1 is not None and r.ts_utc_s > t1):
            continue
        n = per_node.get(r.node_id, 0)
        per_node[r.node_id] = n + 1
        out.append({"node_id": nid, "seq": n, "t_utc_s": float(r.ts_utc_s),
                    "node": r.node_id, "alias_of": r.extra.get("node_alias_of")})
    out.sort(key=lambda d: d["t_utc_s"])
    return out


def census(dets: Sequence[Dict], survey, temp_c: float = 25.0) -> Dict:
    """Per-pair admissible episode counts, from `associate` with min_nodes=2."""
    res = A.associate(list(dets), survey, temp_c=temp_c, min_nodes=2)
    pairs: Dict[str, int] = {}
    by_size: Dict[int, int] = {}
    for ev in res["events"]:
        ids = sorted({int(d["node_id"]) for d in ev["detections"]})
        by_size[len(ids)] = by_size.get(len(ids), 0) + 1
        for a, b in itertools.combinations(ids, 2):
            pairs["%d-%d" % (a, b)] = pairs.get("%d-%d" % (a, b), 0) + 1
    return {"pairs": pairs, "events_by_n_nodes": by_size,
            "events": len(res["events"]), "rejected": len(res["rejected"]),
            "duplicates": len(res["duplicates"]), "n_input": len(dets)}


def null_distribution(dets: Sequence[Dict], survey, node_id: int, draws: int = 200,
                      temp_c: float = 25.0, seed: int = 20260910) -> Dict[str, Dict]:
    """The same census with ONE node's clock circularly shifted, `draws` times.

    Returns per-pair {median, p95, max, mean} over the draws. Only pairs containing `node_id`
    mean anything in it; the rest are printed because they are the control -- a pair the shift
    does not touch must come back unchanged, and if it does not, the shift is doing something
    other than what it claims.
    """
    rng = random.Random(seed)
    mine = [d for d in dets if int(d["node_id"]) == node_id]
    others = [d for d in dets if int(d["node_id"]) != node_id]
    if len(mine) < 2:
        return {}
    t0 = min(d["t_utc_s"] for d in dets)
    span = max(d["t_utc_s"] for d in dets) - t0
    # ⚠️SEEDED WITH THE OBSERVED PAIRS. A pair that scores zero in every draw would otherwise be
    # ABSENT from the report rather than reported as zero -- and absent reads as "not measured",
    # which is the one thing a null must never say about the pair it was run for. Zero draws out
    # of 200 is the strongest result this can produce; it must be printable.
    acc: Dict[str, List[int]] = {k: [] for k in census(dets, survey, temp_c=temp_c)["pairs"]}
    for _ in range(draws):
        off = rng.uniform(0.0, span)
        shifted = [dict(d, t_utc_s=t0 + ((d["t_utc_s"] - t0 + off) % span)) for d in mine]
        c = census(others + shifted, survey, temp_c=temp_c)
        for k in set(list(c["pairs"]) + list(acc)):
            acc.setdefault(k, []).append(c["pairs"].get(k, 0))
    out = {}
    for k, v in acc.items():
        v = sorted(v)
        out[k] = {"median": v[len(v) // 2], "p95": v[min(len(v) - 1, int(0.95 * len(v)))],
                  "max": v[-1], "mean": round(sum(v) / len(v), 3), "draws": len(v)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pool", default="~/hear-pool")
    ap.add_argument("--survey", default="survey.json")
    ap.add_argument("--temp-c", type=float, default=25.0)
    ap.add_argument("--null-draws", type=int, default=0,
                    help="draws for the circular-shift null; 0 skips it")
    ap.add_argument("--null-node", default=None, help="node NAME whose clock the null shifts")
    ap.add_argument("--since", type=float, default=None, help="unix seconds, inclusive")
    ap.add_argument("--until", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    sv = SV.load_survey(os.path.expanduser(a.survey))
    names = {n["name"]: int(n["node_id"]) for n in sv.to_dict()["nodes"] if n.get("name")}
    ids = {v: k for k, v in names.items()}
    pl = P.Pool(os.path.expanduser(a.pool))
    dets = arrivals(pl, names, a.since, a.until)
    rep = census(dets, sv, temp_c=a.temp_c)
    rep["by_node"] = {}
    for d in dets:
        rep["by_node"][d["node"]] = rep["by_node"].get(d["node"], 0) + 1
    rep["aliased_arrivals"] = sum(1 for d in dets if d.get("alias_of"))
    rep["window_s"] = A.max_window_s(sv, a.temp_c)
    rep["pair_names"] = {k: "%s x %s" % (ids[int(k.split("-")[0])], ids[int(k.split("-")[1])])
                         for k in rep["pairs"]}
    if a.null_draws and a.null_node:
        rep["null"] = null_distribution(dets, sv, names[a.null_node], a.null_draws, a.temp_c)
        rep["null_shifted_node"] = a.null_node
    if a.json:
        print(json.dumps(rep, sort_keys=True, indent=2))
    else:
        print("arrivals %d  events %d  window %.3f s" % (len(dets), rep["events"],
                                                        rep["window_s"]))
        for k in sorted(rep["pairs"], key=lambda k: -rep["pairs"][k]):
            print("  %-18s %5d" % (rep["pair_names"][k], rep["pairs"][k]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
