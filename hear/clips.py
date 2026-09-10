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

from . import sketch as SK

import hashlib
import json
import os
import re
import shutil
from typing import Any, Dict, Iterable, List, Optional

#: 2 adds `dur_s` and `header_rate_suspect`. ⚠️A v1 row carries neither, and v1 rows DO include
#: mis-headed 48 kHz clips -- they were stored before this field existed. A reader must not take
#: bytes / wav_header_fs_hz at face value for a v1 row; hear/tags.py `_post_s` re-derives the
#: length by the same uniqueness rule header_rate_suspect() uses.
CLIP_SCHEMA_VERSION = 2
#: 44-byte canonical header + 64000 samples * 2 bytes, at the 16 kHz / 4.0 s geometry.
#: ⚠️KEPT ONLY AS THE HISTORICAL SIZE. It is NOT a validity test any more -- see wav_probe.
CLIP_BYTES_16K_4S = 128044

#: What a clip may be, as a DURATION rather than a byte count. The node's geometry has already
#: changed once -- 4.0 s at 16 kHz became 5.0 s at 48 kHz, 128044 B to 480044 B -- and the fixed
#: total here did not move with it, so the drain refused every clip the fleet wrote
#: ("total_480044_expected_128044") while reporting itself healthy. A magic number layered on a
#: SELF-DESCRIBING format is what made a firmware change into silent data loss.
CLIP_MIN_S, CLIP_MAX_S = 0.5, 30.0
CLIP_PRE_S = 1.0
#: ⚠️A FALLBACK, NOT THE TRUTH. Both clip geometries are live in the corpus at once: the 16 kHz
#: era wrote 1.0 + 3.0 s (128044 B) and the 48 kHz firmware writes 1.0 + 4.0 s (480044 B, sized
#: for Perch's non-overlapping 5 s window). Pinning one number here is the same mistake
#: CLIP_BYTES_16K_4S was -- so `clip_total_s()` reads it off the clip and this is only what a row
#: with no body falls back to. The PRE-roll is 1.0 s in both, which is what makes the split
#: derivable at all.
CLIP_POST_S = 4.0
CLIP_DIR = "/clips"
INDEX_NAME = "index.jsonl"
FS_NOMINAL_HZ = 16000.0
#: Measured header spread is 15986-16000 Hz; 64 Hz is 4x the observed 14 Hz.
FS_TOLERANCE_HZ = 64.0

#: Every terminal and non-terminal state one clip can be in. `index_row` refuses anything else --
#: an outcome that is not on this list cannot be counted, and an uncounted refusal is the failure
#: this whole lane exists to prevent.
OUTCOMES = ("stored", "already_held", "evicted_before_fetch", "refused_bad_body",
            "refused_short", "refused_http", "deferred_by_cap", "refused_name",
            "probed_404", "refused_store")

#: Once an index row reaches one of these, the name is never probed again. Without it the drain
#: re-probes 321 already-dead nyquist names every 15 minutes forever (measured 2026-09-09).
#: ⚠️`probed_404` IS DELIBERATELY NOT HERE. night_node.ino:2436 answers 404 for ANY failed
#: SD.open, not only for a missing file: max_files is 8 and the long-lived set reaches 6
#: (dets.csv + scene.csv + the open clip + /ls's directory and entry + health.csv) with
#: clip_evict_worse_than() taking 2 more during an eviction (night_node.ino:1725-1731). One
#: descriptor-exhausted moment must not retire a clip that is still on the card.
TERMINAL_OUTCOMES = ("stored", "evicted_before_fetch")

#: Consecutive 404s before a name is called destroyed. 2, not 1: a 404 costs ~0.1 s, so
#: confirming the 321 already-dead nyquist names costs one extra ~32 s pass, once.
CONFIRM_404 = 2

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
    # ⚠️A RATE THIS FORMAT CANNOT NAME IS REPORTED, NOT REFUSED. mach once wrote a whole boot
    # headed 22624 Hz while its CSV said 16000; refusing on the header would have discarded every
    # clip of it. `fs_nameable` travels with the row so the disagreement stays visible and the
    # tagger decides -- which is the same rule the index already follows by carrying BOTH the
    # header rate and the dets rate side by side.
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


#: Decimation between the acquisition rate the clip is written at and the FS_NOMINAL rate every
#: other lane runs at. Mirrors DECIM in night_node.ino.
CLIP_DECIM_CANDIDATES = (2, 3, 4)
#: Acquisition rates a node may legally clock the mic at, for confirming a suspected mis-header.
#: MSM261D3526H1CPM Standard Performance Mode caps at 62.5 kHz; 32000 is the earlier target.
CLIP_ACQ_RATES_HZ = (32000.0, 48000.0)
#: A clip longer than this cannot be a clip -- the firmware writes a fixed CLIP_SAMPLES and
#: refuses rather than shortening. Used only to notice a header that must be lying.
CLIP_PLAUSIBLE_MAX_S = 8.0
#: ⚠️EVERY CLIP LENGTH THIS FLEET HAS EVER WRITTEN. 4.0 s is the 16 kHz era (1.0 + 3.0); 5.0 s is
#: the 48 kHz firmware (1.0 + 4.0), sized so Perch's non-overlapping 5 s window is not padded with
#: fabricated silence. A correction is only accepted when it lands on one of these -- a duration
#: "in a plausible range" is not evidence, and admitting a range is what let the /2 reading of a
#: /3 mis-header look just as good as the right one.
CLIP_GEOMETRIES_S = (4.0, 5.0)
#: Clock drift on the header's integer rate. 2% is ~30x the 14 Hz observed header spread.
CLIP_GEOMETRY_TOL = 0.02


def header_rate_suspect(probe: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Is this clip's header rate provably wrong, and by what integer factor?

    ⚠️night_node.ino stamped every 48 kHz clip with the FS_NOMINAL timebase for the whole of the
    48 kHz rollout: the body is CLIP_SAMPLES at FS_ACQ, the header said ~16000 Hz. A 5.0 s clip
    reads back as 15.0 s and plays an octave and a half low, and NOTHING downstream could catch
    it -- 16000 is exactly the rate hear_tag.py's assert_rate wants, so the lying header walks
    straight past the guard built to stop wrong-rate audio reaching a model. Fixed in firmware,
    but clips written before the reflash are on the PVC and are not rewritable.

    ⚠️IT REFUSES TO GUESS, AND THE UNIQUENESS CHECK IS THE WHOLE OF THAT. A correction is
    returned only when EXACTLY ONE factor closes: the recovered duration must land on a length
    this fleet actually writes AND the recovered rate on one the mic can legally be clocked at.
    Written first against a plausible RANGE instead, a 15.0 s body matched /2 (7.5 s at 32 kHz)
    before it matched the correct /3 (5.0 s at 48 kHz), and returned the first hit. Two readings
    that both close means the header is not recoverable, not that the first one wins.
    """
    fs = probe.get("fs_hz")
    dur = probe.get("dur_s")
    if not fs or not dur or dur <= CLIP_PLAUSIBLE_MAX_S:
        return None
    hits = []
    for d in CLIP_DECIM_CANDIDATES:
        true_fs = float(fs) * d
        if not any(abs(true_fs - r) <= FS_TOLERANCE_HZ * d for r in CLIP_ACQ_RATES_HZ):
            continue
        true_dur = dur / d
        if not any(abs(true_dur - g) <= g * CLIP_GEOMETRY_TOL for g in CLIP_GEOMETRIES_S):
            continue
        hits.append((d, true_fs, true_dur))
    if len(hits) != 1:
        return None
    d, true_fs, true_dur = hits[0]
    return {"header_fs_hz": float(fs), "true_fs_hz": true_fs, "decim": d,
            "header_dur_s": dur, "true_dur_s": true_dur,
            "why": ("body is %.1f s at the header's %g Hz, which is not a length this fleet "
                    "writes; at %g Hz it is %.2f s -- the FS_NOMINAL-stamped 48 kHz clip bug"
                    % (dur, fs, true_fs, true_dur))}


def clip_total_s(row: Dict[str, Any]) -> Optional[float]:
    """The clip's length in seconds from its OWN header, mis-header corrected. None if unknowable.

    Both geometries are live in the corpus at once -- 1.0+3.0 s at 16 kHz and 1.0+4.0 s at
    48 kHz -- so a caller that needs the window has to read it off the clip. CLIP_PRE_S is 1.0 s
    in both, which is the only reason the pre/post split is derivable from a total.
    """
    n = row.get("bytes")
    fs = row.get("wav_header_fs_hz")
    if not n or not fs:
        return None
    dur = (int(n) - 44) / float(int(fs) * 2)
    fix = header_rate_suspect({"fs_hz": fs, "dur_s": dur})
    return fix["true_dur_s"] if fix else dur


def _post_s_of(probe: Optional[Dict[str, Any]]) -> float:
    """Post-roll from the clip's own length; CLIP_POST_S only when there is no clip to read."""
    total = _row_dur_s(probe)
    if total is None or not (CLIP_PRE_S < total <= CLIP_PLAUSIBLE_MAX_S):
        return CLIP_POST_S
    return total - CLIP_PRE_S


def _row_dur_s(probe: Optional[Dict[str, Any]]) -> Optional[float]:
    if not probe or not probe.get("dur_s"):
        return None
    fix = header_rate_suspect(probe)
    return fix["true_dur_s"] if fix else probe["dur_s"]


def index_row(*, clip: str, parts: Optional[Dict[str, Any]], node: str, body: Optional[bytes],
              probe: Optional[Dict[str, Any]], dets: Dict[str, Any], path: Optional[str],
              outcome: str, fetched_at: float, reason: Optional[str] = None,
              probe_404s: int = 0) -> Dict[str, Any]:
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
        # ⚠️THE END COMES FROM THE CLIP, NOT FROM A CONSTANT. A 16 kHz-era clip is 1.0+3.0 s and
        # a 48 kHz one is 1.0+4.0 s; both are in the corpus. Adding a fixed CLIP_POST_S put
        # t_end 1.0 s wrong for one era or the other, and every scene/sketch join downstream is
        # built on this pair.
        "t_end_utc_s": (ts + _post_s_of(probe)) if (anchored and ts) else None,
        "uptime_s": dets.get("uptime_s"),
        "fs_hz": dets.get("fs_hz"),
        "wav_header_fs_hz": (probe or {}).get("fs_hz"),
        # ⚠️DERIVED HERE SO NO CONSUMER RE-DERIVES IT. Two clip geometries are live at once and
        # one firmware build stamped the wrong rate, so "how long is this clip" stopped being a
        # constant. Computing it once at index time is what lets hear/tags.py keep its no-import
        # bundle discipline without duplicating the correction logic.
        "dur_s": _row_dur_s(probe),
        "header_rate_suspect": header_rate_suspect(probe) if probe else None,
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
    killed by `activeDeadlineSeconds: 780` mid-write leaks up to 128044 B per node per run onto a
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
