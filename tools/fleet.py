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
tool that exits non-zero on a node with no sky yet trains its operator to ignore it. It also never
declares a node offline from HTTP alone: use the OPNsense DHCP/ARP lookup, the node `/status`
probe, and the `hear-drain` ingestion heartbeat as the mandatory three-layer verification.

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

OFFLINE_VERIFICATION = (
    "1) confirm the node's DHCP lease and ARP entry on the OPNsense gateway; "
    "2) probe the node HTTP /status endpoint; "
    "3) verify hear-drain ingestion/heartbeat for that node"
)

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
PMTK_STATUS_CLASSES = {
    "esp32s3-i2s-gps",
    "esp32s3-speaker",
    "esp32s3-box3",
    "puc-pps",
    "puc-ntp",
}
# Classes whose `/status` is EXPECTED to lack one capability entirely. Keeping these as named
# sets, not inferred from the presence/absence of one JSON block, means the tool reports "this
# class has no PPS" instead of treating "PPS missing" as a failed PPS on every status shape it
# has not seen before.
NO_GPS_STATUS_CLASSES = {"puc-ntp"}
NO_PPS_STATUS_CLASSES = {"puc-ntp"}
NO_SD_STATUS_CLASSES = {"esp32s3-i2s-gps"}


def _gps_proto_label(d: Dict) -> str:
    if not gps_expected(d):
        return "NOGPS"
    return "PMTK" if _status_class(d) in PMTK_STATUS_CLASSES else "UBX"


def _gps_summary(d: Dict) -> str:
    gps = d.get("gps") or {}
    return "%s:%s/%s" % (_gps_proto_label(d), gps.get("fix", "?"), gps.get("sats", "?"))


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
    g = d.get("gps") or {}
    p = d.get("pps") or {}
    t = d.get("time") or {}
    a = d.get("audio") or {}
    gate = d.get("gate") or {}
    # spread is a cumulative high-water mark, not a live figure: one long interval near boot pins
    # it for the life of the run. Shown with the edge count so a big number on a young node reads
    # as what it usually is.
    # rssi and disc are absent on firmware before v0.1.1, and rssi is null when not associated.
    n = d.get("net") or {}
    rssi = n.get("rssi")
    # Keep the raw fix number, but NEVER bare: PMTK GGA quality 1 and UBX fixType 3 are both
    # healthy fixes on different scales, so the protocol travels with the number in the report.
    return ("%-9s %-14s up %6ds  gps %-12s tAcc %5s ns  pps %6d sp %5s us g%-3d  "
            "utc %-5s rej %-4s  dets %4d floor %-5s amb %-5s  rssi %4s disc %-3s  sd %-5s %s"
            % (name, d.get("fw", "?")[:14], int(d.get("uptime_s") or 0),
               _gps_summary(d), g.get("tacc_ns", "?"),
               int(p.get("edges") or 0), p.get("spread_us", "?"), int(p.get("glitches") or 0),
               "yes" if t.get("valid") else "NO", t.get("label_rejects", "?"),
               int(a.get("detections") or 0), gate.get("floor", "?"), a.get("ambient", "?"),
               "?" if rssi is None else rssi, n.get("disc", "?"),
               d.get("sd_free_mb", "?"), "" if d.get("sd") else "NO CARD"))


def _status_class(d: Dict) -> str:
    return str(d.get("class") or "").strip().lower()


def gps_expected(d: Dict) -> bool:
    return _status_class(d) not in NO_GPS_STATUS_CLASSES


def pps_expected(d: Dict) -> bool:
    return _status_class(d) not in NO_PPS_STATUS_CLASSES


def sd_expected(d: Dict) -> bool:
    return _status_class(d) not in NO_SD_STATUS_CLASSES


def gps_fix_ok(d: Dict) -> bool:
    # `gps.fix` is not one scale fleet-wide: PMTK nodes report raw NMEA GGA fix quality
    # (`esp32s3-i2s-gps`/`esp32s3-speaker`/PUC, 1 = a live fix) while UBX nodes report u-blox
    # fixType (`xiao-s3-*`, 3 = 3D). A universal `fix != 3` warning falsely condemns healthy PMTK
    # nodes even when the firmware itself would call them fixed.
    if not gps_expected(d):
        return True
    gps = d.get("gps") or {}
    try:
        fix = int(gps.get("fix"))
    except (TypeError, ValueError):
        return False
    return fix >= 1 if _status_class(d) in PMTK_STATUS_CLASSES else fix >= 3


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("nodes", nargs="+")
    # ⚠️OPT-IN, AND ONLY FOR A VERIFIED ONE-BUILD FLEET. This tool's default stays report-only,
    # because a node with no sky yet is not a failure and a tool that cries wolf teaches its
    # operator to ignore it. But when a scheduled caller asks for the one-build invariant, absence
    # must not read as agreement: a split/canary build hidden behind an unreachable node is
    # exactly the drift this tool exists to catch.
    #
    # So this flag fails CLOSED. The exit code is 2 when the answering nodes are split, 1 when any
    # node did not answer and the invariant therefore could not be verified, and 0 only when every
    # node answered and all reported one build. That mirrors the same "absent must not read as ok"
    # rule now enforced in tools/hear_drain.py's scene-file check.
    ap.add_argument("--require-one-build", action="store_true",
                    help="exit 2 if the answering nodes are split, 1 if any node did not answer "
                         "and the one-build check is inconclusive, 0 only when all nodes answer "
                         "and report one build")
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
        print("  HTTP PROBE FAILED  " + d)
        print("    OFFLINE UNCONFIRMED -- " + OFFLINE_VERIFICATION)

    builds = {d.get("fw", "?") for d in got.values()}
    if len(got) > 1:
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
            if not (d.get("time") or {}).get("valid"):
                why.append("no UTC anchor")
            if gps_expected(d) and not gps_fix_ok(d):
                why.append("fix %s" % ((d.get("gps") or {}).get("fix", "?")))
            if pps_expected(d) and not int(((d.get("pps") or {}).get("edges") or 0)):
                why.append("no PPS lock")
            if sd_expected(d) and not d.get("sd"):
                why.append("no SD card")
            elif sd_expected(d) and isinstance(d.get("sd_free_mb"), int):
                hours = d["sd_free_mb"] / SCENE_MB_PER_H
                if hours < SCENE_HEADROOM_H:
                    why.append("%d MB free = %.1f h of scene rows, below the headroom floor"
                               % (d["sd_free_mb"], hours))
            if why:
                print("  %-9s %s" % (n, "; ".join(why)))
    if a.require_one_build:
        if len(builds) > 1:
            return 2
        if dead:
            print()
            print("⚠️BUILD CHECK INCONCLUSIVE: %d node(s) did not answer, so one-build could not "
                  "be verified" % len(dead))
            return 1
        return 0
    return 0 if not dead else 1


if __name__ == "__main__":
    raise SystemExit(main())
