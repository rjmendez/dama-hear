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

#: Phone `clock_tier` values whose stamp is a UTC MEASUREMENT rather than a wall-clock reading.
#: Copied deliberately, not invented: this is dama-gotchi's own
#: `sensors/tdoa_triangulation.py:449 GOOD_CLOCK_TIERS`, and `EskfFusion.kt:318`
#: `BAD_PEER_CLOCK_TIERS = setOf("wall", "network", "unknown")` is the same split from the other
#: side. ⚠️"network" IS EXCLUDED. It is a NETWORK_PROVIDER fallback anchor, not a GPS one --
#: GPSTimingSync.java:612-616 says in as many words that it is "deliberately NOT treated as
#: clock-trustworthy ... same as 'wall'", and its declared sigma is 25 ms (vs 5 ms for "location")
#: which is 8.6 m at 343 m/s. A tier not listed here is not trusted, including one this version
#: has never heard of: an unrecognised label is a producer we do not understand, and the
#: conservative reading is the one that does not admit it to a solve.
TRUSTED_CLOCK_TIERS = frozenset({"gnss", "location"})


def utc_trusted_of(fields: Dict[str, Any]) -> Optional[bool]:
    """The rungs behind `Record.utc_trusted`, over a plain mapping.

    Read that property's docstring for what each rung is and why. This exists as a function so
    that `Record` and `hear.pool.Pool.stats` -- which asks the same question of a STORED row dict,
    before any Record is built -- cannot answer it two different ways. Two readers of one message
    disagreeing about it is the defect, not the duplication.

    `fields` is anything with `source`, `clock_tier`, `onset_dated` and `utc_trusted` keys, absent
    meaning not stated: a stored pool row, or a Record's own attributes plus its `extra`.
    """
    # ⚠️THE GUARDS RUN FIRST, AND A PRODUCER'S STATEMENT DOES NOT DEFEAT THEM. An earlier version
    # honoured a stated bool before either, so `{"source": "node", "utc_trusted": True}` returned
    # True for a sensor whose clock trust is a different measurement entirely, and
    # `{"onset_dated": False, "utc_trusted": True}` returned True for an onset the producer had
    # just said it could not date. A statement that contradicts a refusal the same producer made
    # is not extra information; the conservative reading is the one that does not admit it.
    if fields.get("source") != "phone":
        return None
    if fields.get("onset_dated") is False:
        return False
    stated = fields.get("utc_trusted")
    if stated is not None:
        # ⚠️A NON-BOOL STATEMENT IS REFUSED, NOT DISCARDED. `isinstance(stated, bool)` alone let
        # `utc_trusted: 0` and `utc_trusted: "false"` fall through to the derivation and come back
        # TRUE -- a producer saying "do not trust me" read as trustworthy, which is this project's
        # recurring failure pointing the worst possible way. An unparseable statement is a
        # producer this version does not understand, and that is exactly the case the unknown-tier
        # rule already refuses.
        return stated if isinstance(stated, bool) else False
    tier = fields.get("clock_tier")
    if tier is None:
        return None
    return tier in TRUSTED_CLOCK_TIERS


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

    @property
    def utc_trusted(self) -> Optional[bool]:
        """Is `ts_utc_s` a UTC measurement, or a wall-clock reading wearing one's clothes?

        ⚠️DERIVED, NOT A FIELD. `hear/backend/associate.py` refuses arrivals on a `utc_trusted`
        key, and nothing has ever published one. The temptation is to add the boolean to the
        phone's payload; the phone has been publishing the same fact all along under the name
        `clock_tier`, and a NEW key would be absent on every row recorded before its rollout --
        which `arrival_is_usable` reads as usable (ABSENT MEANS USABLE, associate.py:90-98). The
        wall-clock stamps this flag exists to refuse would sail through it. Deriving from a field
        that is already on the wire answers correctly for history too, and costs no APK.

        THE INVARIANT THIS RESTS ON, in the CURRENT producer: `AcousticRangingCollector.kt:3479`
        computes `stamp` only when `onsetBootNs != null` (a HAL AudioTimestamp existed), and
        `clock_tier`/`sync_sigma_ns`/`ts_utc_ms` are put only inside `if (stamp != null)`
        (:3498-3500). `GPSTimingSync.java:272` sets tier "wall" on exactly the no-fresh-anchor
        branch. So a stated non-wall tier means BOTH halves of the operator's definition. An older
        build that published a tier without that coupling would over-trust here, which is what
        rung (b) is for, and why `onset_dated` must be carried through the pool alongside it.

        Rungs, in order -- and the GUARDS COME FIRST, deliberately:
          (a) `source != "phone"` -> None. A node's timestamp trust is a DIFFERENT measurement
              (PPS lock, tAcc, `pps_bad` in health.csv). ⚠️A node DOES set `clock_tier` --
              `hear/node/telemetry.py:110` emits "pps" or "free" -- which is precisely why this
              rung tests the SOURCE and not the presence of the field: neither of those labels
              is in TRUSTED_CLOCK_TIERS, so reading a node off this scale would silently call
              every PPS-locked node untrusted.
          (b) `onset_dated is False` -> False. The phone says outright it could not date it.
          (c) `utc_trusted` stated -> that value if it is a bool, else False. A producer stating
              it outright outranks the DERIVATION, but not the two refusals above: a statement
              contradicting the same producer's own "could not date" is not new information.
          (d) `clock_tier is None` -> None. The producer did not say.
          (e) else `clock_tier in TRUSTED_CLOCK_TIERS`.

        None means NOT STATED, and `associate.arrival_is_usable` treats that as usable by design.
        That is deliberate, not an oversight -- see tests/test_associate.py.

        ⚠️THE TIER IS A LABEL; `sync_sigma_ns` IS THE MEASUREMENT OF THE SAME THING, and on this
        fleet they disagree: `tdoa_triangulation._clock_ok` had to add a sigma bar after 3,536
        arrivals claimed a gnss/location tier while stating a sigma worse than the 50 ms wall
        value. No sigma bar is applied here on purpose -- that threshold is dimensioned for
        precision multilateration, not for corpus admission -- so a caller doing geometry must
        read `sync_sigma_ns` as well as this flag.
        """
        return utc_trusted_of(dict(self.extra, source=self.source, clock_tier=self.clock_tier))


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
    # The layout decides what band k MEANS, so a disagreement between the frame and the JSON
    # beside it is not cosmetic -- one of the two producers is not the version we think it is.
    if "layout" in payload and payload["layout"] != d["layout"]:
        raise SkipReason("layout: json says %r, frame flags say %r"
                         % (payload["layout"], d["layout"]))
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
        # ⚠️`onset_found` AND `onset_dated` ARE BOTH LOAD-BEARING, not colour. They are the two
        # keys hear/backend/associate.py refuses arrivals on -- one directly, one through
        # `Record.utc_trusted` -- so a Record that drops them cannot answer the gate's question
        # about itself. `onset_found` was being dropped here entirely
        # (AcousticRangingCollector.kt:3513 publishes it).
        extra=dict({k: payload[k] for k in
                    ("trigger_ts_utc_ms", "onset_offset_us", "onset_dated", "onset_found",
                     "since_prev_s", "utc_trusted")
                    if k in payload},
                   # from the FRAME, not the JSON: it decides whether this row can be aligned
                   # with a row from a sensor running at another rate.
                   layout=d["layout"], valid_bands=d["valid_bands"]),
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
        extra={"layout": d["layout"], "valid_bands": d["valid_bands"]},
    )


def feature_matrix(records: Iterable[Record], fs_hz: float,
                   mode: str = "db"
                   ) -> Tuple[np.ndarray, List[Record], Dict[str, Any]]:
    """[n, bands*frames] for the records on `fs_hz`'s axis, those records, and WHAT IT DROPPED.

    `fs_hz` is REQUIRED and is not inferred from the data. Inferring it makes the common mistake
    -- a corpus of mostly-48 kHz phones with a handful of 16 kHz nodes -- silent: the majority
    wins the inference and the minority is stacked onto an axis it was never measured on.

    Records with no stated rate are excluded. They may well be `fs_hz`; nothing in the frame says
    so, and a training matrix is the wrong place to guess.

    ⚠️THE RATE MATCH IS EXACT AND THAT IS DELIBERATE, BUT IT MUST NOT BE SILENT.

    A frame can only state one of the nine enumerated `SK.FS_CODES` rates, so a frame-stated rate
    is NOMINAL. When the frame states nothing the pool falls back to the dets.csv `fs_hz` column,
    which is the PPS-DISCIPLINED measured rate (`pool.fs_stated_by == "csv"`). Both land in this
    field and `!=` cannot tell them apart.

    Measured on ~/hear-pool 2026-09-08, 1038 records, all from the same two 16 kHz nodes:

        83   frame-stated, exactly 16000.0
        519  csv-stated, which happened to read exactly 16000.0
        436  csv-stated, one of 30 neighbours spanning 15936.0-16034.909 (+/-0.400%)

    `feature_matrix(recs, 16000.0)` kept 602 and dropped 436 -- 42.0% of the corpus -- returning
    a clean (602, 160) matrix and no way to find out. Those 436 are not another sensor and not
    another axis: the nearest is 15992.099 Hz, 0.05% away, whose band edges differ from the
    nominal bank by 0.024 Hz at band 1 and 3.9 Hz at band 19, under 4% of a band width. What
    decided whether a row survived was whether that window's clock discipline happened to round
    to 16000.000. That is a coin flip, not a frequency-axis decision.

    It matters most for exactly the corpus that has nowhere else to go: `aligned_matrix` refuses
    all 955 legacy `nyquist`-layout records and tells the caller to "use feature_matrix() per
    rate", and this is what happens when they do.

    So the tally distinguishes the two cases the exact match cannot: `nearest_dropped_frac` near
    0 means you just threw away YOUR OWN NODE over clock discipline, and near 2.0 means you
    correctly refused a 48 kHz phone against a 16 kHz axis. Widening the match by default would
    silently GROW a training set, which is the same bug pointing the other way -- so the KEPT set
    is unchanged here and only what is said about it is new.

    `aligned_matrix` -- whose ValueError tells the caller to "use feature_matrix() per rate" --
    already returns an info dict. This one now does too, for the same reason and in the same
    shape: the two are the only ways to build a training matrix here, and the loud one hands you
    to this one.

    mode "db"  -- absolute dB, ref_db restored. Amplitude alone was worth AUC 0.90 on the 2026-09-05
                  corpus, so this is the default.
    mode "q"   -- the raw int8, shape only. Use when levels are not comparable across sites.
    """
    if mode not in ("db", "q"):
        raise ValueError("mode must be 'db' or 'q'")
    target = float(fs_hz)
    kept: List[Record] = []
    rows: List[np.ndarray] = []
    n_in = 0
    no_rate = 0
    dropped_rates: Dict[float, int] = {}
    for r in records:
        n_in += 1
        if r.fs_hz is None:
            no_rate += 1
            continue
        rate = float(r.fs_hz)
        if rate != target:
            dropped_rates[rate] = dropped_rates.get(rate, 0) + 1
            continue
        v = r.db if mode == "db" else r.q.astype(float)
        rows.append(v.reshape(-1))
        kept.append(r)

    n_other = sum(dropped_rates.values())
    nearest = min(dropped_rates, key=lambda x: abs(x - target)) if dropped_rates else None
    info: Dict[str, Any] = {
        "fs_hz": target,
        "n_in": n_in,
        "kept": len(kept),
        "dropped_no_rate": no_rate,
        "dropped_other_rate": n_other,
        # Every rate that was refused, so "one stray phone" and "the whole corpus, disciplined"
        # are not the same line in a log.
        "dropped_rates_hz": sorted(dropped_rates.items()),
        "nearest_dropped_rate_hz": nearest,
        # |nearest - fs| / fs. ~0 is clock discipline on the sensor you asked for; ~2.0 is a
        # 48 kHz phone against a 16 kHz axis. The number that says which mistake you just made.
        "nearest_dropped_frac": (abs(nearest - target) / target) if nearest is not None else None,
    }
    # The invariant this function existed without: the parts total the whole.
    assert info["kept"] + no_rate + n_other == n_in, info

    if not rows:
        return np.zeros((0, 0)), [], info
    w = {len(r) for r in rows}
    if len(w) != 1:
        raise ValueError("mixed sketch geometry in one matrix: %s" % sorted(w))
    return np.vstack(rows), kept, info


def aligned_matrix(records: Iterable[Record], mode: str = "db",
                   strict: bool = False) -> Tuple[np.ndarray, List[Record], Dict[str, Any]]:
    """ONE matrix across sensors running at DIFFERENT rates, over the bands they share.

    This is the thing `feature_matrix` refuses to do, and it is only safe because of the fixed
    band layout: under `LAYOUT_FIXED` band k is the same frequency at every rate, so a 16 kHz
    node's bands 0-14 are the SAME MEASUREMENT as a 48 kHz phone's bands 0-14. The bands a slow
    node cannot reach are dropped from every row, including the fast ones -- a matrix is only as
    wide as its narrowest sensor.

    Measured on the 228 labelled events (train 48 kHz, score the same audio at 16 kHz):

        rescaled bank, all 20 bands       0.9141   <- what shipping without this does
        fixed bank, all 20 bands          0.9306   <- fixed axis, empty bands NOT masked
        fixed bank, common 15 bands       0.9473   <- this function

    ⚠️REFUSES ANY `nyquist`-LAYOUT RECORD. Those bands are rescaled per rate, so "band 14" is a
    different frequency in every row and the alignment this function performs would be fiction.
    A frame that does not state its rate is dropped for the same reason.

    Returns (X, kept, info). `info` names the rates included and the band count, because a matrix
    whose width silently depends on which sensors happened to report is not reproducible.
    """
    if mode not in ("db", "q"):
        raise ValueError("mode must be 'db' or 'q'")
    recs = [r for r in records if r.fs_hz is not None]
    bad = [r for r in recs if r.extra.get("layout", SK.LAYOUT_FIXED) != SK.LAYOUT_FIXED]
    if bad:
        raise ValueError(
            "%d record(s) use the %r layout, whose band edges are rescaled per rate; they cannot "
            "be aligned with anything. Re-sketch them or use feature_matrix() per rate."
            % (len(bad), SK.LAYOUT_NYQUIST))
    if not recs:
        return np.zeros((0, 0)), [], {"bands": 0, "frames": 0, "rates_hz": [], "dropped": 0}
    geom = {(r.bands, r.frames) for r in recs}
    if len(geom) != 1:
        raise ValueError("mixed sketch geometry: %s" % sorted(geom))
    bands, frames = geom.pop()
    rates = sorted({float(r.fs_hz) for r in recs})
    k = min(SK.valid_bands(fs, bands, layout=SK.LAYOUT_FIXED, strict=strict) for fs in rates)
    if k <= 0:
        raise ValueError("no band is usable by every rate in %s" % rates)
    rows = []
    for r in recs:
        v = r.db if mode == "db" else r.q.astype(float)
        rows.append(v[:k, :].reshape(-1))
    info = {"bands": k, "frames": frames, "rates_hz": rates, "strict": bool(strict),
            "dropped_bands": bands - k,
            "top_edge_hz": float(SK.band_edges_hz(max(rates), bands,
                                                  layout=SK.LAYOUT_FIXED)[k + 1])}
    return np.vstack(rows), recs, info


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
        X, kept, fm = feature_matrix(recs, a.fs, a.mode)
        print("\nmatrix for %.0f Hz: %s (%d of %d records)"
              % (a.fs, X.shape, len(kept), len(recs)))
        # The drop used to be the difference between two numbers nobody subtracted.
        if fm["dropped_no_rate"] or fm["dropped_other_rate"]:
            print("  dropped %d: %d with no stated rate, %d on another rate"
                  % (fm["dropped_no_rate"] + fm["dropped_other_rate"],
                     fm["dropped_no_rate"], fm["dropped_other_rate"]))
            if fm["nearest_dropped_rate_hz"] is not None:
                print("  nearest dropped rate %.3f Hz (%.3f%% away)%s"
                      % (fm["nearest_dropped_rate_hz"], 100.0 * fm["nearest_dropped_frac"],
                         "  <- clock discipline on this same axis, not another sensor"
                         if fm["nearest_dropped_frac"] < 0.01 else ""))
            for rate, n in fm["dropped_rates_hz"][:10]:
                print("    %12.3f Hz  %5d" % (rate, n))
            if len(fm["dropped_rates_hz"]) > 10:
                print("    ... %d more rate(s)" % (len(fm["dropped_rates_hz"]) - 10))
        if a.npz:
            np.savez(a.npz, X=X, node_id=np.array([r.node_id for r in kept]),
                     ts_utc_s=np.array([-1.0 if r.ts_utc_s is None else r.ts_utc_s for r in kept]),
                     retrigger=np.array([r.retrigger for r in kept]),
                     source=np.array([r.source for r in kept]))
            print("wrote %s" % a.npz)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
