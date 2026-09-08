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
the frame is still a valid training example.

⚠️`anchored` MEANS "A STAMP EXISTS", NOT "THE STAMP IS TRUSTWORTHY". It is the weaker of two
questions and it is easy to read as the stronger one. For a node it is `utc_us > 0`, i.e. PPS
lock. For a PHONE it is `bool(ts)` -- true whenever the payload carried a `ts_utc_ms` at all,
INCLUDING the `clock_tier: "wall"` fallback, which GPSTimingSync declares at sigma 50 ms: about
17 m at 343 m/s, which is not a TDoA arrival. The stored field is deliberately left as it is --
it is content-addressed into every row already written and redefining it would change what those
rows assert -- so the trust question is asked somewhere else. Ask
`hear.corpus.Record.utc_trusted`; `stats()["by_clock_tier"]` says how many phone rows are in
which tier, so the wall population is a counted number rather than a silent exclusion.
"""
from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import os
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import numpy as np

from . import corpus as C
from . import detsfile as DF
from . import scenefile as SF
from . import sketch as SK

SCHEMA_VERSION = 1

#: Stored keys that become `hear.corpus.Record` fields. Everything else a record holds travels in
#: `Record.extra` -- see `Pool.records`. Listed here rather than inline so the two cannot drift.
_RECORD_FIELDS = frozenset({
    "node", "source", "frame_b64", "ref_db", "peak", "bands", "frames", "fs_hz", "node_us",
    "retrigger", "ts_utc_s", "clipped", "clock_tier", "sync_sigma_ns", "schema_version",
})


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
        # ⚠️A SEPARATE STORE, ON PURPOSE. A scene row is one every ~1.024 s whether or not
        # anything happened; a detection row exists only when the gate fired. Putting ~168k
        # continuous rows a day beside 1045 event rows would make `records` a number that means
        # two things at once, and every ratio computed from it wrong. Same discipline that keeps
        # health.csv archived but not ingested.
        self.scene_dir = os.path.join(self.root, "scene")
        self.ledger_path = os.path.join(self.root, "ledger.jsonl")
        self.state_dir = os.path.join(self.root, "state")
        for d in (self.root, self.records_dir, self.scene_dir, self.state_dir):
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
                # ⚠️`corpus.Record.utc_trusted` reads this as its guard against an older phone
                # build that publishes a `clock_tier` without the `stamp != null` coupling the
                # property relies on. Stored here so the pool path can answer the same question
                # `corpus.from_phone` can -- two readers of one message must not disagree.
                "onset_dated": payload.get("onset_dated"),
                # If a producer ever states it outright, it outranks the derivation. Absent is
                # the normal case and stays absent, NOT False: see Record.utc_trusted.
                "utc_trusted": payload.get("utc_trusted"),
            })
        added = self._append(recs)
        entry = {"kind": "mqtt.jsonl", "path": os.path.abspath(path), "origin": origin or path,
                 "sha256": sha, "bytes": len(raw), "rows": rows_seen, "decoded": len(recs),
                 "added": added, "duplicate": len(recs) - added, "skipped": sum(skips.values()),
                 "skip_reasons": skips, "schema_version": SCHEMA_VERSION}
        assert entry["rows"] == added + entry["duplicate"] + entry["skipped"], entry
        self._ledger(entry)
        return entry

    # ---------------------------------------------------------------- scene

    def _scene_path(self, day: str, node: str) -> str:
        d = os.path.join(self.scene_dir, day)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "%s.jsonl.gz" % node)

    def _scene_keys(self, day: str, node: str) -> set:
        """Keys already stored for ONE day and node.

        ⚠️Deliberately NOT a whole-pool key set. Scene is ~84k rows per node per day, so a month
        is millions of keys and holding them all to deduplicate one 15-minute fetch trades a
        bounded problem for an unbounded one. A fetch only ever appends to the day(s) it covers,
        so only those days need loading.
        """
        p = self._scene_path(day, node)
        ks = set()
        if not os.path.exists(p):
            return ks
        with gzip.open(p, "rt") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ks.add(json.loads(line)["key"])
                except Exception:
                    continue
        return ks

    def ingest_scene(self, path: str, default_node: Optional[str] = None,
                     origin: Optional[str] = None, partial: bool = False) -> Dict[str, Any]:
        """Ingest one `scene.csv` (or a byte-range tail of one). Idempotent.

        `partial=True` says this text came from `GET /sd?file=scene.csv&tail=N`, which starts
        mid-line and carries no header. The leading fragment is DROPPED and the drop is reported
        -- see `hear.scenefile.read_text`. Off by default so a whole file that begins mid-row
        still reads as the corruption it is.
        """
        raw = open(path, "rb").read()
        sha = hashlib.sha256(raw).hexdigest()
        read = SF.read_text(raw.decode("utf-8", "replace"), default_node=default_node,
                            allow_partial_first_line=partial)

        recs: List[Dict[str, Any]] = []
        bad: Dict[str, int] = {}
        for row in read.rows:
            try:
                d = SF.decode_row(row)
            except ValueError as e:
                r = "decode_" + str(e).split(":")[0].split(",")[0].strip().replace(" ", "_")[:40]
                bad[r] = bad.get(r, 0) + 1
                continue
            except Exception as e:
                r = "decode_" + type(e).__name__
                bad[r] = bad.get(r, 0) + 1
                continue
            utc_us = int(row.get("utc_us") or 0)
            node = row["node"]
            ts = (utc_us / 1e6) if utc_us > 0 else None
            recs.append({
                "schema_version": SCHEMA_VERSION,
                # The mel bytes are in the key for the same reason the sketch frame is: utc_us is
                # 0 for every pre-PPS row, and a boot's worth of them would otherwise collapse
                # into one record.
                "key": key("scene", node, utc_us, row.get("sample"), d["q"].tobytes()),
                "source": "scene", "node": node, "node_from": row.get("node_from"),
                "utc_us": utc_us, "anchored": utc_us > 0, "ts_utc_s": ts,
                "mel_b64": base64.b64encode(d["q"].tobytes()).decode(),
                "ref_db": d["ref_db"], "bands": d["bands"],
                # `slices`, never `frames`. ⚠️And `frames_summed` is the ROW TOTAL (firmware
                # SCENE_FRAMES = slices * frames_per_slice = 4 * 16 = 64), NOT the per-slice
                # count -- use scenefile.frames_per_slice() for that. Neither is the shape.
                "slices": d["slices"], "frames_summed": d["frames_summed"],
                "span_ms": d["span_ms"],
                "f_lo_hz": float(row["f_lo_hz"]) if row.get("f_lo_hz") else None,
                "f_hi_hz": float(row["f_hi_hz"]) if row.get("f_hi_hz") else None,
                "fft_us": row.get("fft_us"),
                "sample": row.get("sample"), "uptime_s": row.get("uptime_s"),
                "scene_schema": row.get("schema"),
            })

        by_bucket: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for r in recs:
            by_bucket.setdefault((_day(r.get("ts_utc_s")), r["node"]), []).append(r)
        added = 0
        for (day, node), rows in by_bucket.items():
            seen = self._scene_keys(day, node)
            fresh = []
            for r in rows:
                if r["key"] in seen:
                    continue
                seen.add(r["key"])
                fresh.append(json.dumps(r, sort_keys=True))
            if not fresh:
                continue
            with gzip.open(self._scene_path(day, node), "at") as fh:
                fh.write("\n".join(fresh) + "\n")
            added += len(fresh)

        reasons = dict(read.counts)
        for k, v in bad.items():
            reasons[k] = reasons.get(k, 0) + v
        skipped = len(read.skips) + sum(bad.values())
        entry = {
            "kind": "scene.csv", "path": os.path.abspath(path), "origin": origin or path,
            "sha256": sha, "bytes": len(raw), "generation": read.generation.name,
            "rows": len(read.rows) + len(read.skips), "decoded": len(recs), "added": added,
            "duplicate": len(recs) - added, "skipped": skipped, "skip_reasons": reasons,
            "decode_errors": bad, "partial_first_line": read.partial_first_line,
            "schema_version": SCHEMA_VERSION,
        }
        assert entry["rows"] == added + entry["duplicate"] + skipped, entry
        self._ledger(entry)
        return entry

    def scene(self, node: Optional[str] = None, day: Optional[str] = None,
              anchored_only: bool = False) -> Iterator[Dict[str, Any]]:
        """Every stored scene row, oldest partition first. A generator: a month of these is
        millions of rows and materialising them as a list is not something a caller should do by
        accident."""
        if not os.path.isdir(self.scene_dir):
            return
        for d in sorted(os.listdir(self.scene_dir)):
            if day and d != day:
                continue
            p = os.path.join(self.scene_dir, d)
            if not os.path.isdir(p):
                continue
            for fn in sorted(os.listdir(p)):
                if not fn.endswith(".jsonl.gz"):
                    continue
                if node and fn != "%s.jsonl.gz" % node:
                    continue
                with gzip.open(os.path.join(p, fn), "rt") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        r = json.loads(line)
                        if anchored_only and not r.get("anchored"):
                            continue
                        yield r

    def scene_matrix(self, node: Optional[str] = None, day: Optional[str] = None,
                     mode: str = "db", limit: Optional[int] = None
                     ) -> Tuple[np.ndarray, List[Dict[str, Any]]]:
        """[n, bands*slices] over stored scene rows, and the rows behind it.

        Refuses to stack rows of differing geometry rather than reshaping to fit: `bands` and
        `slices` are firmware's to change, and a matrix whose width silently depends on which
        firmware happened to be running is not a dataset.
        """
        if mode not in ("db", "q"):
            raise ValueError("mode must be 'db' or 'q'")
        rows, out = [], []
        geom = None
        for r in self.scene(node=node, day=day):
            g = (r["bands"], r["slices"])
            if geom is None:
                geom = g
            elif g != geom:
                raise ValueError("mixed scene geometry: %s then %s -- select a day or node "
                                 "whose firmware did not change" % (geom, g))
            q = np.frombuffer(base64.b64decode(r["mel_b64"]), dtype=np.int8) \
                .reshape(r["bands"], r["slices"]).astype(float)
            rows.append((q / 2.0 + r["ref_db"] if mode == "db" else q).reshape(-1))
            out.append(r)
            if limit and len(rows) >= limit:
                break
        if not rows:
            return np.zeros((0, 0)), []
        return np.vstack(rows), out

    def scene_stats(self) -> Dict[str, Any]:
        """What the scene store holds. Counted by walking it, because a row count taken from the
        ledger would credit rows a later gzip write could have lost."""
        n = 0
        by_node: Dict[str, int] = {}
        by_day: Dict[str, int] = {}
        geom: Dict[str, int] = {}
        anchored = 0
        first = last = None
        for r in self.scene():
            n += 1
            by_node[r["node"]] = by_node.get(r["node"], 0) + 1
            d = _day(r.get("ts_utc_s"))
            by_day[d] = by_day.get(d, 0) + 1
            g = "%dx%d" % (r["bands"], r["slices"])
            geom[g] = geom.get(g, 0) + 1
            anchored += bool(r.get("anchored"))
            t = r.get("ts_utc_s")
            if t:
                first = t if first is None else min(first, t)
                last = t if last is None else max(last, t)
        bytes_on_disk = 0
        for dirpath, _dirs, files in os.walk(self.scene_dir):
            for f in files:
                bytes_on_disk += os.path.getsize(os.path.join(dirpath, f))
        return {"rows": n, "by_node": by_node, "by_day": by_day, "geometry": geom,
                "anchored": anchored, "unanchored": n - anchored,
                "first_utc_s": first, "last_utc_s": last,
                "bytes_on_disk": bytes_on_disk,
                "bytes_per_row": round(bytes_on_disk / n, 1) if n else None}

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
                # ⚠️EVERYTHING THE STORE HELD THAT IS NOT ALREADY A FIELD. This was a fixed
                # whitelist, and a whitelist silently drops whatever a producer adds next: it
                # was already losing `onset_found`, which hear/backend/associate.py REFUSES
                # arrivals on, so a phone that honestly reported its onset was unmeasurable had
                # that report deleted between the pool and the gate meant to read it. Passing
                # the remainder through means a new quality flag arrives by default and the
                # failure mode becomes an unexpected key rather than a missing measurement.
                extra={k: v for k, v in r.items() if k not in _RECORD_FIELDS},
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
        # ⚠️PHONE ONLY, AND COUNTED RATHER THAN EXCLUDABLE. `anchored` is true for a phone row
        # stamped on the wall clock (see the module docstring), so the population a solver must
        # hold out is invisible in the anchored/unanchored split. A selector that quietly drops
        # rows without saying how many is the failure this pool has already had twice -- the G3
        # 730 and the `flags & 2` 88 -- so the tiers are reported, not filtered. Nodes are absent
        # from this breakdown because they do not have a clock_tier: their time comes from PPS,
        # and folding a `None` bucket in here would read as a phone that failed to state one.
        by_tier: Dict[str, int] = {}
        by_trust: Dict[str, int] = {"true": 0, "false": 0, "not_stated": 0}
        anchored = no_ctx = unstated = 0
        n = 0
        for r in self.raw():
            n += 1
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1
            by_node[r["node"]] = by_node.get(r["node"], 0) + 1
            by_fs[str(r.get("fs_hz"))] = by_fs.get(str(r.get("fs_hz")), 0) + 1
            by_day[_day(r.get("ts_utc_s"))] = by_day.get(_day(r.get("ts_utc_s")), 0) + 1
            if r.get("source") == "phone":
                by_tier[str(r.get("clock_tier"))] = by_tier.get(str(r.get("clock_tier")), 0) + 1
                t = C.utc_trusted_of(r)
                by_trust["not_stated" if t is None else ("true" if t else "false")] += 1
            anchored += bool(r.get("anchored"))
            no_ctx += bool(r.get("no_context"))
            unstated += r.get("fs_hz") is None
        # ⚠️ONLY THE SKETCH INGESTS. The ledger is one log for both stores, so an unfiltered read
        # here credited this summary with scene.csv's S1/S2 generations and scene's skip tallies
        # -- a count describing two different things again, which is the bug this pool exists to
        # not commit.
        led = [e for e in self.ledger() if e.get("kind") in ("dets.csv", "mqtt.jsonl")]
        skips: Dict[str, int] = {}
        for e in led:
            for k, v in (e.get("skip_reasons") or {}).items():
                skips[k] = skips.get(k, 0) + v
            for k, v in (e.get("decode_errors") or {}).items():
                skips[k] = skips.get(k, 0) + v
        return {"records": n, "by_source": by_source, "by_node": by_node, "by_fs_hz": by_fs,
                "by_day": by_day, "anchored": anchored, "unanchored": n - anchored,
                "by_clock_tier": by_tier,
                "trusted_clock_tiers": sorted(C.TRUSTED_CLOCK_TIERS),
                # Off `corpus.utc_trusted_of`, the SAME function `Record.utc_trusted` uses -- not
                # re-derived from `by_clock_tier`, which would miss the `onset_dated` rung and
                # give the pool's summary and its own records two different answers.
                "phone_utc_trusted": by_trust,
                "no_context": no_ctx, "fs_unstated": unstated,
                "ingests": len(led),
                "files_seen": len({e["sha256"] for e in led}),
                "skipped_at_ingest": skips,
                "generations": sorted({e.get("generation") for e in led if e.get("generation")})}
