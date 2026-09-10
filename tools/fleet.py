#!/usr/bin/env python3
"""One line per node: is the fleet on the same build, and is each node fit to contribute tonight?

    python3 tools/fleet.py nyquist mach rankine                 # needs mDNS
    python3 tools/fleet.py nyquist=172.16.100.105 mach=172.16.100.116   # anywhere

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

# A node flushing its SD card blocks its HTTP loop for tens of milliseconds, and a mDNS lookup
# can take a second on its own. 8 s was enough to report mach as UNREACHABLE while it was serving
# 200s to a plain curl a moment later -- a false negative from the tool meant to decide whether
# tonight's capture can run.
TIMEOUT_S = 15.0
RETRIES = 2

# MEASURED, not assumed: nyquist wrote 2,689,167 B of scene.csv across 8,018 rows = 335 B/row, at
# one row per 1.024 s. Everything else on the card is bounded or negligible -- the clip budget is a
# hard 6 MB cap, health.csv runs ~7 KB/h, dets.csv less.
#
# ⚠️The first version of this check flagged anything under 200 MB as unable to contribute, on my
# guess that rankine's 72 MB was "about two hours". It is about sixty. 200 MB is a week of
# continuous capture, so that threshold condemned a card with days of headroom -- and a tool that
# cries wolf about tonight's run is worse than no tool.
SCENE_MB_PER_H = 335 * (3600 / 1.024) / 1e6      # 1.18 MB/h
NIGHT_H = 14.0                                    # dusk to well past dawn


def split_target(node: str) -> tuple:
    """`name`, `name=host`, or a bare URL -> (display name, status URL).

    ⚠️`.local` DOES NOT RESOLVE from WSL or from inside k3s -- /etc/nsswitch.conf is `files dns`
    with no mDNS, and DHCP registers the chip hostname (`esp32s3-5B4B40`) rather than the friendly
    one. So the bare-name form works only from a host with mDNS, which is not where this gets run:
    the drain that reaches every node hourly lives in the cluster and addresses them by IP.

    That is not a cosmetic gap. This tool exists to catch exactly the drift it then missed -- three
    nodes on three different builds, one of them a hand-built sketch reporting `fw: unknown` for
    long enough that nobody could say what it was running. It could not have caught that, because
    it could not resolve a single node from the machine anyone was going to run it on.

    `name=host` is the form tools/hear_drain.py already takes, so one map serves both.
    """
    if node.startswith("http"):
        return node, node if node.rstrip("/").endswith("/status") else node.rstrip("/") + "/status"
    if "=" in node:
        name, _, host = node.partition("=")
        return name, "http://%s/status" % host
    return node, "http://%s.local/status" % node


def fetch(node: str) -> Dict:
    _, url = split_target(node)
    last: Optional[Exception] = None
    for _ in range(RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
                return json.load(r)
        except Exception as e:      # noqa: BLE001 -- one retry, then report the last reason
            last = e
    raise last if last else RuntimeError("unreachable")


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
    # ⚠️OPT-IN, AND ONLY FOR THE SPLIT. This tool's default is to report and not gate, because a
    # node with no sky yet is not a failure and a tool that cries wolf about tonight teaches its
    # operator to ignore it. A SPLIT FLEET is different in kind: it is never transient, never
    # self-healing, and it silently invalidates the capture -- arrivals from different builds are
    # not comparable. So a scheduled caller can ask for that one condition to be an error, and
    # gets nothing else. Unreachability still only affects the exit code as it always did.
    ap.add_argument("--require-one-build", action="store_true",
                    help="exit non-zero if the nodes that ANSWERED are not all on one build")
    a = ap.parse_args(argv)

    got: Dict[str, Dict] = {}
    dead: List[str] = []
    names: Dict[str, str] = {}
    for n in a.nodes:
        names[n] = split_target(n)[0]
        try:
            got[n] = fetch(n)
        except Exception as e:
            dead.append("%s: %s" % (names[n], e))

    print("%-9s %-14s %s" % ("node", "fw", "state"))
    for n in a.nodes:
        if n in got:
            # the node's OWN name when it gave one -- an argument is what was asked for, and a
            # flash that landed the wrong identity is the failure this whole column exists for
            print(row(got[n].get("node") or names[n], got[n]))
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
            n = d.get("node") or names.get(n, n)
            why = []
            if not d["time"]["valid"]:
                why.append("no UTC anchor")
            if d["gps"]["fix"] != 3:
                why.append("fix %d" % d["gps"]["fix"])
            if not d["sd"]:
                why.append("no SD card")
            elif isinstance(d.get("sd_free_mb"), int):
                hours = d["sd_free_mb"] / SCENE_MB_PER_H
                if hours < NIGHT_H:
                    why.append("%d MB free = %.1f h of scene rows, short of a night"
                               % (d["sd_free_mb"], hours))
            if why:
                print("  %-9s %s" % (n, "; ".join(why)))
    if a.require_one_build and len({d.get("fw", "?") for d in got.values()}) > 1:
        return 2
    return 0 if not dead else 1


if __name__ == "__main__":
    raise SystemExit(main())
