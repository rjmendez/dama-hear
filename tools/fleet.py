#!/usr/bin/env python3
"""One line per node: is the fleet on the same build, and is each node fit to contribute?

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
import time
import urllib.request
from typing import Dict, List, Optional, Sequence

# A node flushing its SD card blocks its HTTP loop for tens of milliseconds, and a mDNS lookup
# can take a second on its own. 8 s was enough to report mach as UNREACHABLE while it was serving
# 200s to a plain curl a moment later -- a false negative from the tool meant to decide whether
# a capture can run.
TIMEOUT_S = 15.0
RETRIES = 2

# ⚠️A CONNECTION REFUSED IS NOT NECESSARILY A DEAD NODE. tools/hear_drain.py:346-349 measured it
# directly: "the ESP32 core serves one client at a time and resets the rest rather than queueing
# them" -- reproduced live 2026-09-10, rankine answering ICMP while refusing TCP:80 outright for
# the seconds another client held it. A same-instant retry can land on the very connection that
# is still holding the node, so the retries here back off -- same 1.5 s as
# tools/hear_drain.py's LS_RETRY_BACKOFF_S, the other place this exact collision is measured and
# waited out, so a future change to that number is one place to make, not two.
RETRY_BACKOFF_S = 1.5

# MEASURED, not assumed: nyquist wrote 2,689,167 B of scene.csv across 8,018 rows = 335 B/row, at
# one row per 1.024 s. Everything else on the card is bounded or negligible -- the clip budget is a
# hard 6 MB cap, health.csv runs ~7 KB/h, dets.csv less.
#
# ⚠️The first version of this check flagged anything under 200 MB as unable to contribute, on my
# guess that rankine's 72 MB was "about two hours". It is about sixty. 200 MB is a week of
# continuous capture, so that threshold condemned a card with days of headroom -- and a tool that
# cries wolf is worse than no tool.
SCENE_MB_PER_H = 335 * (3600 / 1.024) / 1e6      # 1.18 MB/h
SCENE_HEADROOM_H = 14.0                           # scene-row hours the card must hold if the drain stops


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
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
                return json.load(r)
        except Exception as e:      # noqa: BLE001 -- one retry, then report the last reason
            last = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_BACKOFF_S)
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
    # ⚠️OPT-IN, AND ONLY FOR THE SPLIT -- UNREACHABILITY DOES NOT COUNT. This tool's default is to
    # report and not gate, because a node with no sky yet is not a failure and a tool that cries
    # wolf teaches its operator to ignore it. A SPLIT FLEET is different in kind: it is never
    # transient, never self-healing, and it silently invalidates the capture -- arrivals from
    # different builds are not comparable. So a scheduled caller can ask for that one condition to
    # be an error, and gets nothing else: with this flag, the exit code is 2 for a split and 0
    # otherwise, full stop -- a node that did not answer is neither.
    #
    # ⚠️IT USED TO FALL THROUGH TO `0 if not dead else 1` even with this flag set, so
    # deploy/k8s/hear-drain.yaml's hear-drain-check (the only caller) went red on an unreachable
    # node -- transient (a reboot, or the ESP32 refusing a second concurrent client, see
    # RETRY_BACKOFF_S) or not -- even though its own comment already says "gates on the SPLIT
    # ALONE. A node with no fix yet is not a failure". Reachability has its own, better check
    # already: tools/hear_drain.py --check's per-sensor last-success staleness gate. This flag is
    # not a second one.
    ap.add_argument("--require-one-build", action="store_true",
                    help="exit 2 if the nodes that ANSWERED are not all on one build, 0 "
                         "otherwise -- unreachability is not gated by this flag at all")
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

        # Anything that would make this node's capture unusable, said once rather than left to be
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
                if hours < SCENE_HEADROOM_H:
                    why.append("%d MB free = %.1f h of scene rows, below the headroom floor"
                               % (d["sd_free_mb"], hours))
            if why:
                print("  %-9s %s" % (n, "; ".join(why)))
    if a.require_one_build:
        return 2 if len({d.get("fw", "?") for d in got.values()}) > 1 else 0
    return 0 if not dead else 1


if __name__ == "__main__":
    raise SystemExit(main())
