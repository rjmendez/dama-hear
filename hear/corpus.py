#!/usr/bin/env python3
"""One corpus from two kinds of sensor.

A Tier-1 node ships a 172-byte sketch over Meshtastic because that is all the airtime it has. A
dama phone ships the SAME 172 bytes over MQTT because it does not need to -- it holds an 8 s ring
and a fat link. The phones are the only sensors carrying operator labels, so emitting the node's
format is what makes them the nodes' training corpus instead of a second, incompatible dataset.

This module is where the two meet. It decodes both, keeps what each can honestly say about
itself, and REFUSES to stack frames whose bands do not mean the same frequencies.

⚠️THE FS GUARD IS THE POINT. `mel_filterbank` clamps its top edge to Nyquist, so 20 bands span
300 Hz-20 kHz at 48 kHz and 300 Hz-7.84 kHz at 16 kHz. Band 12 is 4.6 kHz on one and 1.9 kHz on
the other. Stacking them into one feature matrix trains a model on a frequency axis that moves
between rows, and nothing about the resulting number looks wrong. `feature_matrix` will not do it
without being told, in as many words, which rate it is building for.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from . import sketch as SK


@dataclass
class Record:
    """One decoded sketch, from either kind of sensor."""
    node_id: str
    source: str                      # "phone" | "node"
    q: np.ndarray                    # int8 [bands, frames]
    ref_db: float
    peak: int
    bands: int
    frames: int
    fs_hz: Optional[float]           # None == the frame did not state it
    node_us: int
    retrigger: bool
    ts_utc_s: Optional[float] = None
    clipped: Optional[bool] = None
    clock_tier: Optional[str] = None
    sync_sigma_ns: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def db(self) -> np.ndarray:
        """Absolute dB. The int8 carries shape; `ref_db` is what puts it back on a scale."""
        return self.q.astype(float) / 2.0 + self.ref_db

    def band_edges_hz(self) -> Optional[np.ndarray]:
        return None if self.fs_hz is None else SK.band_edges_hz(self.fs_hz, self.bands)


class SkipReason(Exception):
    """Why a message produced no record. Raised, never returned as an empty Record: a caller that
    silently drops rows cannot tell an absent sensor from a broken decoder."""


def _decode(frame: bytes) -> Dict:
    """SK.unpack's ValueErrors become SkipReason: a malformed frame is a message this corpus
    could not use, not a crash in the reader."""
    try:
        return SK.unpack(frame)
    except ValueError as e:
        raise SkipReason(str(e))


def from_phone(payload: Dict[str, Any], node_id: Optional[str] = None) -> Record:
    """Decode one `dama/<node>/acoustic_sketch` message.

    The phone states `sketch_skipped` when it could not sketch an onset -- a capture drop, a ring
    that had not caught up. That is a real event with no sketch, not a malformed message, and it
    is raised as a skip with its own reason rather than fabricated into a record.
    """
    if "sketch_skipped" in payload:
        raise SkipReason("phone skipped: %s" % payload["sketch_skipped"])
    b64 = payload.get("sketch_b64")
    if not b64:
        raise SkipReason("no sketch_b64")
    d = _decode(base64.b64decode(b64))
    nid = node_id or payload.get("node_id") or payload.get("node") or "?"
    # The JSON repeats geometry the header already carries. If they disagree, one of the two
    # producers is not what we think it is -- do not pick a winner.
    for key, got in (("bands", d["q"].shape[0]), ("frames", d["q"].shape[1])):
        if key in payload and int(payload[key]) != got:
            raise SkipReason("%s: json says %s, header says %d" % (key, payload[key], got))
    fs = d["fs_hz"]
    if fs is None and payload.get("fs"):
        # An older phone build that packed no rate code but reported it alongside. Believable,
        # and recorded as coming from the JSON rather than the frame.
        fs = float(payload["fs"])
    ts = payload.get("ts_utc_ms")
    return Record(
        node_id=str(nid), source="phone", q=d["q"], ref_db=d["ref_db"], peak=d["peak"],
        bands=d["q"].shape[0], frames=d["q"].shape[1], fs_hz=fs, node_us=d["node_us"],
        retrigger=bool(d["retrigger"]),
        ts_utc_s=None if ts is None else float(ts) / 1000.0,
        clipped=payload.get("clipped"),
        clock_tier=payload.get("clock_tier"),
        sync_sigma_ns=payload.get("sync_sigma_ns"),
        extra={k: payload[k] for k in
               ("trigger_ts_utc_ms", "onset_offset_us", "onset_dated", "since_prev_s")
               if k in payload},
    )


def from_node(frame: bytes, node_id: str, second_utc_s: Optional[int] = None,
              fs_hz: Optional[float] = None) -> Record:
    """Decode one Meshtastic frame.

    `node_us` is microseconds WITHIN a second; the second itself comes from the mesh's own clock
    and is not in the frame. Pass `second_utc_s` if the gateway knows it -- without it the record
    carries no absolute time, which is the truth, not a defect to paper over with arrival time.
    """
    d = _decode(frame)
    fs = d["fs_hz"] if d["fs_hz"] is not None else fs_hz
    ts = None if second_utc_s is None else float(second_utc_s) + d["node_us"] / 1e6
    return Record(
        node_id=str(node_id), source="node", q=d["q"], ref_db=d["ref_db"], peak=d["peak"],
        bands=d["q"].shape[0], frames=d["q"].shape[1], fs_hz=fs, node_us=d["node_us"],
        retrigger=bool(d["retrigger"]), ts_utc_s=ts,
    )


def feature_matrix(records: Iterable[Record], fs_hz: float,
                   mode: str = "db") -> Tuple[np.ndarray, List[Record]]:
    """[n, bands*frames] for the records that belong on `fs_hz`'s frequency axis, and those records.

    `fs_hz` is REQUIRED and is not inferred from the data. Inferring it makes the common mistake
    -- a corpus of mostly-48 kHz phones with a handful of 16 kHz nodes -- silent: the majority
    wins the inference and the minority is stacked onto an axis it was never measured on.

    Records with no stated rate are excluded. They may well be `fs_hz`; nothing in the frame says
    so, and a training matrix is the wrong place to guess.

    mode "db"  -- absolute dB, ref_db restored. Amplitude alone was worth AUC 0.90 on the 2026-09-05
                  corpus, so this is the default.
    mode "q"   -- the raw int8, shape only. Use when levels are not comparable across sites.
    """
    if mode not in ("db", "q"):
        raise ValueError("mode must be 'db' or 'q'")
    kept: List[Record] = []
    rows: List[np.ndarray] = []
    for r in records:
        if r.fs_hz is None or float(r.fs_hz) != float(fs_hz):
            continue
        v = r.db if mode == "db" else r.q.astype(float)
        rows.append(v.reshape(-1))
        kept.append(r)
    if not rows:
        return np.zeros((0, 0)), []
    w = {len(r) for r in rows}
    if len(w) != 1:
        raise ValueError("mixed sketch geometry in one matrix: %s" % sorted(w))
    return np.vstack(rows), kept


def summarise(records: Iterable[Record]) -> Dict[str, Any]:
    """What a corpus can defend about itself: how it splits by rate and by source.

    A single count would hide the split that matters -- rows on different frequency axes are not
    one corpus, however much they look like one in a row count.
    """
    recs = list(records)
    by_fs: Dict[str, int] = {}
    by_src: Dict[str, int] = {}
    for r in recs:
        by_fs[str(r.fs_hz)] = by_fs.get(str(r.fs_hz), 0) + 1
        by_src[r.source] = by_src.get(r.source, 0) + 1
    return {
        "records": len(recs),
        "by_fs_hz": by_fs,
        "by_source": by_src,
        "unstated_fs": by_fs.get("None", 0),
        "retriggers": sum(1 for r in recs if r.retrigger),
        "clipped": sum(1 for r in recs if r.clipped),
        "nodes": sorted({r.node_id for r in recs}),
    }


def read_mqtt_jsonl(path: str) -> Tuple[List[Record], List[str]]:
    """Read a JSONL capture of `dama/+/acoustic_sketch` -- one {"topic":..., "payload":{...}} per
    line, or a bare payload with `node_id` in it. Returns (records, skip reasons).

    Skips are RETURNED, not logged and dropped: a corpus that cannot say how much it discarded and
    why is not auditable.
    """
    out: List[Record] = []
    skips: List[str] = []
    with open(path) as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as e:
                skips.append("line %d: bad json: %s" % (n, e))
                continue
            payload = obj.get("payload", obj)
            # The node can be stated three ways and they are checked in order of how load-bearing
            # they are: the corpus worker records it at the top level having taken it from the
            # TOPIC (the only place the broker guarantees), a raw capture has the topic itself,
            # and a bare payload may carry its own claim. A payload's self-report is last because
            # it is the one a misconfigured device can get wrong.
            nid = obj.get("node_id")
            if not nid:
                topic = obj.get("topic")
                if topic:
                    parts = str(topic).split("/")
                    if len(parts) >= 3:
                        nid = parts[1]
            try:
                out.append(from_phone(payload, node_id=nid))
            except SkipReason as e:
                skips.append("line %d: %s" % (n, e))
            except Exception as e:                     # malformed base64, short frame, ...
                skips.append("line %d: %r" % (n, e))
    return out, skips


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("jsonl", help="captured dama/+/acoustic_sketch messages, one per line")
    ap.add_argument("--fs", type=float, help="build a feature matrix for this rate")
    ap.add_argument("--mode", choices=("db", "q"), default="db")
    ap.add_argument("--npz", help="write the matrix here")
    a = ap.parse_args(argv)

    recs, skips = read_mqtt_jsonl(a.jsonl)
    s = summarise(recs)
    print(json.dumps(s, indent=2))
    if skips:
        print("\nskipped %d:" % len(skips))
        for r in skips[:20]:
            print("  " + r)
        if len(skips) > 20:
            print("  ... %d more" % (len(skips) - 20))
    if a.fs:
        X, kept = feature_matrix(recs, a.fs, a.mode)
        print("\nmatrix for %.0f Hz: %s (%d of %d records)"
              % (a.fs, X.shape, len(kept), len(recs)))
        if a.npz:
            np.savez(a.npz, X=X, node_id=np.array([r.node_id for r in kept]),
                     ts_utc_s=np.array([-1.0 if r.ts_utc_s is None else r.ts_utc_s for r in kept]),
                     retrigger=np.array([r.retrigger for r in kept]),
                     source=np.array([r.source for r in kept]))
            print("wrote %s" % a.npz)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
