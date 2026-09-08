#!/usr/bin/env python3
"""Survey the nodes from their own logged fixes -- and say honestly what that can and cannot give.

    python3 tools/node_survey.py nyquist mach --out survey.json --heights nyquist=0 mach=3.1

WHY THIS EXISTS. The nodes now log their own position, and the obvious next step -- average it and
call it a survey -- does not work, for a reason that only showed up against ground truth.

MEASURED over 7.2 hours, 26,000 epochs per node, both nodes indoors at windows:

    horizontal separation   16.29 m median, sd 1.71 m over hourly medians, 5.95 m spread
    vertical difference     wandered from -9.35 m to +4.59 m: a 14 m swing
    nyquist  east sd 4.55 m   hAcc CLAIMED 1.59 m    hell peak-to-peak 61.17 m
    mach     east sd 2.36 m   hAcc CLAIMED 1.00 m    hell peak-to-peak 56.41 m

Two things follow, and neither is fixed by averaging longer.

  1. THE RECEIVER'S OWN ACCURACY ESTIMATE IS OPTIMISTIC BY 2-4x here. hAcc/vAcc model geometry and
     signal quality; they do not model multipath, and a node at a window sees a biased half of the
     sky. So sigma must come from OBSERVED SCATTER, not from the number the receiver reports. This
     tool computes it from the series and prints the ratio, because a claimed sigma that is 4x too
     small propagates into every later residual as unearned confidence.

  2. GNSS HEIGHT IS NOT USABLE FOR THIS ARRAY AT ALL. Ground truth is that mach is one floor above
     nyquist -- about +3 m. The 26,000-epoch means said mach was 4.32 m BELOW, and the hourly
     medians swung 14 m. The true difference is buried a long way inside the error. Averaging does
     not help because this is a BIAS, not noise: each node sees its own restricted sky through its
     own window and gets its own persistent offset.

     So this tool REFUSES to write a GNSS height into a survey. Heights must be given with
     --heights, from a tape measure or a floor plan. A storey is about 3 m and a tape measure is
     good to a centimetre; GNSS here is good to about ten metres. That is not a close call.

The horizontal IS usable, at roughly +/-2 m, which is +/-6 ms of TDoA. Good enough to place nodes
on a site plan, not good enough to be the last word -- if you can measure the horizontal by hand
too, do that instead and use this only to check it.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import statistics as st
import sys
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from hear import geodesy as G                    # noqa: E402
# The frame/units strings come from the loader that will read this file back, never from a
# literal here. Hardcoding "enu" produced a survey the loader refused -- it wants "enu_local" --
# so the tool emitted a file it could not itself load, and nothing caught it until it was run.
from hear.backend.survey import _FRAME, _UNITS    # noqa: E402

# Above this, the receiver is telling us the epoch is bad and we believe it. This is a floor on
# obvious garbage, NOT a quality bar -- the epochs that survive it are still multipath-biased.
HACC_REJECT_M = 10.0
# Ratio of observed scatter to claimed accuracy past which the claim is called out. 2.0 is not a
# physical constant; it is "twice as bad as advertised", which is worth a line of output.
OPTIMISM_FLAG = 2.0


def fetch(node: str, timeout: float = 300.0) -> str:
    url = node if node.startswith("http") else "http://%s.local/sd?file=/health.csv" % node
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def epochs(text: str) -> List[Dict]:
    """3D-fix rows with a usable position. Everything else is dropped and counted, never zeroed."""
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        try:
            if r.get("fix") != "3" or not r.get("lat") or r["lat"] in ("", "0.0000000"):
                continue
            ha = float(r["hacc_m"])
            if not (0.0 < ha <= HACC_REJECT_M):
                continue
            out.append({"utc_us": int(r["utc_us"]), "lat": float(r["lat"]), "lon": float(r["lon"]),
                        "hell": float(r["hell_m"]), "hacc": ha, "vacc": float(r["vacc_m"]),
                        "sats": int(r["sats"])})
        except (KeyError, ValueError, TypeError):
            continue
    return out


# Block length for the sigma estimate. It must be long enough that consecutive blocks are close
# to independent, and MUST NOT be near the period of whatever is actually moving: an error whose
# period equals the block length has the same shape in every block, so every block median matches
# and the estimator confidently reports sigma 0. Multipath follows the GPS ground-track repeat of
# about 11 h 58 m, so an hour is clear of it -- but that is why this is a named constant with a
# reason attached rather than a number chosen because it looked round.
BLOCK_S = 3600.0


def _block_medians(rows: Sequence[Dict], enu: Sequence[Tuple[float, float, float]],
                   block_s: float = BLOCK_S):
    """Median position per time block. The unit of independent information, not the epoch."""
    t0 = rows[0]["utc_us"]
    buckets: Dict[int, List[Tuple[float, float, float]]] = {}
    for r, p in zip(rows, enu):
        buckets.setdefault(int((r["utc_us"] - t0) / (block_s * 1e6)), []).append(p)
    return [tuple(st.median(v[k] for v in b) for k in range(3))
            for b in buckets.values() if len(b) >= 3]


def summarise(rows: Sequence[Dict], origin: Tuple[float, float, float]) -> Dict:
    """Median position and EMPIRICAL scatter in the local frame.

    Median, not mean. The firmware keeps a running mean because it cannot store the series, and a
    mean is dragged by the 60 m outliers this data has; the median is not. Where both are
    available the median is the one to survey from.
    """
    enu = [G.geodetic_to_enu(r["lat"], r["lon"], r["hell"], *origin) for r in rows]
    e = [p[0] for p in enu]
    n = [p[1] for p in enu]
    u = [p[2] for p in enu]
    med = (st.median(e), st.median(n), st.median(u))
    # SIGMA OF THE SURVEYED POSITION, not of a single epoch. These are different by a lot and
    # picking the wrong one is the whole game:
    #
    #   per-epoch scatter          6.05 m  -- what one fix is worth; far too pessimistic for a
    #                                         position averaged over 26,000 of them
    #   scatter / sqrt(N_epochs)   0.04 m  -- what independence would give, and independence is
    #                                         exactly what multipath does not provide
    #   scatter of hourly medians  1.71 m  -- what was actually observed hour to hour
    #
    # The third is the honest one. Multipath is driven by the satellite geometry, which repeats
    # slowly, so consecutive epochs are strongly correlated and sqrt(N) over epochs is a fantasy.
    # Blocking to an hour and looking at how much the block medians move measures the correlated
    # error directly, without needing a model of it.
    blocks = _block_medians(rows, enu)
    if len(blocks) >= 3:
        be = [b[0] for b in blocks]
        bn = [b[1] for b in blocks]
        # sd of the block medians / sqrt(n_blocks): the standard error of their mean.
        sigma_h = math.hypot(st.pstdev(be), st.pstdev(bn)) / math.sqrt(len(blocks))
        sigma_src = "%d hourly blocks" % len(blocks)
    else:
        rad = sorted(math.hypot(a - med[0], b - med[1]) for a, b in zip(e, n))
        sigma_h = rad[int(0.68 * (len(rad) - 1))]
        sigma_src = "per-epoch scatter (too few blocks to do better)"
    rad = sorted(math.hypot(a - med[0], b - med[1]) for a, b in zip(e, n))
    return {
        "horiz_sigma_m": sigma_h,
        "sigma_source": sigma_src,
        "n_blocks": len(blocks),
        "epoch_sigma_m": rad[int(0.68 * (len(rad) - 1))],
        "n_epochs": len(rows),
        "e_m": med[0], "n_m": med[1], "u_m": med[2],
        "e_sd_m": st.pstdev(e), "n_sd_m": st.pstdev(n), "u_sd_m": st.pstdev(u),
        "u_ptp_m": max(u) - min(u),
        "hacc_claimed_m": st.median([r["hacc"] for r in rows]),
        "vacc_claimed_m": st.median([r["vacc"] for r in rows]),
        "sats_median": st.median([r["sats"] for r in rows]),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("nodes", nargs="+", help="node names (uses http://<name>.local) or full URLs")
    ap.add_argument("--out", help="write a survey JSON here")
    ap.add_argument("--heights", nargs="*", default=[], metavar="NAME=METRES",
                    help="HAND-MEASURED height per node, metres, any consistent datum. "
                         "Required to write a survey: GNSS height is refused.")
    ap.add_argument("--ids", nargs="*", default=[], metavar="NAME=ID",
                    help="node_id per node; defaults to 1..N in the order given")
    a = ap.parse_args(argv)

    heights = {}
    for kv in a.heights:
        k, _, v = kv.partition("=")
        heights[k] = float(v)
    ids = {}
    for kv in a.ids:
        k, _, v = kv.partition("=")
        ids[k] = int(v)

    series = {}
    for name in a.nodes:
        rows = epochs(fetch(name))
        if not rows:
            print("%s: no usable 3D fixes" % name, file=sys.stderr)
            return 2
        series[name] = rows

    # One origin for every node, so the numbers are comparable. Height zero: the vertical from
    # GNSS is not trusted here and must not leak into the frame through the anchor either.
    first = series[a.nodes[0]]
    origin = (st.median([r["lat"] for r in first]), st.median([r["lon"] for r in first]), 0.0)

    print("origin  %.7f, %.7f  (median of %s)\n" % (origin[0], origin[1], a.nodes[0]))
    print("%-10s %6s %9s %9s %8s %9s   %8s %7s   %s"
          % ("node", "n", "east", "north", "epoch_sd", "sigma_pos", "hAcc", "ratio", "vertical"))
    S = {}
    for name in a.nodes:
        s = summarise(series[name], origin)
        S[name] = s
        ratio = s["epoch_sigma_m"] / s["hacc_claimed_m"] if s["hacc_claimed_m"] else float("inf")
        print("%-10s %6d %9.2f %9.2f %8.2f %9.2f   %8.2f %6.1fx   sd %.1f m, spread %.1f m"
              % (name, s["n_epochs"], s["e_m"], s["n_m"], s["epoch_sigma_m"],
                 s["horiz_sigma_m"], s["hacc_claimed_m"], ratio, s["u_sd_m"], s["u_ptp_m"]))
        if ratio > OPTIMISM_FLAG:
            print("           ^ observed scatter is %.1fx the receiver's own hAcc: multipath, "
                  "which hAcc does not model. Survey sigma uses the observed number." % ratio)

    if len(a.nodes) == 2:
        p, q = (S[n] for n in a.nodes)
        d = math.hypot(q["e_m"] - p["e_m"], q["n_m"] - p["n_m"])
        sig = math.hypot(p["horiz_sigma_m"], q["horiz_sigma_m"])
        print("\nhorizontal separation %.2f m +/- %.2f m  ->  max |TDoA| %.1f +/- %.1f ms"
              % (d, sig, d / 343.0 * 1000.0, sig / 343.0 * 1000.0))

    print("\nVERTICAL FROM GNSS IS NOT USED. Measured here: per-node spread of %.0f m against a "
          "storey of about 3 m." % max(s["u_ptp_m"] for s in S.values()))

    if a.out:
        missing = [n for n in a.nodes if n not in heights]
        if missing:
            print("\nrefusing to write %s: no --heights for %s. A survey with a GNSS height in it "
                  "is worse than no survey, because nothing downstream can tell."
                  % (a.out, ", ".join(missing)), file=sys.stderr)
            return 3
        nodes = []
        for i, name in enumerate(a.nodes, 1):
            s = S[name]
            nodes.append({"node_id": ids.get(name, i), "name": name,
                          "e_m": round(s["e_m"], 3), "n_m": round(s["n_m"], 3),
                          "u_m": heights[name],
                          "sigma_m": round(s["horiz_sigma_m"], 3)})
        doc = {"frame": _FRAME, "units": _UNITS,
               "origin": {"lat_deg": origin[0], "lon_deg": origin[1], "h_ell_m": 0.0,
                          "source": "median of %s; height datum is the --heights argument, "
                                    "NOT GNSS" % a.nodes[0]},
               "nodes": nodes}
        with open(a.out, "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        print("\nwrote %s -- horizontal from GNSS medians, heights from --heights, sigma from "
              "observed scatter" % a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
