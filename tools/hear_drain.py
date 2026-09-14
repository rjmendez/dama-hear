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

⚠️CLIPS ARE COLLECTED, AND UNTIL NOW THEY WERE NOT. Measured 2026-09-09: 625 4.0 s WAVs written
across the fleet, ~478 already destroyed by the node's own 49-clip eviction, and NOT ONE HAD EVER
LEFT A NODE. The names come from the `clip` column of `dets.csv` -- `fetch_sd` cannot fetch the
files themselves, because its header sniff reads a `RIFF` body as an ABSENT file -- and the fetch
is strictly sequential, capped per run, and indexed by name in `clips/index.jsonl` so a clip is
never asked for twice and a destroyed one is never re-probed. Every refusal is counted by reason.
See `drain_clips` and docs/acoustic-stack.md section 0.3, which states the ordering constraint:
draining must precede any increase of the node's CLIP_BUDGET_B.

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

⚠️THE SECOND SILENT FAILURE IS THE TAIL OUTGROWING ITSELF. The scene fetch takes the last
`SCENE_TAIL_BYTES` of a file that grows forever, so it reaches back a fixed number of BYTES, not a
fixed number of hours. When the comment below was written that was assumed to be ~2.3 h and to
overlap every previous run. Derived from this pool's own ledger (54 entries, 10 scene tails per
node): 235.0 B per row on nyquist and 232.1 B on mach, at one row per 1.024 s, so the 2 MB window
reaches back 8,510 / 8,620 rows = 2.42 / 2.45 h. That is a measurement of the WINDOW, not of the
loss, and it is shrinking as the row size grows.

⚠️THE LOSS ITSELF WAS NEVER MEASURED, WHICH IS WHY THE DRAIN NOW MEASURES IT. 60.05% of every
scene row this pool has read was already a duplicate (62,410 of 103,927 in the same ledger), so a
window that stopped overlapping would look identical in every count the ledger keeps: the drain
would fetch, add rows, and exit 0 while a stretch of the file aged past the tail untouched. No
figure is asserted here for how much has already been lost that way. The exported corpus at
`~/hear-corpus-export/corpus` cannot supply one -- its two dominant holes are ~58,000 s each on
both nodes, which is the drain not running at all rather than the tail falling short, and the
sub-hour inter-row gaps sum to 1,858 s across both nodes with no way to attribute any of it to
reach-back. That is the point: the quantity was unmeasurable, and the change below is what makes
it measurable from now on.

So the drain MEASURES the reach-back instead of assuming it, and does not try to close it:

  * `/ls` gives the file's size. `window_start = size_now - len(body)` is the first byte this
    fetch actually saw, and `<pool>/state/scene_fetch.json` remembers where the last successful
    fetch ended. `unfetched_bytes = window_start - prev_size` is arithmetic over the FILE, not
    over the window, and it is immune to the 7.6% of scene rows that carry no PPS anchor.
  * the watermark advances only to the size measured BEFORE the fetch, and only after a
    successful ingest. Both directions of that are deliberate: the file grows during the fetch,
    so the pre-fetch size is the one the body is guaranteed to have reached, and a run that
    failed must not move the mark past rows it never stored.
  * `None` is a first-class answer. A run that could not measure says so -- it never says zero.

⚠️A CATCH-UP REFETCH IS DELIBERATELY ABSENT, AND THIS IS THE SECOND TIME THAT HAS BEEN DECIDED.
The node has no offset argument and no Range header, so the only reach-back control is a bigger
`tail=` -- and `/sd` seeks against the size AT REFETCH TIME (hear_node.ino:2899,
`size_t remain = f.size(); if (tail > 0 && remain > (size_t)tail) f.seek(remain - tail)`). The
file grows underneath for the whole of the first fetch (2 MB at the measured 40-135 KB/s is
15-50 s, ~3.5-12 KB at ~235 B/s), so a tail sized against the pre-fetch `size_now` lands FORWARD
of where the gap is. Measured against a fixture whose file grows during the fetch: the refetch
left 40 of 520 rows on the card and the drain reported 0 B unfetched, because the residual was
recomputed against the same stale `size_now`. It did not merely fail to close the gap -- it
erased the measurement. Reaching back is a second mechanism with its own failure modes; what this
module owes its caller is an honest number.

⚠️THE GAP IS BIASED TOWARDS SILENCE, ON PURPOSE. `size_now` is read before the body, and the file
grows ~235 B/s underneath, so `window_start` is understated by roughly the round trip -- a few
hundred bytes. A sub-row gap therefore reads as zero, thresholded against the fetched body's OWN
mean row size rather than a constant. That direction is chosen because 60% of normal traffic is
overlap: a bias the other way would fire on every healthy run and be turned off within a week.
Both raw numbers (`size_now`, `fetched_bytes`) are on the ledger so the arithmetic stays auditable.
"""
from __future__ import annotations

import argparse
import binascii
import glob
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from hear import clips as CL                                        # noqa: E402
from hear import detsfile as DF                                     # noqa: E402
from hear import pool as P                                          # noqa: E402
from hear import identity as ID                                     # noqa: E402

# The card files worth pulling every run. `dets.csv` and its rolled predecessor carry the
# sketches; `health.csv` carries the PPS/fs context a later reader needs to judge them and is
# archived but NOT ingested -- the pool holds sketches, and mixing a second row shape into it
# would make its counts mean two things.
DETS_FILES = ("dets.csv", "dets-prev.csv")
# ⚠️THE SCENE IS THE CORPUS. dets.csv only exists where the impulse gate fired, so a pool built
# from it alone can hold nothing but impulsive events. scene.csv carries a row every ~1.024 s
# regardless -- the ambient world this project is also about. See hear/scenefile.py.
SCENE_FILES = ("scene.csv", "scene-prev.csv")

# ⚠️THE SCENE FILE NAMES ARE FIRMWARE'S TO CHANGE AND IT CHANGED THEM. Since fw 7f84d29 (commit
# 4dbfe26, "daily files with the oldest rolled off") the nodes write scene-YYYYMMDD.csv and leave
# the legacy scene.csv frozen. A drain that asks for two hardcoded names then fetches a file that
# never grows again and ingests nothing -- while reporting `ok`, because zero new rows out of a
# successful fetch is indistinguishable from a quiet node. Measured 2026-09-09 05:20 UTC: the lane
# had been dead 3.27 h, nyquist's scene.csv frozen at 20,751,993 B while scene-20260909.csv grew,
# and rankine served no scene.csv at all.
#
# So the names are DISCOVERED from /ls rather than declared. SCENE_FILES stays as the fallback for
# firmware too old to have the endpoint, and as the answer when /ls fails -- a blind run must still
# fetch something rather than nothing.
SCENE_GLOB = "scene"
SCENE_DATED_RE = re.compile(r"^scene-(\d{8})(-prev)?\.csv$")


def _scene_key(name: str) -> Tuple[int, int, str]:
    """Oldest -> newest, with a rolled `-prev` partition before its same-day live file."""
    m = SCENE_DATED_RE.match(name)
    if not m:
        return (-1, 0 if name.endswith("-prev.csv") else 1, name)
    return (int(m.group(1)), 0 if m.group(2) else 1, name)


def scene_names(sizes: Optional[Dict[str, int]]) -> Tuple[Tuple[str, ...], Optional[str]]:
    """(names to fetch, which one is live). Live is tailed; the rest are rolled and fetched whole.

    ⚠️DATED PARTITIONS WIN AS A FAMILY. Since fw 7f84d29 the live file is scene-YYYYMMDD.csv and
    the frozen legacy scene.csv is a compatibility leftover, so when one dated file exists the
    drain fetches ONLY the dated family and ignores legacy names entirely. Legacy scene.csv /
    scene-prev.csv stay as the blind fallback when /ls is unavailable and as the old-firmware path
    when no dated file exists at all.

    ⚠️scene-00000000.csv is the PRE-LOCK file -- rows the node wrote before it knew the date -- and
    it is a real file with real rows, not a placeholder, so it is fetched like any other rolled
    file. hear/pool.py keeps its rows and marks them unanchored.

    Rolled daily scene partitions (such as scene-YYYYMMDD-prev.csv and scene-prev.csv) created
    when headers change or dates rollover are also discovered and fetched whole.
    """
    if sizes is None:
        return SCENE_FILES, "scene.csv"
    dated = sorted((n for n in sizes if SCENE_DATED_RE.match(n)), key=_scene_key)
    if dated:
        current = [n for n in dated if not n.endswith("-prev.csv")]
        live = current[-1] if current else dated[-1]
        return tuple(dated), live
    legacy = [n for n in SCENE_FILES if n in sizes]
    if legacy:
        return SCENE_FILES, "scene.csv"
    return (), None
CONTEXT_FILES = ("health.csv",)

# ⚠️CONTEXT FILES ARE TAILED FROM A WATERMARK, NOT FETCHED WHOLE. health.csv was 2.4-2.6 MB on the
# card nodes on 2026-09-13 and was pulled whole every 15 minutes, holding the node's single-client
# HTTP loop for most of a minute per run (#99). A run now asks for the bytes past the last mark plus
# CONTEXT_OVERLAP_BYTES, which covers more than one row (health rows measured <= 665 B), so the first
# row kept is always complete. With no size from /ls the mark cannot move, so a bounded tail is
# archived instead and the next listed run resumes from the old mark.
CONTEXT_OVERLAP_BYTES = 4096
CONTEXT_BLIND_TAIL_BYTES = 64_000

# ⚠️SCENE IS FETCHED BY TAIL, NOT WHOLE. It grows without bound (16,771,742 B on nyquist and
# 16,435,916 B on mach in tests/fixtures/ls_*.txt, captured 2026-09-08; 2-7 minutes to pull at the
# node's measured 40-135 KB/s) and re-fetching all of it every 15 minutes would spend the whole
# interval on rows already stored. The ingest is content-addressed, so the overlap costs nothing.
#
# ⚠️THIS WINDOW IS A BYTE COUNT AND ITS REACH-BACK IN HOURS SHRINKS AS THE ROW RATE OR ROW SIZE
# RISES. It is NOT self-healing by construction; it is self-healing only while it is still wider
# than the interval between two successful runs, and that is now a measurement, not an assumption:
# 2 MB / 235.0 B per row / (1024 ms per row) = 2.42 h on nyquist, 2.45 h on mach, both derived
# from the pool's ledger. See `scene_gap()` -- the drain measures its own reach-back every run and
# REPORTS what it did not cover. It does not refetch; see the module docstring for why not.
SCENE_TAIL_BYTES = 2_000_000

# `<pool>/state/scene_fetch.json`: per node, per file, the file SIZE at the end of the last
# successful window. Pool already creates `state/`.
SCENE_STATE_FILE = "scene_fetch.json"

# `<pool>/state/node_positions.json`: each node's own GPS mean as /status last reported it, read by
# hear-tdoa to position a node with no survey entry (hear/backend/survey.py: augment_from_node_gps).
NODE_POSITIONS_FILE = "node_positions.json"
# hear_node.ino DETS_HTTP_MAX: /detections serves only the newest this many rows.
LIVE_RING_HTTP_MAX = 128

# --check fails above this many unfetched bytes. Zero is the right default because `scene_gap()`
# has already clamped anything smaller than one row to zero: what survives to the heartbeat is a
# whole row of scene the pool will never see, and there is no acceptable number of those.
DEFAULT_MAX_UNFETCHED_BYTES = 0

# ⚠️THE CHECK RUNS ONE FIFTH AS OFTEN AS THE DRAIN, so a per-run field it reads is 4 runs out of
# date the moment it is written. deploy/k8s/hear-drain.yaml schedules the drain `*/15 * * * *` and
# the check `17 * * * *`: 4 drains land between two checks and the 5th, 6th and 7th most recent
# measurements are already gone by the time anything looks. So the heartbeat keeps a RING of
# per-run measurements and `check()` sums the ones inside this window rather than reading the last
# one. 7200 s is two check periods, so one missed check run does not lose a report -- and it is
# the same tolerance `DEFAULT_MAX_STALE_S` already uses for a silence.
DEFAULT_UNFETCHED_WINDOW_S = 7200.0
# How many per-run measurements the ring keeps. At the CronJob's 15 min that is 16 h of history,
# comfortably longer than the window above even if the check stops running for most of a day.
UNFETCHED_RING = 64

# The advisory boot cross-check walks stored scene partitions, so its cost grows with how long the
# node has been up. Past this it is SKIPPED and says so -- the byte watermark is the number that
# matters and a cross-check must not turn a 15-minute drain into an hour of gzip.
BOOT_AUDIT_MAX_DAYS = 3

DEFAULT_TIMEOUT_S = 30.0
# Slower Wi-Fi transfer rate floor to size adaptive timeouts for large whole-file fetches
# (40-135 KB/s measured from ESP32 nodes).
MIN_TRANSFER_RATE_BPS = 40_000.0
# Sensible default timeout for whole rolled files when size is unknown
DEFAULT_ROLLED_TIMEOUT_S = 300.0


def rolled_file_timeout(size_bytes: Optional[int],
                        base_timeout: float = DEFAULT_TIMEOUT_S) -> float:
    """Adaptive timeout for fetching a full rolled file off the node over Wi-Fi.

    A 20 MB file at the measured 40-135 KB/s transfer rate takes 150-500 s, so a fixed 30 s
    timeout aborts large rolled file transfers.
    """
    if size_bytes is not None and size_bytes > 0:
        needed = (float(size_bytes) / MIN_TRANSFER_RATE_BPS) + base_timeout
        return max(base_timeout, needed)
    return max(base_timeout, DEFAULT_ROLLED_TIMEOUT_S)


# A node reporting detections at the measured field rate goes quiet for hours in daylight, so
# staleness is measured on the FETCH, not on new rows. 2 h is comfortably longer than any timer
# interval and short enough to catch a node that fell off the wifi before its buffer rolls over.
DEFAULT_MAX_STALE_S = 7200.0
# Ring wall span above this means the node heard more wall time than the ring should cover.
# Section 0.2 of docs/acoustic-stack.md uses 1.02 as the line between the healthy rankine
# control (~1.002) and the lossy nyquist/mach nodes (~1.09-1.12).
DEFAULT_MAX_RING_WALL_SPAN = 1.02
# A known-healthy control must land within this of 1.000 or the denominator is wrong.
RING_WALL_SPAN_SELF_TEST_S = 0.003

# ---------------------------------------------------------------- clip lane budget
#
# The card holds a rolling window of clips: 6291456 // 480044 = 13. A backlog is therefore bounded
# at 13 per node however long the drain was down -- the difference from scene.csv -- and a clip
# that rolls off before it is fetched is the design working, not a loss.
CLIP_MAX_PER_NODE_DEFAULT = 13
# 3 x 13 clips is 18.7 MB: 112 s at the measured 168 KB/s, 470 s at the 40 KB/s contended floor.
# 307 + 470 = 777 s of a 900 s interval is too tight, so the per-node deadline binds instead of
# the schedule; at 40 KB/s it buys about 10 clips. `clips_cap_hit` reaches the heartbeat ring.
CLIP_DEADLINE_S_DEFAULT = 120.0
# 142 MB/day fleet-wide; 2 GiB is ~14 days of rolling audio on a PVC shared with scene/ and raw/.
CLIP_STORE_MAX_BYTES_DEFAULT = 2 * 1024 ** 3
# A floor the clip lane will not eat into, whatever the audio cap says. The PVC also carries the
# index, the ledger and the scene partitions, and none of those are prunable.
CLIP_FREE_RESERVE_B = 512 * 1024 ** 2

# --check fails above this many clips deferred by the cap over the window. 0 is the right default:
# deferral is a DESIGN INVARIANT (the cap exists so the drain cannot overrun its schedule), so any
# deferral at all means the cap is binding and the schedule or the budget needs looking at.
DEFAULT_MAX_CLIPS_DEFERRED = 0
# ⚠️-1 MEANS REPORT, NEVER FAIL, AND IT IS DELIBERATE. 666 clips are already destroyed and still
# named in current dets.csv files, so a gate armed today fires on the backlog and gets muted on
# day one -- which is worse than no gate. Set it from 7 days of measured distribution.
DEFAULT_MAX_CLIPS_LOST = -1


def _get(url: str, timeout: float = DEFAULT_TIMEOUT_S) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def fetch_status(ip: str, timeout: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    return json.loads(_get("http://%s/status" % ip, timeout).decode("utf-8", "replace"))


def fetch_audio_status(ip: str, timeout: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    return json.loads(_get("http://%s/audio" % ip, timeout).decode("utf-8", "replace"))


def fetch_detections(ip: str, timeout: float = DEFAULT_TIMEOUT_S) -> bytes:
    return _get("http://%s/detections" % ip, timeout)


def parse_live_ring(body: bytes) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """(rows, truncated body length or None): the /detections array, or its whole-row prefix.

    ⚠️A TRUNCATED BODY IS THE NODE OUT OF HEAP, NOT A BAD RUN. h_dets() builds the whole array in
    one String, and on gold (psram 0, heap_min 5684 B, 2026-09-13) it stopped growing and the node
    sent a 200 cut mid-row at 37,199 B, twice in a row. The rows are oldest first, so the whole
    ones before the cut are good and the newest simply arrive on a later run.
    """
    text = body.decode("utf-8", "replace")
    try:
        obj = json.loads(text)
    except ValueError:
        end = text.rfind("}")
        if end < 0 or not text.lstrip().startswith("["):
            raise
        obj = json.loads(text[:end + 1] + "]")
        truncated: Optional[int] = len(body)
    else:
        truncated = None
    if not isinstance(obj, list):
        raise ValueError("/detections body is %s, not a list" % type(obj).__name__)
    return obj, truncated


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


class LsListing(Dict[str, int]):
    """`{name: bytes}` plus the one thing a plain dict cannot carry: whether the node stopped
    early. It IS a dict, so every existing caller and `scene_names()` are unaffected.

    ⚠️`truncated_at` IS THE DIFFERENCE BETWEEN A SHORT CARD AND A SHORT ANSWER. The /ls handler
    caps at LS_MAX_ENTRIES and emits `! truncated at <n> entries`; that line starts with `!`, so
    the `- ` parse below drops it and a PARTIAL listing would otherwise be byte-indistinguishable
    from a complete one. A census that silently under-counts is the exact failure this module
    exists to prevent.
    """

    truncated_at: Optional[int] = None


_LS_TRUNCATED = re.compile(r"^!\s*truncated at (\d+) entries")


def _ls_parse(body: str) -> LsListing:
    """The `/ls` body -> the listing. Split out from the fetch so a captured fixture can be fed
    through the very parser the drain runs, with no node and no socket."""
    out = LsListing()
    for line in body.splitlines():
        line = line.strip()
        m = _LS_TRUNCATED.match(line)
        if m:
            out.truncated_at = int(m.group(1))
            continue
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


def _ls_sizes(ip: str, timeout: float = DEFAULT_TIMEOUT_S,
              dir: Optional[str] = None) -> LsListing:
    """`GET /ls` -> {filename: bytes}. Raises on a transport failure; never returns a guess.

    The node prints one line per card entry, `- <name>  <N> B` for a file and `d ` for a
    directory (hear_node.ino, the /ls handler). Only files are returned, and the name is
    normalised without its leading slash because the core has served it both ways.

    ⚠️AN UNPARSEABLE LINE IS DROPPED, WHICH MAKES ITS FILE ABSENT FROM THE RESULT, WHICH THE
    CALLER MUST READ AS `size unknown` AND NOT AS `no loss`. That distinction is the whole
    contract here: a measurement that failed and a measurement that came back clean are the two
    things this module exists to keep apart. `LsListing.truncated_at` carries the node's own
    admission of the same thing.

    ⚠️`dir` REACHES FIRMWARE THE FLEET IS NOT RUNNING. The flashed nodes hardcode SD.open("/")
    and ignore every argument, so a `dir` they do not understand comes back as the ROOT listing
    -- names with no `<dir>/` prefix. That is self-identifying rather than silent, because the
    handler that DOES understand `dir` qualifies every name with it, and it is why
    `ls_candidates()` can be run against either firmware and be right about both.
    """
    url = "http://%s/ls" % ip
    if dir is not None:
        url += "?dir=" + urllib.parse.quote(dir, safe="/")
    return _ls_parse(_get(url, timeout).decode("utf-8", "replace"))


# ⚠️A RESET IS NOT A MISSING ENDPOINT. `/ls` failing sends `scene_names()` to its blind fallback,
# which asks for the LEGACY names -- and on current firmware that means re-tailing a frozen
# scene.csv and ingesting nothing while the dated file the node is actually writing goes
# uncollected for that run. The fallback is right for firmware too old to have the endpoint and
# wrong for a node that simply dropped one connection, and before this the drain could not tell
# the two apart.
#
# MEASURED 2026-09-09 on nyquist: `/ls` answers in 35-118 ms when the node is idle, degrades to
# 7.3 s while a large `/sd` transfer is in flight, and is REFUSED outright when a second client
# is mid-request -- the ESP32 core serves one client at a time and resets the rest rather than
# queueing them. nyquist was the only node with a second poller (a watch.py on the workstation,
# every 30 s) and the only node whose `/ls` failed: 2 of 4 runs, each costing it its scene lane.
#
# So a transport failure is RETRIED. An HTTP status is not: a 404 is the node answering, and
# answering "no such endpoint" is exactly the old firmware this fallback exists for.
LS_RETRIES = 3
LS_RETRY_BACKOFF_S = 1.5


def ls_sizes_retrying(ip: str, timeout: float = DEFAULT_TIMEOUT_S, retries: int = LS_RETRIES,
                      backoff: float = LS_RETRY_BACKOFF_S,
                      sleep=None, dir: Optional[str] = None
                      ) -> Tuple[Optional[LsListing], Optional[str], int]:
    """(sizes, error repr, attempts). None sizes means every attempt failed.

    ⚠️`sleep` RESOLVES AT CALL TIME, NOT AT DEF TIME. A `sleep=time.sleep` default binds the
    function object when the module is imported, so patching `time.sleep` afterwards does nothing
    -- which made two of this module's own tests sleep for real while appearing to be patched.

    ⚠️THE LsListing IS RETURNED WHOLE, NOT REBUILT AS A PLAIN DICT. `truncated_at` rides on the
    object; a retry that copied it into a dict would drop the node's own admission that its
    census was capped, turning a partial listing back into one indistinguishable from a
    complete one -- the exact distinction _ls_sizes exists to preserve.

    ⚠️THE ATTEMPT COUNT IS RETURNED SO THE RUN RECORD CAN SAY A RETRY HAPPENED. A retry that
    silently succeeds turns a node with a real contention problem into a node that looks healthy,
    and the contention is worth seeing before it becomes a failure.
    """
    if sleep is None:
        sleep = time.sleep
    last = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            # `dir` is only passed when asked for, so a caller or a stub with the original
            # two-argument signature keeps working unchanged.
            got = _ls_sizes(ip, timeout) if dir is None else _ls_sizes(ip, timeout, dir=dir)
            return got, None, attempt
        except urllib.error.HTTPError as e:
            # The node answered. 404 is old firmware without the endpoint; do not hammer it.
            return None, repr(e), attempt
        except (TypeError, AttributeError, NameError):
            # ⚠️A PROGRAMMING ERROR IS NOT A TRANSPORT FAULT AND MUST NOT BE RETRIED. A bare
            # `except Exception` here swallowed a signature mismatch as a dropped connection,
            # retried it three times with real backoff, and then reported the node unreachable --
            # so a bug in this process read exactly like a node refusing to answer. Raise it.
            raise
        except Exception as e:
            last = repr(e)
            if attempt < retries:
                sleep(backoff * attempt)
    return None, last, max(1, retries)


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
      first_run     no watermark yet. The ~16.8 MB behind the first window is not loss; it is
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

    ⚠️THE KEY NAMES ARE THE FIRMWARE'S, verified against hear_node.ino's /status writer and
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
        # fs_used_hz and fs_step_ppm travel with fs_clean_hz on purpose: the estimate alone does
        # not say whether it was fit to be used. Its step is 16000/fs_win_s ppm, and until the
        # window reaches FS_TIMEBASE_MIN_WIN_S the node dates its samples with the nominal rate
        # instead -- fs_used_hz is which of the two actually converted this drain's timestamps.
        "acq": {k: acq.get(k) for k in ("fs_clean_hz", "win_s", "fs_win_s", "fs_step_ppm",
                                        "fs_used_hz", "drop_s", "drop_samples", "over_s")},
    }


def ring_wall_span_ratio(span_us: Any, samples: Any, fs_hz: Any) -> Optional[float]:
    """Observed wall span / expected span, or None if the inputs do not describe one.

    Both `raw.cap_samples` from `/status` and `addressable_samples` from bare `/audio` are
    counted in the node's FS_NOMINAL-domain sample counter. The ratio therefore divides each
    span by ITS OWN sample count at the SAME nominal rate; cross-pairing `/audio`'s span with
    `cap_samples` is the documented trap in docs/acoustic-stack.md section 0.2.
    """
    try:
        span_us = float(span_us)
        samples = float(samples)
        fs_hz = float(fs_hz)
    except (TypeError, ValueError):
        return None
    if span_us < 0 or samples <= 0 or fs_hz <= 0:
        return None
    return (span_us / 1_000_000.0) / (samples / fs_hz)


def ring_wall_span_measurement(status: Dict[str, Any], audio: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the raw- and audio-reported ring wall span ratios from one poll.

    `/status` reports the WHOLE raw ring (`raw.cap_samples`); bare `/audio` reports the SAFE
    addressable window (`addressable_samples`). Each ratio carries its own denominator so tests
    can pin the documented wrong-denominator regression.
    """
    raw = status.get("raw") or {}
    i2s = status.get("i2s") or {}
    fs_hz = i2s.get("nominal_hz")
    out: Dict[str, Any] = {"fs_hz": fs_hz, "raw": {}, "audio": {}}
    for key, block, sample_key in (("raw", raw, "cap_samples"),
                                   ("audio", audio or {}, "addressable_samples")):
        lane: Dict[str, Any] = {"sample_key": sample_key, "samples": block.get(sample_key),
                                "from_utc_us": block.get("from_utc_us"),
                                "to_utc_us": block.get("to_utc_us"),
                                "span_us": None, "expected_s": None,
                                "ratio": None, "reason": None}
        if fs_hz in (None, ""):
            lane["reason"] = "status carried no i2s.nominal_hz"
            out[key] = lane
            continue
        if lane["from_utc_us"] in (None, "") or lane["to_utc_us"] in (None, ""):
            lane["reason"] = "%s carried no UTC span" % key
            out[key] = lane
            continue
        try:
            lane["span_us"] = float(lane["to_utc_us"]) - float(lane["from_utc_us"])
        except (TypeError, ValueError):
            lane["reason"] = "%s carried a non-numeric UTC span" % key
            out[key] = lane
            continue
        ratio = ring_wall_span_ratio(lane["span_us"], lane["samples"], fs_hz)
        if ratio is None:
            lane["reason"] = "%s carried no usable %s at fs %.6g" % (
                key, sample_key, float(fs_hz))
            out[key] = lane
            continue
        lane["expected_s"] = float(lane["samples"]) / float(fs_hz)
        lane["ratio"] = ratio
        out[key] = lane
    return out


def ring_wall_span_self_test(measurement: Dict[str, Any], which: str = "audio",
                             expect: float = 1.0,
                             tolerance: float = RING_WALL_SPAN_SELF_TEST_S) -> Dict[str, Any]:
    """Does a known-healthy control land close enough to 1.000 to prove the denominator?"""
    lane = (measurement.get(which) or {}) if isinstance(measurement, dict) else {}
    ratio = lane.get("ratio")
    ok = ratio is not None and abs(float(ratio) - float(expect)) <= float(tolerance)
    return {"which": which, "ratio": ratio, "expect": expect,
            "tolerance": tolerance, "ok": ok, "reason": lane.get("reason")}


def poll_ring_wall_span(node: str, ip: str, timeout: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """One live `/status` + bare `/audio` poll from the same node in the same check pass."""
    try:
        status = fetch_status(ip, timeout)
    except Exception as e:
        return {"node": node, "ip": ip, "ok": False, "reason": "status: %r" % (e,)}
    said = str(status.get("node", ""))
    if said != node:
        return {"node": node, "ip": ip, "ok": False,
                "reason": ("identity: %s answers as %r, not %r" % (ip, said, node))}
    try:
        audio = fetch_audio_status(ip, timeout)
    except Exception as e:
        return {"node": node, "ip": ip, "ok": False, "reason": "audio: %r" % (e,)}
    return {"node": node, "ip": ip, "ok": True,
            "measurement": ring_wall_span_measurement(status, audio)}


def _ring_wall_span_note(poll: Optional[Dict[str, Any]], max_ratio: float) -> Tuple[str, bool]:
    """(text, is-failure) for the live ring-wall-span health signal."""
    if not poll:
        return "", False
    if not poll.get("ok"):
        return "  ring wall span UNKNOWN (%s)" % (poll.get("reason") or "not measured"), False
    m = poll["measurement"]
    bad = False
    pieces = []
    unknown = []
    for key in ("raw", "audio"):
        lane = m.get(key) or {}
        if lane.get("ratio") is None:
            unknown.append("%s %s" % (key, lane.get("reason") or "not measured"))
            continue
        ratio = float(lane["ratio"])
        pieces.append("%s %.5f" % (key, ratio))
        if ratio > max_ratio:
            bad = True
    text = ("  RING WALL SPAN " if bad else "  ring wall span ") + " / ".join(
        pieces or ["UNKNOWN"])
    if unknown:
        text += " (%s)" % "; ".join(unknown)
    return text, bad


def boot_audit(pl: "P.Pool", node: str, st: Dict[str, Any], now: float,
               tolerance_s: float = 60.0,
               max_days: int = BOOT_AUDIT_MAX_DAYS) -> Dict[str, Any]:
    """ADVISORY second route to the same loss, from the pool's rows rather than from bytes.

    A row's boot is `utc_us/1e6 - uptime_s`, which is constant within a boot; the current boot is
    `now - status.uptime_s`. Counting the stored rows that land on it gives what the pool holds
    for this boot, against what the node says it wrote.

    ⚠️7.6% OF SCENE ROWS (3,175 of 41,507 in `~/hear-corpus-export/corpus`) CARRY `utc_us == 0`
    AND CANNOT BE CLUSTERED THIS WAY. They are not attributed to a boot by guesswork -- an
    attribution rule that got them wrong would report ~7.6% phantom loss. They bound the answer
    instead: `outstanding_max` assumes none of them belong to this boot, `outstanding_min` assumes
    as many as possibly could.

    ⚠️"AS MANY AS POSSIBLY COULD" IS NOT "ALL OF THEM", AND READING IT THAT WAY MADE THE LOWER
    BOUND MEANINGLESS. An unanchored row still carries `uptime_s`, and within one boot `uptime_s`
    is strictly increasing (rows are 1.024 s apart, so consecutive integer seconds differ by 1 or
    2 and never repeat). So this boot can hold at most ONE unanchored row per distinct `uptime_s`
    value, and none at all above the node's current `uptime_s`. Measured on the exported corpus:
    mach has 2,896 unanchored rows across only 786 distinct `uptime_s` values (13-808 s -- they
    are the cold-start window before the PPS lock), so counting them all made the lower bound
    subtract roughly 3.7 boots' worth of rows from ONE boot. The distinct-value count is a real
    upper bound; the raw count was not a bound on anything.

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
    on_boot = 0
    # Distinct `uptime_s` values, not rows: see the docstring. A value above the node's current
    # uptime cannot have been produced by the boot that is still running.
    unanchored_uptimes: set = set()
    unanchored_rows = 0
    for d in days:
        if d == "unanchored":
            for r in pl.scene(node=node, day=d):
                unanchored_rows += 1
                ru = r.get("uptime_s")
                if ru in (None, ""):
                    continue
                try:
                    ru = int(float(ru))
                except (TypeError, ValueError):
                    continue
                if ru <= float(uptime):
                    unanchored_uptimes.add(ru)
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
    out["unanchored_rows_stored"] = unanchored_rows
    out["unanchored_attributable_max"] = len(unanchored_uptimes)
    if out["written"] is not None:
        w = int(out["written"])
        out["outstanding_max"] = max(0, w - on_boot)
        out["outstanding_min"] = max(0, w - on_boot - len(unanchored_uptimes))
    return out


def archive(root: str, node: str, name: str, body: bytes, stamp: int) -> str:
    d = os.path.join(root, "raw", node)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%d-%s" % (stamp, name))
    with open(p, "wb") as fh:
        fh.write(body)
    return p


def drain_context_file(pl: "P.Pool", node: str, ip: str, name: str, sizes: Optional[Dict[str, int]],
                       node_wm: Dict[str, Any], timeout: float, stamp: int,
                       out: Dict[str, Any]) -> bool:
    """Archive what one context file gained since the last run. True when `node_wm` moved.

    A first sighting, a mark with no header, and a file smaller than its mark (a roll) are fetched
    whole and the header is kept. Otherwise only the bytes past the mark are asked for; the leading
    fragment is dropped and the header prepended, so every archive is a CSV on its own and the rows
    of consecutive archives overlap by a few rows rather than leaving a hole.
    """
    size_now = sizes.get(name) if sizes is not None else None
    mark = node_wm.get(name) or {}
    prev, header = mark.get("size"), mark.get("header")
    entry: Dict[str, Any] = {"name": name, "ingested": False}
    rolled = size_now is not None and prev is not None and int(size_now) < int(prev)
    if size_now is not None and prev is not None and header and int(size_now) == int(prev):
        out["files"].append({**entry, "bytes": 0, "skipped_unchanged": True, "size": int(size_now)})
        return False
    if size_now is None and not header:
        # A blind tail with no header would archive rows that are not a CSV on their own. The
        # next listed run fetches the file whole, so nothing is lost by waiting for it.
        out["files"].append({**entry, "bytes": 0, "skipped_unmeasured": True})
        return False
    if size_now is not None and (prev is None or rolled or not header):
        tail, file_timeout = None, rolled_file_timeout(size_now, timeout)
    elif size_now is None:
        tail, file_timeout = CONTEXT_BLIND_TAIL_BYTES, timeout
    else:
        tail, file_timeout = int(size_now) - int(prev) + CONTEXT_OVERLAP_BYTES, timeout
    try:
        body = fetch_sd(ip, name, file_timeout, tail=tail)
    except Exception as e:
        out["errors"].append("%s: %r" % (name, e))
        return False
    if not body:
        return False
    fetched = len(body)
    # /sd serves the whole file when it is no longer than the tail asked for -- a small file, or
    # one that rolled between /ls and /sd -- and then the first line is the file's own header.
    whole_response = bool(tail) and (fetched < tail or body.startswith((b"node,", b"utc_us,")))
    # A whole file as served IS its size; /ls may still describe the file it replaced.
    mark_size = fetched if whole_response else size_now
    if tail and not whole_response:
        rows = body[body.index(b"\n") + 1:]
        body = (header.encode() + b"\n" + rows) if header else rows
    else:
        header = body.split(b"\n", 1)[0].rstrip(b"\r").decode("utf-8", "replace")
    entry.update({"bytes": fetched, "tail": tail, "size": mark_size,
                  "archived": archive(pl.root, node, name, body, stamp)})
    if rolled:
        entry["rolled"] = True
    if whole_response:
        entry["whole_response"] = True
    out["files"].append(entry)
    if mark_size is None:
        return False
    node_wm[name] = {"size": int(mark_size), "at": stamp, "header": header}
    return True


# ---------------------------------------------------------------- the clip lane


def fetch_clip(ip: str, name: str, timeout: float = DEFAULT_TIMEOUT_S
               ) -> Tuple[Optional[bytes], Optional[str]]:
    """One clip WAV off the card: `(body, None)` on a real clip, `(None, reason)` otherwise.

    ⚠️THIS CANNOT BE `fetch_sd`. `fetch_sd` requires the body to start with `b"node"` or
    `b"utc_us"` -- a header sniff that is right for a CSV and reports a present WAV as
    ABSENT, because a WAV starts with `b"RIFF"`. Proven against a live node 2026-09-09.

    ⚠️200 IS NOT PROOF OF A FILE. `/sd?file=/clips` answers 200 with a 0-byte body (measured), so
    the magic and the length are CHECKED here rather than assumed from the status code.

    It never raises for a node-side problem: every failure comes back as one of the reason strings
    below, and every one of them is counted by `drain_clips`. A refusal nobody can name is the
    failure this lane exists to prevent.
    """
    url = "http://%s/sd?file=%s" % (ip, name)
    try:
        body = _get(url, timeout)
    except urllib.error.HTTPError as e:
        return None, ("http_404" if e.code == 404 else "http_%d" % e.code)
    except Exception:
        return None, "transport"
    if not body:
        return None, "empty"
    if body[:4] != b"RIFF":
        return None, "not_riff"
    # ⚠️A FLOOR, NOT AN EQUALITY, AND NOT ONE FIRMWARE'S SIZE. wav_probe decides validity from the
    # header the clip carries; this only rejects a body too short to hold a 44-byte header plus
    # CLIP_MIN_S of the slowest rate the format can name.
    if len(body) < 44 + int(CL.CLIP_MIN_S * 8000 * 2):
        return None, "short"
    return body, None


# A refusal reason from `fetch_clip` or `wav_probe` -> the index outcome that records it. Anything
# not listed is a malformed body, which is `refused_bad_body`; the mapping is a function rather
# than a dict so a NEW probe reason (they carry measured numbers, e.g. `channels_2`) still lands
# somewhere countable instead of raising inside the drain.
def _refusal_outcome(reason: str) -> str:
    if reason == "short":
        return "refused_short"
    if reason == "transport" or reason.startswith("http_"):
        return "refused_http"
    return "refused_bad_body"


def clip_candidates(bodies: Sequence[Tuple[str, bytes]], node: str) -> List[Dict[str, Any]]:
    """This run's archived `dets.csv` bodies -> the clip work list, oldest first.

    Parsed with `hear.detsfile.read_text`, never a hand-rolled CSV split: G3 declares a `node`
    column its writer never emits, so a header-name reader shifts every value one column left and
    reads the CLIP PATH out of `frame_hex`. That is not hypothetical -- it returned 0 usable rows
    from 730 detections once already.

    ⚠️ORDER IS BY EVICTION RISK, NOT BY PRIORITY. The checkout firmware evicts oldest-first in
    `CL.eviction_key` order (older-format names, then boot sequence, then sample), so that order
    IS most-at-risk-first. The fleet flashed before it writes the `%02u-` prefix and evicts
    lowest-priority-first; oldest-first is harmless there. `prio` is RECORDED when the name
    carries it and is never read for ordering.

    A cell that does not parse as a clip path is kept as a candidate with `parts: None` rather
    than dropped. A name firmware wrote and this parser refuses is a disagreement between the two,
    and one that vanished from the list would be indistinguishable from a clip never written.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for name, body in bodies:
        origin = "%s:/%s" % (node, name)
        try:
            read = DF.read_text(body.decode("utf-8", "replace"), default_node=node)
        except Exception:
            # An unknown or missing schema is the dets lane's refusal to report, not this one's:
            # `drain_node` already books it. Here it simply yields no names.
            continue
        for row in read.rows:
            clip = (row.get("clip") or "").strip()
            if not clip:
                continue
            parts: Optional[Dict[str, Any]] = None
            bad_name: Optional[str] = None
            try:
                parts = CL.parse_clip_name(clip)
            except ValueError as e:
                bad_name = str(e)
            k = (CL.clip_key(parts["node"], parts["boot"], parts["sample"]) if parts
                 else CL.bad_name_key(node, clip))
            if k in seen:
                continue
            seen.add(k)
            try:
                utc_us = int(float(row.get("utc_us") or 0))
            except (TypeError, ValueError):
                utc_us = 0
            fs = row.get("fs_hz")
            out.append({
                "clip": clip,
                "parts": parts,
                "bad_name": bad_name,
                "clip_key": k,
                "node": node,
                "utc_us": utc_us,
                "ts_utc_s": (utc_us / 1e6) if utc_us > 0 else None,
                "anchored": utc_us > 0,
                "uptime_s": row.get("uptime_s"),
                "fs_hz": float(fs) if fs not in (None, "") else None,
                "trigger": row.get("trigger"),
                "clip_why": row.get("clip_why"),
                "record_key": _record_key(row, node, utc_us),
                "dets_origin": origin,
            })
    # A name that would not parse has no (boot, sample) to sort on, so it sorts LAST rather than
    # under an invented zero -- it costs no request and must not displace one that does.
    out.sort(key=lambda c: ((0,) + CL.eviction_key(c["parts"]["basename"]) if c["parts"]
                            else (1,)))
    return out


def ls_candidates(sizes: Optional[Dict[str, int]], node: str) -> List[Dict[str, Any]]:
    """A `/ls` listing -> the clip work list it names. The SECOND discovery source, and the only
    one that can see a clip whose dets row has already rolled off the card.

    ⚠️IT RETURNS [] AGAINST THE FLASHED FLEET, AND THAT IS A MEASUREMENT, NOT A STUB. `/ls` on the
    running firmware lists ROOT and nothing else, and root holds `clips` as a DIRECTORY entry --
    `d clips  0 B` in tests/fixtures/ls_nyquist.txt -- which `_ls_parse` drops with every other
    `d ` line. So there is no clip name in a root listing to find, on either firmware, and this
    runs on every drain at no extra request rather than waiting behind a flag nobody will flip.
    Point `_ls_sizes(..., dir=CL.CLIP_DIR)` at a node running the handler in this checkout and the
    same function starts returning names, because that handler qualifies them `clips/<basename>`.

    A dets row is strictly richer -- it carries utc_us, fs_hz, trigger and the parent record key,
    none of which a directory listing has -- so `drain_node` unions the two with dets WINNING on
    clip_key. What survives from here is exactly the clips dets no longer names.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for name in sorted(sizes or {}):
        raw = "/" + name.lstrip("/")
        if not raw.startswith(CL.CLIP_DIR + "/"):
            continue
        try:
            parts = CL.parse_clip_name(raw)
        except ValueError:
            # Something under /clips that is not a clip name. It is not a lost detection and
            # there is no dets row claiming otherwise, so it is skipped rather than booked as a
            # refusal -- unlike a dets cell, which IS a firmware/parser disagreement.
            continue
        k = CL.clip_key(parts["node"], parts["boot"], parts["sample"])
        if k in seen:
            continue
        seen.add(k)
        out.append({
            "clip": raw, "parts": parts, "bad_name": None, "clip_key": k, "node": node,
            # ⚠️A LISTING CARRIES NO TIME. Every one of these is None rather than an invented
            # zero: the clip lands under `unanchored/` and `index_row` copies what it is given.
            "utc_us": 0, "ts_utc_s": None, "anchored": False, "uptime_s": None, "fs_hz": None,
            "trigger": None, "clip_why": None, "record_key": None,
            "dets_origin": "%s:/ls?dir=%s" % (node, CL.CLIP_DIR),
            "ls_bytes": sizes[name] if sizes else None,
        })
    out.sort(key=lambda c: CL.eviction_key(c["parts"]["basename"]))
    return out


def merge_candidates(dets: List[Dict[str, Any]],
                     ls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Union the two discovery sources by clip_key, DETS WINNING, dets order preserved.

    Dets first because it is the richer row and because its order is the eviction order the fetch
    budget is spent against; the /ls-only remainder is appended, still oldest-first among itself.
    """
    out = list(dets)
    have = {c["clip_key"] for c in dets}
    for c in ls:
        if c["clip_key"] not in have:
            have.add(c["clip_key"])
            out.append(c)
    return out


def _record_key(row: Dict[str, Any], node: str, utc_us: int) -> Optional[str]:
    """The parent dets row's pool key, so a clip joins to its sketch without a second index.

    `None` when the frame will not decode: the clip is still worth fetching, and inventing a key
    for it would put it in the pool's namespace pointing at nothing.
    """
    fh = (row.get("frame_hex") or "").strip()
    if not fh:
        return None
    if len(fh) % 2 != 0:
        fh = fh[:-1]
    try:
        frame = binascii.unhexlify(fh)
    except Exception:
        return None
    return P.key("node", node, utc_us, row.get("sample"), frame)


def _store_clip(root: str, node: str, cand: Dict[str, Any], body: bytes) -> str:
    """Write one WAV under `clips/<day>/<node>/` and return its path RELATIVE to the pool root.

    tmp + `os.replace`, because a pod killed mid-write would otherwise leave a truncated WAV that
    the index calls `stored` -- and `stored` is terminal, so it would never be fetched again.

    The pid is IN the temp name so `CL.sweep_tmp` can reclaim an abandoned part-file without ever
    racing a live writer: `activeDeadlineSeconds: 780` makes the killed-mid-write case a designed
    event, and `_audio_files` cannot see a `.tmp` to charge it against the cap.
    """
    day = CL._day(cand["ts_utc_s"] if cand.get("anchored") else None)
    p = CL.store_path(root, day, node, cand["parts"]["basename"])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = "%s.%d.tmp" % (p, os.getpid())
    with open(tmp, "wb") as fh:
        fh.write(body)
    os.replace(tmp, p)
    return os.path.relpath(p, root)


def drain_clips(pl: "P.Pool", node: str, ip: str, candidates: List[Dict[str, Any]],
                timeout: float = DEFAULT_TIMEOUT_S,
                max_per_node: int = CLIP_MAX_PER_NODE_DEFAULT,
                deadline_s: float = CLIP_DEADLINE_S_DEFAULT,
                store_max_bytes: int = CLIP_STORE_MAX_BYTES_DEFAULT,
                now: Optional[float] = None) -> Dict[str, Any]:
    """Fetch what is still on the card, STRICTLY SEQUENTIALLY, one request at a time.

    ⚠️ONE CLIENT AT A TIME IS A PROPERTY OF THE NODE, NOT A STYLE CHOICE. The ESP32 serves one
    client and REFUSES the rest rather than queueing: `/ls` answers in 35-118 ms idle, degrades to
    7.3 s during a large transfer and is refused outright when a second client is mid-request. So
    this is a plain `for` loop with one `_get` in flight, and `main()`'s node loop is a list
    comprehension for the same reason. Nothing here may become concurrent.

    ⚠️THE NEGATIVE CACHE IS LOAD-BEARING. A clip_key whose last index outcome is terminal is not
    probed again: `stored` counts as `already_held`, `evicted_before_fetch` as `already_gone`.
    Without it the drain re-probes 321 already-dead nyquist names every 15 minutes forever
    (measured 2026-09-09: 321 of 370 dets-named clips are already 404).

    ⚠️`clips_unknown` STARTS True AND IS CLEARED ONLY BY A COMPLETED PASS. A run that never
    reached the node has MEASURED NO CLIPS, and must never report `clips_gone: 0`.
    """
    t0 = time.time()
    now = t0 if now is None else now
    out: Dict[str, Any] = {
        "clips_seen": len(candidates), "clips_fetched": 0, "clips_already_held": 0,
        "clips_already_gone": 0, "clips_gone": 0, "clips_probed_404": 0, "clips_refused": {},
        "clips_deferred_by_cap": 0, "clips_cap_hit": False, "clips_cap_reason": None,
        "clips_bytes": 0, "clips_elapsed_s": 0.0,
        "clips_unknown": True,
        "clips_reason": "the clip pass ended before it finished its work list",
        "prune": None,
    }
    # ⚠️PRUNE BEFORE THE FETCH, so the room the fetch needs exists before it is needed rather than
    # after. It deletes WAVs only; index lines are the durable record and are never deleted.
    out["prune"] = CL.prune(pl.root, store_max_bytes, now)

    held = CL.read_outcomes(pl.root)

    # ⚠️ONE APPEND PER CLIP, AS IT RESOLVES, NEVER A BATCH AT THE END. `_store_clip` makes the
    # bytes durable immediately; buffering the ledger meant a pod killed between the two kept the
    # audio and lost the row, and the NEXT run's 404 then wrote `evicted_before_fetch` -- which is
    # terminal -- over clips whose bytes were sitting on the PVC. `activeDeadlineSeconds: 780`
    # against a 218-307 s run plus 3 x (120 s clip deadline + a 30 s final fetch) makes that kill
    # a designed event. append_index opens and closes per call; 49 appends per node is nothing.
    def emit(**kw) -> None:
        CL.append_index(pl.root, (CL.index_row(node=node, fetched_at=now, **kw),))

    def refuse(reason: str) -> None:
        out["clips_refused"][reason] = out["clips_refused"].get(reason, 0) + 1

    for cand in candidates:
        parts = cand["parts"]
        prev = held.get(cand["clip_key"]) or {}
        outcome_before = prev.get("outcome")
        if outcome_before == "stored":
            out["clips_already_held"] += 1
            continue
        if outcome_before == "evicted_before_fetch":
            out["clips_already_gone"] += 1
            continue
        if parts is None:
            refuse("bad_name")
            emit(clip=cand["clip"], parts=None, body=None, probe=None, dets=cand, path=None,
                 outcome="refused_name", reason=cand["bad_name"])
            continue
        named = parts["node"]
        if named != node and ID.alias_of(named) != node:
            refuse("node_mismatch")
            emit(clip=cand["clip"], parts=parts, body=None, probe=None, dets=cand, path=None,
                 outcome="refused_node",
                 reason="clip name says node %r, fetched from %r" % (named, node))
            continue
        # The cap is tested BEFORE the request is spent, so `deferred_by_cap` means "still on the
        # card, not asked for", never "asked for and lost".
        if not out["clips_cap_hit"]:
            if out["clips_fetched"] >= max_per_node:
                out["clips_cap_hit"], out["clips_cap_reason"] = True, "count"
            elif (time.time() - t0) >= deadline_s:
                out["clips_cap_hit"], out["clips_cap_reason"] = True, "deadline"
            elif CL.free_bytes(pl.root) < CLIP_FREE_RESERVE_B:
                out["clips_cap_hit"], out["clips_cap_reason"] = True, "disk"
        if out["clips_cap_hit"]:
            out["clips_deferred_by_cap"] += 1
            # ⚠️ONE DEFERRAL ROW PER CLIP, NOT ONE PER RUN. The row is worth writing once: when
            # the clip is evicted before it is ever fetched AND its dets row has rolled off the
            # card, it is the only record the clip existed. Rewriting it every 15 minutes for a
            # standing backlog is pure index growth, and the index is never compacted.
            if outcome_before != "deferred_by_cap":
                emit(clip=cand["clip"], parts=parts, body=None, probe=None, dets=cand, path=None,
                     outcome="deferred_by_cap", reason=out["clips_cap_reason"])
            continue

        body, reason = fetch_clip(ip, cand["clip"], timeout)
        if reason == "http_404":
            # ⚠️ONE 404 IS NOT PROOF OF AN EVICTION. hear_node.ino:2436 answers 404 for ANY
            # failed SD.open -- the no-card case is a 503 at :2435, but descriptor exhaustion,
            # which the firmware's own comment at :2340-2344 says is reachable with max_files 8,
            # collapses to 404 as well. Calling that terminal on first sight writes "the node
            # destroyed this" into the durable record for a clip still on the card, and the name
            # is then never probed again. It takes CL.CONFIRM_404 consecutive 404s.
            n404 = int(prev.get("probe_404s") or 0) + 1
            if n404 >= CL.CONFIRM_404:
                out["clips_gone"] += 1
                emit(clip=cand["clip"], parts=parts, body=None, probe=None, dets=cand, path=None,
                     outcome="evicted_before_fetch", reason="http_404 x%d" % n404,
                     probe_404s=n404)
            else:
                out["clips_probed_404"] += 1
                emit(clip=cand["clip"], parts=parts, body=None, probe=None, dets=cand, path=None,
                     outcome="probed_404", reason="http_404", probe_404s=n404)
            continue
        if reason is not None:
            refuse(reason)
            emit(clip=cand["clip"], parts=parts, body=None, probe=None, dets=cand, path=None,
                 outcome=_refusal_outcome(reason), reason=reason)
            continue
        probe = CL.wav_probe(body)
        if not probe["ok"]:
            refuse(probe["reason"])
            emit(clip=cand["clip"], parts=parts, body=body, probe=probe, dets=cand, path=None,
                 outcome="refused_bad_body", reason=probe["reason"])
            continue
        try:
            path = _store_clip(pl.root, node, cand, body)
        except OSError as e:
            # The bytes reached us and could not be written -- a read-only or full PVC. NOT
            # terminal, so the next run asks again, but a ROW IS WRITTEN: without one the clip was
            # invisible in the index as well as at the gate, and every run re-fetched 128 kB to
            # store nothing while `check` printed a clean line.
            refuse("store_%s" % type(e).__name__)
            emit(clip=cand["clip"], parts=parts, body=body, probe=probe, dets=cand, path=None,
                 outcome="refused_store", reason="%s: %s" % (type(e).__name__, e))
            continue
        out["clips_fetched"] += 1
        out["clips_bytes"] += len(body)
        emit(clip=cand["clip"], parts=parts, body=body, probe=probe, dets=cand, path=path,
             outcome="stored")

    out["clips_elapsed_s"] = time.time() - t0
    out["clips_unknown"] = False
    out["clips_reason"] = None
    # ⚠️THE CENSUS TOTALS ITSELF, AS AN ASSERTION AND NOT AS A HOPE. Every name found reaches
    # exactly one bucket; a bucket added later without a home here fails loudly and immediately.
    seen = (out["clips_fetched"] + out["clips_already_held"] + out["clips_already_gone"]
            + out["clips_gone"] + out["clips_probed_404"] + sum(out["clips_refused"].values())
            + out["clips_deferred_by_cap"])
    assert seen == out["clips_seen"], (seen, out)
    return out


def record_node_position(root: str, node: str, st: Dict[str, Any],
                         now: float) -> Optional[Dict[str, Any]]:
    """File this node's GPS mean from /status under state/, or None when it reported none.

    The return value is what the run report carries: fix count and hAcc, never a coordinate.
    """
    pos = st.get("pos")
    if not isinstance(pos, dict) or pos.get("mean_lat") is None or pos.get("mean_lon") is None:
        return None
    gps = st.get("gps") if isinstance(st.get("gps"), dict) else {}
    p = os.path.join(root, "state", NODE_POSITIONS_FILE)
    doc: Dict[str, Any] = {"schema": "hear.node_positions.v1", "nodes": {}}
    if os.path.exists(p):
        try:
            with open(p) as fh:
                got = json.load(fh)
            if isinstance(got, dict) and isinstance(got.get("nodes"), dict):
                doc = got
        except (OSError, ValueError):
            pass
    # ⚠️pos.n 0 IS AN ABSENCE, NOT A PLACE. The firmware averages a position only from UBX
    # NAV-PVT; the PMTK/NMEA boards (gold, ageev, kasami) parse no coordinate from GGA and report
    # means of exactly 0.0. Filing that would be a node at lat 0, lon 0, so any entry is dropped.
    if not pos.get("n"):
        if doc["nodes"].pop(node, None) is not None:
            _write_json_atomic(p, doc)
        return None
    doc["nodes"][node] = {
        "class": st.get("class"), "lat_deg": pos.get("mean_lat"), "lon_deg": pos.get("mean_lon"),
        "h_ell_m": pos.get("mean_hell_m"), "hacc_m": pos.get("hacc_m"),
        "vacc_m": pos.get("vacc_m"), "fixes": pos.get("n"), "fix": gps.get("fix"),
        "at": now, "uptime_s": st.get("uptime_s"), "source": "/status pos.mean_*"}
    _write_json_atomic(p, doc)
    return {"fixes": pos.get("n"), "hacc_m": pos.get("hacc_m")}


def _write_json_atomic(p: str, doc: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    os.replace(tmp, p)


#: Two runs saw the same boot when the boot EPOCH they imply (run time minus uptime) agrees to
#: within this. Comparing uptimes alone reads a reboot as continuity once the new boot has been up
#: longer than the old one was.
BOOT_EPOCH_TOLERANCE_S = 300.0


def _same_boot(prev: Dict[str, Any], uptime_s: Optional[int], total: Optional[int],
               stamp: float) -> bool:
    if prev.get("uptime_s") is None or prev.get("at") is None or uptime_s is None:
        return False
    if total is not None and int(total) < int(prev.get("last_i", -1)) + 1:
        return False
    implied = float(stamp) - float(uptime_s)
    was = float(prev["at"]) - float(prev["uptime_s"])
    return abs(implied - was) <= BOOT_EPOCH_TOLERANCE_S


def live_ring_loss(prev: Dict[str, Any], served: Sequence[int], total: Optional[int],
                   uptime_s: Optional[int], stamp: float) -> Tuple[Optional[int], Optional[str]]:
    """(detections the pool will never see, why that number is partial or absent).

    `served` is every `i` this run's /detections carried and `total` is /status audio.detections,
    the node's detection count this boot. None is UNMEASURED, never zero.
    """
    first = min(served) if served else None
    if not prev:
        return None, ("first sighting of this node's live ring, so rows before this run are "
                      "unmeasured")
    if not _same_boot(prev, uptime_s, total, stamp):
        aged = first if first is not None else total
        return (None if aged is None else int(aged)), (
            "the node rebooted since the last run: counted are this boot's rows that aged out "
            "before this run; the previous boot's rows after the last run are unmeasured")
    last = int(prev.get("last_i", -1))
    # ⚠️THE GAP BELOW THE OLDEST ROW SERVED IS THE LOSS, NOT `total` MINUS WHAT CAME BACK. A body
    # the node truncated leaves its newest rows unserved; they are still in the ring and arrive
    # later, so counting them now would report a loss that has not happened.
    if first is None:
        if total is not None and int(total) <= last + 1:
            return 0, None
        return None, "the ring body carried no row, so nothing past the last run was measured"
    return max(0, first - last - 1), None


def _drain_live_ring(pl: "P.Pool", node: str, ip: str, st: Dict[str, Any], out: Dict[str, Any],
                     timeout: float, stamp: int) -> Dict[str, Any]:
    """A node reporting `sd: false` has no card files, so its detections exist only in the live
    ring. Scene and clips are NOT APPLICABLE to it, which is a different answer from UNKNOWN."""
    out["no_card"] = True
    out["unfetched_unknown"] = False
    out["unfetched_reason"] = "the node reports no card, so it writes no scene file"
    out["clips_unknown"] = False
    out["clips_reason"] = "the node reports no card, so it writes no clips"
    out["live_ring_rows"] = None
    out["live_ring_lost"] = None
    try:
        body = fetch_detections(ip, timeout)
        ring, truncated = parse_live_ring(body)
    except Exception as e:
        out["errors"].append("detections: %r" % (e,))
        out["live_ring_reason"] = "/detections could not be fetched, so nothing was measured"
        return out
    out["live_ring_truncated_bytes"] = truncated
    if truncated is not None:
        archive(pl.root, node, "detections-truncated.raw", body, stamp)
        body = json.dumps(ring).encode()
    path = archive(pl.root, node, "detections.json", body, stamp)
    try:
        entry = pl.ingest_detections_json(path, default_node=node,
                                          origin="%s:/detections" % node)
    except Exception as e:
        out["errors"].append("detections: ingest: %r" % (e,))
        out["live_ring_reason"] = "the /detections body did not ingest, so no row was stored"
        return out
    out["added"] += entry["added"]
    out["files"].append({"name": "/detections", "bytes": len(body), "ingested": True,
                         "live_ring": True, "archived": path,
                         **{k: entry[k] for k in ("generation", "rows", "added", "duplicate",
                                                  "skipped", "skip_reasons")}})
    served = sorted(int(d["i"]) for d in ring
                    if isinstance(d, dict) and isinstance(d.get("i"), int))
    audio = st.get("audio") if isinstance(st.get("audio"), dict) else {}
    total = audio.get("detections") if isinstance(audio.get("detections"), int) else None
    up = st.get("uptime_s")
    wm = read_watermarks(pl.root)
    node_wm = dict(wm.get(node) or {})
    prev = node_wm.get("live_ring") or {}
    lost, why = live_ring_loss(prev, served, total, up, stamp)
    out["live_ring_rows"] = len(served)
    out["live_ring_lost"] = lost
    out["live_ring_reason"] = why
    out["live_ring_pending"] = (max(0, int(total) - 1 - max(served))
                                if total is not None and served else None)
    same_boot = _same_boot(prev, up, total, stamp)
    last_i = max(served) if served else (int(prev.get("last_i", -1)) if same_boot else -1)
    node_wm["live_ring"] = {"last_i": last_i, "uptime_s": up, "at": stamp}
    wm[node] = node_wm
    write_watermarks(pl.root, wm)
    out["ok"] = not out["errors"]
    return out


def drain_node(pl: "P.Pool", node: str, ip: str, timeout: float = DEFAULT_TIMEOUT_S,
               stamp: Optional[int] = None,
               clip_max_per_node: int = CLIP_MAX_PER_NODE_DEFAULT,
               clip_deadline_s: float = CLIP_DEADLINE_S_DEFAULT,
               clip_store_max_bytes: int = CLIP_STORE_MAX_BYTES_DEFAULT) -> Dict[str, Any]:
    """Fetch, archive and ingest one node. Never raises for a node-side problem; reports it.

    The scene fetch also measures its own reach-back against the file on the card -- see the
    module docstring and `scene_gap()`. `unfetched_bytes` is the number of bytes of scene.csv no
    fetch has ever asked for: rows that are on the card, are not in the pool, and will roll off it.

    ⚠️THREE ANSWERS, NOT TWO, AND THE THIRD IS THE POINT OF THE MODULE. `unfetched_bytes` is an
    integer only when a measurement actually happened; it is `None` whenever one did not, and
    `unfetched_unknown` says so alongside `unfetched_reason`. A run that could not reach `/status`
    at all, or that refused the node on identity, has measured NOTHING -- and a module whose whole
    contract is that an unmeasured thing must not read as a clean one cannot then return 0 for it.
    `unfetched_unknown` therefore starts True and is cleared only by a completed measurement.
    """
    stamp = int(stamp if stamp is not None else time.time())
    out: Dict[str, Any] = {"node": node, "ip": ip, "at": stamp, "ok": False,
                           "files": [], "added": 0, "scene_added": 0, "errors": [],
                           "unfetched_bytes": None, "unfetched_unknown": True,
                           "unfetched_reason": "the run ended before any scene tail was measured",
                           "scene_missing": False,
                           # ⚠️None, NOT 0. A run that never reached the node measured NO clips,
                           # and `clips_gone: 0` would read as "nothing was destroyed" -- the
                           # unmeasured-looks-clean failure this whole module is built against.
                           "clips_seen": None, "clips_fetched": None, "clips_already_held": None,
                           "clips_already_gone": None, "clips_gone": None,
                           "clips_probed_404": None,
                           "clips_refused": None, "clips_deferred_by_cap": None,
                           "clips_cap_hit": None, "clips_cap_reason": None,
                           "clips_bytes": None, "clips_elapsed_s": None,
                           "clips_unknown": True,
                           "clips_reason": "the run ended before the clip lane ran"}
    try:
        st = fetch_status(ip, timeout)
    except Exception as e:
        out["errors"].append("status: %r" % (e,))
        out["unfetched_reason"] = "/status was unreachable, so nothing about this node was measured"
        out["clips_reason"] = "/status was unreachable, so no clip on this node was measured"
        return out
    said = str(st.get("node", ""))
    out["identity"] = said
    out["uptime_s"] = st.get("uptime_s")
    if said != node:
        out["errors"].append(
            "identity: %s answers as %r, not %r -- refusing to file its rows under the wrong "
            "name (check the DHCP lease)" % (ip, said, node))
        out["unfetched_reason"] = ("%s answered as %r, so no file of %r's was measured"
                                   % (ip, said, node))
        out["clips_reason"] = ("%s answered as %r, so no clip of %r's was measured"
                               % (ip, said, node))
        return out

    try:
        out["position"] = record_node_position(pl.root, node, st, stamp)
    except Exception as e:
        # advisory: a position that did not file must not cost the node its data lanes
        out["position_error"] = repr(e)
    if st.get("sd") is False:
        return _drain_live_ring(pl, node, ip, st, out, timeout, stamp)

    # ⚠️SIZES ARE READ BEFORE THE BODY. That makes `size_now` a lower bound on the file at fetch
    # time, so the computed gap is biased towards zero rather than towards a false alarm, and it
    # makes the watermark a size the fetched body is guaranteed to have reached.
    #
    # ⚠️A FAILED /ls IS NOT A FAILED RUN. It blinds the loss accounting, which is reported as
    # UNKNOWN and never as zero, but the fetch and the ingest are unaffected and firmware old
    # enough to lack the endpoint would otherwise be marked permanently failed -- and then STALE
    # -- while its data flowed normally. It is recorded in its own field so `ok` keeps meaning
    # "the data moved" and the blind spot is still visible.
    sizes, ls_err, ls_attempts = ls_sizes_retrying(ip, timeout)
    if ls_err:
        out["ls_error"] = ls_err
    out["ls_ok"] = sizes is not None
    out["ls_attempts"] = ls_attempts
    if sizes is not None and ls_attempts > 1:
        out["ls_retried"] = ls_attempts
    # ⚠️A CAPPED LISTING IS A PARTIAL CENSUS, NOT A SHORT CARD. The handler stops at
    # LS_MAX_ENTRIES and says so; carrying the number here is what keeps "the card holds this"
    # apart from "this is as far as /ls counted".
    out["ls_truncated_at"] = getattr(sizes, "truncated_at", None)

    wm = read_watermarks(pl.root)
    node_wm = dict(wm.get(node) or {})
    wm_dirty = False
    status_block = status_audit(st)

    for name in CONTEXT_FILES:
        wm_dirty |= drain_context_file(pl, node, ip, name, sizes, node_wm, timeout, stamp, out)

    scene_fetch, scene_live = scene_names(sizes)
    out["scene_files"] = list(scene_fetch)
    out["scene_live"] = scene_live
    if not scene_fetch and sizes is not None:
        out["scene_missing"] = True
        out["unfetched_unknown"] = False
        out["unfetched_bytes"] = 0
        out["unfetched_reason"] = "the node served no scene file of any name"
    for name in scene_fetch:
        # A rolled file does not grow, so it is fetched whole. Only the live file is tailed.
        tail = SCENE_TAIL_BYTES if name == scene_live else None
        # ⚠️WHOLE MEANS WHOLE, AND THE REPEAT IS NOT FREE. The content-addressed ingest makes a
        # refetch free in STORAGE; it costs the full transfer every run. Once the nodes started
        # writing scene-YYYYMMDD.csv the legacy scene.csv became a ROLLED file -- 16.7 MB on
        # nyquist, 2-7 min at the measured 40-135 KB/s -- so discovery alone would have pulled it
        # whole every 15 minutes and spent most of the interval making the node deaf. It is
        # fetched once and then skipped while /ls reports the size it had when it last INGESTED.
        # No size (/ls failed, or first sighting) means fetch: a blind run must fetch, not guess.
        #
        # ⚠️ONLY A WHOLE-FILE MARK LICENSES A SKIP. Yesterday's dated file was the LIVE file and
        # was TAILED, so its mark is a size whose earlier bytes this drain never pulled. Skipping
        # on that mark would strand everything before the last SCENE_TAIL_BYTES permanently, the
        # moment the date rolled. A mark only permits a skip if it was written by a fetch that
        # reached byte 0.
        if not tail:
            mark = node_wm.get(name) or {}
            seen = mark.get("size") if mark.get("whole_file") else None
            size_now = sizes.get(name) if sizes is not None else None
            if seen is not None and size_now is not None and int(seen) == int(size_now):
                out["files"].append({"name": name, "bytes": 0, "skipped_unchanged": True,
                                     "size": int(size_now), "scene": True})
                continue
            file_timeout = rolled_file_timeout(size_now, timeout)
        else:
            file_timeout = timeout
        try:
            body = fetch_sd(ip, name, file_timeout, tail=tail)
        except Exception as e:
            out["errors"].append("%s: %r" % (name, e))
            if tail:
                out["unfetched_reason"] = "%s could not be fetched, so it was not measured" % name
            continue
        if body is None:
            out["files"].append({"name": name, "absent": True})
            if tail:
                out["unfetched_reason"] = "the node served no %s to measure" % name
            continue

        if not tail:
            # A whole-file fetch reaches back to byte 0 by construction. Stated, not assumed away.
            gap: Dict[str, Any] = {"whole_file": True, "unfetched_bytes": 0,
                                   "fetched_bytes": len(body)}
        else:
            size_now = sizes.get(name) if sizes is not None else None
            prev = (node_wm.get(name) or {}).get("size")
            gap = scene_gap(size_now, len(body), prev, mean_row_bytes(body))
            # ⚠️THE MEASUREMENT IS BOOKED HERE, BEFORE THE INGEST, BECAUSE IT IS A PROPERTY OF THE
            # FETCH. Booking it after the store meant an ingest that raised took the number with
            # it: a run that measured a real gap and then failed to write reported 0 -- the exact
            # unmeasured-reads-as-clean failure this module exists to prevent, reintroduced by
            # control flow. Whether the fetched rows landed is a SEPARATE loss, on `errors`.
            if gap.get("unfetched_bytes") is None:
                out["unfetched_reason"] = ("/ls gave no size for %s, so its reach-back is "
                                           "unmeasured" % name)
            else:
                out["unfetched_bytes"] = (out["unfetched_bytes"] or 0) + int(gap["unfetched_bytes"])
                out["unfetched_unknown"] = False
                out["unfetched_reason"] = None

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
            if gap.get("size_now") is not None:
                node_wm[name] = {"size": int(gap["size_now"]), "at": stamp,
                                 "mean_row_bytes": gap.get("mean_row_bytes")}
                wm_dirty = True
        else:
            # ⚠️BOOKED AFTER THE INGEST, for the same reason the tailed mark is: rows fetched but
            # not stored must be fetched again. A whole-file mark is the size the file had when
            # its rows landed, so a file that grows again is refetched rather than skipped.
            whole_size = sizes.get(name) if sizes is not None else None
            if whole_size is None:
                whole_size = len(body)
            node_wm[name] = {"size": int(whole_size), "at": stamp, "whole_file": True}
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

    # ⚠️THE DETS BODIES ARE KEPT. dets.csv is where clip names come from -- `det_flush` refuses to
    # write a detection's row until its clip has resolved (hear_node.ino), so a name in this file
    # is a clip that already landed on the card. Discovery via /ls?dir= needs a reflash and this
    # does not, which is why this is the shipping path.
    dets_bodies: List[Tuple[str, bytes]] = []
    dets_failed: List[str] = []
    for name in DETS_FILES:
        try:
            body = fetch_sd(ip, name, timeout)
        except Exception as e:
            out["errors"].append("%s: %r" % (name, e))
            dets_failed.append(name)
            continue
        if body is None:
            out["files"].append({"name": name, "absent": True})
            continue
        dets_bodies.append((name, body))
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

    # ⚠️AFTER DETS, INSIDE drain_node, AND NEVER IN PARALLEL. After dets because that is where the
    # names are. Inside drain_node because `main()` already runs the nodes one at a time -- the
    # ESP32 serves ONE client and refuses the rest, so a second job or a thread pool converts a
    # slow run into a refused run. Do not touch that sequencing.
    #
    # ⚠️NO `archive()` FOR CLIPS. It stamps the filename, so a refetch would write another 128 kB
    # copy under raw/ every run. The clip store is content-addressed by NAME and lives under
    # clips/<day>/<node>/.
    if clip_max_per_node <= 0:
        out["clips_reason"] = "the clip lane is disabled (--clip-max-per-node 0)"
    elif DETS_FILES[0] in dets_failed:
        # No live dets file means no discoverable names. That is an UNMEASURED run for the clip
        # lane, not a clean one: the clips are still on the card and nothing here looked.
        out["clips_reason"] = ("%s could not be fetched, so no clip name was discoverable"
                               % DETS_FILES[0])
    else:
        # ⚠️THE DETS-FAILED BRANCH ABOVE STAYS UNKNOWN EVEN THOUGH /ls IS A SECOND SOURCE. A run
        # that could not read dets.csv has not measured what dets would have named, and on the
        # flashed fleet `ls_candidates` returns [] regardless -- so there is nothing to trade for
        # the honesty. Revisit that branch when, and only when, a node runs the /ls?dir= handler.
        # ⚠️CONTAINED, LIKE EVERY OTHER LANE. The nodes are drained in one list comprehension in
        # main(); an OSError out of prune / read_outcomes / append_index / free_bytes here -- a
        # full PVC, an unreadable clips/ subtree, a permissions fault on index.jsonl -- used to
        # propagate out of drain_node and kill the process before write_heartbeat, so the nodes
        # AFTER this one were never contacted and no ring entry was written for any of them. The
        # clip lane must not be able to take down the lanes that were resilient before it existed.
        try:
            cand = merge_candidates(clip_candidates(dets_bodies, node),
                                    ls_candidates(sizes, node))
            out.update(drain_clips(pl, node, ip, cand, timeout,
                                   max_per_node=clip_max_per_node, deadline_s=clip_deadline_s,
                                   store_max_bytes=clip_store_max_bytes, now=stamp))
        except Exception as e:
            out["errors"].append("clips: %r" % (e,))
            out["clips_unknown"] = True
            out["clips_reason"] = "the clip lane raised %r, so no clip was measured" % (e,)

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


#: Every node whose files a backfill may name from their own path. ⚠️It is the drain's `--node`
#: roster written down: a name missing here is not a mis-named ingest, it is an UNCHECKED one.
KNOWN_NODES = ("nyquist", "mach", "rankine", "puc")


def node_from_path(path: str, default_node: Optional[str] = None) -> Optional[str]:
    """The node a historical file belongs to, from its path COMPONENTS, else `default_node`.

    Component-wise on purpose: `known in path` made `/home/machine/drains/x_dets.csv` mach's.
    The archive layout this reads is `<pool>/raw/<node>/<stamp>-dets.csv`, plus the hand-made
    drains' `<dir>/<node>/dets.csv` and `<dir>/<node>_dets.csv`.
    """
    parts = [q for p in os.path.abspath(path).split(os.sep) for q in (p, p.split("_")[0])]
    for known in KNOWN_NODES:
        if known in parts:
            return known
    return default_node


def backfill(pl: "P.Pool", paths: List[str], default_node: Optional[str] = None,
             scene_is_tail: bool = False) -> List[Dict[str, Any]]:
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

    ⚠️AN ARCHIVED scene.csv IS USUALLY A TAIL, AND A TAIL CANNOT BE RE-INGESTED BY DEFAULT.
    `archive()` stores the body of `GET /sd?file=scene-YYYYMMDD.csv&tail=N` verbatim, so it
    begins mid-row and carries no header -- and `ingest_scene(partial=False)` refuses the WHOLE
    file as UnknownSchema, naming half a hex mel string as the header. Measured on the live pool:
    13 archived rankine scene files, 10 of them refused entire. `scene_is_tail` (CLI
    `--ingest-scene-tails`) says these files are tails, which drops the leading fragment and
    reports it. It is an ASSERTION BY THE CALLER, not a sniff: a whole file that begins mid-row
    is corruption, and the default keeps reading it as corruption.

    ⚠️THE PATH IS READ BY COMPONENT, AND THE ROSTER HAS TO BE COMPLETE. Both halves were wrong:
    the roster was ("nyquist", "mach", "puc") -- no `rankine`, though the pool has archived 23 of
    its dets files -- and the test was `known in p`, a SUBSTRING match on the whole path, which
    files everything under a directory called `machine/` as mach. A missing name is the worse of
    the two here: with `--ingest-node` unset it leaves `default_node` None, `_node_mismatch` then
    has nothing to compare, and the file's own labels are believed unconditionally -- which is
    how a re-ingest could file rankine's card under whatever its rows happened to say.
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
                out.append(pl.ingest_scene(p, default_node=node_from_path(p, default_node),
                                           partial=scene_is_tail))
            else:
                out.append(pl.ingest_dets(p, default_node=node_from_path(p, default_node)))
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
        # ⚠️RECORDED ON EVERY ATTEMPT, NOT ONLY ON SUCCESS. The run that loses rows is often the
        # same run that reports an error, so filing the loss under `if r["ok"]` would hide it in
        # the one case it exists for. `None` means the measurement did not happen and is kept
        # distinct from 0, which means it happened and found nothing missing.
        measured = None if r.get("unfetched_unknown") else r.get("unfetched_bytes")
        s["last_unfetched_bytes"] = measured
        s["last_unfetched_reason"] = r.get("unfetched_reason")
        s["last_scene_missing"] = bool(r.get("scene_missing"))
        # A listing the node itself says it cut short is a PARTIAL CENSUS: scene_names() and
        # ls_candidates() both work off it, so it must not read as a short card.
        s["ls_truncated_at"] = r.get("ls_truncated_at")
        # ⚠️APPENDED, NOT OVERWRITTEN. The drain runs every 15 min and the check hourly, so a
        # per-run field is 4 runs stale by the time anything reads it and 3 of every 4 loss
        # reports were invisible to the gate. The ring keeps them; `check()` sums a window of it.
        ring = s.get("unfetched_recent")
        if not isinstance(ring, list):
            ring = []
        ring.append({"at": now, "bytes": measured, "reason": r.get("unfetched_reason"),
                     "missing": bool(r.get("scene_missing")),
                     # present only on a card-less run, so every other ring entry is unchanged
                     **({"no_card": True} if r.get("no_card") else {})})
        s["unfetched_recent"] = ring[-UNFETCHED_RING:]
        # ⚠️THE CLIP COUNTERS GO IN THE SAME RING, FOR THE SAME REASON. A cap that binds during a
        # burst is the failure mode the deadline creates, and `check` runs `17 * * * *` against a
        # drain on `*/15 * * * *` -- a per-run field is four runs stale before the gate reads it.
        # An unknown run carries None counts, never zeroes: it measured nothing.
        c_unknown = bool(r.get("clips_unknown"))
        cring = s.get("clips_recent")
        if not isinstance(cring, list):
            cring = []
        # ⚠️`refused` IS IN HERE BECAUSE THE GATE IS BLIND WITHOUT IT. A node answering every
        # /sd?file= with a truncated body produced `+0 fetched / 0 destroyed / 0 deferred` --
        # character-for-character what an empty card produces -- and `check` exited 0. The house
        # standard is that refusals are counted BY REASON where something reads them.
        cring.append({"at": now,
                      "seen": None if c_unknown else r.get("clips_seen"),
                      "fetched": None if c_unknown else r.get("clips_fetched"),
                      "gone": None if c_unknown else r.get("clips_gone"),
                      "deferred": None if c_unknown else r.get("clips_deferred_by_cap"),
                      "probed_404": None if c_unknown else r.get("clips_probed_404"),
                      "refused": None if c_unknown else (r.get("clips_refused") or {}),
                      "cap_reason": r.get("clips_cap_reason"),
                      "unknown": c_unknown,
                      **({"no_card": True} if r.get("no_card") else {}),
                      "reason": r.get("clips_reason")})
        s["clips_recent"] = cring[-UNFETCHED_RING:]
        if r.get("no_card"):
            s["no_card"] = True
            lring = s.get("live_recent")
            if not isinstance(lring, list):
                lring = []
            lring.append({"at": now, "rows": r.get("live_ring_rows"), "added": r.get("added"),
                          "lost": r.get("live_ring_lost"), "reason": r.get("live_ring_reason"),
                          "truncated": r.get("live_ring_truncated_bytes") is not None})
            s["live_recent"] = lring[-UNFETCHED_RING:]
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


def _unfetched_note(s: Dict[str, Any], now: float, max_unfetched_bytes: int,
                    window_s: float) -> Tuple[str, bool]:
    """(text for the check line, is-it-a-failure) for one sensor's reach-back accounting.

    ⚠️READS THE RING, NOT THE LAST RUN. `unfetched_recent` holds one entry per drain; the drain
    runs 4x as often as this check, so keying on `last_unfetched_bytes` discarded 3 of every 4
    measurements before anything looked at them. Every entry inside `window_s` counts.

    ⚠️THREE STATES, KEPT APART. A ring entry whose `bytes` is None is a run that MEASURED NOTHING
    -- an unreachable node, a refused identity, a failed `/ls`. It is reported as UNKNOWN with the
    run's own reason, never as a clean zero, and deliberately not fatal on its own: a check that
    fails on an unreachable node duplicates the staleness check below and cries wolf. A sensor
    with no ring at all is a heartbeat written before this existed and reads `n/a`.
    """
    ring = s.get("unfetched_recent")
    if not isinstance(ring, list) or not ring:
        # Fall back to the single pre-ring field so an old heartbeat still says something true.
        if "last_unfetched_bytes" not in s:
            return "  unfetched n/a", False
        if s.get("last_scene_missing"):
            why = s.get("last_unfetched_reason") or "the node served no scene file of any name"
            return "  NO SCENE FILE (%s)" % why, True
        v = s["last_unfetched_bytes"]
        if v is None:
            why = s.get("last_unfetched_reason") or "not measured"
            return "  unfetched UNKNOWN (%s)" % why, False
        if int(v) > max_unfetched_bytes:
            return "  UNFETCHED %d B of scene never asked for" % int(v), True
        return "", False

    recent = [e for e in ring if float(e.get("at") or 0) >= now - window_s]
    if not recent:
        return "  unfetched none in the last %.0f s" % window_s, False
    if all(e.get("no_card") for e in recent):
        return "  scene n/a (no card)", False
    recent = [e for e in recent if not e.get("no_card")]
    total = sum(int(e["bytes"]) for e in recent if e.get("bytes") is not None)
    unknown = [e for e in recent if e.get("bytes") is None]
    missing = [e for e in recent if e.get("missing")]
    lossy = [e for e in recent if e.get("bytes")]
    note, bad = "", False
    if missing:
        why = missing[-1].get("reason") or "the node served no scene file of any name"
        note += ("  NO SCENE FILE for %d of %d run(s) (%s)"
                 % (len(missing), len(recent), why))
        bad = True
    if total > max_unfetched_bytes:
        note += ("  UNFETCHED %d B of scene never asked for, by %d of the %d run(s) in the last "
                 "%.0f s" % (total, len(lossy), len(recent), window_s))
        bad = True
    if unknown:
        note += ("  unfetched UNKNOWN for %d of %d run(s) (%s)"
                 % (len(unknown), len(recent),
                    unknown[-1].get("reason") or "no reason recorded"))
    return note, bad


def _clip_note(s: Dict[str, Any], now: float, max_deferred: int, max_lost: int,
               window_s: float) -> Tuple[str, bool]:
    """(text for the check line, is-it-a-failure) for one sensor's clip accounting.

    Same three-state discipline as `_unfetched_note`, for the same reason: a run whose counts are
    None MEASURED NOTHING and is reported as UNKNOWN with its own reason, never as a clean zero.
    An unknown run is deliberately not fatal by itself -- the staleness check below already covers
    an unreachable node, and a gate that fires twice for one cause gets muted.

    ⚠️A MEASURED ZERO PRINTS. The whole point of this lane is that 478 clips were destroyed while
    every dashboard read green, so "0 fetched, 0 gone" is stated rather than left blank.

    ⚠️AND IT PRINTS HOW MANY WERE NAMED, AND WHY EACH REFUSAL HAPPENED. `+0 fetched` out of 6
    named and `+0 fetched` out of 0 named rendered identically before, so a node refusing every
    clip read exactly like an empty card. A `store_*` refusal FAILS: the bytes arrived and the
    pool could not write them, which is a fault on this side of the wire and never a quiet one.
    """
    ring = s.get("clips_recent")
    if not isinstance(ring, list) or not ring:
        return "  clips n/a", False
    recent = [e for e in ring if float(e.get("at") or 0) >= now - window_s]
    if not recent:
        return "  clips none in the last %.0f s" % window_s, False
    if all(e.get("no_card") for e in recent):
        return "  clips n/a (no card)", False
    recent = [e for e in recent if not e.get("no_card")]
    known = [e for e in recent if not e.get("unknown")]
    unknown = [e for e in recent if e.get("unknown")]
    fetched = sum(int(e.get("fetched") or 0) for e in known)
    gone = sum(int(e.get("gone") or 0) for e in known)
    deferred = sum(int(e.get("deferred") or 0) for e in known)
    named = sum(int(e.get("seen") or 0) for e in known)
    probed = sum(int(e.get("probed_404") or 0) for e in known)
    caps = [e.get("cap_reason") for e in known if e.get("cap_reason")]
    refused: Dict[str, int] = {}
    for e in known:
        for reason, n in (e.get("refused") or {}).items():
            refused[reason] = refused.get(reason, 0) + int(n)
    note, bad = "", False
    if known:
        note += ("  clips %d named: +%d fetched / %d destroyed / %d deferred over %d run(s)"
                 % (named, fetched, gone, deferred, len(known)))
        if probed:
            note += "  (%d name(s) 404 once, not yet confirmed destroyed)" % probed
    if refused:
        note += "  ⚠️REFUSED %s" % json.dumps(refused, sort_keys=True)
        if any(k.startswith("store_") for k in refused):
            note += " -- the bytes arrived and the pool could not write them"
            bad = True
    if max_deferred >= 0 and deferred > max_deferred:
        note += ("  ⚠️CAP BOUND: %d clip(s) left on the card unfetched (%s) -- they "
                 "are one eviction from gone"
                 % (deferred, ", ".join(sorted(set(caps))) or "no reason"))
        bad = True
    elif caps:
        note += "  (cap hit: %s)" % ", ".join(sorted(set(caps)))
    if max_lost >= 0 and gone > max_lost:
        note += "  ⚠️%d clip(s) were destroyed before this drain reached them" % gone
        bad = True
    if unknown:
        note += ("  clips UNKNOWN for %d of %d run(s) (%s)"
                 % (len(unknown), len(recent),
                    unknown[-1].get("reason") or "no reason recorded"))
    return note, bad


def _live_ring_note(s: Dict[str, Any], now: float, window_s: float) -> Tuple[str, bool]:
    """(text, is-it-a-failure) for a card-less node's live ring, with the same three states: a
    run whose loss is None measured nothing and is UNKNOWN, never a clean zero."""
    ring = s.get("live_recent")
    if not isinstance(ring, list) or not ring:
        return "", False
    recent = [e for e in ring if float(e.get("at") or 0) >= now - window_s]
    if not recent:
        return "  live ring none in the last %.0f s" % window_s, False
    rows = sum(int(e.get("rows") or 0) for e in recent)
    added = sum(int(e.get("added") or 0) for e in recent)
    lost = sum(int(e["lost"]) for e in recent if e.get("lost") is not None)
    unknown = [e for e in recent if e.get("lost") is None]
    note = "  live ring %d row(s) read, +%d new over %d run(s)" % (rows, added, len(recent))
    bad = False
    if lost > 0:
        note += ("  ⚠️LIVE RING LOST %d detection(s) the drain never read (/detections serves only "
                 "the newest %d)" % (lost, LIVE_RING_HTTP_MAX))
        bad = True
    cut = [e for e in recent if e.get("truncated")]
    if cut:
        note += ("  body TRUNCATED by the node in %d of %d run(s) (out of heap; newest rows "
                 "arrive on a later run)" % (len(cut), len(recent)))
    if unknown:
        note += ("  loss UNKNOWN for %d of %d run(s) (%s)"
                 % (len(unknown), len(recent), unknown[-1].get("reason") or "no reason recorded"))
    return note, bad


def check(root: str, max_stale_s: float = DEFAULT_MAX_STALE_S,
          now: Optional[float] = None,
          max_unfetched_bytes: int = DEFAULT_MAX_UNFETCHED_BYTES,
          unfetched_window_s: float = DEFAULT_UNFETCHED_WINDOW_S,
          max_clips_deferred: int = DEFAULT_MAX_CLIPS_DEFERRED,
          max_clips_lost: int = DEFAULT_MAX_CLIPS_LOST,
          nodes: Optional[Sequence[Tuple[str, str]]] = None,
          timeout: float = DEFAULT_TIMEOUT_S,
          max_ring_wall_span: float = DEFAULT_MAX_RING_WALL_SPAN) -> Tuple[int, List[str]]:
    """(exit code, lines). Non-zero on a stale sensor OR on a sensor that skipped scene bytes.

    ⚠️STALENESS WAS ONLY HALF OF IT. A drain whose HTTP succeeded, whose ingest succeeded and
    whose window silently started hours after the last one ended looks perfect here: it is fresh,
    it added rows, it exits 0. That is the case this check could not see. The reach-back
    measurement is already thresholded at one row by `scene_gap()`, so the default of 0 means
    "any whole row skipped" -- there is no acceptable number of scene rows nobody ever asked for.

    ⚠️AND IT HAS TO LOOK AT MORE THAN THE LAST RUN. deploy/k8s/hear-drain.yaml runs the drain
    `*/15 * * * *` and this check `17 * * * *`, so four drains land between two checks. Reading a
    field the drain overwrites each run meant 3 of every 4 measurements were gone before the gate
    saw them. `_unfetched_note` sums the heartbeat's ring over `unfetched_window_s` instead.

    When `nodes` are supplied, the same `--check` pass also polls each live node's `/status` and
    bare `/audio` to measure the ring wall span signal from docs/acoustic-stack.md section 0.2.
    """
    now = time.time() if now is None else now
    ring_polls = {name: poll_ring_wall_span(name, ip, timeout) for name, ip in (nodes or [])}
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
        gap_note, gap_bad = _unfetched_note(s, now, max_unfetched_bytes, unfetched_window_s)
        bad += gap_bad
        clip_note, clip_bad = _clip_note(s, now, max_clips_deferred, max_clips_lost,
                                         unfetched_window_s)
        bad += clip_bad
        ring_note, ring_bad = _ring_wall_span_note(ring_polls.get(name), max_ring_wall_span)
        bad += ring_bad
        live_note, live_bad = _live_ring_note(s, now, unfetched_window_s)
        bad += live_bad
        if s.get("ls_truncated_at"):
            # Not fatal: the fetch and the ingest are unaffected. But scene_names() and the clip
            # work list are both built from a listing the node says it cut short, so a run on a
            # partial census must not read as a run on a complete one.
            clip_note += ("  ls listing TRUNCATED at %d entries -- the census is partial"
                          % int(s["ls_truncated_at"]))
        last = s.get("last_success_s")
        if last is None:
            lines.append("%-10s NEVER succeeded (last error: %s)%s%s%s%s"
                         % (name, s.get("last_error"), gap_note, clip_note, live_note, ring_note))
            bad += 1
            continue
        age = now - float(last)
        state = "STALE" if age > max_stale_s else "ok"
        bad += state == "STALE"
        lines.append("%-10s %-5s last success %.0f s ago%s%s%s%s%s"
                     % (name, state, age, gap_note, clip_note, live_note, ring_note, "" if not s.get("last_error")
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
    ap.add_argument("--max-unfetched-bytes", type=int, default=DEFAULT_MAX_UNFETCHED_BYTES,
                    help="--check fails above this many scene bytes no fetch has asked for. "
                         "Sub-row gaps are already clamped to 0, so the default of 0 means "
                         "'any whole row skipped'")
    ap.add_argument("--max-ring-wall-span", type=float, default=DEFAULT_MAX_RING_WALL_SPAN,
                    help="--check fails above this live ring wall span ratio. `/status` raw and "
                         "bare `/audio` are both polled when --node is given, and each span is "
                         "divided by its OWN sample count per docs/acoustic-stack.md section 0.2")
    ap.add_argument("--unfetched-window-s", type=float, default=DEFAULT_UNFETCHED_WINDOW_S,
                    help="--check sums the heartbeat's per-run reach-back measurements over this "
                         "many seconds. Must cover every drain since the previous check ran or "
                         "the measurements in between are never looked at")
    ap.add_argument("--clip-max-per-node", type=int, default=CLIP_MAX_PER_NODE_DEFAULT,
                    help="most clips to fetch from one node in one run. The card physically "
                         "holds 6291456 // 480044 = 13, so a backlog is bounded at 13 however long "
                         "the drain was down. 0 disables the clip lane entirely")
    ap.add_argument("--clip-deadline-s", type=float, default=CLIP_DEADLINE_S_DEFAULT,
                    help="stop fetching clips from one node after this much wall clock. The "
                         "remainder is indexed `deferred_by_cap` and retried next run -- the "
                         "clip lane must not be able to overrun the CronJob's interval")
    ap.add_argument("--clip-store-max-b", type=int, default=CLIP_STORE_MAX_BYTES_DEFAULT,
                    help="audio byte cap for clips/. Pruned oldest UTC day first BEFORE each "
                         "fetch. Index and tag lines are never pruned")
    ap.add_argument("--max-clips-deferred", type=int, default=DEFAULT_MAX_CLIPS_DEFERRED,
                    help="--check fails above this many clips left unfetched by the cap over the "
                         "window. Deferral is a design invariant, so the default is 0")
    ap.add_argument("--max-clips-lost", type=int, default=DEFAULT_MAX_CLIPS_LOST,
                    help="--check fails above this many clips destroyed before the drain reached "
                         "them. -1 reports and never fails, which is the default: 666 are "
                         "already gone and a gate armed today fires on the backlog")
    ap.add_argument("--check", action="store_true",
                    help="report sensor staleness from the heartbeat and exit; drains nothing")
    ap.add_argument("--ingest", action="append", default=[], metavar="PATH",
                    help="backfill a dets.csv / sketches-*.jsonl, or a directory of them; "
                         "repeatable and idempotent. Node name is taken from the file when it "
                         "carries one and from --ingest-node otherwise")
    ap.add_argument("--ingest-scene-tails", action="store_true",
                    help="the scene.csv files named by --ingest are archived byte-range TAILS "
                         "(what hear-drain's archive/ holds), so a leading part-row is dropped "
                         "and reported rather than refusing the file. Do NOT pass it for whole "
                         "files: a whole file that starts mid-row is corruption")
    ap.add_argument("--ingest-node", default=None,
                    help="node name for backfilled dets.csv files whose schema carries none "
                         "(G1-G3). Recorded as asserted, not as read from the file")
    ap.add_argument("--stats", action="store_true", help="print what the pool holds and exit")
    ap.add_argument("--json", action="store_true", help="machine-readable run report on stdout")
    a = ap.parse_args(argv)

    root = os.path.expanduser(a.pool)
    nodes: List[Tuple[str, str]] = []
    for spec in a.node:
        if "=" not in spec:
            ap.error("--node wants NAME=IP, got %r" % spec)
        name, ip = spec.split("=", 1)
        nodes.append((name.strip(), ip.strip()))
    if a.check:
        code, lines = check(root, a.max_stale_s, max_unfetched_bytes=a.max_unfetched_bytes,
                            unfetched_window_s=a.unfetched_window_s,
                            max_clips_deferred=a.max_clips_deferred,
                            max_clips_lost=a.max_clips_lost, nodes=nodes, timeout=a.timeout,
                            max_ring_wall_span=a.max_ring_wall_span)
        print("\n".join(lines))
        return code

    pl = P.Pool(root)
    if a.stats:
        print(json.dumps({"sketches": pl.stats(), "scene": pl.scene_stats()},
                         indent=2, sort_keys=True))
        return 0

    if a.ingest:
        entries = backfill(pl, a.ingest, default_node=a.ingest_node,
                           scene_is_tail=a.ingest_scene_tails)
        for e in entries:
            print("%-46s %s" % (os.path.basename(e["path"]),
                                {k: e[k] for k in ("generation", "rows", "added", "duplicate",
                                                   "skipped")
                                 if k in e} if "error" not in e else "REFUSED " + e["error"]))
        print("\n" + json.dumps(pl.stats(), indent=2, sort_keys=True))
        return 0 if all("error" not in e for e in entries) else 1

    # ⚠️ONE NODE AT A TIME. A list comprehension, not a thread pool: the ESP32 serves one client
    # and refuses the rest, so parallelism here turns slow runs into refused runs.
    results = [drain_node(pl, n, ip, a.timeout,
                          clip_max_per_node=a.clip_max_per_node,
                          clip_deadline_s=a.clip_deadline_s,
                          clip_store_max_bytes=a.clip_store_max_b) for n, ip in nodes]
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
            if r.get("no_card"):
                lost = r.get("live_ring_lost")
                print("    no card: live ring %s row(s) read, %s%s"
                      % (r.get("live_ring_rows"),
                         "%d lost" % lost if lost is not None
                         else "loss UNMEASURED (%s)" % r.get("live_ring_reason"),
                         "  body TRUNCATED by the node at %d B, %s newer row(s) left for a later "
                         "run" % (r["live_ring_truncated_bytes"], r.get("live_ring_pending"))
                         if r.get("live_ring_truncated_bytes") is not None else ""))
            if r.get("clips_unknown"):
                print("    clips UNMEASURED this run, not clean: %s" % r.get("clips_reason"))
            elif r.get("clips_seen"):
                print("    clips %d named: +%d fetched (%.1f MB), %d already held, %d already "
                      "gone, %d destroyed now, %d 404 once, %d deferred%s%s"
                      % (r["clips_seen"], r["clips_fetched"], r["clips_bytes"] / 1e6,
                         r["clips_already_held"], r["clips_already_gone"], r["clips_gone"],
                         r["clips_probed_404"], r["clips_deferred_by_cap"],
                         "  CAP HIT (%s)" % r["clips_cap_reason"] if r["clips_cap_hit"] else "",
                         "  refused %s" % r["clips_refused"] if r["clips_refused"] else ""))
            if r.get("ls_truncated_at"):
                print("    ⚠️/ls stopped at %d entries -- this census is PARTIAL, not a short "
                      "card; scene files and clip names past that point were not seen"
                      % r["ls_truncated_at"])
            if r.get("ls_error"):
                print("    /ls failed after %d attempt(s) (%s) -- reach-back UNMEASURED this "
                      "run, not clean" % (r.get("ls_attempts", 1), r["ls_error"]))
            elif r.get("ls_retried"):
                # Succeeded, but not first time. Contention worth seeing before it becomes loss.
                # ⚠️STATES THE MEASUREMENT, NOT THE CAUSE. Concurrent-client refusal is the
                # mechanism seen on nyquist, but any transport fault retries the same way and a
                # log line must not name a cause it did not establish.
                print("    /ls needed %d attempts -- the node did not answer first time"
                      % r["ls_retried"])
            elif r.get("unfetched_unknown"):
                print("    reach-back UNMEASURED this run, not clean: %s"
                      % r.get("unfetched_reason"))
            for f in r["files"]:
                if f.get("absent"):
                    print("    %-14s absent" % f["name"])
                elif f.get("ingested"):
                    print("    %-15s %s %6d row(s) -> +%-6d new, %6d dup, %d skipped %s%s"
                          % (f["name"], f["generation"], f["rows"], f["added"], f["duplicate"],
                             f["skipped"], f["skip_reasons"] or "",
                             "  [tail]" if f.get("partial_first_line") else ""))
                    if f.get("live_ring"):
                        continue
                    g = f.get("gap") or {}
                    if g.get("unfetched_bytes") is None and not g.get("whole_file"):
                        print("      reach-back UNKNOWN: /ls gave no size for this file")
                    elif g.get("unfetched_bytes"):
                        print("      ⚠️%d B (~%s row(s)) of this file were never fetched by any "
                              "run and will roll off the card"
                              % (g["unfetched_bytes"], g.get("unfetched_rows_est")))
                else:
                    print("    %-14s %d B archived" % (f["name"], f["bytes"]))
        if phone is not None:
            print("phones    %-4s +%d record(s)%s"
                  % ("ok" if phone.get("ok") else "FAIL", phone["added"],
                     "" if phone.get("ok") else "  " + "; ".join(phone["errors"])))
        s = report["stats"]
        print("\npool %s\n  sketches %d  %s  (%d anchored / %d not)"
              % (root, s["records"], s["by_source"], s["anchored"], s["unanchored"]))
        # ⚠️`anchored` above answers "is there a stamp", not "is the stamp a measurement". For a
        # phone those differ: a wall-tier row is anchored and is ~50 ms out. Printed only when
        # phone rows exist, so the node-only drain this runs as today is unchanged.
        if s.get("by_clock_tier"):
            print("  phone clock %s  utc_trusted %s"
                  % (s["by_clock_tier"], s["phone_utc_trusted"]))
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
