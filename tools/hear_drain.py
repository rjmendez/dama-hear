#!/usr/bin/env python3
"""Drain every hear sensor into one pool, on a timer, and say loudly when one stops.

    python3 tools/hear_drain.py --pool ~/hear-pool \
        --node nyquist=172.16.100.105 --node mach=172.16.100.116 \
        --phone-corpus ~/sketch_corpus

    python3 tools/hear_drain.py --pool ~/hear-pool --check     # exit 1 if a sensor has gone quiet

WHY THIS EXISTS. Until now every byte of node data was moved by a hand-run `curl` into a dated
directory, and the only scheduled process touching the nodes was `watch.py` polling ONE node's
`/status` -- health, not data, and not even both nodes. The cost was not theoretical:

  * the 2026-09-07 drain sat on a laptop, was not in any corpus, and was found only by asking
  * 730 of its detections read as 0 rows under a header-name parser and raised nothing
  * `dets.csv` rolls to `dets-prev.csv` on reflash, so a missed window loses a file outright

⚠️FETCH BOTH `dets.csv` AND `dets-prev.csv`, ALWAYS. The roll happens on reflash and on a schema
change, and whichever one this run did not fetch is the one that rolls off next. They are cheap
(tens of KB) and the pool deduplicates, so fetching both every time costs nothing and closes the
window that would otherwise silently drop a boot's worth of detections.

⚠️IDENTITY IS CHECKED, NOT ASSUMED. A node is named by `--node <name>=<ip>`, and `/status` is
read back before anything it served is ingested. DHCP moves; two nodes swapping leases would
otherwise file each other's detections under each other's names, and every TDoA solution built on
that pool would be wrong in a way no later check could find. A mismatch refuses THAT node and the
run continues with the others -- one bad lease must not stop the drain.

⚠️RAW BYTES ARE ARCHIVED BEFORE THEY ARE PARSED. `<pool>/raw/<node>/<utc>-<file>` keeps exactly
what the node served. Every parser in this repo has been wrong at least once about a real file;
re-parsing an archive is free, re-fetching a file the node has since rolled is impossible.

STALENESS IS THE FAILURE MODE THAT MATTERS, and it is silent by nature: a drainer whose HTTP
succeeds and whose node has stopped detecting exits 0 forever. `--check` reads the heartbeat and
fails if any sensor's last SUCCESSFUL fetch is older than `--max-stale-s`, which is what a timer
or a monitor should key on -- not on this script's own exit status during a normal run.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from hear import pool as P                                          # noqa: E402

# The card files worth pulling every run. `dets.csv` and its rolled predecessor carry the
# sketches; `health.csv` carries the PPS/fs context a later reader needs to judge them and is
# archived but NOT ingested -- the pool holds sketches, and mixing a second row shape into it
# would make its counts mean two things.
DETS_FILES = ("dets.csv", "dets-prev.csv")
CONTEXT_FILES = ("health.csv",)

DEFAULT_TIMEOUT_S = 30.0
# A node reporting detections at the measured night rate goes quiet for hours in daylight, so
# staleness is measured on the FETCH, not on new rows. 2 h is comfortably longer than any timer
# interval and short enough to catch a node that fell off the wifi before a night is lost.
DEFAULT_MAX_STALE_S = 7200.0


def _get(url: str, timeout: float = DEFAULT_TIMEOUT_S) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def fetch_status(ip: str, timeout: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    return json.loads(_get("http://%s/status" % ip, timeout).decode("utf-8", "replace"))


def fetch_sd(ip: str, name: str, timeout: float = DEFAULT_TIMEOUT_S) -> Optional[bytes]:
    """One file off the card, or None if the node does not have it.

    A missing `dets-prev.csv` is NORMAL -- it exists only after a roll -- so it is not an error.
    The node answers a missing file with a short body rather than a 404, so the body is checked:
    anything that is not a CSV header is treated as absent and recorded as such.
    """
    try:
        body = _get("http://%s/sd?file=%s" % (ip, name), timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    head = body[:200].lstrip()
    if not head or not (head.startswith(b"node") or head.startswith(b"utc_us")):
        return None
    return body


def archive(root: str, node: str, name: str, body: bytes, stamp: int) -> str:
    d = os.path.join(root, "raw", node)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%d-%s" % (stamp, name))
    with open(p, "wb") as fh:
        fh.write(body)
    return p


def drain_node(pl: "P.Pool", node: str, ip: str, timeout: float = DEFAULT_TIMEOUT_S,
               stamp: Optional[int] = None) -> Dict[str, Any]:
    """Fetch, archive and ingest one node. Never raises for a node-side problem; reports it."""
    stamp = int(stamp if stamp is not None else time.time())
    out: Dict[str, Any] = {"node": node, "ip": ip, "at": stamp, "ok": False,
                           "files": [], "added": 0, "errors": []}
    try:
        st = fetch_status(ip, timeout)
    except Exception as e:
        out["errors"].append("status: %r" % (e,))
        return out
    said = str(st.get("node", ""))
    out["identity"] = said
    out["uptime_s"] = st.get("uptime_s")
    if said != node:
        out["errors"].append(
            "identity: %s answers as %r, not %r -- refusing to file its rows under the wrong "
            "name (check the DHCP lease)" % (ip, said, node))
        return out

    for name in CONTEXT_FILES:
        try:
            body = fetch_sd(ip, name, timeout)
            if body:
                out["files"].append({"name": name, "bytes": len(body), "ingested": False,
                                     "archived": archive(pl.root, node, name, body, stamp)})
        except Exception as e:
            out["errors"].append("%s: %r" % (name, e))

    for name in DETS_FILES:
        try:
            body = fetch_sd(ip, name, timeout)
        except Exception as e:
            out["errors"].append("%s: %r" % (name, e))
            continue
        if body is None:
            out["files"].append({"name": name, "absent": True})
            continue
        path = archive(pl.root, node, name, body, stamp)
        try:
            entry = pl.ingest_dets(path, default_node=node, origin="%s:/%s" % (node, name))
        except Exception as e:
            out["errors"].append("%s: ingest: %r" % (name, e))
            continue
        out["added"] += entry["added"]
        out["files"].append({"name": name, "bytes": len(body), "ingested": True,
                             "archived": path, **{k: entry[k] for k in
                                                  ("generation", "rows", "added", "duplicate",
                                                   "skipped", "skip_reasons")}})
    out["ok"] = not out["errors"]
    return out


def drain_phone_corpus(pl: "P.Pool", corpus_dir: str, days: int = 3) -> Dict[str, Any]:
    """Ingest the most recent `sketches-*.jsonl` the phone corpus worker has written.

    Only the last `days` files are re-read: they are the only ones that can still grow, and the
    pool deduplicates whatever overlaps. Older days are already in and re-reading a month of them
    on every timer tick is wasted I/O, not extra safety.
    """
    out: Dict[str, Any] = {"dir": corpus_dir, "files": [], "added": 0, "errors": []}
    if not os.path.isdir(corpus_dir):
        out["errors"].append("no such directory: %s" % corpus_dir)
        return out
    paths = sorted(glob.glob(os.path.join(corpus_dir, "sketches-*.jsonl")))[-days:]
    for p in paths:
        try:
            e = pl.ingest_mqtt_jsonl(p, origin="phone:%s" % os.path.basename(p))
        except Exception as ex:
            out["errors"].append("%s: %r" % (os.path.basename(p), ex))
            continue
        out["added"] += e["added"]
        out["files"].append({"name": os.path.basename(p), **{k: e[k] for k in
                                                             ("decoded", "added", "duplicate",
                                                              "skipped", "skip_reasons")}})
    out["ok"] = not out["errors"]
    return out


def backfill(pl: "P.Pool", paths: List[str], default_node: Optional[str] = None
             ) -> List[Dict[str, Any]]:
    """Ingest historical files: a dets.csv, a sketches-*.jsonl, or a directory of either.

    ⚠️A REFUSAL IS RECORDED, NOT SWALLOWED. An unreadable file returns an entry carrying `error`
    and the run reports a non-zero status. A backfill that skipped a file quietly is how the
    2026-09-07 drain sat unnoticed on a laptop for a day; the whole point of this path is that it
    can be re-run over everything anyone has kept and the pool's ledger then says what it made of
    each one.

    The node name for a G1-G3 file cannot come from the file -- those schemas have no node
    column -- so it comes from `default_node` and is recorded as asserted. When a directory is
    given, a file whose NAME begins `<something>_` is allowed to name its own node from that
    prefix, which is how the existing hand-made drains are laid out.
    """
    out: List[Dict[str, Any]] = []
    todo: List[str] = []
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            todo += sorted(glob.glob(os.path.join(p, "**", "*dets*.csv"), recursive=True))
            todo += sorted(glob.glob(os.path.join(p, "**", "sketches-*.jsonl"), recursive=True))
        else:
            todo.append(p)
    for p in todo:
        base = os.path.basename(p)
        try:
            if base.startswith("sketches-") and base.endswith(".jsonl"):
                out.append(pl.ingest_mqtt_jsonl(p))
            else:
                node = default_node
                for known in ("nyquist", "mach", "puc"):
                    if known in p:
                        node = known
                        break
                out.append(pl.ingest_dets(p, default_node=node))
        except Exception as e:
            out.append({"path": os.path.abspath(p), "error": "%s: %s" % (type(e).__name__, e)})
    return out


# ---------------------------------------------------------------- heartbeat

def heartbeat_path(root: str) -> str:
    return os.path.join(root, "heartbeat.json")


def write_heartbeat(root: str, results: List[Dict[str, Any]], phone: Optional[Dict[str, Any]],
                    now: Optional[float] = None) -> Dict[str, Any]:
    """Merge this run into the heartbeat, keeping each sensor's last SUCCESS.

    ⚠️A failed run must not overwrite a sensor's last-success time with `now`. That is exactly how
    a staleness check becomes a no-op: it would report the drainer's liveness, which is never in
    doubt when it is the thing doing the reporting, instead of the sensor's.
    """
    now = time.time() if now is None else now
    p = heartbeat_path(root)
    hb: Dict[str, Any] = {"sensors": {}}
    if os.path.exists(p):
        try:
            hb = json.load(open(p))
            hb.setdefault("sensors", {})
        except Exception:
            hb = {"sensors": {}}
    hb["last_run_s"] = now
    for r in results:
        s = hb["sensors"].setdefault(r["node"], {})
        s["kind"] = "node"
        s["last_attempt_s"] = now
        s["last_error"] = r["errors"][0] if r["errors"] else None
        if r["ok"]:
            s["last_success_s"] = now
            s["last_added"] = r["added"]
            s["uptime_s"] = r.get("uptime_s")
    if phone is not None:
        s = hb["sensors"].setdefault("phones", {})
        s["kind"] = "phone-corpus"
        s["last_attempt_s"] = now
        s["last_error"] = phone["errors"][0] if phone.get("errors") else None
        if phone.get("ok"):
            s["last_success_s"] = now
            s["last_added"] = phone["added"]
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(hb, fh, indent=2, sort_keys=True)
    os.replace(tmp, p)
    return hb


def check(root: str, max_stale_s: float = DEFAULT_MAX_STALE_S,
          now: Optional[float] = None) -> Tuple[int, List[str]]:
    """(exit code, lines). Non-zero if any sensor's last SUCCESS is older than `max_stale_s`."""
    now = time.time() if now is None else now
    p = heartbeat_path(root)
    if not os.path.exists(p):
        return 1, ["no heartbeat at %s -- the drain has never completed a run" % p]
    hb = json.load(open(p))
    lines, bad = [], 0
    sensors = hb.get("sensors") or {}
    if not sensors:
        return 1, ["heartbeat names no sensors"]
    for name in sorted(sensors):
        s = sensors[name]
        last = s.get("last_success_s")
        if last is None:
            lines.append("%-10s NEVER succeeded (last error: %s)" % (name, s.get("last_error")))
            bad += 1
            continue
        age = now - float(last)
        state = "STALE" if age > max_stale_s else "ok"
        bad += state == "STALE"
        lines.append("%-10s %-5s last success %.0f s ago%s"
                     % (name, state, age, "" if not s.get("last_error")
                        else "  (last error: %s)" % s["last_error"]))
    return (1 if bad else 0), lines


# ---------------------------------------------------------------- cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pool", default="~/hear-pool", help="pool root (created if absent)")
    ap.add_argument("--node", action="append", default=[], metavar="NAME=IP",
                    help="a node to drain; repeatable")
    ap.add_argument("--phone-corpus", default=None,
                    help="directory of sketches-YYYY-MM-DD.jsonl from the corpus worker")
    ap.add_argument("--phone-days", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--max-stale-s", type=float, default=DEFAULT_MAX_STALE_S)
    ap.add_argument("--check", action="store_true",
                    help="report sensor staleness from the heartbeat and exit; drains nothing")
    ap.add_argument("--ingest", action="append", default=[], metavar="PATH",
                    help="backfill a dets.csv / sketches-*.jsonl, or a directory of them; "
                         "repeatable and idempotent. Node name is taken from the file when it "
                         "carries one and from --ingest-node otherwise")
    ap.add_argument("--ingest-node", default=None,
                    help="node name for backfilled dets.csv files whose schema carries none "
                         "(G1-G3). Recorded as asserted, not as read from the file")
    ap.add_argument("--stats", action="store_true", help="print what the pool holds and exit")
    ap.add_argument("--json", action="store_true", help="machine-readable run report on stdout")
    a = ap.parse_args(argv)

    root = os.path.expanduser(a.pool)
    if a.check:
        code, lines = check(root, a.max_stale_s)
        print("\n".join(lines))
        return code

    pl = P.Pool(root)
    if a.stats:
        print(json.dumps(pl.stats(), indent=2, sort_keys=True))
        return 0

    if a.ingest:
        entries = backfill(pl, a.ingest, default_node=a.ingest_node)
        for e in entries:
            print("%-46s %s" % (os.path.basename(e["path"]),
                                {k: e[k] for k in ("generation", "rows", "added", "duplicate",
                                                   "skipped")
                                 if k in e} if "error" not in e else "REFUSED " + e["error"]))
        print("\n" + json.dumps(pl.stats(), indent=2, sort_keys=True))
        return 0 if all("error" not in e for e in entries) else 1

    nodes: List[Tuple[str, str]] = []
    for spec in a.node:
        if "=" not in spec:
            ap.error("--node wants NAME=IP, got %r" % spec)
        name, ip = spec.split("=", 1)
        nodes.append((name.strip(), ip.strip()))

    results = [drain_node(pl, n, ip, a.timeout) for n, ip in nodes]
    phone = drain_phone_corpus(pl, os.path.expanduser(a.phone_corpus), a.phone_days) \
        if a.phone_corpus else None
    write_heartbeat(root, results, phone)

    report = {"results": results, "phone": phone, "stats": pl.stats()}
    if a.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for r in results:
            print("%-9s %-4s +%d record(s)%s" % (
                r["node"], "ok" if r["ok"] else "FAIL", r["added"],
                "" if r["ok"] else "  " + "; ".join(r["errors"])))
            for f in r["files"]:
                if f.get("absent"):
                    print("    %-14s absent" % f["name"])
                elif f.get("ingested"):
                    print("    %-14s %s  %d row(s) -> +%d new, %d dup, %d skipped %s"
                          % (f["name"], f["generation"], f["rows"], f["added"], f["duplicate"],
                             f["skipped"], f["skip_reasons"] or ""))
                else:
                    print("    %-14s %d B archived" % (f["name"], f["bytes"]))
        if phone is not None:
            print("phones    %-4s +%d record(s)%s"
                  % ("ok" if phone.get("ok") else "FAIL", phone["added"],
                     "" if phone.get("ok") else "  " + "; ".join(phone["errors"])))
        s = report["stats"]
        print("\npool %s: %d record(s), %s, %d anchored / %d not"
              % (root, s["records"], s["by_source"], s["anchored"], s["unanchored"]))

    # A run where every sensor failed is a failure. A run where one of several failed is not --
    # the pool still gained the others, and a timer that gives up on all of them because one node
    # is off the wifi is how the remaining nodes' data goes missing too.
    if results and all(not r["ok"] for r in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
