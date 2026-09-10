#!/usr/bin/env python3
"""The clip store: names off a node, WAV bytes into the pool, and one append-only index of both.

⚠️NOT ONE CLIP HAD EVER LEFT A NODE. Measured 2026-09-09 across the fleet: 625 clips written,
~478 already destroyed. Every node writes 4.0 s WAVs into /clips against a 6,291,456 B budget --
exactly 49 files of 128,044 B -- and evicts to make room. The pool is the archive; the card is a
buffer that is already full on all three nodes (`budget_left_clips 0`).

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

import hashlib
import json
import os
import re
import shutil
from typing import Any, Dict, Iterable, List, Optional

CLIP_SCHEMA_VERSION = 1
#: 44-byte canonical header + 64000 samples * 2 bytes. night_node.ino CLIP_BYTES.
CLIP_BYTES = 128044
CLIP_PRE_S = 1.0
CLIP_POST_S = 3.0
CLIP_DIR = "/clips"
INDEX_NAME = "index.jsonl"
FS_NOMINAL_HZ = 16000.0
#: Measured header spread is 15986-16000 Hz; 64 Hz is 4x the observed 14 Hz.
FS_TOLERANCE_HZ = 64.0

#: Every terminal and non-terminal state one clip can be in. `index_row` refuses anything else --
#: an outcome that is not on this list cannot be counted, and an uncounted refusal is the failure
#: this whole lane exists to prevent.
OUTCOMES = ("stored", "already_held", "evicted_before_fetch", "refused_bad_body",
            "refused_short", "refused_http", "deferred_by_cap", "refused_name")

#: Once an index row reaches one of these, the name is never probed again. Without it the drain
#: re-probes 321 already-dead nyquist names every 15 minutes forever (measured 2026-09-09).
TERMINAL_OUTCOMES = ("stored", "evicted_before_fetch")

#: ⚠️BOTH SHIPPED SHAPES. The flashed fleet writes `<node>-<boot>-<sample>.wav`; the checkout's
#: clip_name() prepends `%02u-` priority. A parser that knew only one would refuse the entire
#: live fleet or the entire next reflash.
_NAME_RE = re.compile(r"^(?:(\d{2})-)?([A-Za-z0-9_]+)-([0-9a-fA-F]+)-(\d+)\.wav$")
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
    m = _NAME_RE.match(basename)
    if not m:
        raise ValueError("clip basename %r is not <prio->?<node>-<boot>-<sample>.wav" % basename)
    prio, node, boot, sample = m.groups()
    return {"raw": raw, "basename": basename, "node": node, "boot": boot,
            "sample": int(sample), "prio": None if prio is None else int(prio)}


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

    ⚠️IT DOES NOT REQUIRE 16000 Hz. mach shipped a whole boot headed 22624 Hz, and refusing it
    here would delete the evidence of the fs_clean latch bug rather than record it. The rate is
    REPORTED; whether it is usable is the tagger's decision, not the store's.
    """
    out: Dict[str, Any] = {"ok": False, "reason": None, "fs_hz": None, "channels": None,
                           "bits": None, "data_bytes": None, "total_bytes": len(body)}
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
    if len(body) != CLIP_BYTES:
        out["reason"] = "total_%d_expected_%d" % (len(body), CLIP_BYTES)
        return out
    out["ok"] = True
    return out


def index_row(*, clip: str, parts: Optional[Dict[str, Any]], node: str, body: Optional[bytes],
              probe: Optional[Dict[str, Any]], dets: Dict[str, Any], path: Optional[str],
              outcome: str, fetched_at: float, reason: Optional[str] = None) -> Dict[str, Any]:
    """One index line.

    ⚠️IT NEVER INVENTS A TIME. Every time field is copied from the parent dets row or left None,
    and `t_start/t_end` are null for an unanchored clip rather than derived from `fetched_at`.
    They exist at all so no consumer re-derives CLIP_PRE_SAMPLES for itself.

    ⚠️`fs_hz` AND `wav_header_fs_hz` TRAVEL SIDE BY SIDE, ALWAYS. The CSV's estimate and the
    file's own header disagreed by 6,624 Hz for a whole boot on mach. Keeping one would have made
    that invisible; keeping the disagreement is the point.
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
        "trigger": dets.get("trigger"),
        "clip_why": dets.get("clip_why"),
        "dets_origin": dets.get("dets_origin"),
        "record_key": dets.get("record_key"),
        "fetched_at": fetched_at,
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
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                out.append({"day": day, "node": node, "basename": fn, "path": p, "bytes": sz})
    return out


def audio_bytes(root: str) -> int:
    return sum(f["bytes"] for f in _audio_files(root))


def _day_order(day: str) -> tuple:
    """Oldest UTC day first. `unanchored` sorts LAST because it has no day to compare and any
    ordering imposed on it would be a guess presented as a measurement."""
    return (1, "") if day == "unanchored" else (0, day)


def prune(root: str, max_bytes: int, now: float) -> Dict[str, Any]:
    """Delete whole WAV files, oldest UTC day first, until the audio tree is at or under max_bytes.

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
    if max_bytes is None or before <= max_bytes:
        return out
    by_basename = {r.get("basename"): r for r in read_index(root).values() if r.get("basename")}
    files.sort(key=lambda f: (_day_order(f["day"]), f["node"], f["basename"]))
    running, deleted, days, rows = before, 0, [], []
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
        row = by_basename.get(f["basename"])
        if row is not None:
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
