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
from . import identity as ID
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


def _geom_str(g: Tuple[Any, Any, Any, Any]) -> str:
    """A scene row's geometry as one printable key: `20x4@62.5-7812.5`.

    The band EDGES are part of it. Two banks can share 20x4 and disagree about what band k is --
    the scene bank spans 62.5-7812.5 Hz where the detection bank spans 312.5-8000 -- so a key that
    named only the shape would report one geometry for two axes. `?` marks an edge the row never
    declared (an S0 row has no f_lo_hz/f_hi_hz columns); it is not the same as any stated edge.

    ⚠️THE KEY MUST NOT ROUND TWO AXES TOGETHER. `%g` is 6 significant digits, so a bare `%g` would
    print 62.5 for both 62.5 and 62.50000001 -- and this string is `scene_stats`' dict key, the one
    place a mix is counted WITHOUT raising, so a collapsed key undercounts distinct geometries
    exactly where nothing else would notice. `scene_matrix` compares the exact tuple, so a bare
    `%g` also lets it refuse two rows while printing one key for both. Every edge is therefore
    round-tripped: `%g` is kept only when it reads back as the same float, else `repr`. Today's
    firmware writes MELS_F_LO/MELS_F_HI at one decimal, so this never fires on shipped rows.
    """
    bands, slices, lo, hi = g

    def fmt(v):
        if v is None:
            return "?"
        v = float(v)
        s = "%g" % v
        return s if float(s) == v else repr(v)

    return "%sx%s@%s-%s" % (bands, slices, fmt(lo), fmt(hi))


def _why_geoms_differ(a: Tuple[Any, Any, Any, Any], b: Tuple[Any, Any, Any, Any]) -> str:
    """Name what actually differs between two scene geometries, not what might have.

    ⚠️Keyed on the DIFFERENCE, not on the presence of a None. A message that decided by asking
    "is any edge undeclared?" reported band EDGES for a pure shape change whose edges were
    byte-identical (20x4@300-7840 against 16x4@300-7840), and reported an undeclared axis for two
    S0 rows whose edges were equally undeclared and therefore not different at all. A refusal that
    misnames its own cause sends the reader to the wrong firmware constant.
    """
    why = []
    if (a[0], a[1]) != (b[0], b[1]):
        why.append("the SHAPE differs -- %sx%s against %sx%s, which is not one matrix width"
                   % (a[0], a[1], b[0], b[1]))
    if (a[2], a[3]) != (b[2], b[3]):
        # ⚠️"Undeclared" only when the DIFFERENCE is itself a declared-vs-absent one. Asking
        # "is any edge None?" put an UNDECLARED message on a pair whose f_lo genuinely differed
        # (62.5 against 300) merely because both left f_hi absent -- the same misnaming one level
        # down from the bug this function replaced.
        differing = [i for i in (2, 3) if a[i] != b[i]]
        if any((a[i] is None) != (b[i] is None) for i in differing):
            why.append("one of them leaves its band axis UNDECLARED (an S0 row carries no "
                       "f_lo_hz/f_hi_hz, shown as ?). An axis that is not stated cannot be "
                       "shown to be the axis that is, so it is refused rather than assumed")
        else:
            why.append("the shape can match while the band EDGES do not, and then band k is "
                       "a different frequency in the two")
    return "; and ".join(why) if why else "they compare unequal on no named field"


def _resolve_identity(row: Dict[str, Any]) -> Optional[str]:
    """Rename an UNPROVISIONED id in place to the node it is declared to be. Returns the raw id
    when it renamed one, None otherwise.

    ⚠️CALLED BEFORE `_node_mismatch`, NEVER INSTEAD OF IT. The guard below still has to agree
    with the fetch afterwards; all this does is decide which name the guard is comparing. See
    `hear.identity` for what may be renamed (only night_node.ino:88's `hear-<mac tail>` form, and
    only with an entry stating its evidence) and why the raw id is kept on the row rather than
    overwritten.
    """
    node, node_from, raw = ID.resolve(row.get("node"), row.get("node_from"))
    if raw is None:
        return None
    row["node"], row["node_from"], row["node_alias_of"] = node, node_from, raw
    return raw


def _node_mismatch(row: Dict[str, Any], expect: Optional[str]) -> Optional[str]:
    """The name the ROW carries against the name the FETCH says it came from. Returns the row's
    name when they disagree, None when they agree or when there is nothing to compare.

    ⚠️MEASURED, 2026-09-07 drain. `mach/scene.csv` is one file off one card written across 15
    boot sessions, and rows 2610-2885 of it -- one complete boot, uptime 14->298 s, sample 0 to
    275*16384, every row utc_us == 0 -- carry `node` = "nyquist". The floor of that block is
    38.50 dB and its quiet-time band spread 0.75 dB, inside mach's other fourteen sessions
    (36.75-39.00 dB, 0.50-1.00 dB) and nowhere near nyquist's four (23.75-26.25 dB, 1.50-16.50
    dB): whatever hardware stood at mach's position recorded them. The `node` column is NODE_ID,
    a compile-time #define from tools/gen_secrets.py, so a mid-file identity change means the
    BINARY changed -- gen_secrets.py:30-31 names the failure verbatim ("copying the file by hand
    is how a node ends up flashed with another node's identity").

    Without this check `ingest_scene` buckets on the row's own label, so those 276 rows landed in
    scene/unanchored/nyquist.jsonl.gz. The whole drain holds exactly ONE legitimately unanchored
    nyquist row, so that partition was 276/277 = 99.6% another node's microphone, and `scene()`
    walks `unanchored` by default while `scene_matrix()` cannot even switch it off.

    ⚠️FOUR THINGS THAT LOOKED LIKE THIS GUARD AND ARE NOT. (a) `origin` reaches the ledger entry
    and was never compared to anything. (b) `default_node` does NOT do this job on its own:
    `scenefile.read_text` consults it only when the node cell is EMPTY, and an S2 row always
    carries one, so the argument is structurally unreachable for exactly this file. (c)
    `node_from` is stored on every record and read by nothing outside a test. (d) the ledger
    assertion `rows == added + duplicate + skipped` still closed -- the rows were MIS-ROUTED, not
    dropped, which is the loss a conservation check cannot see. That is why the refusal below is
    counted into `skip_reasons`: the assertion has to keep closing for a reason, not by luck.

    Refusing rather than relabelling is deliberate. The label is evidence that a node was flashed
    with the wrong identity; rewriting it to the fetch's name would file the rows correctly and
    destroy the only trace of the flashing error.

    ⚠️ONE THING IS RENAMED BEFORE THIS RUNS, AND IT IS NOT A NAME. `_resolve_identity` maps an
    UNPROVISIONED id -- night_node.ino:88's `hear-<mac tail>`, emitted only by a build with no
    NODE_ID compiled in -- to the node that board is declared to be, keeping the raw id on the
    row as `node_alias_of`. That is a different act from the one refused above: `nyquist` is a
    name someone chose and can be wrong about, a MAC tail is the board itself. This guard is
    unchanged and still fires on the renamed name, so a `hear-...` row that turns up on the wrong
    card is refused exactly as it was. See `hear.identity`.
    """
    if not expect:
        return None
    node = row.get("node")
    # ⚠️"alias" IS A NAME THE ROW CARRIES, exactly as "file" is. Only "argument" means the name
    # came from `expect` itself and so cannot disagree with it. Reading this as `!= "file"`
    # disarmed the guard for every renamed row -- caught by
    # tests/test_node_alias.py::test_an_unnamed_boot_on_the_WRONG_card_is_still_refused, which
    # put rankine's card in mach's fetch and watched six rows walk in.
    if not node or row.get("node_from") not in ("file", "alias"):
        return None            # the name came from `expect` itself: nothing disagrees
    return None if node == expect else str(node)


def _record_from_node_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One `hear.detsfile` row -> one pool record. Raises ValueError on an undecodable frame."""
    frame = binascii.unhexlify(row["frame_hex"])
    d = SK.unpack(frame)
    utc_us = int(row.get("utc_us") or 0)
    node = row["node"]
    # fs: the FRAME is authoritative when it states a rate; the CSV column is only the node's
    # running estimate.
    fs_csv = row.get("fs_hz")
    fs_csv = float(fs_csv) if fs_csv not in (None, "") else None
    return {
        "schema_version": SCHEMA_VERSION,
        "key": key("node", node, utc_us, row.get("sample"), frame),
        "source": "node",
        "node": node,
        "node_from": row.get("node_from"),
        # Present ONLY on a renamed row: absent is not False, it is "this row was never
        # renamed". Every row already written is absent, and stays byte-identical.
        **({"node_alias_of": row["node_alias_of"]} if row.get("node_alias_of") else {}),
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
        # ⚠️THE INGEST USED TO DROP THESE TWO, WHICH IS WHY AN UNANCHORED ROW WAS UNRECOVERABLE
        # THE MOMENT IT LANDED. Every generation of dets.csv has carried them -- they are in
        # `hear.detsfile._BASE`, i.e. even G1 -- and they are the only fields that place a row on
        # the node's own PPS edge counter. `utc_us == 0` says the node could not NAME the edge;
        # `pps_n` still says WHICH edge, and `us_since_pps` how far into it. Without the pair, a
        # row whose neighbours in the same boot ARE anchored cannot be placed even in principle,
        # and 517 mach records were stored that way (2026-09-10, /pool/corpus/records).
        # Keeping them does NOT make a row anchored -- see hear/unanchored.py for what may and
        # may not be reconstructed from them, and why the pre-lock case is refused.
        # ⚠️Records already written keep the shape they were written with: the pool is
        # content-addressed and a re-fetch of the same row dedupes rather than rewrites. These
        # fields appear on rows ingested from here on, not retroactively.
        "pps_n": row.get("pps_n"),
        "us_since_pps": row.get("us_since_pps"),
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
        recs, bad, mism, aliased = [], {}, {}, {}
        for row in read.rows:
            raw_id = _resolve_identity(row)
            if raw_id:
                aliased[raw_id] = aliased.get(raw_id, 0) + 1
            wrong = _node_mismatch(row, default_node)
            if wrong:
                mism[wrong] = mism.get(wrong, 0) + 1
                continue
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
        if mism:
            # ONE reason key, not one per name: a garbage file must not be able to grow the
            # breakdown without bound. The names live in their own field, like decode_errors.
            reasons["node_mismatch"] = reasons.get("node_mismatch", 0) + sum(mism.values())
        skipped = len(read.skips) + sum(bad.values()) + sum(mism.values())
        entry = {
            "kind": "dets.csv", "path": os.path.abspath(path), "origin": origin or path,
            "sha256": sha, "bytes": len(raw), "generation": read.generation.name,
            "rows": len(read.rows) + len(read.skips), "decoded": len(recs), "added": added,
            "duplicate": len(recs) - added,
            "skipped": skipped, "skip_reasons": reasons, "node_mismatch": mism,
            # ⚠️NOT a skip reason and not part of the sum: an aliased row was KEPT. It is its own
            # field so "how many rows did this file need renaming to be readable at all" is a
            # number the ledger answers, beside the refusals rather than inside them.
            "aliased": aliased,
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
                     origin: Optional[str] = None, partial: bool = False,
                     fetch_audit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Ingest one `scene.csv` (or a byte-range tail of one). Idempotent.

        `partial=True` says this text came from `GET /sd?file=scene.csv&tail=N`, which starts
        mid-line and carries no header. The leading fragment is DROPPED and the drop is reported
        -- see `hear.scenefile.read_text`. Off by default so a whole file that begins mid-row
        still reads as the corruption it is.

        `fetch_audit` is what the FETCH knows and this file cannot: how far back the tail reached
        against the file it was cut from, and therefore how many bytes of that file no fetch has
        ever asked for. It lands as its own top-level ledger key. Rows this read refused are
        `skipped`; bytes never requested are not rows and are counted apart from them.
        """
        raw = open(path, "rb").read()
        sha = hashlib.sha256(raw).hexdigest()
        read = SF.read_text(raw.decode("utf-8", "replace"), default_node=default_node,
                            allow_partial_first_line=partial)

        recs: List[Dict[str, Any]] = []
        bad: Dict[str, int] = {}
        mism: Dict[str, int] = {}
        aliased: Dict[str, int] = {}
        for row in read.rows:
            # BEFORE the decode, because a row whose identity is wrong is not a row this file may
            # contribute no matter how well it decodes. See `_node_mismatch`.
            raw_id = _resolve_identity(row)
            if raw_id:
                aliased[raw_id] = aliased.get(raw_id, 0) + 1
            wrong = _node_mismatch(row, default_node)
            if wrong:
                mism[wrong] = mism.get(wrong, 0) + 1
                continue
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
                **({"node_alias_of": row["node_alias_of"]} if row.get("node_alias_of") else {}),
                "utc_us": utc_us, "anchored": utc_us > 0, "ts_utc_s": ts,
                "mel_b64": base64.b64encode(d["q"].tobytes()).decode(),
                "ref_db": d["ref_db"], "bands": d["bands"],
                # `slices`, never `frames`. ⚠️And `frames_summed` is the ROW TOTAL (firmware
                # SCENE_FRAMES = slices * frames_per_slice = 4 * 16 = 64), NOT the per-slice
                # count -- use scenefile.frames_per_slice() for that. Neither is the shape.
                "slices": d["slices"], "frames_summed": d["frames_summed"],
                "span_ms": d["span_ms"],
                # `not in (None, "")` rather than truthiness, as stated intent. ⚠️It changes
                # NOTHING on the CSV path: the value arrives as a string and "0" is truthy, so
                # both forms agree on "62.5", "0", "" and absent -- measured. Only a float 0.0
                # differs, which no CSV yields. Written this way so the rule ("empty means
                # undeclared, zero means zero") is legible if a non-CSV caller ever appears.
                "f_lo_hz": (float(row["f_lo_hz"]) if row.get("f_lo_hz") not in (None, "")
                            else None),
                "f_hi_hz": (float(row["f_hi_hz"]) if row.get("f_hi_hz") not in (None, "")
                            else None),
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
        if mism:
            reasons["node_mismatch"] = reasons.get("node_mismatch", 0) + sum(mism.values())
        skipped = len(read.skips) + sum(bad.values()) + sum(mism.values())
        entry = {
            "kind": "scene.csv", "path": os.path.abspath(path), "origin": origin or path,
            "sha256": sha, "bytes": len(raw), "generation": read.generation.name,
            "rows": len(read.rows) + len(read.skips), "decoded": len(recs), "added": added,
            "duplicate": len(recs) - added, "skipped": skipped, "skip_reasons": reasons,
            # Which OTHER node's name the refused rows carried, and how many. A count in
            # skip_reasons says the drain refused something; this says a node is flashed wrong.
            "node_mismatch": mism,
            # Kept rows that had to be renamed first. See `ingest_dets` for why it sits outside
            # `skip_reasons` and outside the conservation sum.
            "aliased": aliased,
            "decode_errors": bad, "partial_first_line": read.partial_first_line,
            "schema_version": SCHEMA_VERSION,
        }
        if fetch_audit is not None:
            # ⚠️TOP LEVEL, NOT INSIDE `skip_reasons`. `skipped` is len(read.skips)+sum(bad) and
            # `skip_reasons` is the breakdown that must sum to it; a byte-accounting key filed in
            # there would make one counter mean two things, which is the bug class this whole
            # audit exists to catch. Bytes the fetch never asked for are not rows this read
            # skipped, so they are counted somewhere else entirely.
            entry["fetch_audit"] = fetch_audit
        # This file's rows are fully accounted for, and the breakdown totals the count it breaks
        # down. The second half was true before it was checked, which is exactly how it stops
        # being true silently.
        assert entry["rows"] == added + entry["duplicate"] + skipped, entry
        assert sum(reasons.values()) == skipped, entry
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

        Refuses to stack rows of differing geometry rather than reshaping to fit. The geometry is
        `bands`, `slices` AND the band edges `f_lo_hz`/`f_hi_hz`: all four are firmware's to
        change, and two banks at one 20x4 shape stack into a matrix whose column k is a different
        frequency in different rows -- which reads as a dataset and is not one. The edges are what
        separate them; `bands`/`slices` alone cannot.

        ⚠️An S0 row declares no edges at all (the columns arrive as None). A row whose axis is
        unknown cannot be shown to share an axis with one whose axis is known, so an S0 row and an
        S1/S2 row refuse to stack even if the same firmware wrote both. That is the intended
        reading of an undeclared axis, not a bug; select a day or node to get one generation.

        ⚠️`limit` BOUNDS WHAT IS BUILT, NOT WHAT IS CHECKED. The selection is walked to the end
        whatever the limit, and a differing row past it still raises. Stopping the walk at the
        limit would have made the keyword silently disable the guard the paragraph above promises,
        and it would have handed back a sample drawn only from whichever geometry sorted first --
        biased in a way the caller has no way to see. Measured on a 41,507-row pool: the full walk
        costs 0.240 s against 0.009 s for the old early break, while the full build is 0.818 s. The
        limit still bounds MEMORY, which is what it is for; only `limit` rows are ever decoded.
        """
        if mode not in ("db", "q"):
            raise ValueError("mode must be 'db' or 'q'")
        rows, out = [], []
        geom = None
        for r in self.scene(node=node, day=day):
            g = (r["bands"], r["slices"], r.get("f_lo_hz"), r.get("f_hi_hz"))
            if geom is None:
                geom = g
            elif g != geom:
                raise ValueError("mixed scene geometry: %s then %s -- %s. Select a day or node "
                                 "whose firmware did not change."
                                 % (_geom_str(geom), _geom_str(g), _why_geoms_differ(geom, g)))
            if limit is not None and len(rows) >= limit:
                continue                    # keep CHECKING; stop collecting
            q = np.frombuffer(base64.b64decode(r["mel_b64"]), dtype=np.int8) \
                .reshape(r["bands"], r["slices"]).astype(float)
            rows.append((q / 2.0 + r["ref_db"] if mode == "db" else q).reshape(-1))
            out.append(r)
        if not rows:
            return np.zeros((0, 0)), []
        return np.vstack(rows), out

    def scene_stats(self) -> Dict[str, Any]:
        """What the scene store holds. Counted by walking it, because a row count taken from the
        ledger would credit rows a later gzip write could have lost.

        `geometry` keys on `_geom_str` -- shape AND band edges, e.g. `20x4@62.5-7812.5`. This is
        the only place a mix of banks is visible WITHOUT raising, so the key has to carry enough
        to tell two 20x4 banks apart; a bare `20x4` would total two axes into one number.
        """
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
            g = _geom_str((r["bands"], r["slices"], r.get("f_lo_hz"), r.get("f_hi_hz")))
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
                # ⚠️"(unstated)", not str(None). A `str()` here buckets a row with no tier under
                # the literal key "None", which is indistinguishable in the drain output from a
                # producer that emitted the string "None" as its tier. The parenthesised form
                # cannot collide with any tier a producer could publish.
                tier = r.get("clock_tier")
                tk = "(unstated)" if tier is None else str(tier)
                by_tier[tk] = by_tier.get(tk, 0) + 1
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
