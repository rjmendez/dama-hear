#!/usr/bin/env python3
"""One line per node: is the fleet on the same build, and is each node fit to contribute tonight?

    python3 tools/fleet.py nyquist mach rankine

WHY THIS EXISTS. Three nodes reported byte-identical /status SHAPES while running binaries built
from different commits, and nothing in the fleet could tell them apart -- "are they all on the
same version?" was answerable only from memory of who was flashed when. A capture whose nodes
silently differ is not one capture, and the difference shows up as a bias nobody can attribute.
`fw` (gen_secrets.py, from git describe --always --dirty) is what closes that; this reads it back.

It reports and does not gate. The exit code says whether every node ANSWERED, never whether the
answers were good -- the same rule tools/validate_scene.py follows, and for the same reason: a
tool that exits non-zero on a node with no sky yet trains its operator to ignore it.

⚠️A `-dirty` build did not come from any commit. Two nodes both reporting the same -dirty string
are NOT thereby on the same code; the string names the last tag, not the working tree.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import Dict, List, Optional, Sequence

TIMEOUT_S = 8.0


def fetch(node: str) -> Dict:
    url = node if node.startswith("http") else "http://%s.local/status" % node
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
        return json.load(r)


def row(name: str, d: Dict) -> str:
    g, p, t, a = d["gps"], d["pps"], d["time"], d["audio"]
    # spread is a cumulative high-water mark, not a live figure: one long interval near boot pins
    # it for the life of the run. Shown with the edge count so a big number on a young node reads
    # as what it usually is.
    return ("%-9s %-14s up %6ds  fix %d/%-2d tAcc %5s ns  pps %6d sp %5s us g%-3d  "
            "utc %-5s rej %-4s  dets %4d floor %-5s amb %-5s  sd %-5s %s"
            % (name, d.get("fw", "?")[:14], d["uptime_s"], g["fix"], g["sats"], g["tacc_ns"],
               p["edges"], p["spread_us"], p["glitches"],
               "yes" if t["valid"] else "NO", t["label_rejects"],
               a["detections"], d["gate"]["floor"], a["ambient"],
               d.get("sd_free_mb", "?"), "" if d["sd"] else "NO CARD"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("nodes", nargs="+")
    a = ap.parse_args(argv)

    got: Dict[str, Dict] = {}
    dead: List[str] = []
    for n in a.nodes:
        try:
            got[n] = fetch(n)
        except Exception as e:
            dead.append("%s: %s" % (n, e))

    print("%-9s %-14s %s" % ("node", "fw", "state"))
    for n in a.nodes:
        if n in got:
            print(row(n, got[n]))
    for d in dead:
        print("  UNREACHABLE  " + d)

    if len(got) > 1:
        builds = {d.get("fw", "?") for d in got.values()}
        print()
        if len(builds) == 1:
            b = builds.pop()
            print("all %d nodes on %s" % (len(got), b)
                  + ("   ⚠️-dirty: this did not come from a commit" if b.endswith("-dirty")
                     else ""))
        else:
            print("⚠️FLEET IS SPLIT across %d builds: %s" % (len(builds), ", ".join(sorted(builds))))
            print("  Arrivals from different builds are not comparable until you know what "
                  "changed between them.")

        # Anything that would make tonight's capture unusable, said once rather than left to be
        # spotted in a column.
        for n, d in got.items():
            why = []
            if not d["time"]["valid"]:
                why.append("no UTC anchor")
            if d["gps"]["fix"] != 3:
                why.append("fix %d" % d["gps"]["fix"])
            if not d["sd"]:
                why.append("no SD card")
            elif isinstance(d.get("sd_free_mb"), int) and d["sd_free_mb"] < 200:
                why.append("%d MB free" % d["sd_free_mb"])
            if why:
                print("  %-9s cannot contribute arrivals tonight: %s" % (n, "; ".join(why)))
    return 0 if not dead else 1


if __name__ == "__main__":
    raise SystemExit(main())
