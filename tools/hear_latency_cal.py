#!/usr/bin/env python3
"""Measure a phone's acoustic latency against a co-located PPS node, and say when it cannot.

    python3 tools/hear_latency_cal.py --pool ~/hear-pool \
        --pair myasshurts-9669aa0e=mach --pair fancyantsy-96b6d5a8=nyquist
    python3 tools/hear_latency_cal.py --pool ... --pair ... --emit calibration.json

WHY THIS EXISTS. `android/app/src/main/assets/acoustic_latency_calibration.json` is the table the
phone consults to correct its own onset timestamps, and NOTHING PRODUCES IT. Measured 2026-09-10,
its three-line contents were: one phone with 13.122 ms, one with 293.499 ms -- refused by the
app's own 100 ms plausibility bound and so not applied -- and one absent. `by_model` was empty, so
the model fallback in the app could never fire either. Two of three phones therefore ran with NO
correction at all, and an uncorrected phone carries its whole audio-path latency into every
arrival: 13 ms is 4.5 m of range, against a 183 us budget (docs/node-hardware.md). That is the
largest error in the phone chain by two orders of magnitude, and it was a hand-edited constant.

WHAT IS BEING MEASURED. The app applies `corrected = raw + offset`
(AcousticLatencyCalibration.kt), so the number this writes is defined as

    offset = true_arrival_utc - phone_raw_onset_utc

and the whole problem is obtaining `true_arrival_utc`. A phone cannot supply it: its own clock is
the thing under test. A `xiao-s3-pps` node can -- it is disciplined by GPS PPS to a measured
25-30 ns (tools/fleet.py reports tAcc per node), which is four orders below the millisecond being
measured, so for this purpose the node IS the true time.

⚠️THE CO-LOCATION IS AN ASSUMPTION AND IT IS THE OPERATOR'S, NOT THIS TOOL'S. Reducing the
problem to a subtraction requires that the phone and the node hear the same wavefront at the same
instant, which is true only if they are in the same place. Every metre of separation is 2.9 ms of
pure bias -- 22% of the number being measured at myasshurts' 13 ms -- and NOTHING IN THE DATA CAN
DETECT IT. A confident wrong answer is the specific failure here, so `--pair` is required, its
meaning is stated in the output, and the separation the operator accepted is recorded in the
emitted JSON rather than left in someone's memory. Put the phone against the node. Not nearby.

⚠️AND IT REPORTS A DISTRIBUTION, NEVER A POINT. Each pairing yields one offset per coincident
event; what matters is whether they agree. A tight cluster is a latency. A wide one means the
events being matched are not the same event -- different sources, a reflection paired with its
direct path, or two unrelated sounds inside the window -- and its median is a number with no
referent. `--max-mad-ms` refuses that case rather than emitting its middle.
"""
from __future__ import annotations

import argparse
import bisect
import glob
import json
import os
import statistics
import sys
from typing import Dict, List, Optional, Sequence, Tuple

#: Speed of sound, for turning a separation into the bias it causes. Nominal: this is used to
#: EXPLAIN an error to the operator, never to correct one.
C_MPS = 343.0

#: The app's own bound (AcousticRangingCollector.MAX_PLAUSIBLE_LATENCY_OFFSET_NS). Anything past
#: it is refused there and would be dead weight in the table, so it is refused here too -- at the
#: point where it can still be explained, rather than silently on the phone.
MAX_PLAUSIBLE_OFFSET_MS = 100.0

#: A coincidence window. Wide enough to hold a co-located pair's true offset (which is the latency
#: itself, tens of ms) plus a metre or two of slop, narrow enough that unrelated events in a busy
#: room do not routinely fall inside it. Pairs are matched nearest-first and each node event is
#: used at most once, so a wide window costs precision rather than double-counting.
DEFAULT_WINDOW_MS = 60.0

#: Refuse a fit whose spread says the matches are not one population. MAD rather than stdev: a
#: single mispaired reflection moves a standard deviation far more than it moves a median
#: absolute deviation, and one bad pair should not condemn an otherwise clean run.
DEFAULT_MAX_MAD_MS = 8.0
DEFAULT_MIN_PAIRS = 12


class CalError(Exception):
    """A refusal with a reason the operator can act on."""


def _load(pool: str) -> Dict[str, List[Tuple[float, dict]]]:
    """{node name: [(t_utc_s, record)]} from the pool's anchored records, both sources."""
    out: Dict[str, List[Tuple[float, dict]]] = {}
    pats = [os.path.join(pool, "records", "*", "node.jsonl"),
            os.path.join(pool, "records", "*", "phone.jsonl")]
    seen_any = False
    for pat in pats:
        for path in sorted(glob.glob(pat)):
            # unanchored rows carry no UTC at all; they are not a small error, they are no
            # measurement, and including them would silently pull every fit toward zero
            if os.sep + "unanchored" + os.sep in path:
                continue
            seen_any = True
            with open(path) as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    t = rec.get("ts_utc_s")
                    name = rec.get("node")
                    if t is None or not name:
                        continue
                    out.setdefault(name, []).append((float(t), rec))
    if not seen_any:
        raise CalError("no anchored record files under %s/records/*/ -- is this a pool?" % pool)
    for v in out.values():
        v.sort(key=lambda kv: kv[0])
    return out


def match(phone_ts: Sequence[float], node_ts: Sequence[float],
          window_ms: float) -> List[Tuple[float, float]]:
    """Nearest-neighbour pairs within the window, each node event used at most once.

    Greedy-nearest and one-use is what stops a single loud node event from being paired with every
    phone event in a burst, which would report a spread of zero from one coincidence.
    """
    used = set()
    pairs: List[Tuple[float, float]] = []
    node_ts = list(node_ts)
    w = window_ms / 1000.0
    for pt in phone_ts:
        lo = bisect.bisect_left(node_ts, pt - w)
        best: Optional[int] = None
        i = lo
        while i < len(node_ts) and node_ts[i] <= pt + w:
            if i not in used and (best is None or abs(node_ts[i] - pt) < abs(node_ts[best] - pt)):
                best = i
            i += 1
        if best is not None:
            used.add(best)
            pairs.append((pt, node_ts[best]))
    return pairs


def fit(pairs: Sequence[Tuple[float, float]]) -> Dict:
    """offset = node - phone, per pair, summarised. Milliseconds throughout.

    ⚠️THE WINDOW BOUNDS WHAT IS MEASURABLE. A true latency larger than `--window-ms` produces no
    matches at all, not a large answer -- the pairing never sees the two events as coincident. So
    "no coincidences" has two causes that look identical here and are distinguished by the caller:
    the phone and node genuinely heard nothing together, or the offset is off the end of the
    ruler. Widen the window before concluding the former.
    """
    if not pairs:
        return {"n": 0, "median_ms": float("nan"), "mad_ms": float("nan"),
                "p10_ms": float("nan"), "p90_ms": float("nan"),
                "min_ms": float("nan"), "max_ms": float("nan")}
    offs = sorted((n - p) * 1000.0 for p, n in pairs)
    med = statistics.median(offs)
    mad = statistics.median([abs(o - med) for o in offs]) if offs else float("nan")
    return {
        "n": len(offs),
        "median_ms": med,
        "mad_ms": mad,
        "p10_ms": offs[int(len(offs) * 0.10)] if offs else float("nan"),
        "p90_ms": offs[int(len(offs) * 0.90)] if offs else float("nan"),
        "min_ms": offs[0] if offs else float("nan"),
        "max_ms": offs[-1] if offs else float("nan"),
    }


def verdict(f: Dict, min_pairs: int, max_mad_ms: float) -> Optional[str]:
    """None if the fit is usable, else why it is not."""
    if f["n"] < min_pairs:
        return ("only %d coincident events (need %d) -- not enough to call a spread"
                % (f["n"], min_pairs))
    if f["mad_ms"] > max_mad_ms:
        return ("MAD %.1f ms exceeds %.1f: the matches are not one population, so their median "
                "is a number with no referent. Suspect separation, reflections, or two sources."
                % (f["mad_ms"], max_mad_ms))
    if abs(f["median_ms"]) > MAX_PLAUSIBLE_OFFSET_MS:
        return ("median %.1f ms exceeds the app's own %.0f ms plausibility bound, so the phone "
                "would refuse it (OFFSET_OUT_OF_RANGE) even if written"
                % (f["median_ms"], MAX_PLAUSIBLE_OFFSET_MS))
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pool", required=True)
    ap.add_argument("--pair", action="append", default=[], metavar="PHONE=NODE",
                    help="a phone and the PPS node it was CO-LOCATED with (repeatable)")
    ap.add_argument("--window-ms", type=float, default=DEFAULT_WINDOW_MS)
    ap.add_argument("--min-pairs", type=int, default=DEFAULT_MIN_PAIRS)
    ap.add_argument("--max-mad-ms", type=float, default=DEFAULT_MAX_MAD_MS)
    ap.add_argument("--separation-m", type=float, default=0.0,
                    help="the separation the operator accepts, recorded in --emit and used to "
                         "state the bias it causes; it is NOT subtracted")
    ap.add_argument("--since", type=float, default=None, metavar="UTC_S",
                    help="ignore events before this, e.g. the moment the phone was placed")
    ap.add_argument("--emit", metavar="PATH",
                    help="write/merge acoustic_latency_calibration.json (only usable fits)")
    a = ap.parse_args(argv)

    if not a.pair:
        print("--pair PHONE=NODE is required. This tool cannot discover co-location and must not\n"
              "guess it: every metre of separation is %.1f ms of undetectable bias." % (1000.0 / C_MPS),
              file=sys.stderr)
        return 2
    try:
        rows = _load(a.pool)
    except CalError as e:
        print("refused: %s" % e, file=sys.stderr)
        return 2

    bias_ms = a.separation_m / C_MPS * 1000.0
    print("co-location is the operator's assertion, not a measurement.")
    print("  stated separation %.2f m => %.1f ms of bias, NOT corrected for\n" % (a.separation_m, bias_ms))

    results: Dict[str, Dict] = {}
    rc = 0
    for spec in a.pair:
        if "=" not in spec:
            print("refused: --pair wants PHONE=NODE, got %r" % spec, file=sys.stderr)
            return 2
        phone, _, node = spec.partition("=")
        pv, nv = rows.get(phone), rows.get(node)
        if not pv or not nv:
            missing = phone if not pv else node
            print("%-26s SKIPPED  %r has no anchored records in the pool" % (phone[:26], missing))
            rc = max(rc, 1)
            continue
        pt = [t for t, _ in pv if a.since is None or t >= a.since]
        nt = [t for t, _ in nv if a.since is None or t >= a.since]
        pairs = match(pt, nt, a.window_ms)
        if not pairs:
            print("%-26s SKIPPED  no coincidences with %s inside +-%.0f ms (%d vs %d events)"
                  % (phone[:26], node, a.window_ms, len(pt), len(nt)))
            print("    a latency LARGER than the window produces no matches rather than a large "
                  "answer;\n    widen --window-ms before concluding they heard nothing together")
            rc = max(rc, 1)
            continue
        f = fit(pairs)
        why = verdict(f, a.min_pairs, a.max_mad_ms)
        print("%-26s vs %-9s  n=%-4d median %+8.2f ms  MAD %5.2f  p10..p90 %+.1f..%+.1f"
              % (phone[:26], node, f["n"], f["median_ms"], f["mad_ms"], f["p10_ms"], f["p90_ms"]))
        if why:
            print("    REFUSED: %s" % why)
            rc = max(rc, 1)
            continue
        print("    usable: offset_ns = %d   (%.2f ms)" % (round(f["median_ms"] * 1e6), f["median_ms"]))
        results[phone] = {"fit": f, "node": node}

    if a.emit and results:
        # ⚠️MERGED, never rewritten: this table is hand-maintained today and an entry this run
        # could not measure must survive a run that only measured one phone.
        existing = {"schema": "acoustic_latency_calibration.v1", "by_node_id": {}, "by_model": {}}
        if os.path.exists(a.emit):
            with open(a.emit) as fh:
                existing.update(json.load(fh))
        existing.setdefault("by_node_id", {})
        existing.setdefault("by_model", {})
        prov = existing.setdefault("_measured", {})
        for phone, r in results.items():
            existing["by_node_id"][phone] = int(round(r["fit"]["median_ms"] * 1e6))
            prov[phone] = {
                "reference_node": r["node"],
                "method": "co-located PPS node; offset = node_onset_utc - phone_onset_utc",
                "n_events": r["fit"]["n"],
                "mad_ms": round(r["fit"]["mad_ms"], 3),
                "stated_separation_m": a.separation_m,
                "uncorrected_separation_bias_ms": round(bias_ms, 3),
                "window_ms": a.window_ms,
            }
        with open(a.emit, "w") as fh:
            json.dump(existing, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print("\nwrote %d entr%s to %s (merged; existing entries kept)"
              % (len(results), "y" if len(results) == 1 else "ies", a.emit))
    elif a.emit:
        print("\nnothing usable to write; %s left untouched" % a.emit)

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
