"""A miniature, entirely synthetic `/pool` for the object-store tests.

⚠️CONSTRUCTED, NEVER LIVE. No real audio, no real coordinates, no node identity copied from the
fleet, nothing read from `/pool`. Every byte here is generated from a fixed seed by this file, so
a digest asserted in a test is a statement about the rules and not about the day the test ran.

It carries the shapes that have actually broken readers in this repository, because a fixture that
only contains the easy case proves the easy case: an `unanchored` partition, a byte-identical
duplicate raw archive under two paths, a raw archive whose recorded ledger digest no longer
matches its bytes (corruption at rest), a JSONL with a torn final line, and a multi-member
`.jsonl.gz` produced by appending twice the way `hear/pool.py` appends scene files.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from typing import Dict, List, Tuple

CLIP_BYTES = b"RIFF$\x00\x00\x00WAVEfmt " + bytes(range(64))


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _write(path: str, body: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(body)
    return _sha(body)


def record_rows(n: int, *, day: str = "2026-09-12", node: str = "mach") -> List[Dict[str, object]]:
    return [{"key": "%032x" % (i + 1), "node": node, "source": "node", "ts_utc_s": None,
             "day": day, "sample": 1000 + i, "anchored": False} for i in range(n)]


def build(root: str) -> Dict[str, object]:
    """Lay down the mini pool and return the facts a test needs to assert against."""
    raw_body = b"seq,node_us,flags\n" + b"".join(b"%d,%d,0\n" % (i, i * 1000) for i in range(64))
    dup_a = os.path.join(root, "corpus/raw/mach/20260912T000000Z-dets.csv")
    dup_b = os.path.join(root, "corpus/raw/mach/20260912T001500Z-dets.csv")
    raw_digest = _write(dup_a, raw_body)
    _write(dup_b, raw_body)

    # A third archive whose bytes have rotted since the ledger recorded its digest.
    rotten = os.path.join(root, "corpus/raw/nyquist/20260912T000000Z-dets.csv")
    _write(rotten, raw_body.replace(b"0,0,0", b"0,0,9"))
    rotten_recorded = raw_digest  # what the ledger says; deliberately not what the file holds

    clip_a = os.path.join(root, "corpus/clips/2026-09-12/mach/mach-0a1b2c3d-1000.wav")
    clip_b = os.path.join(root, "corpus/clips/unanchored/mach/mach-0a1b2c3d-2000.wav")
    clip_digest = _write(clip_a, CLIP_BYTES)
    _write(clip_b, CLIP_BYTES)  # two silent clips, identical bytes: two objects, one blob

    rows = record_rows(6)
    jsonl = os.path.join(root, "corpus/records/2026-09-12/node.jsonl")
    body = b"".join(json.dumps(r, sort_keys=True).encode("utf-8") + b"\n" for r in rows)
    torn_row = json.dumps(record_rows(7)[6], sort_keys=True).encode("utf-8") + b"\n"
    torn = torn_row[:20]  # the append was cut short: a real, expected, torn final line
    _write(jsonl, body + torn)

    scene = os.path.join(root, "corpus/scene/2026-09-12/mach.jsonl.gz")
    os.makedirs(os.path.dirname(scene), exist_ok=True)
    scene_rows = record_rows(4, node="mach")
    with gzip.open(scene, "wt") as fh:
        for r in scene_rows[:2]:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    with gzip.open(scene, "at") as fh:  # second member: exactly how hear/pool.py appends
        for r in scene_rows[2:]:
            fh.write(json.dumps(r, sort_keys=True) + "\n")

    ledger = os.path.join(root, "corpus/ledger.jsonl")
    entries = [
        {"path": "corpus/raw/mach/20260912T000000Z-dets.csv", "sha256": raw_digest,
         "bytes": len(raw_body), "kind": "dets.csv"},
        {"path": "corpus/raw/mach/20260912T001500Z-dets.csv", "sha256": raw_digest,
         "bytes": len(raw_body), "kind": "dets.csv"},
        {"path": "corpus/raw/nyquist/20260912T000000Z-dets.csv", "sha256": rotten_recorded,
         "bytes": len(raw_body), "kind": "dets.csv"},
    ]
    _write(ledger, b"".join(json.dumps(e, sort_keys=True).encode("utf-8") + b"\n" for e in entries))

    index = os.path.join(root, "corpus/clips/index.jsonl")
    index_rows = [
        {"clip_key": "9f2c" + "0" * 28, "path": "2026-09-12/mach/mach-0a1b2c3d-1000.wav",
         "sha256": clip_digest, "bytes": len(CLIP_BYTES), "outcome": "fetched"},
        {"clip_key": "7e1d" + "0" * 28, "path": "unanchored/mach/mach-0a1b2c3d-2000.wav",
         "sha256": clip_digest, "bytes": len(CLIP_BYTES), "outcome": "fetched"},
        {"clip_key": "5c0b" + "0" * 28, "path": "2026-09-11/mach/mach-0a1b2c3d-0500.wav",
         "sha256": None, "outcome": "refused_bad_body", "audio_pruned_at": 1789000000},
    ]
    _write(index, b"".join(json.dumps(r, sort_keys=True).encode("utf-8") + b"\n" for r in index_rows))

    return {
        "root": root,
        "raw_duplicate_paths": (dup_a, dup_b),
        "raw_digest": raw_digest,
        "raw_bytes": len(raw_body),
        "rotten_path": rotten,
        "rotten_recorded_digest": rotten_recorded,
        "clip_paths": (clip_a, clip_b),
        "clip_digest": clip_digest,
        "clip_bytes": len(CLIP_BYTES),
        "records_path": jsonl,
        "records_rows": rows,
        "records_torn_tail": len(torn),
        "records_torn_row": torn_row,
        "records_torn_remainder": torn_row[len(torn):],
        "scene_path": scene,
        "scene_rows": scene_rows,
        "ledger_path": ledger,
        "index_path": index,
    }
