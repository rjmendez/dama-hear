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

⚠️THE SECOND SILENT FAILURE IS THE TAIL OUTGROWING ITSELF, and it is already happening. The scene
fetch takes the last `SCENE_TAIL_BYTES` of a file that grows forever, so it reaches back a fixed
number of BYTES, not a fixed number of hours. When the comment below was written that was ~2.3 h
and every run overlapped the last one. Measured 2026-09-08: scene.csv is 16.6 MB on nyquist and
16.3 MB on mach, and at the measured 235.0 / 232.0 B per row the 2 MB window reaches back
8,510 / 8,620 rows = 2.42 / 2.45 h -- against a current nyquist boot of 11,706 rows = 3.33 h. The
window no longer covers a boot, and 60.05% of every scene row ever read was already a duplicate,
so a shrinking overlap is invisible in every count the ledger keeps. Two outages have already cost
11,205 s = 3.11 h of scene rows that no run will ever ask for again.

So the drain now measures the reach-back instead of assuming it, and CLOSES it:

  * `/ls` gives the file's size. `window_start = size_now - len(body)` is the first byte this
    fetch actually saw, and `<pool>/state/scene_fetch.json` remembers where the last successful
    fetch ended. `unfetched_bytes = window_start - prev_size` is arithmetic over the FILE, not
    over the window, and it is immune to the 7.6% of scene rows that carry no PPS anchor.
  * a positive gap is then REFETCHED with a bigger `tail=` -- the node's only reach-back control
    (`f.seek(size - tail)`; there is no offset argument and no Range) -- capped by
    `--max-catchup-bytes`. Whatever the cap refuses is reported as the residual that is genuinely
    lost. An accounting that stopped at naming the gap would leave the rows on the card.
  * the watermark advances only to the size measured BEFORE the fetch, and only after a
    successful ingest. Both directions of that are deliberate: the file grows during the fetch,
    so the pre-fetch size is the one the body is guaranteed to have reached, and a run that
    failed must not move the mark past rows it never stored.

⚠️THE GAP IS BIASED TOWARDS SILENCE, ON PURPOSE. `size_now` is read before the body, and the file
grows ~235 B/s underneath, so `window_start` is understated by roughly the round trip -- a few
hundred bytes. A sub-row gap therefore reads as zero, thresholded against the fetched body's OWN
mean row size rather than a constant. That direction is chosen because 60% of normal traffic is
overlap: a bias the other way would fire on every healthy run and be turned off within a week.
Both raw numbers (`size_now`, `fetched_bytes`) are on the ledger so the arithmetic stays auditable.
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
# ⚠️THE SCENE IS THE CORPUS. dets.csv only exists where the impulse gate fired, so a pool built
# from it alone can hold nothing but impulsive events. scene.csv carries a row every ~1.024 s
# regardless -- the ambient world this project is also about. See hear/scenefile.py.
SCENE_FILES = ("scene.csv", "scene-prev.csv")
CONTEXT_FILES = ("health.csv",)

# ⚠️SCENE IS FETCHED BY TAIL, NOT WHOLE. It grows without bound (16.6 MB on 2026-09-08; 2-7
# minutes to pull at the node's measured 40-135 KB/s) and re-fetching all of it every 15 minutes
# would spend the whole interval on rows already stored. The ingest is content-addressed, so the
# overlap costs nothing.
#
# ⚠️THIS WINDOW IS A BYTE COUNT AND ITS REACH-BACK IN HOURS SHRINKS AS THE ROW RATE OR ROW SIZE
# RISES. It is NOT self-healing by construction; it is self-healing only while it is still wider
# than the interval between two successful runs, and that is now a measurement, not an assumption:
# 2 MB / 235.0 B per row / (1024 ms per row) = 2.42 h on nyquist, 2.45 h on mach. See
# `scene_gap()` -- the drain measures its own reach-back every run and refetches past it.
SCENE_TAIL_BYTES = 2_000_000

# How far back a catch-up refetch may reach in one go. Sized against the CronJob period, not
# against the file: at the measured 40 KB/s floor, 8 MB is ~3.4 min and at 135 KB/s ~1 min, both
# comfortably inside the 15 min in deploy/k8s/hear-drain.yaml even with the normal tail, the dets
# files and a second node in the same run. That CronJob is `concurrencyPolicy: Forbid` (verified,
# hear-drain.yaml:30), so an overrun would SKIP the next tick rather than double up -- which is
# still a missed window, so raising this trades a bounded catch-up for a skipped run.
DEFAULT_MAX_CATCHUP_BYTES = 8_000_000

# `<pool>/state/scene_fetch.json`: per node, per file, the file SIZE at the end of the last
# successful window. Pool already creates `state/`.
SCENE_STATE_FILE = "scene_fetch.json"

# --check fails above this many unfetched bytes. Zero is the right default because `scene_gap()`
# has already clamped anything smaller than one row to zero: what survives to the heartbeat is a
# whole row of scene the pool will never see, and there is no acceptable number of those.
DEFAULT_MAX_UNFETCHED_BYTES = 0

# The advisory boot cross-check walks stored scene partitions, so its cost grows with how long the
# node has been up. Past this it is SKIPPED and says so -- the byte watermark is the number that
# matters and a cross-check must not turn a 15-minute drain into an hour of gzip.
BOOT_AUDIT_MAX_DAYS = 3

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


def fetch_sd(ip: str, name: str, timeout: float = DEFAULT_TIMEOUT_S,
             tail: Optional[int] = None) -> Optional[bytes]:
    """One file off the card, or None if the node does not have it.

    A missing `dets-prev.csv` is NORMAL -- it exists only after a roll -- so it is not an error.
    The node answers a missing file with a short body rather than a 404, so the body is checked:
    anything that is not a CSV header is treated as absent and recorded as such.
    """
    url = "http://%s/sd?file=%s" % (ip, name)
    if tail:
        url += "&tail=%d" % int(tail)
    try:
        body = _get(url, timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if not body.strip():
        return None
    # A whole-file fetch must start with a header. A TAIL fetch starts mid-row and legitimately
    # does not, so it is only checked for having a complete row in it -- the reader drops the
    # leading fragment and says that it did.
    head = body[:200].lstrip()
    if tail:
        return body if b"\n" in body else None
    if not (head.startswith(b"node") or head.startswith(b"utc_us")):
        return None
    return body


def _ls_sizes(ip: str, timeout: float = DEFAULT_TIMEOUT_S) -> Dict[str, int]:
    """`GET /ls` -> {filename: bytes}. Raises on a transport failure; never returns a guess.

    The node prints one line per card entry, `- <name>  <N> B` for a file and `d ` for a
    directory (night_node.ino, the /ls handler). Only files are returned, and the name is
    normalised without its leading slash because the core has served it both ways.

    ⚠️AN UNPARSEABLE LINE IS DROPPED, WHICH MAKES ITS FILE ABSENT FROM THE RESULT, WHICH THE
    CALLER MUST READ AS `size unknown` AND NOT AS `no loss`. That distinction is the whole
    contract here: a measurement that failed and a measurement that came back clean are the two
    things this module exists to keep apart.
    """
    body = _get("http://%s/ls" % ip, timeout).decode("utf-8", "replace")
    out: Dict[str, int] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("- ") or not line.endswith(" B"):
            continue
        rest = line[2:-2].rstrip()
        name, sep, num = rest.rpartition("  ")
        if not sep:
            name, sep, num = rest.rpartition(" ")
        try:
            n = int(num.strip())
        except ValueError:
            continue
        name = name.strip().lstrip("/")
        if name:
            out[name] = n
    return out


# ---------------------------------------------------------------- byte watermark

def watermark_path(root: str) -> str:
    return os.path.join(root, "state", SCENE_STATE_FILE)


def read_watermarks(root: str) -> Dict[str, Any]:
    """{node: {file: {size, at, mean_row_bytes}}}, or {} when there is none or it is unreadable.

    An unreadable watermark reads as "first run" for every node, which costs one window of
    re-fetched overlap and cannot invent a gap. The alternative -- trusting a half-written file --
    could advance a mark past rows nobody has.
    """
    p = watermark_path(root)
    if not os.path.exists(p):
        return {}
    try:
        wm = json.load(open(p))
    except Exception:
        return {}
    return wm if isinstance(wm, dict) else {}


def write_watermarks(root: str, wm: Dict[str, Any]) -> str:
    """Write atomically. A torn watermark is a silent gap next run, so tmp + os.replace."""
    p = watermark_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(wm, fh, indent=2, sort_keys=True)
    os.replace(tmp, p)
    return p


def mean_row_bytes(body: bytes) -> Optional[float]:
    """The fetched body's OWN mean row size, or None if it holds no complete row.

    ⚠️MEASURED, NEVER PINNED. S1 rows are ~226 B and S2 rows ~235 B, both present in this pool's
    ledger, and the geometry columns are firmware's to change again. A hardcoded row size would
    be a threshold validated against the one sample it was taken from.
    """
    n = body.count(b"\n")
    return (len(body) / n) if n else None


def scene_gap(size_now: Optional[int], fetched_bytes: int, prev_size: Optional[int],
              row_bytes: Optional[float]) -> Dict[str, Any]:
    """How many bytes of this file no fetch has ever asked for. Pure arithmetic, no I/O.

    `size_now` is the file's size from `/ls`, `fetched_bytes` is what the tail actually returned,
    `prev_size` is the size at the end of the last successful window. The first byte this fetch
    saw is `size_now - fetched_bytes`, so anything between `prev_size` and there was skipped.

    Four cases are named rather than folded into the number:

      size_unknown  `/ls` failed or did not list the file. `unfetched_bytes` is None, NOT 0 --
                    an absent measurement must never render as a clean one.
      first_run     no watermark yet. The 16.6 MB behind the first window is not loss; it is
                    history this drain was never running for, and reporting it as loss on every
                    fresh pool would train everyone to ignore the field.
      rolled        `size_now < prev_size`: scene.csv rolled to scene-prev.csv on a schema change
                    or a reflash. Read naively this is a huge negative; it is a reset.
      sub-row       0 < gap < one row. `size_now` was read BEFORE the body and the file grew
                    underneath, so the gap carries a few hundred bytes of noise in exactly this
                    direction. Clamped to 0 and the raw value kept as `raw_gap_bytes`.
    """
    out: Dict[str, Any] = {"size_now": size_now, "fetched_bytes": fetched_bytes,
                           "prev_size": prev_size, "mean_row_bytes": row_bytes,
                           "window_start": None, "raw_gap_bytes": None,
                           "unfetched_bytes": None, "unfetched_rows_est": None}
    if size_now is None:
        out["size_unknown"] = True
        return out
    window_start = max(0, size_now - fetched_bytes)
    out["window_start"] = window_start
    if prev_size is None:
        out["first_run"] = True
        out["unfetched_bytes"] = 0
        out["unfetched_rows_est"] = 0
        return out
    if size_now < prev_size:
        out["rolled"] = True
        out["unfetched_bytes"] = 0
        out["unfetched_rows_est"] = 0
        return out
    raw = window_start - int(prev_size)
    out["raw_gap_bytes"] = raw
    if raw <= 0:
        out["unfetched_bytes"] = 0
        out["unfetched_rows_est"] = 0
        return out
    if row_bytes and raw < row_bytes:
        # Not a row. The two round trips cannot resolve it and pretending otherwise would fire on
        # every healthy run -- 60% of normal scene traffic is overlap.
        out["sub_row"] = True
        out["unfetched_bytes"] = 0
        out["unfetched_rows_est"] = 0
        return out
    out["unfetched_bytes"] = raw
    out["unfetched_rows_est"] = int(round(raw / row_bytes)) if row_bytes else None
    return out


def status_audit(st: Dict[str, Any]) -> Dict[str, Any]:
    """The node's own production counters, so the ledger can be checked against the source.

    ⚠️THE KEY NAMES ARE THE FIRMWARE'S, verified against night_node.ino's /status writer and
    against both live nodes: `acq.drop_s` (not drop_seconds) and `acq.fs_clean_hz` (not fs_clean).
    A wrong name here reads as None and an audit full of Nones looks like a node with nothing to
    report rather than like a reader with the wrong spelling.

    ⚠️`scene.short_blocks` IS NOT THE COUNTER FOR THESE GAPS. It reads 0 on both live nodes while
    `acq.drop_s` reads 59 s and 84 s. It is carried because it is cheap and it bounds a different
    failure, not because it measures the one this module is about.
    """
    sc = st.get("scene") or {}
    acq = st.get("acq") or {}
    return {
        "uptime_s": st.get("uptime_s"),
        "scene": {k: sc.get(k) for k in ("rows", "written", "short_blocks", "write_fail")},
        "acq": {k: acq.get(k) for k in ("fs_clean_hz", "win_s", "drop_s", "drop_samples")},
    }


def boot_audit(pl: "P.Pool", node: str, st: Dict[str, Any], now: float,
               tolerance_s: float = 60.0,
               max_days: int = BOOT_AUDIT_MAX_DAYS) -> Dict[str, Any]:
    """ADVISORY second route to the same loss, from the pool's rows rather than from bytes.

    A row's boot is `utc_us/1e6 - uptime_s`, which is constant within a boot; the current boot is
    `now - status.uptime_s`. Counting the stored rows that land on it gives what the pool holds
    for this boot, against what the node says it wrote.

    ⚠️7.6% OF SCENE ROWS (3,175 of 41,507) CARRY `utc_us == 0` AND CANNOT BE CLUSTERED THIS WAY.
    They are not attributed to a boot by guesswork -- an attribution rule that got them wrong
    would report ~7.6% phantom loss. They are counted into their own bucket and the outstanding
    figure is reported as the RANGE they make it: `outstanding_max` assumes none of them belong to
    this boot, `outstanding_min` assumes all of them do. The truth is inside.

    ⚠️ADVISORY, AND SUBORDINATE TO THE BYTE WATERMARK. `scene_gap()` is the number to trust. This
    one depends on GPS state, on the pool's day partitioning and on the node's uptime clock; when
    the two disagree, the byte arithmetic is the one that measured the file.
    """
    out: Dict[str, Any] = {"advisory": True}
    sc = st.get("scene") or {}
    uptime = st.get("uptime_s")
    out["produced"] = sc.get("rows")
    out["written"] = sc.get("written")
    if out["produced"] is not None and out["written"] is not None:
        out["not_written"] = int(out["produced"]) - int(out["written"])
    if uptime in (None, ""):
        out["skipped"] = "status carried no uptime_s, so this boot has no epoch"
        return out
    boot_epoch = float(now) - float(uptime)
    out["boot_epoch_s"] = round(boot_epoch, 1)
    if float(uptime) > max_days * 86400.0:
        out["skipped"] = ("uptime %.0f s spans more than %d day partition(s); the walk is not "
                          "worth a drain's runtime" % (float(uptime), max_days))
        return out

    days = sorted(d for d in os.listdir(pl.scene_dir)
                  if os.path.isdir(os.path.join(pl.scene_dir, d))) \
        if os.path.isdir(pl.scene_dir) else []
    boot_day = P._day(boot_epoch)
    on_boot, unanchored = 0, 0
    for d in days:
        if d == "unanchored":
            for _r in pl.scene(node=node, day=d):
                unanchored += 1
            continue
        if d < boot_day:
            continue
        for r in pl.scene(node=node, day=d):
            u = r.get("utc_us") or 0
            ru = r.get("uptime_s")
            if not u or ru in (None, ""):
                continue
            try:
                epoch = float(u) / 1e6 - float(ru)
            except (TypeError, ValueError):
                continue
            if abs(epoch - boot_epoch) <= tolerance_s:
                on_boot += 1
    out["ingested_this_boot"] = on_boot
    out["unattributed_unanchored"] = unanchored
    if out["written"] is not None:
        w = int(out["written"])
        out["outstanding_max"] = max(0, w - on_boot)
        out["outstanding_min"] = max(0, w - on_boot - unanchored)
    return out


def archive(root: str, node: str, name: str, body: bytes, stamp: int) -> str:
    d = os.path.join(root, "raw", node)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%d-%s" % (stamp, name))
    with open(p, "wb") as fh:
        fh.write(body)
    return p


def _catch_up(ip: str, name: str, timeout: float, body: bytes, gap: Dict[str, Any],
              size_now: Optional[int], prev_size: Optional[int], max_catchup_bytes: int,
              out: Dict[str, Any]) -> Tuple[bytes, Dict[str, Any]]:
    """Refetch with a bigger `tail=` so the gap is CLOSED, not merely named.

    The node has no offset argument and no Range header -- `/sd` does `f.seek(size - tail)` and
    nothing else -- so a larger tail is the only reach-back that exists. The request is
    `fetched + gap + one row`, capped by `--max-catchup-bytes`; the extra row is there because
    the first byte of a window lands mid-row and is dropped by the reader.

    Whatever the cap refuses stays in `unfetched_bytes`: bounded and stated, never absorbed. The
    residual is recomputed against the SAME `size_now` the first window used, so both windows are
    measured on one basis and the residual carries the same sub-row uncertainty as the original.
    """
    wanted = int(len(body) + gap["unfetched_bytes"] + (gap.get("mean_row_bytes") or 0) + 1)
    tail = min(wanted, int(max_catchup_bytes))
    info = {"gap_before_bytes": gap["unfetched_bytes"], "wanted_tail": wanted,
            "requested_tail": tail, "capped": tail < wanted}
    if tail <= len(body):
        # The cap is already below what the first window took; a refetch would move backwards.
        info["skipped"] = "cap %d is not larger than the window already fetched" % tail
        gap["catchup"] = info
        return body, gap
    try:
        bigger = fetch_sd(ip, name, timeout, tail=tail)
    except Exception as e:
        info["error"] = repr(e)
        gap["catchup"] = info
        return body, gap
    if not bigger or len(bigger) <= len(body):
        info["error"] = "refetch returned %s, not more than the %d B already held" % (
            "nothing" if not bigger else "%d B" % len(bigger), len(body))
        gap["catchup"] = info
        return body, gap
    info["fetched_bytes"] = len(bigger)
    info["gained_bytes"] = len(bigger) - len(body)
    gap2 = scene_gap(size_now, len(bigger), prev_size, mean_row_bytes(bigger))
    gap2["catchup"] = info
    gap2["first_window"] = {k: gap.get(k) for k in
                            ("fetched_bytes", "window_start", "raw_gap_bytes",
                             "unfetched_bytes", "mean_row_bytes")}
    if gap2.get("unfetched_bytes"):
        out["errors"].append(
            "%s: %d B (~%s row(s)) of scene were never fetched and the catch-up cap of %d B could "
            "not reach them" % (name, gap2["unfetched_bytes"], gap2.get("unfetched_rows_est"),
                                max_catchup_bytes))
    return bigger, gap2


def drain_node(pl: "P.Pool", node: str, ip: str, timeout: float = DEFAULT_TIMEOUT_S,
               stamp: Optional[int] = None,
               max_catchup_bytes: int = DEFAULT_MAX_CATCHUP_BYTES) -> Dict[str, Any]:
    """Fetch, archive and ingest one node. Never raises for a node-side problem; reports it.

    The scene fetch also measures its own reach-back against the file on the card and refetches
    past whatever the last successful window did not cover -- see the module docstring and
    `scene_gap()`. `unfetched_bytes` on the result is what survived the catch-up cap: rows that
    are on the card, are not in the pool, and will roll off it. Zero is the normal answer and
    None means the measurement itself failed, which is not the same thing.
    """
    stamp = int(stamp if stamp is not None else time.time())
    out: Dict[str, Any] = {"node": node, "ip": ip, "at": stamp, "ok": False,
                           "files": [], "added": 0, "scene_added": 0, "errors": [],
                           "unfetched_bytes": 0, "unfetched_unknown": False}
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

    # ⚠️SIZES ARE READ BEFORE THE BODY. That makes `size_now` a lower bound on the file at fetch
    # time, so the computed gap is biased towards zero rather than towards a false alarm, and it
    # makes the watermark a size the fetched body is guaranteed to have reached.
    #
    # ⚠️A FAILED /ls IS NOT A FAILED RUN. It blinds the loss accounting, which is reported as
    # UNKNOWN and never as zero, but the fetch and the ingest are unaffected and firmware old
    # enough to lack the endpoint would otherwise be marked permanently failed -- and then STALE
    # -- while its data flowed normally. It is recorded in its own field so `ok` keeps meaning
    # "the data moved" and the blind spot is still visible.
    sizes: Optional[Dict[str, int]] = None
    try:
        sizes = _ls_sizes(ip, timeout)
    except Exception as e:
        out["ls_error"] = repr(e)
    out["ls_ok"] = sizes is not None

    wm = read_watermarks(pl.root)
    node_wm = dict(wm.get(node) or {})
    wm_dirty = False
    status_block = status_audit(st)

    for name in SCENE_FILES:
        # scene-prev.csv is the rolled predecessor and does not grow, so it is fetched whole --
        # once. The live file is tailed.
        tail = SCENE_TAIL_BYTES if name == "scene.csv" else None
        try:
            body = fetch_sd(ip, name, timeout, tail=tail)
        except Exception as e:
            out["errors"].append("%s: %r" % (name, e))
            continue
        if body is None:
            out["files"].append({"name": name, "absent": True})
            continue

        if not tail:
            # A whole-file fetch reaches back to byte 0 by construction. Stated, not assumed away.
            gap: Dict[str, Any] = {"whole_file": True, "unfetched_bytes": 0,
                                   "fetched_bytes": len(body)}
        else:
            size_now = sizes.get(name) if sizes is not None else None
            prev = (node_wm.get(name) or {}).get("size")
            gap = scene_gap(size_now, len(body), prev, mean_row_bytes(body))
            if gap.get("unfetched_bytes"):
                body, gap = _catch_up(ip, name, timeout, body, gap, size_now, prev,
                                      max_catchup_bytes, out)

        path = archive(pl.root, node, name, body, stamp)
        audit = {"file": name, "gap": gap, "status": status_block}
        if tail:
            try:
                audit["boot_audit"] = boot_audit(pl, node, st, stamp)
            except Exception as e:
                audit["boot_audit"] = {"advisory": True, "error": repr(e)}
        try:
            entry = pl.ingest_scene(path, default_node=node, origin="%s:/%s" % (node, name),
                                   partial=bool(tail), fetch_audit=audit)
        except Exception as e:
            # ⚠️THE WATERMARK DOES NOT MOVE. The rows were fetched and archived but not stored, so
            # the next run must still see this gap rather than start from where this one reached.
            out["errors"].append("%s: ingest: %r" % (name, e))
            continue
        if tail:
            if gap.get("unfetched_bytes") is None:
                out["unfetched_unknown"] = True
            else:
                out["unfetched_bytes"] += int(gap["unfetched_bytes"])
            if gap.get("size_now") is not None:
                node_wm[name] = {"size": int(gap["size_now"]), "at": stamp,
                                 "mean_row_bytes": gap.get("mean_row_bytes")}
                wm_dirty = True
        out["scene_added"] = out.get("scene_added", 0) + entry["added"]
        out["files"].append({"name": name, "bytes": len(body), "ingested": True,
                             "scene": True, "archived": path, "gap": gap,
                             **{k: entry[k] for k in
                                ("generation", "rows", "added", "duplicate", "skipped",
                                 "skip_reasons", "partial_first_line")}})

    if wm_dirty:
        wm[node] = node_wm
        write_watermarks(pl.root, wm)

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
            todo += sorted(glob.glob(os.path.join(p, "**", "*scene*.csv"), recursive=True))
            todo += sorted(glob.glob(os.path.join(p, "**", "sketches-*.jsonl"), recursive=True))
        else:
            todo.append(p)
    for p in todo:
        base = os.path.basename(p)
        try:
            if base.startswith("sketches-") and base.endswith(".jsonl"):
                out.append(pl.ingest_mqtt_jsonl(p))
            elif "scene" in base:
                node = default_node
                for known in ("nyquist", "mach", "puc"):
                    if known in p:
                        node = known
                        break
                out.append(pl.ingest_scene(p, default_node=node))
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
        # ⚠️RECORDED ON EVERY ATTEMPT, NOT ONLY ON SUCCESS. A capped catch-up is exactly the run
        # that both loses rows AND reports an error, so filing the loss under `if r["ok"]` would
        # hide it in the one case it exists for. `None` means the measurement failed and is kept
        # distinct from 0, which means it succeeded and found nothing missing.
        s["last_unfetched_bytes"] = (None if r.get("unfetched_unknown")
                                     else r.get("unfetched_bytes"))
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
          now: Optional[float] = None,
          max_unfetched_bytes: int = DEFAULT_MAX_UNFETCHED_BYTES) -> Tuple[int, List[str]]:
    """(exit code, lines). Non-zero on a stale sensor OR on a sensor that skipped scene bytes.

    ⚠️STALENESS WAS ONLY HALF OF IT. A drain whose HTTP succeeded, whose ingest succeeded and
    whose window silently started three hours after the last one ended looks perfect here: it is
    fresh, it added rows, it exits 0. That is the case this check could not see and the case that
    has already cost 3.11 h of scene. `last_unfetched_bytes` is per sensor and is already
    thresholded at one row by `scene_gap()`, so the default of 0 means "any whole row skipped".

    A missing `last_unfetched_bytes` is a heartbeat written before this existed and reads as
    `n/a`, not as 0 -- all 54 ledger entries and every heartbeat predating this change lack it.
    `None` means the run's `/ls` failed: reported as UNKNOWN, and deliberately not fatal, because
    an unreachable `/ls` beside a successful fetch is a flaky endpoint rather than evidence of
    loss, and a check that cries wolf gets switched off. It is never printed as a clean zero.
    """
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
        if "last_unfetched_bytes" not in s:
            gap_note = "  unfetched n/a"
        elif s["last_unfetched_bytes"] is None:
            gap_note = "  unfetched UNKNOWN (/ls failed)"
        elif int(s["last_unfetched_bytes"]) > max_unfetched_bytes:
            gap_note = "  UNFETCHED %d B of scene never asked for" % s["last_unfetched_bytes"]
            bad += 1
        else:
            gap_note = ""
        last = s.get("last_success_s")
        if last is None:
            lines.append("%-10s NEVER succeeded (last error: %s)%s"
                         % (name, s.get("last_error"), gap_note))
            bad += 1
            continue
        age = now - float(last)
        state = "STALE" if age > max_stale_s else "ok"
        bad += state == "STALE"
        lines.append("%-10s %-5s last success %.0f s ago%s%s"
                     % (name, state, age, gap_note, "" if not s.get("last_error")
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
    ap.add_argument("--max-catchup-bytes", type=int, default=DEFAULT_MAX_CATCHUP_BYTES,
                    help="how far back one catch-up refetch may reach when the scene tail did "
                         "not cover the gap since the last successful run. Whatever this refuses "
                         "is reported as still-unfetched, not absorbed")
    ap.add_argument("--max-unfetched-bytes", type=int, default=DEFAULT_MAX_UNFETCHED_BYTES,
                    help="--check fails above this many scene bytes no fetch has asked for. "
                         "Sub-row gaps are already clamped to 0, so the default of 0 means "
                         "'any whole row skipped'")
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
        code, lines = check(root, a.max_stale_s, max_unfetched_bytes=a.max_unfetched_bytes)
        print("\n".join(lines))
        return code

    pl = P.Pool(root)
    if a.stats:
        print(json.dumps({"sketches": pl.stats(), "scene": pl.scene_stats()},
                         indent=2, sort_keys=True))
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

    results = [drain_node(pl, n, ip, a.timeout, max_catchup_bytes=a.max_catchup_bytes)
               for n, ip in nodes]
    phone = drain_phone_corpus(pl, os.path.expanduser(a.phone_corpus), a.phone_days) \
        if a.phone_corpus else None
    write_heartbeat(root, results, phone)

    report = {"results": results, "phone": phone, "stats": pl.stats(),
              "scene_stats": pl.scene_stats()}
    if a.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for r in results:
            print("%-9s %-4s +%d sketch, +%d scene%s" % (
                r["node"], "ok" if r["ok"] else "FAIL", r["added"], r.get("scene_added", 0),
                "" if r["ok"] else "  " + "; ".join(r["errors"])))
            if r.get("ls_error"):
                print("    /ls failed (%s) -- reach-back UNMEASURED this run, not clean"
                      % r["ls_error"])
            for f in r["files"]:
                if f.get("absent"):
                    print("    %-14s absent" % f["name"])
                elif f.get("ingested"):
                    print("    %-15s %s %6d row(s) -> +%-6d new, %6d dup, %d skipped %s%s"
                          % (f["name"], f["generation"], f["rows"], f["added"], f["duplicate"],
                             f["skipped"], f["skip_reasons"] or "",
                             "  [tail]" if f.get("partial_first_line") else ""))
                    g = f.get("gap") or {}
                    if g.get("unfetched_bytes") is None and not g.get("whole_file"):
                        print("      reach-back UNKNOWN: /ls gave no size for this file")
                    elif g.get("unfetched_bytes"):
                        print("      ⚠️%d B (~%s row(s)) never fetched; caught up %s"
                              % (g["unfetched_bytes"], g.get("unfetched_rows_est"),
                                 (g.get("catchup") or {}).get("gained_bytes", 0)))
                    elif g.get("catchup"):
                        print("      caught up %s B the tail did not cover"
                              % (g["catchup"].get("gained_bytes"),))
                else:
                    print("    %-14s %d B archived" % (f["name"], f["bytes"]))
        if phone is not None:
            print("phones    %-4s +%d record(s)%s"
                  % ("ok" if phone.get("ok") else "FAIL", phone["added"],
                     "" if phone.get("ok") else "  " + "; ".join(phone["errors"])))
        s = report["stats"]
        print("\npool %s\n  sketches %d  %s  (%d anchored / %d not)"
              % (root, s["records"], s["by_source"], s["anchored"], s["unanchored"]))
        sc = report["scene_stats"]
        print("  scene    %d rows  %s  geom %s  %.1f MB on disk (%s B/row)"
              % (sc["rows"], sc["by_node"], sc["geometry"],
                 sc["bytes_on_disk"] / 1e6, sc["bytes_per_row"]))

    # A run where every sensor failed is a failure. A run where one of several failed is not --
    # the pool still gained the others, and a timer that gives up on all of them because one node
    # is off the wifi is how the remaining nodes' data goes missing too.
    if results and all(not r["ok"] for r in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
