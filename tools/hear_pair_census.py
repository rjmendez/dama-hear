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

import numpy as np

from hear import nodeclass as NC                                  # noqa: E402
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


# ================================================================= PART B: the receiver census
#
# THE PAIR CENSUS ABOVE ANSWERS "how often do two SURVEYED nodes coincide". It is built on
# `associate()`, which never sees a receiver that is not already in `survey.json` -- so it cannot
# answer the question an operator actually has before soldering anything: what would admitting a
# NEW receiver -- a phone, an unsurveyed node -- actually cost and buy. That question has three
# independent parts, and this section keeps them separate on purpose:
#
#   THE CLOCK    a PER-DETECTION quantity. `sync_sigma_ns` is a number each row states for
#                itself, so "how many arrivals pass" is a count, not a class-wide yes/no.
#   THE BIAS     a PER-CLASS quantity. `hear/nodeclass.py`'s `path_bias_s` is one number for an
#                entire hardware class -- there is no per-detection version, because nothing
#                about this measurement changes row to row. It is one verdict per receiver.
#   THE POSITION a PER-RECEIVER, OFTEN ABSENT quantity. `hear/solve/point.py` takes a position
#                for every arrival it uses; a receiver survey.json has never surveyed (every
#                phone, today) HAS NO POSITION, and no clock or bias number changes that. This is
#                checked FIRST, because a receiver that fails it cannot contribute an arrival at
#                all, and reporting its clock/bias numbers beside a geometry column of `null`
#                would read as "everything but geometry is fine" when nothing downstream of a
#                missing position can be fine.
#
# ⚠️"HOW MANY PASS THE CLOCK GATE" HAS TWO HONEST ANSWERS AND BOTH ARE REPORTED, NEVER JUST ONE.
# `nodeclass.stamp_admissible()` -- the function the shipped pipeline actually calls -- short-
# circuits on the RECEIVER'S CLASS-LEVEL `clock_admissible()` before it ever reads the
# detection's own stated sigma (nodeclass.py: `if not self.clock_admissible(): return False`).
# `gotchi-phone`'s class t_sigma_s is 5 ms -- GPSTimingSync's "location" tier, not the "gnss" tier
# the fleet is actually running -- so every phone row is refused there regardless of what it
# states. `clock_pass_deployed_gate` is that number (0, on this pool, for every phone). Beside it,
# `clock_pass_stated_sigma_only` tests the row's OWN `sync_sigma_ns` against the per-node budget
# directly, with no class term at all: the number the per-detection machinery was built to
# produce, and the one a fixed class constant would unblock. The gap between the two columns IS
# the finding, not a bug in this tool.
_PHONE_ONLY_CLASS = "gotchi-phone"          # the one phone class nodeclass.py registers
#: The class every `source="node"` row in survey.json is, in HARDWARE, without saying so: none of
#: nyquist/mach/rankine carries a `class` key (nodeclass.py's own registry notes "all three
#: answered GET /status with this class 2026-09-10"). Used ONLY to describe a bias verdict for an
#: unstated-class node row in this census; never fed back into an admission decision -- that
#: stays tools/hear_tdoa.py's REFERENCE_ARRIVAL_CLASS, a separate constant so this file staying
#: descriptive cannot silently change what gets published.
_UNSTATED_NODE_CLASS = "xiao-s3-pps"


def _geometry_if_admitted(survey: "SV.Survey", nid: int, c_mps: float) -> Dict:
    """What baseline the array would gain by admitting node `nid`, against its CURRENT
    arrival-class members (nid excluded even if already present, so re-running this on an
    already-admitted node reports what it already contributes rather than double-counting it)."""
    others = [i for i in survey.arrival_ids() if i != nid]
    if not others:
        return {"n_existing_arrival_receivers": 0,
                "note": "no other arrival-class receiver exists to pair against yet"}
    p = survey.position(nid)
    d = {survey.names[i] or str(i): float(np.linalg.norm(p - survey.position(i))) for i in others}
    dvals = list(d.values())
    if len(others) < 2:
        old_diam = 0.0                    # one point has no pair to be a diameter of
    else:
        P = survey.positions(others)
        old_diam = float(np.linalg.norm(P[:, None, :] - P[None, :, :], axis=2).max())
    return {
        "n_existing_arrival_receivers": len(others),
        "separation_m": d,
        "min_separation_m": min(dvals), "max_separation_m": max(dvals),
        "pair_bound_ms": {k: v / c_mps * 1e3 for k, v in d.items()},
        "old_diameter_m": old_diam,
        "new_diameter_m": max(old_diam, max(dvals)),
    }


def receiver_census(rows, survey: "SV.Survey", latency_cal: Optional[Dict] = None,
                    phone_position_accuracy_m: Optional[Dict[str, float]] = None,
                    c_mps: float = 343.0) -> Dict[str, Dict]:
    """One entry per receiver NAME the pool rows carry -- surveyed or not, node or phone.

    `rows` is any iterable of stored pool dicts (`hear.pool.Pool.raw()`); nothing here decodes a
    sketch frame, so this is cheap even over a large pool. `latency_cal` is a loaded
    `acoustic_latency_calibration.json` (schema `by_node_id: {name: offset_ns}`); if given, a
    receiver with an entry there is judged on ITS OWN measured bias rather than on the single
    class-wide `gotchi-phone` constant, which today blends three physically different handsets
    into one number (see docs/hear-latency-calibration-runbook.md).
    """
    id_by_name = {n: i for i, n in survey.names.items() if n}
    class_by_name = {n: (survey.classes.get(i) or None) for n, i in id_by_name.items()}
    by_node_id_ns = (latency_cal or {}).get("by_node_id") or {}
    budget_m = NC.ARRIVAL_ONE_WAY_BUDGET_S * float(c_mps)

    acc: Dict[str, Dict] = {}
    for row in rows:
        name = row.get("node")
        if not name:
            continue
        b = acc.setdefault(name, {"source": row.get("source"), "rows_total": 0,
                                  "rows_anchored": 0, "sync_sigma_stated": 0,
                                  "clock_pass_deployed_gate": 0, "clock_fail_deployed_gate": 0,
                                  "clock_unstated_deployed_gate": 0,
                                  "clock_pass_stated_sigma_only": 0,
                                  "clock_fail_stated_sigma_only": 0})
        b["rows_total"] += 1
        if not row.get("anchored"):
            continue
        b["rows_anchored"] += 1
        cname = class_by_name.get(name)
        assumed = False
        if cname is None:
            cname = _PHONE_ONLY_CLASS if row.get("source") == "phone" else _UNSTATED_NODE_CLASS
            assumed = True
        b["_class_assumed"] = assumed
        ssig = row.get("sync_sigma_ns")
        if ssig is not None:
            b["sync_sigma_stated"] += 1
            if float(ssig) <= NC.ARRIVAL_T_SIGMA_MAX_S * 1e9:
                b["clock_pass_stated_sigma_only"] += 1
            else:
                b["clock_fail_stated_sigma_only"] += 1
        verdict = NC.stamp_admissible(ssig, cname)
        if verdict is True:
            b["clock_pass_deployed_gate"] += 1
        elif verdict is False:
            b["clock_fail_deployed_gate"] += 1
        else:
            b["clock_unstated_deployed_gate"] += 1

    out: Dict[str, Dict] = {}
    for name, b in acc.items():
        nid = id_by_name.get(name)
        assumed = b.pop("_class_assumed")
        cname = class_by_name.get(name)
        if cname is None:
            cname = _PHONE_ONLY_CLASS if b["source"] == "phone" else _UNSTATED_NODE_CLASS
        cls = NC.CLASSES.get(cname) if cname else None
        row = dict(b)
        row["node_id"] = nid
        row["class"] = cname or "(unstated)"
        row["class_assumed"] = assumed
        row["class_clock_admissible"] = cls.clock_admissible() if cls else None
        row["class_bias_bounded"] = cls.capture_bias_bounded() if cls else None
        row["class_path_bias_m"] = cls.path_bias_m(c_mps) if cls else None
        off_ns = by_node_id_ns.get(name)
        row["device_latency_cal_ns"] = off_ns
        row["device_bias_bounded"] = (None if off_ns is None else
                                      abs(float(off_ns)) / 1e9 <= NC.ARRIVAL_PATH_BIAS_MAX_S)
        row["device_bias_m"] = None if off_ns is None else abs(float(off_ns)) / 1e9 * c_mps
        row["has_survey_position"] = nid is not None
        if nid is not None:
            row["geometry_if_admitted"] = _geometry_if_admitted(survey, nid, c_mps)
            row["position_accuracy_m"] = None
            row["position_note"] = None
        else:
            acc_m = (phone_position_accuracy_m or {}).get(name)
            row["geometry_if_admitted"] = None
            row["position_accuracy_m"] = acc_m
            row["position_note"] = (
                "no survey entry: this receiver CANNOT contribute a TDoA arrival at all, "
                "whatever its clock or bias -- hear/solve/point.py takes a position for every "
                "arrival it uses, and a device that moves and was never surveyed has none")
            row["implied_baseline_error_budget_multiple"] = (
                None if acc_m is None else acc_m / budget_m)
        out[name] = row
    return out


def _parse_kv_floats(items: Sequence[str]) -> Dict[str, float]:
    out = {}
    for it in items:
        if "=" not in it:
            raise SystemExit("refused: expected NAME=METRES, got %r" % it)
        k, _, v = it.partition("=")
        out[k] = float(v)
    return out


def format_receiver_census(rep: Dict[str, Dict]) -> str:
    lines = ["%-24s %-14s %6s %6s %-16s %-14s %6s %8s %8s %8s" % (
        "receiver", "class", "offer", "anch", "clk_deployed(P/F/-)", "clk_own(P/F)", "bias",
        "dev_bias", "has_pos", "acc_m")]
    for name in sorted(rep, key=lambda n: -rep[n]["rows_anchored"]):
        r = rep[name]
        lines.append("%-24s %-14s %6d %6d %5d/%-5d/%-5d %5d/%-8d %6s %8s %8s %8s" % (
            name[:24], r["class"] + ("*" if r["class_assumed"] else ""),
            r["rows_total"], r["rows_anchored"],
            r["clock_pass_deployed_gate"], r["clock_fail_deployed_gate"],
            r["clock_unstated_deployed_gate"],
            r["clock_pass_stated_sigma_only"], r["clock_fail_stated_sigma_only"],
            "yes" if r["class_bias_bounded"] else ("no" if r["class_bias_bounded"] is not None
                                                    else "?"),
            ("n/a" if r["device_bias_bounded"] is None
             else "yes" if r["device_bias_bounded"] else "no"),
            "yes" if r["has_survey_position"] else "NO",
            "-" if r["position_accuracy_m"] is None else "%.2f" % r["position_accuracy_m"]))
    lines.append("* class assumed (no survey entry states one). clk_deployed = what "
                 "nodeclass.stamp_admissible() (the SHIPPED gate) says pass/fail/unstated, out of "
                 "rows_anchored -- its class-level short-circuit can make this 0 pass even when "
                 "clk_own is mostly pass. clk_own = the row's OWN sync_sigma_ns tested directly "
                 "against the 129.4 us per-node budget, pass/fail, out of sync_sigma_stated only.")
    return "\n".join(lines)


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
    ap.add_argument("--receiver-census", action="store_true",
                    help="PART B: per-receiver arrivals/clock/bias/geometry, for EVERY receiver "
                         "the pool has seen -- surveyed or not, node or phone. Independent of "
                         "the pair census above; runs instead of it")
    ap.add_argument("--latency-cal", default=None,
                    help="--receiver-census only: acoustic_latency_calibration.json, so a phone "
                         "with a measured entry is judged on its OWN bias, not the blended "
                         "class-wide gotchi-phone constant")
    ap.add_argument("--phone-accuracy-m", action="append", default=[], metavar="NAME=METRES",
                    help="--receiver-census only: a phone's OWN measured GPS horizontal accuracy "
                         "(repeatable). Not read from anywhere automatically -- state where the "
                         "number came from when you pass it")
    a = ap.parse_args(argv)

    sv = SV.load_survey(os.path.expanduser(a.survey))
    names = {n["name"]: int(n["node_id"]) for n in sv.to_dict()["nodes"] if n.get("name")}
    ids = {v: k for k, v in names.items()}
    pl = P.Pool(os.path.expanduser(a.pool))

    if a.receiver_census:
        cal = None
        if a.latency_cal:
            with open(os.path.expanduser(a.latency_cal)) as fh:
                cal = json.load(fh)
        rep = receiver_census(pl.raw(), sv, latency_cal=cal,
                              phone_position_accuracy_m=_parse_kv_floats(a.phone_accuracy_m))
        if a.json:
            print(json.dumps(rep, sort_keys=True, indent=2))
        else:
            print(format_receiver_census(rep))
        return 0

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
