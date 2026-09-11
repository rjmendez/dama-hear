#!/usr/bin/env python3
"""The clip store: names off a node, WAV bytes into the pool, and one append-only index of both.

Every node writes 5.0 s 48 kHz WAVs into /clips, a rolling window of 13 files (6,291,456 B of
480,044 B each) that evicts its oldest to make room. The drain fetches a clip before it rolls off,
and the pool holds it for identification under its own byte cap.

WHAT THIS MODULE IS FOR, and what it deliberately is not:

  * it turns a node-supplied string into a local path, in ONE place, refusing everything else
  * it probes the 44-byte WAV header without decoding audio, and names every way that can fail
  * it keeps `clips/index.jsonl`, which is the record of what arrived AND of what was destroyed

⚠️THE INDEX IS THE DURABLE RECORD; THE AUDIO IS A CACHE. `prune()` deletes WAVs and never index
lines. A clip the node has already evicted cannot be re-fetched from anywhere, so the row that
says it existed, when, on which node and why it did not arrive is the only thing left of it.

⚠️IDENTITY IS THE NAME, NOT THE CONTENT. `clip_key` hashes node + boot + sample, which is unique
by construction and is known BEFORE the bytes are -- and skipping a fetch is the entire cost this
module exists to avoid. A content hash would collide two clips of the same silence and drop a
real timestamped event. hear/pool.py:71 solved the same problem the same way.
"""
from __future__ import annotations

from . import sketch as SK

import hashlib
import json
import os
import re
import shutil
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: 3 drops `header_rate_suspect`: one clip geometry, and old clips roll off.
CLIP_SCHEMA_VERSION = 3
#: What a clip may be, as a duration: the header proves the body, not a byte count.
CLIP_MIN_S, CLIP_MAX_S = 0.5, 30.0
CLIP_PRE_S = 1.0
CLIP_POST_S = 4.0
#: The firmware writes exactly this many seconds per clip.
CLIP_TOTAL_S = CLIP_PRE_S + CLIP_POST_S
#: Slack on a header-derived duration: the header's integer rate against the node's clock.
CLIP_DUR_TOL = 0.02
CLIP_DIR = "/clips"
INDEX_NAME = "index.jsonl"

#: Every terminal and non-terminal state one clip can be in. `index_row` refuses anything else --
#: an outcome that is not on this list cannot be counted, and an uncounted refusal is the failure
#: this whole lane exists to prevent.
OUTCOMES = ("stored", "already_held", "evicted_before_fetch", "refused_bad_body",
            "refused_short", "refused_http", "deferred_by_cap", "refused_name",
            "probed_404", "refused_store")

#: Once an index row reaches one of these, the name is never probed again. Without it the drain
#: re-probes 321 already-dead nyquist names every 15 minutes forever (measured 2026-09-09).
#: ⚠️`probed_404` IS DELIBERATELY NOT HERE. hear_node.ino:2436 answers 404 for ANY failed
#: SD.open, not only for a missing file: max_files is 8 and the long-lived set reaches 6
#: (dets.csv + scene.csv + the open clip + /ls's directory and entry + health.csv) with
#: the pre-FIFO firmware's clip_evict_worse_than() taking 2 more during an eviction. One
#: descriptor-exhausted moment must not retire a clip that is still on the card.
TERMINAL_OUTCOMES = ("stored", "evicted_before_fetch")

#: Consecutive 404s before a name is called destroyed. 2, not 1: a 404 costs ~0.1 s, so
#: confirming the 321 already-dead nyquist names costs one extra ~32 s pass, once.
CONFIRM_404 = 2

#: ⚠️EVERY SHIPPED SHAPE. The oldest firmware wrote `<node>-<8 hex boot>-<sample>.wav`, the
#: priority-eviction firmware prepended `%02u-`, and the FIFO firmware writes a 12 hex boot
#: (6 hex boot sequence + 6 random). Older names stay on a card until they are evicted.
#: A node id may contain dashes: gen_secrets.py allows them and a build without NODE_ID names
#: itself `hear-<mac tail>` (rankine's 30 `hear-5c4c94` clips were refused as bad_name).
_NODE = r"[A-Za-z0-9_][A-Za-z0-9_-]*"
_FIFO_RE = re.compile(r"^(%s)-([0-9a-f]{12})-(\d{10})\.wav$" % _NODE)
_NAME_RE = re.compile(r"^(?:(\d{2})-)?(%s)-([0-9a-fA-F]+)-(\d+)\.wav$" % _NODE)
_MAX_BASENAME = 64


def parse_clip_name(name: str) -> Dict[str, Any]:
    """A node-side clip path -> its parts. Raises ValueError on anything else.

    ⚠️THIS IS THE ONLY PLACE A NODE-SUPPLIED STRING BECOMES A LOCAL PATH. The dets.csv `clip`
    column is written by firmware and read by a process with the PVC mounted; `..`, a nested
    separator and a non-.wav suffix are all refused HERE so no caller has to remember to. Callers
    build local paths from `basename`, never from `raw`.
    """
    if not isinstance(name, str):
        raise ValueError("clip name is %r, not a string" % (name,))
    raw = name.strip()
    if not raw:
        raise ValueError("empty clip name")
    if ".." in raw:
        raise ValueError("clip name %r contains '..'" % raw)
    if not raw.startswith(CLIP_DIR + "/"):
        raise ValueError("clip name %r is not under %s/" % (raw, CLIP_DIR))
    basename = raw[len(CLIP_DIR) + 1:]
    if "/" in basename:
        raise ValueError("clip name %r nests below %s/" % (raw, CLIP_DIR))
    if len(basename) > _MAX_BASENAME:
        raise ValueError("clip basename is %d chars, over the %d cap" % (len(basename),
                                                                        _MAX_BASENAME))
    m = _FIFO_RE.match(basename)
    if m:
        prio, (node, boot, sample) = None, m.groups()
    else:
        m = _NAME_RE.match(basename)
        if not m:
            raise ValueError("clip basename %r is not <prio->?<node>-<boot>-<sample>.wav" % basename)
        prio, node, boot, sample = m.groups()
    return {"raw": raw, "basename": basename, "node": node, "boot": boot,
            "sample": int(sample), "prio": None if prio is None else int(prio)}


_FIFO_TAIL_RE = re.compile(r"-([0-9a-fA-F]{12})-(\d{10})\.wav$")


def eviction_key(basename: str) -> Tuple[Any, ...]:
    """Sort key that orders clips the way the node evicts them, oldest first.

    Mirrors firmware/hear_node/clip_order.h (tests/test_clip_eviction.py runs both on the same
    names): any name not of the FIFO shape first, by name; then by (boot sequence, sample).
    """
    m = _FIFO_TAIL_RE.search(basename)
    if m is None or m.start() == 0 or int(m.group(2)) > 0xFFFFFFFF:
        return (0, basename)
    return (1, int(m.group(1)[:6], 16), int(m.group(2)), basename)


def clip_key(node: str, boot: str, sample: int) -> str:
    """The identity of one clip. Same shape as hear.pool.key(): sha256 truncated to 32 hex."""
    h = hashlib.sha256()
    for part in ("clip", node, boot, str(sample)):
        h.update(part.encode())
        h.update(b"\x1f")
    return h.hexdigest()[:32]


def bad_name_key(node: str, raw: str) -> str:
    """Identity for a dets cell that would not parse, so it can be counted and de-duplicated.

    It gets a key of its own rather than being dropped: a name the firmware wrote and this parser
    refuses is a firmware/parser disagreement, and one that vanished from the census would be
    indistinguishable from a clip that was never written.
    """
    h = hashlib.sha256()
    for part in ("clip-badname", node, raw):
        h.update(part.encode())
        h.update(b"\x1f")
    return h.hexdigest()[:32]


def _day(ts_utc_s: Optional[float]) -> str:
    """UTC day partition, hear.pool._day() semantics: `unanchored` rather than a guessed day."""
    if not ts_utc_s:
        return "unanchored"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts_utc_s, _dt.timezone.utc).strftime("%Y-%m-%d")


def clips_dir(root: str) -> str:
    return os.path.join(root, "clips")


def store_path(root: str, day: str, node: str, basename: str) -> str:
    return os.path.join(clips_dir(root), day, node, basename)


def index_path(root: str) -> str:
    return os.path.join(clips_dir(root), INDEX_NAME)


def wav_probe(body: bytes) -> Dict[str, Any]:
    """Parse the 44-byte canonical header. No audio is decoded.

    The rate is REPORTED, not required; whether it is usable is the tagger's decision, not the
    store's.
    """
    out: Dict[str, Any] = {"ok": False, "reason": None, "fs_hz": None, "channels": None,
                           "bits": None, "data_bytes": None, "total_bytes": len(body), "dur_s": None, "fs_nameable": None}
    if len(body) < 44:
        out["reason"] = "header_short_%d" % len(body)
        return out
    if body[0:4] != b"RIFF":
        out["reason"] = "not_riff"
        return out
    if body[8:12] != b"WAVE":
        out["reason"] = "not_wave"
        return out
    if body[12:16] != b"fmt ":
        out["reason"] = "no_fmt_chunk"
        return out
    fmt = int.from_bytes(body[20:22], "little")
    out["channels"] = int.from_bytes(body[22:24], "little")
    out["fs_hz"] = int.from_bytes(body[24:28], "little")
    out["bits"] = int.from_bytes(body[34:36], "little")
    if body[36:40] != b"data":
        out["reason"] = "no_data_chunk"
        return out
    out["data_bytes"] = int.from_bytes(body[40:44], "little")
    if fmt != 1:
        out["reason"] = "audio_format_%d" % fmt
        return out
    if out["channels"] != 1:
        out["reason"] = "channels_%d" % out["channels"]
        return out
    if out["bits"] != 16:
        out["reason"] = "bits_%d" % out["bits"]
        return out
    if 44 + out["data_bytes"] != len(body):
        out["reason"] = "data_len_%d_body_%d" % (out["data_bytes"], len(body))
        return out
    # ⚠️THE HEADER ALREADY PROVED THE BODY. `44 + data_bytes == len(body)` above catches a
    # truncated or padded fetch, so what is left to check is whether this is a PLAUSIBLE CLIP --
    # a question about duration and rate, not about one firmware's byte count.
    # A rate this format cannot name is reported, not refused: `fs_nameable` travels with the row
    # and the tagger decides, the same rule the index follows by carrying both the header rate and
    # the dets rate.
    if not out["fs_hz"]:
        out["reason"] = "fs_zero"
        return out
    out["fs_nameable"] = SK.fs_code(float(out["fs_hz"])) != 0
    out["dur_s"] = out["data_bytes"] / float(out["fs_hz"] * 2)
    if not (CLIP_MIN_S <= out["dur_s"] <= CLIP_MAX_S):
        out["reason"] = "duration_%.3f_s_outside_%.1f_%.1f" % (out["dur_s"], CLIP_MIN_S, CLIP_MAX_S)
        return out
    out["ok"] = True
    return out


def index_row(*, clip: str, parts: Optional[Dict[str, Any]], node: str, body: Optional[bytes],
              probe: Optional[Dict[str, Any]], dets: Dict[str, Any], path: Optional[str],
              outcome: str, fetched_at: float, reason: Optional[str] = None,
              probe_404s: int = 0) -> Dict[str, Any]:
    """One index line.

    ⚠️IT NEVER INVENTS A TIME. Every time field is copied from the parent dets row or left None,
    and `t_start/t_end` are null for an unanchored clip rather than derived from `fetched_at`.
    They exist at all so no consumer re-derives CLIP_PRE_SAMPLES for itself.

    `fs_hz` and `wav_header_fs_hz` travel side by side, so a disagreement between them is visible.
    """
    if outcome not in OUTCOMES:
        raise ValueError("unknown clip outcome %r; known: %s" % (outcome, ", ".join(OUTCOMES)))
    ts = dets.get("ts_utc_s")
    anchored = bool(dets.get("anchored"))
    row: Dict[str, Any] = {
        "schema_version": CLIP_SCHEMA_VERSION,
        "clip_key": (clip_key(parts["node"], parts["boot"], parts["sample"]) if parts
                     else bad_name_key(node, clip)),
        "clip": clip,
        "basename": parts["basename"] if parts else None,
        "node": node,
        "boot": parts["boot"] if parts else None,
        "sample": parts["sample"] if parts else None,
        "prio": parts["prio"] if parts else None,
        "outcome": outcome,
        "reason": reason,
        "path": path,
        "bytes": len(body) if body is not None else None,
        "sha256": hashlib.sha256(body).hexdigest() if body is not None else None,
        "utc_us": dets.get("utc_us"),
        "ts_utc_s": ts,
        "anchored": anchored,
        "t_start_utc_s": (ts - CLIP_PRE_S) if (anchored and ts) else None,
        "t_end_utc_s": (ts + CLIP_POST_S) if (anchored and ts) else None,
        "uptime_s": dets.get("uptime_s"),
        "fs_hz": dets.get("fs_hz"),
        "wav_header_fs_hz": (probe or {}).get("fs_hz"),
        "dur_s": (probe or {}).get("dur_s"),
        "trigger": dets.get("trigger"),
        "clip_why": dets.get("clip_why"),
        "dets_origin": dets.get("dets_origin"),
        "record_key": dets.get("record_key"),
        "fetched_at": fetched_at,
        "probe_404s": probe_404s,
    }
    return row


def read_index(root: str) -> Dict[str, Dict[str, Any]]:
    """clip_key -> the LAST line for that key. One pass, the way Pool.keys() does it.

    An unreadable line is skipped rather than fatal: the index is append-only and a torn tail
    from a killed pod must not make the whole negative cache unreadable, which would re-probe
    every dead name on the card.
    """
    p = index_path(root)
    out: Dict[str, Dict[str, Any]] = {}
    if not os.path.exists(p):
        return out
    with open(p, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            k = row.get("clip_key")
            if isinstance(k, str):
                out[k] = row
    return out


def read_outcomes(root: str) -> Dict[str, Dict[str, Any]]:
    """clip_key -> just the two fields the negative cache needs. The SAME last-line-wins pass as
    `read_index`, at a measured 191 B resident per key against that function's 3,096 B.

    ⚠️THE DRAIN MUST NOT MATERIALISE THE WHOLE INDEX. The index is append-only and never
    compacted; at the spec's 1,109 clips/day the full-row dict reaches the drain container's
    512Mi limit in ~152 days and the drain then OOMKills, which returns the fleet to destroying
    every clip. The same walk at 191 B/key is ~7 years, and nothing here reads a field the
    dispatch loop does not use.
    """
    p = index_path(root)
    out: Dict[str, Dict[str, Any]] = {}
    if not os.path.exists(p):
        return out
    with open(p, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            k = row.get("clip_key")
            if isinstance(k, str):
                out[k] = {"outcome": row.get("outcome"), "probe_404s": row.get("probe_404s") or 0}
    return out


def rows_for_basenames(root: str, names) -> Dict[str, Dict[str, Any]]:
    """basename -> its LAST index row, for the given basenames only.

    Same reason as `read_outcomes`: `prune` needs whole rows, but only for the handful of files it
    actually deleted, so it streams the index rather than holding all of it.
    """
    want = set(names)
    out: Dict[str, Dict[str, Any]] = {}
    p = index_path(root)
    if not want or not os.path.exists(p):
        return out
    with open(p, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            b = row.get("basename")
            if b in want:
                out[b] = row
    return out


def append_index(root: str, rows: Iterable[Dict[str, Any]]) -> int:
    p = index_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    n = 0
    with open(p, "a") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
            n += 1
    return n


def _audio_files(root: str) -> List[Dict[str, Any]]:
    """Every stored WAV under clips/<day>/<node>/, with its day, node and size."""
    base = clips_dir(root)
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(base):
        return out
    for day in sorted(os.listdir(base)):
        d = os.path.join(base, day)
        if not os.path.isdir(d):
            continue
        for node in sorted(os.listdir(d)):
            nd = os.path.join(d, node)
            if not os.path.isdir(nd):
                continue
            for fn in sorted(os.listdir(nd)):
                if not fn.endswith(".wav"):
                    continue
                p = os.path.join(nd, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append({"day": day, "node": node, "basename": fn, "path": p,
                            "bytes": st.st_size, "mtime": st.st_mtime})
    return out


def sweep_tmp(root: str, now: float, older_than_s: float = 900.0) -> Dict[str, Any]:
    """Delete abandoned `*.tmp` part-files under clips/<day>/<node>/.

    ⚠️`_audio_files` FILTERS ON `.wav`, SO A `.wav.<pid>.tmp` IS INVISIBLE TO THE CAP. A pod
    killed by `activeDeadlineSeconds: 780` mid-write leaks up to 480044 B per node per run onto a
    5Gi PVC that nothing else would ever reclaim. `older_than_s` is one drain interval, and the
    pid in the name means a live writer's file is never the one being swept.
    """
    base = clips_dir(root)
    out: Dict[str, Any] = {"tmp_deleted": 0, "tmp_bytes": 0}
    if not os.path.isdir(base):
        return out
    for day in sorted(os.listdir(base)):
        d = os.path.join(base, day)
        if not os.path.isdir(d):
            continue
        for node in sorted(os.listdir(d)):
            nd = os.path.join(d, node)
            if not os.path.isdir(nd):
                continue
            for fn in sorted(os.listdir(nd)):
                if not fn.endswith(".tmp"):
                    continue
                p = os.path.join(nd, fn)
                try:
                    st = os.stat(p)
                    if now - st.st_mtime < older_than_s:
                        continue
                    os.remove(p)
                except OSError:
                    continue
                out["tmp_deleted"] += 1
                out["tmp_bytes"] += st.st_size
    return out


def audio_bytes(root: str) -> int:
    return sum(f["bytes"] for f in _audio_files(root))


def _day_order(day: str) -> tuple:
    """Oldest UTC day first. `unanchored` sorts LAST because it has no day to compare and any
    ordering imposed on it would be a guess presented as a measurement."""
    return (1, "") if day == "unanchored" else (0, day)


def _prune_order(f: Dict[str, Any]) -> tuple:
    """Deletion order: `unanchored` FIRST, by file mtime, then the dated days oldest first.

    ⚠️THE OPPOSITE OF `_day_order`, AND DELIBERATELY. An anchored clip carries utc_us,
    t_start/t_end and a record_key that joins it to its sketch and its scene rows; an unanchored
    one carries none of that and joins to nothing. Sorting unanchored last paid the whole cap out
    of the joinable clips and protected the ones with the least recoverable context. Within
    `unanchored` the order is mtime -- a measured arrival time, not a day guessed from nothing.
    """
    if f["day"] == "unanchored":
        return (0, f["mtime"], f["node"], f["basename"])
    return (1, f["day"], f["node"], f["basename"])


def prune(root: str, max_bytes: int, now: float) -> Dict[str, Any]:
    """Delete whole WAV files, `unanchored` first and then oldest UTC day, until the audio tree
    is at or under max_bytes. Abandoned `*.tmp` part-files are swept on every call, cap or no cap.

    ⚠️AUDIO IS THE ONLY PRUNABLE THING. The index row survives and gains `audio_pruned_at`, which
    is appended as a new line (the index is append-only and `read_index` keeps the last line per
    key) rather than rewritten in place. The row's outcome stays `stored`, so a pruned clip is
    still never re-probed: the node destroyed it long ago and the 404 would cost a request to
    learn nothing.
    """
    files = _audio_files(root)
    before = sum(f["bytes"] for f in files)
    out: Dict[str, Any] = {"bytes_before": before, "bytes_after": before,
                           "files_deleted": 0, "days_touched": []}
    out.update(sweep_tmp(root, now))
    if max_bytes is None or before <= max_bytes:
        return out
    files.sort(key=_prune_order)
    running, deleted, days, gone = before, 0, [], []
    for f in files:
        if running <= max_bytes:
            break
        try:
            os.remove(f["path"])
        except OSError:
            continue
        running -= f["bytes"]
        deleted += 1
        if f["day"] not in days:
            days.append(f["day"])
        gone.append(f["basename"])
    rows = []
    for row in rows_for_basenames(root, gone).values():
        r = dict(row)
        r["audio_pruned_at"] = now
        rows.append(r)
    if rows:
        append_index(root, rows)
    out["bytes_after"] = running
    out["files_deleted"] = deleted
    out["days_touched"] = days
    return out


def free_bytes(root: str) -> int:
    """Free bytes on the filesystem holding the pool. The clip lane refuses to fill a PVC."""
    os.makedirs(root, exist_ok=True)
    return shutil.disk_usage(root).free
