#!/usr/bin/env python3
"""One append-only pool holding every sketch both kinds of sensor have ever produced.

    from hear import pool
    p = pool.Pool("~/hear-pool")
    p.ingest_dets("nyquist_dets.csv", default_node="nyquist")   # node, off the SD card
    p.ingest_mqtt_jsonl("2026-09-08.jsonl")                     # phone, off MQTT
    recs = p.records()                                          # hear.corpus.Record, both sources

WHAT THE POOL IS FOR. TDoA needs arrivals from several sensors on one clock, and the two sensors
that exist emit the same 172-byte sketch over different transports: a node writes it to an SD
card as hex in `dets.csv`, a phone publishes it to MQTT as base64. Until they are in one store
with one dedup rule, "all the data" is a directory listing someone has to remember, and it has
already gone wrong twice -- see the ledger note below.

⚠️IDEMPOTENT INGEST IS THE POINT, NOT A FEATURE. The node's card holds `dets.csv` and
`dets-prev.csv`, both roll on reflash, and every drain so far has been a hand-run `curl` into a
dated directory. So the same detection arrives repeatedly: in a live file, again in that file's
`-prev` after the roll, and again in every earlier drain someone kept. Ingest is therefore keyed
on CONTENT -- `key()` below -- so re-ingesting a file, a whole directory, or two overlapping
drains adds nothing. Draining more often is then always safe, which is what lets it be automatic.

⚠️THE LEDGER IS NOT A LOG. `ledger.jsonl` records, per source file, its sha256 and byte length,
which schema generation was recognised, and the full skip tally by reason. It exists because a
reader that could not say what it dropped has twice returned a confident wrong answer here: the
G3 header/writer mismatch turned 730 detections into 0 rows with no error, and a `flags & 2`
filter silently removed 88 more. `stats()` reads the ledger, so "how much data do we have" is
answered from what was actually written, never from a file count.

TIME. `utc_us == 0` means the node had no PPS lock yet; the row is KEPT and marked
`anchored: false`. Dropping it at ingest would make the pool's own count depend on GPS state, and
the frame is still a valid training example. Solvers filter on `anchored`.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from . import corpus as C
from . import detsfile as DF
from . import sketch as SK

SCHEMA_VERSION = 1


def key(source: str, node: str, utc_us: int, sample: Optional[str], frame: bytes) -> str:
    """The content address of one sketch. Same detection from any drain -> same key.

    `frame` is in it because `utc_us` is 0 for every pre-lock row, and a node emitting several
    unanchored detections in one boot would otherwise collapse them all into one record. `sample`
    is the node's own monotonic counter and separates them within a boot; the frame separates
    them across boots, where `sample` restarts.
    """
    h = hashlib.sha256()
    for part in (source, node, str(utc_us), "" if sample is None else str(sample)):
        h.update(part.encode())
        h.update(b"\x1f")
    h.update(frame)
    return h.hexdigest()[:32]


def _day(ts_utc_s: Optional[float]) -> str:
    """UTC day partition. Unanchored rows go to `unanchored` rather than to a guessed day."""
    if not ts_utc_s:
        return "unanchored"
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts_utc_s, _dt.timezone.utc).strftime("%Y-%m-%d")


def _record_from_node_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One `hear.detsfile` row -> one pool record. Raises ValueError on an undecodable frame."""
    frame = binascii.unhexlify(row["frame_hex"])
    d = SK.unpack(frame)
    utc_us = int(row.get("utc_us") or 0)
    node = row["node"]
    # fs: the FRAME is authoritative when it states a rate, because the CSV column is the node's
    # running estimate and the fs_clean latch has already put 22624.0 in it for a whole boot.
    fs_csv = row.get("fs_hz")
    fs_csv = float(fs_csv) if fs_csv not in (None, "") else None
    return {
        "schema_version": SCHEMA_VERSION,
        "key": key("node", node, utc_us, row.get("sample"), frame),
        "source": "node",
        "node": node,
        "node_from": row.get("node_from"),
        "utc_us": utc_us,
        "anchored": utc_us > 0,
        "ts_utc_s": (utc_us / 1e6) if utc_us > 0 else None,
        "frame_b64": base64.b64encode(frame).decode(),
        "fs_hz": d["fs_hz"] if d["fs_hz"] is not None else fs_csv,
        "fs_stated_by": "frame" if d["fs_hz"] is not None else ("csv" if fs_csv else None),
        "fs_csv_hz": fs_csv,
        "layout": d["layout"],
        "valid_bands": d["valid_bands"],
        "bands": int(d["q"].shape[0]),
        "frames": int(d["q"].shape[1]),
        "peak": d["peak"],
        "ref_db": d["ref_db"],
        "node_us": d["node_us"],
        "retrigger": bool(d["retrigger"]),
        # event_flags, not the raw flags word: bit 1 is "no context" only in v1, and is a profile
        # bit in v2. Masking the raw word tags good v2 frames as broken.
        "no_context": bool(d["event_flags"] & SK.FLAG_NO_CONTEXT),
        "sketch_back": (int(row["sketch_back"]) if row.get("sketch_back") not in (None, "")
                        else None),
        "sample": row.get("sample"),
        "uptime_s": row.get("uptime_s"),
        "trigger": row.get("trigger"),
        "clip": row.get("clip") or None,
        "dets_schema": row.get("schema"),
    }



class Pool:
    """An append-only, content-addressed store of sketches under one directory."""

    def __init__(self, root: str):
        self.root = os.path.expanduser(root)
        self.records_dir = os.path.join(self.root, "records")
        self.ledger_path = os.path.join(self.root, "ledger.jsonl")
        self.state_dir = os.path.join(self.root, "state")
        for d in (self.root, self.records_dir, self.state_dir):
            os.makedirs(d, exist_ok=True)
        self._keys: Optional[set] = None

    # ---------------------------------------------------------------- keys

    def keys(self) -> set:
        """Every key already stored. Read once and cached for the life of the object."""
        if self._keys is None:
            ks = set()
            for day in sorted(os.listdir(self.records_dir)):
                p = os.path.join(self.records_dir, day)
                if not os.path.isdir(p):
                    continue
                for fn in sorted(os.listdir(p)):
                    if not fn.endswith(".jsonl"):
                        continue
                    with open(os.path.join(p, fn)) as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                ks.add(json.loads(line)["key"])
                            except Exception:
                                continue
            self._keys = ks
        return self._keys

    # ---------------------------------------------------------------- write

    def _append(self, recs: Iterable[Dict[str, Any]]) -> int:
        """Append new records, skipping any key already held. Returns how many were written."""
        seen = self.keys()
        buckets: Dict[Tuple[str, str], List[str]] = {}
        n = 0
        for r in recs:
            if r["key"] in seen:
                continue
            seen.add(r["key"])
            buckets.setdefault((_day(r.get("ts_utc_s")), r["source"]), []).append(
                json.dumps(r, sort_keys=True))
            n += 1
        for (day, source), lines in buckets.items():
            d = os.path.join(self.records_dir, day)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "%s.jsonl" % source), "a") as fh:
                fh.write("\n".join(lines) + "\n")
        return n

    def _ledger(self, entry: Dict[str, Any]) -> None:
        with open(self.ledger_path, "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    # ---------------------------------------------------------------- ingest

    def ingest_dets(self, path: str, default_node: Optional[str] = None,
                    origin: Optional[str] = None) -> Dict[str, Any]:
        """Ingest one node `dets.csv`. Idempotent: re-ingesting adds 0.

        Refuses nothing quietly -- an unknown schema raises, and every unusable row is counted by
        reason into the ledger entry this returns.
        """
        raw = open(path, "rb").read()
        sha = hashlib.sha256(raw).hexdigest()
        read = DF.read_text(raw.decode("utf-8", "replace"), default_node=default_node)
        recs, bad = [], {}
        for row in read.rows:
            try:
                recs.append(_record_from_node_row(row))
            except Exception as e:                       # short frame, bad hex, bad header
                r = type(e).__name__
                bad[r] = bad.get(r, 0) + 1
        added = self._append(recs)
        # ⚠️THE ARITHMETIC MUST CLOSE. `rows_seen == added + duplicate + skipped` for every file,
        # and decode failures are counted into `skipped` rather than living in a field nothing
        # totals. A 5-row file that reported "0 new, 0 duplicate, 0 skipped" is what a whole
        # deploy of this tool actually printed while discarding all five, and the reason it could
        # is that `skipped` counted only the READER's refusals and not the decoder's.
        reasons = dict(read.counts)
        for k, v in bad.items():
            reasons["decode_" + k] = reasons.get("decode_" + k, 0) + v
        skipped = len(read.skips) + sum(bad.values())
        entry = {
            "kind": "dets.csv", "path": os.path.abspath(path), "origin": origin or path,
            "sha256": sha, "bytes": len(raw), "generation": read.generation.name,
            "rows": len(read.rows) + len(read.skips), "decoded": len(recs), "added": added,
            "duplicate": len(recs) - added,
            "skipped": skipped, "skip_reasons": reasons,
            "decode_errors": bad, "schema_version": SCHEMA_VERSION,
        }
        assert entry["rows"] == added + entry["duplicate"] + skipped, entry
        self._ledger(entry)
        return entry

    def ingest_mqtt_jsonl(self, path: str, origin: Optional[str] = None) -> Dict[str, Any]:
        """Ingest one day of captured `dama/+/acoustic_sketch`. Idempotent, same as above."""
        raw = open(path, "rb").read()
        sha = hashlib.sha256(raw).hexdigest()
        recs: List[Dict[str, Any]] = []
        skips: Dict[str, int] = {}
        rows_seen = 0
        for n, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            rows_seen += 1
            try:
                obj = json.loads(line)
            except ValueError:
                skips["bad_json"] = skips.get("bad_json", 0) + 1
                continue
            payload = obj.get("payload", obj)
            nid = obj.get("node_id")
            if not nid:
                topic = str(obj.get("topic", ""))
                parts = topic.split("/")
                nid = parts[1] if len(parts) >= 3 else None
            b64 = payload.get("sketch_b64")
            if not b64:
                r = "phone_skipped" if "sketch_skipped" in payload else "no_sketch_b64"
                skips[r] = skips.get(r, 0) + 1
                continue
            try:
                frame = base64.b64decode(b64)
                d = SK.unpack(frame)
            except Exception as e:
                r = type(e).__name__
                skips[r] = skips.get(r, 0) + 1
                continue
            ts_ms = payload.get("ts_utc_ms")
            ts = None if ts_ms is None else float(ts_ms) / 1000.0
            utc_us = int(round(ts * 1e6)) if ts else 0
            node = str(nid or payload.get("node_id") or "?")
            recs.append({
                "schema_version": SCHEMA_VERSION,
                "key": key("phone", node, utc_us, payload.get("trigger_ts_utc_ms"), frame),
                "source": "phone", "node": node, "node_from": "topic" if nid else "payload",
                "utc_us": utc_us, "anchored": bool(ts), "ts_utc_s": ts,
                "frame_b64": base64.b64encode(frame).decode(),
                "fs_hz": d["fs_hz"] if d["fs_hz"] is not None else payload.get("fs"),
                "fs_stated_by": "frame" if d["fs_hz"] is not None else
                                ("json" if payload.get("fs") else None),
                "layout": d["layout"], "valid_bands": d["valid_bands"],
                "bands": int(d["q"].shape[0]), "frames": int(d["q"].shape[1]),
                "peak": d["peak"], "ref_db": d["ref_db"], "node_us": d["node_us"],
                "retrigger": bool(d["retrigger"]),
                "no_context": bool(d["event_flags"] & SK.FLAG_NO_CONTEXT),
                "sketch_back": None,
                "clipped": payload.get("clipped"),
                "clock_tier": payload.get("clock_tier"),
                "sync_sigma_ns": payload.get("sync_sigma_ns"),
                "onset_found": payload.get("onset_found"),
                "onset_offset_us": payload.get("onset_offset_us"),
            })
        added = self._append(recs)
        entry = {"kind": "mqtt.jsonl", "path": os.path.abspath(path), "origin": origin or path,
                 "sha256": sha, "bytes": len(raw), "rows": rows_seen, "decoded": len(recs),
                 "added": added, "duplicate": len(recs) - added, "skipped": sum(skips.values()),
                 "skip_reasons": skips, "schema_version": SCHEMA_VERSION}
        assert entry["rows"] == added + entry["duplicate"] + entry["skipped"], entry
        self._ledger(entry)
        return entry

    # ---------------------------------------------------------------- read

    def raw(self, source: Optional[str] = None, day: Optional[str] = None
            ) -> Iterator[Dict[str, Any]]:
        """Every stored record as its stored dict, oldest partition first."""
        for d in sorted(os.listdir(self.records_dir)):
            if day and d != day:
                continue
            p = os.path.join(self.records_dir, d)
            if not os.path.isdir(p):
                continue
            for fn in sorted(os.listdir(p)):
                if not fn.endswith(".jsonl"):
                    continue
                if source and fn != "%s.jsonl" % source:
                    continue
                with open(os.path.join(p, fn)) as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            yield json.loads(line)

    def records(self, source: Optional[str] = None, anchored_only: bool = False,
                usable_only: bool = False) -> List[C.Record]:
        """Stored records as `hear.corpus.Record`, so the existing matrix and solve code reads
        the pool with no adapter.

        `usable_only` drops the frames a solver must not use: no stated rate, no context, or the
        nyquist band layout. It is OFF by default -- the pool's job is to hold everything and say
        what it holds; deciding what is usable is the caller's, and a reader that quietly applied
        it would make the pool's count disagree with the ledger's.
        """
        out: List[C.Record] = []
        for r in self.raw(source=source):
            if anchored_only and not r.get("anchored"):
                continue
            if usable_only and (r.get("fs_hz") is None or r.get("no_context")
                                or r.get("layout") != SK.LAYOUT_FIXED):
                continue
            d = SK.unpack(base64.b64decode(r["frame_b64"]))
            out.append(C.Record(
                node_id=r["node"], source=r["source"], q=d["q"], ref_db=d["ref_db"],
                peak=d["peak"], bands=d["q"].shape[0], frames=d["q"].shape[1],
                fs_hz=r.get("fs_hz"), node_us=d["node_us"], retrigger=bool(d["retrigger"]),
                ts_utc_s=r.get("ts_utc_s"), clipped=r.get("clipped"),
                clock_tier=r.get("clock_tier"), sync_sigma_ns=r.get("sync_sigma_ns"),
                extra={"layout": r.get("layout"), "valid_bands": r.get("valid_bands"),
                       "key": r["key"], "no_context": r.get("no_context"),
                       "sketch_back": r.get("sketch_back"), "utc_us": r.get("utc_us"),
                       "anchored": r.get("anchored")},
            ))
        return out

    # ---------------------------------------------------------------- report

    def ledger(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.ledger_path):
            return []
        out = []
        with open(self.ledger_path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def stats(self) -> Dict[str, Any]:
        """What the pool holds, and what ingest threw away getting there.

        The skip tally comes from the LEDGER, not from the records: by the time a row is a record
        the reasons it might not have been are gone, and those reasons are the whole audit.
        """
        by_source: Dict[str, int] = {}
        by_node: Dict[str, int] = {}
        by_fs: Dict[str, int] = {}
        by_day: Dict[str, int] = {}
        anchored = no_ctx = unstated = 0
        n = 0
        for r in self.raw():
            n += 1
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1
            by_node[r["node"]] = by_node.get(r["node"], 0) + 1
            by_fs[str(r.get("fs_hz"))] = by_fs.get(str(r.get("fs_hz")), 0) + 1
            by_day[_day(r.get("ts_utc_s"))] = by_day.get(_day(r.get("ts_utc_s")), 0) + 1
            anchored += bool(r.get("anchored"))
            no_ctx += bool(r.get("no_context"))
            unstated += r.get("fs_hz") is None
        led = self.ledger()
        skips: Dict[str, int] = {}
        for e in led:
            for k, v in (e.get("skip_reasons") or {}).items():
                skips[k] = skips.get(k, 0) + v
            for k, v in (e.get("decode_errors") or {}).items():
                skips[k] = skips.get(k, 0) + v
        return {"records": n, "by_source": by_source, "by_node": by_node, "by_fs_hz": by_fs,
                "by_day": by_day, "anchored": anchored, "unanchored": n - anchored,
                "no_context": no_ctx, "fs_unstated": unstated,
                "ingests": len(led),
                "files_seen": len({e["sha256"] for e in led}),
                "skipped_at_ingest": skips,
                "generations": sorted({e.get("generation") for e in led if e.get("generation")})}
