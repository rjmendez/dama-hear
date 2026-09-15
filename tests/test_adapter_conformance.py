#!/usr/bin/env python3
"""The suite EVERY ingress adapter has to pass, run against each adapter with no live service.

`docs/standalone-migration.md` names the independence gate concretely: "a clean-room install with
only the core, one node adapter, local durable storage and test doubles passes this suite". The
other test modules here check one function at a time and are the right shape for that. This one is
the other axis: it takes the failures that have actually cost this project data and asserts that
EVERY adapter answers them the same way, so a second transport cannot quietly be laxer than the
first.

⚠️THE POINT IS THE PARAMETRISATION, NOT THE ASSERTIONS. `ADAPTERS` holds the two node ingresses
that exist today -- a card-backed XIAO's `dets.csv` and a cardless ESP32's `/detections` ring --
behind one `write()` signature. A third adapter (HTTPS batch push, per the migration plan's phase
4) becomes one entry in that list and inherits every case below on the day it lands. That is what
makes this a conformance suite rather than a second pile of unit tests: a new transport cannot be
merged half-conformant, because there is nowhere to put it that skips these.

The seven cases, and what each one is a memory of:

  1. STALE ANCHORS. A XIAO latches `time_valid` at its first fix and never clears it, so a node
     whose GPS UART died keeps stamping at 4.2-11.7 ppm -- about 30 ms/h, invisible in every
     other counter. The stated sigma and clock state must survive ingest unaltered, a stated 0
     must not read as a perfect clock, and ABSENT must stay usable so a new column cannot
     retroactively delete history.
  2. READER LAPS AND TORN COPIES. A file is fetched while the node is still writing it, and a
     ring is read after it has lapped. Both must end as a COUNTED refusal or a NAMED gap, never
     as a short read that looks like a quiet period.
  3. TIMESTAMP FALLBACK. `utc_us == 0` is a row with no anchor, and a phone's `wall` tier is a
     stamp that is not a measurement. Both are KEPT and neither is promoted to an arrival.
  4. MISSING SAMPLE-RATE / PROFILE METADATA. Profile 0 states no rate. The record must say that
     it does not know, and say who told it what it does know.
  5. TRANSPORT LOSS, RETRY AND IDEMPOTENCY. Every delivery is at-least-once: a retried file, an
     overlapping page and a re-read cursor add one event, and a cursor never advances past what
     was actually ingested.
  6. CROSS-PROFILE EVENT IDENTITY. One detection has one identity across transports, and two
     detections that differ only in what their bytes MEAN must not collapse into one.
  7. RECOUPLING IMPORT BOUNDARY. Merge-blocking, and it lives in tools/recoupling_guard.py with
     its own tests; the case here is the one line that makes a red gate fail the suite too.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import struct
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import corpus as C                                        # noqa: E402
from hear import detsfile as DF                                     # noqa: E402
from hear import nodeclass as NC                                    # noqa: E402
from hear import pool as P                                          # noqa: E402
from hear import sketch as SK                                       # noqa: E402
from hear import wire as WR                                         # noqa: E402
from hear.backend import associate as AS                            # noqa: E402
from tools import hear_drain as HD                                  # noqa: E402
from tools import recoupling_guard as RG                            # noqa: E402

NODE = "nyquist"
BOOT_ID = "0011223344556677"
ANCHORED_UTC_US = 1788763952189911


# --------------------------------------------------------------------------- frames

def sketch_frame(seed: int, *, profile_id: int = 1, us_of_day: int = 50_000_123,
                 node_id: int = 202, seq: int = 7) -> bytes:
    """One packed v2 frame. Deterministic in `seed` so two adapters can be handed the same bytes."""
    rng = np.random.default_rng(seed)
    q, ref = SK.sketch(rng.normal(0, 1000, 4096), 48000.0)
    return WR.pack_v2(us_of_day=us_of_day, node_id=node_id, seq=seq, ref_db=ref, peak=1500,
                      q=q, profile_id=profile_id)


def legacy_profile0_frame(seed: int) -> bytes:
    """A v2 frame carrying the rate-unstated legacy profile, built the only way one can be.

    ⚠️`pack_v2` REFUSES profile 0 ON PURPOSE -- "a frame that cannot say its own rate is the
    defect, not a choice" -- so a new one cannot be minted and this rewrites the profile field of
    a minted frame instead. That is not a trick: frames with profile 0 in them are already in the
    corpus and on the cards, and the property under test is that a READER handed one of those
    says it does not know the rate rather than defaulting to the current one.
    """
    b = bytearray(sketch_frame(seed, profile_id=1))
    flags = struct.unpack_from("<H", b, 11)[0]
    struct.pack_into("<H", b, 11, flags & ~(WR._F_PROFILE_MASK << WR._F_PROFILE_SHIFT))
    return bytes(b)


def hexed(frame: bytes) -> str:
    return binascii.hexlify(frame).decode()


# --------------------------------------------------------------------------- one detection

@dataclass
class Det:
    """One detection as a node states it, transport-independent.

    Only the fields a case actually varies are here. Everything else is a constant, because a
    conformance case that has to spell nineteen columns to say "this row has a stale anchor" is
    one nobody reads.
    """
    frame: bytes
    utc_us: int = ANCHORED_UTC_US
    sample: str = "1234"
    uptime_s: str = "5000"
    fs_hz: Optional[str] = "48000.000"
    sync_sigma_ns: Optional[str] = None
    clock_state: Optional[str] = None
    anchor_age_us: Optional[str] = None
    clip: str = ""

    def cells(self) -> Dict[str, Any]:
        return {
            "node_id": NODE,
            "utc_us": str(self.utc_us),
            "uptime_s": self.uptime_s,
            "sample": self.sample,
            "pps_n": "42",
            "us_since_pps": "597174",
            "trigger": "dedupe",
            "flags": "5889",
            "fs_hz": "" if self.fs_hz is None else self.fs_hz,
            "sketch_back": "192",
            "frame_hex": hexed(self.frame),
            "clip": self.clip,
            "clip_why": "",
            "sync_sigma_ns": "" if self.sync_sigma_ns is None else self.sync_sigma_ns,
            "clock_state": "" if self.clock_state is None else self.clock_state,
            "anchor_age_us": "" if self.anchor_age_us is None else self.anchor_age_us,
            "boot_epoch_us": "1788763951000000",
            "boot_id": BOOT_ID,
            "clock_discontinuity_flags": "0",
        }


# --------------------------------------------------------------------------- the adapters

def _write_dets_csv(tmp_path, name: str, dets: List[Det], *, truncate_last: int = 0) -> str:
    """The card-backed adapter: a G7 `dets.csv` exactly as firmware writes one.

    `truncate_last` cuts characters off the final line WITHOUT a newline, which is what a fetch
    that raced the node's own writer leaves behind.
    """
    lines = [",".join(DF.G7.declared)]
    for d in dets:
        cells = d.cells()
        lines.append(",".join(cells[c] for c in DF.G7.written))
    text = "\n".join(lines) + "\n"
    if truncate_last:
        text = text[:-(truncate_last + 1)]
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def _write_live_ring(tmp_path, name: str, dets: List[Det], *, first_i: int = 0,
                     short_frame_last: int = 0) -> str:
    """The cardless adapter: one archived `/detections` cursor-v1 page.

    `short_frame_last` drops hex characters off the last row's frame while leaving `frame_len`
    stating the length the node MEANT to send -- a body truncated in flight, which is otherwise
    indistinguishable from a smaller sketch.
    """
    rows = []
    for n, d in enumerate(dets):
        cells = d.cells()
        row: Dict[str, Any] = {k: cells[k] for k in P.LIVE_RING_COLUMNS if cells.get(k) != ""}
        fh = hexed(d.frame)
        row["frame_len"] = len(d.frame)
        if short_frame_last and n == len(dets) - 1:
            fh = fh[:-short_frame_last]
        row["frame"] = fh
        row["i"] = first_i + n
        rows.append(row)
    body = {
        "contract": "cursor-v1",
        "boot_id": BOOT_ID,
        "rows": rows,
        "returned": len(rows),
        "has_more": False,
        "cursor": HD.live_ring_cursor(BOOT_ID, first_i),
        "oldest_cursor": HD.live_ring_cursor(BOOT_ID, first_i),
        "next_cursor": HD.live_ring_cursor(BOOT_ID, first_i + len(rows)),
        "until_cursor": HD.live_ring_cursor(BOOT_ID, first_i + len(rows)),
    }
    p = tmp_path / name
    p.write_text(json.dumps(body))
    return str(p)


@dataclass(frozen=True)
class Adapter:
    """One ingress, behind the one signature every case below drives."""
    name: str
    write: Callable[..., str]
    ingest: str

    def run(self, pl: "P.Pool", tmp_path, filename: str, dets: List[Det], **kw) -> Dict[str, Any]:
        path = self.write(tmp_path, filename, dets, **kw)
        return getattr(pl, self.ingest)(path, default_node=NODE)


ADAPTERS = (
    Adapter("dets-csv", _write_dets_csv, "ingest_dets"),
    Adapter("live-ring", _write_live_ring, "ingest_detections_json"),
)
ADAPTER_IDS = [a.name for a in ADAPTERS]


@pytest.fixture(params=ADAPTERS, ids=ADAPTER_IDS)
def adapter(request) -> Adapter:
    return request.param


@pytest.fixture
def pl(tmp_path) -> "P.Pool":
    return P.Pool(str(tmp_path / "pool"))


def _one(pl: "P.Pool") -> Dict[str, Any]:
    rows = list(pl.raw())
    assert len(rows) == 1, rows
    return rows[0]


# =========================================================== 1. stale anchors

class TestCase1StaleAnchor:
    """A frozen time anchor is a clock claim, and it must arrive as one or not at all."""

    def test_the_stated_anchor_error_and_clock_state_survive_ingest_unaltered(self, pl, tmp_path,
                                                                              adapter):
        """The node is the only thing that can measure its own anchor age; ingest may not edit it.

        5 ms is a XIAO that has been free-running for roughly ten minutes. The record has to carry
        the number the node stated, in the unit it stated it in, or the class gate downstream is
        grading a value this layer invented.
        """
        adapter.run(pl, tmp_path, "stale", [Det(sketch_frame(1), sync_sigma_ns="5000000",
                                                clock_state="holdover",
                                                anchor_age_us="600000000")])
        rec = _one(pl)
        assert rec["sync_sigma_ns"] == 5_000_000.0
        assert rec["clock_state"] == "HOLDOVER"
        assert rec["anchor_age_us"] == "600000000"

    def test_that_stated_anchor_error_is_refused_as_an_arrival_by_the_class_gate(self, pl,
                                                                                 tmp_path,
                                                                                 adapter):
        """The refusal is the class's to make, and it must name the clock rather than the sound."""
        adapter.run(pl, tmp_path, "stale", [Det(sketch_frame(2), sync_sigma_ns="5000000",
                                                clock_state="HOLDOVER")])
        sigma = _one(pl)["sync_sigma_ns"]
        assert NC.stamp_admissible(sigma, "xiao-s3-pps") is False
        why = NC.stamp_refusal(sigma, "xiao-s3-pps")
        assert why and "clock" in why.lower()
        assert not AS.arrival_is_usable({"stamp_admissible": False})

    def test_a_fresh_anchor_is_admitted_rather_than_refused_for_having_a_number(self, pl,
                                                                                tmp_path,
                                                                                adapter):
        """The gate is a budget and not a hostility to stated uncertainty. A XIAO on a fresh
        anchor states tens of microseconds against a 129.4 us per-node bound, and a gate that
        refused a stated number for being a number would refuse the healthy fleet along with the
        free-running one -- which is how the whole per-detection budget was unreachable before.
        """
        adapter.run(pl, tmp_path, "fresh", [Det(sketch_frame(3), sync_sigma_ns="50000",
                                                clock_state="LOCKED", anchor_age_us="570000")])
        rec = _one(pl)
        assert rec["clock_state"] == "LOCKED"
        assert NC.stamp_admissible(rec["sync_sigma_ns"], "xiao-s3-pps") is True

    def test_a_stated_zero_is_not_read_as_a_perfect_clock(self, pl, tmp_path, adapter):
        """⚠️THE ONE THAT POINTED THE WRONG WAY. "0" is a non-empty string, so a truthiness test
        let a firmware that wrote 0 produce the row the class gate scores as a PERFECT anchor --
        sqrt(100 us^2 + 0) -- which is inside the per-node bound. Unstated is the only honest
        reading of it, and unstated is not admissible-by-number, it is simply not a claim.
        """
        adapter.run(pl, tmp_path, "zero", [Det(sketch_frame(4), sync_sigma_ns="0",
                                               clock_state="FAULT")])
        rec = _one(pl)
        assert rec["sync_sigma_ns"] is None
        assert NC.stamp_admissible(rec["sync_sigma_ns"], "xiao-s3-pps") is None

    def test_an_unstated_anchor_stays_usable_so_a_new_column_cannot_delete_history(self, pl,
                                                                                   tmp_path,
                                                                                   adapter):
        """Every row written before G6 states no sigma. Absent must mean usable, or adding the
        column would retroactively refuse the entire corpus that predates it."""
        adapter.run(pl, tmp_path, "silent", [Det(sketch_frame(5))])
        rec = _one(pl)
        assert rec["sync_sigma_ns"] is None and rec["clock_state"] is None
        assert AS.arrival_is_usable({"stamp_admissible": NC.stamp_admissible(None, "xiao-s3-pps")})


# ================================================ 2. reader laps and torn copies

class TestCase2TornCopyAndReaderLap:
    """A short read is the failure mode that looks exactly like success."""

    def test_a_torn_final_row_is_a_counted_refusal_and_the_good_rows_still_land(self, pl,
                                                                               tmp_path,
                                                                               adapter):
        """The fetch raced the writer. Two rows are whole and one is not, and the ledger has to
        say so by reason -- a reader that cannot name what it dropped has returned a confident
        wrong answer here twice.
        """
        dets = [Det(sketch_frame(10), sample="1"), Det(sketch_frame(11), sample="2"),
                Det(sketch_frame(12), sample="3")]
        kw = {"truncate_last": 40} if adapter.name == "dets-csv" else {"short_frame_last": 40}
        entry = adapter.run(pl, tmp_path, "torn", dets, **kw)
        assert entry["added"] == 2
        assert entry["skipped"] == 1
        # The reason has to name the SHAPE that was wrong, not merely that something was: a
        # ledger saying "1 skipped" and nothing else is the report that hid 730 detections twice.
        reasons = list(entry["skip_reasons"])
        assert len(reasons) == 1
        assert any(w in reasons[0] for w in ("frame_hex_len", "frame_len_mismatch", "row_width")), \
            reasons

    def test_the_arithmetic_closes_on_a_torn_file(self, pl, tmp_path, adapter):
        """`rows == added + duplicate + skipped`, per file, always. A deploy of this tool once
        printed "0 new, 0 duplicate, 0 skipped" while discarding all five rows it read."""
        dets = [Det(sketch_frame(13), sample=str(i)) for i in range(4)]
        kw = {"truncate_last": 40} if adapter.name == "dets-csv" else {"short_frame_last": 40}
        entry = adapter.run(pl, tmp_path, "torn", dets, **kw)
        assert entry["rows"] == entry["added"] + entry["duplicate"] + entry["skipped"]

    def test_an_empty_body_is_an_error_and_not_a_quiet_period(self, pl, tmp_path):
        """A file the node created but never wrote, and a file whose content was lost, read
        identically. Neither is "nothing happened", so neither may be ingested as zero rows."""
        p = tmp_path / "empty.csv"
        p.write_text("")
        with pytest.raises(ValueError):
            pl.ingest_dets(str(p), default_node=NODE)

    def test_a_truncated_page_carries_the_cursor_only_as_far_as_its_contiguous_rows(self):
        """⚠️A TRUNCATED PAGE'S HEADER STILL CLAIMS THE WHOLE PAGE. `returned`/`next_cursor` are
        written before the first row streams, so trusting them walks the cursor over rows that
        never arrived. Only a contiguous prefix counts.
        """
        rows = [{"i": 7}, {"i": 8}, {"i": 10}]
        kept, pos = HD._salvaged_rows(rows, 7)
        assert [r["i"] for r in kept] == [7, 8]
        assert pos == 9

    def test_a_lapped_ring_reports_the_rows_the_pool_will_never_see(self):
        """The reader came back after the ring wrapped. The gap below the oldest row served IS
        the loss, and it has to be a number rather than an absence."""
        prev = {"last_i": 100, "uptime_s": 1000, "at": 2000.0}
        lost, why = HD.live_ring_loss(prev, served=[140, 141], total=142, uptime_s=1100,
                                      stamp=2100.0)
        assert lost == 39 and why is None

    def test_an_unmeasured_ring_reports_none_and_never_zero(self):
        """None is "nobody looked"; 0 is "nothing was lost". Collapsing them is how a drain that
        had never run reported a clean sheet."""
        lost, why = HD.live_ring_loss({}, served=[0, 1], total=2, uptime_s=10, stamp=100.0)
        assert lost is None and "unmeasured" in why

    def test_a_cursor_overrun_is_a_named_gap_rather_than_a_silent_reseek(self):
        """The firmware states the gap; the drain must turn it into the same counted loss the
        legacy path reports instead of quietly resuming at the node's oldest row."""
        page = {"until_cursor": HD.live_ring_cursor(BOOT_ID, 500),
                "gap": {"kind": "overrun", "lost_rows": 12,
                        "resume_cursor": HD.live_ring_cursor(BOOT_ID, 488)}}
        lost, why = HD._live_ring_gap_loss({"last_i": 475}, page)
        assert lost == 12
        assert "aged behind" in why and "12" in why


# ================================================== 3. timestamp fallback

class TestCase3TimestampFallback:
    """A stamp that is not a measurement is kept, labelled, and never promoted."""

    def test_an_unanchored_row_is_kept_and_not_dated(self, pl, tmp_path, adapter):
        """`utc_us == 0` is a node with no PPS lock yet. Dropping it makes the pool's own count
        depend on GPS state; dating it invents the one number nothing downstream could check."""
        adapter.run(pl, tmp_path, "unanchored", [Det(sketch_frame(20), utc_us=0)])
        rec = _one(pl)
        assert rec["anchored"] is False
        assert rec["ts_utc_s"] is None
        assert os.path.isdir(os.path.join(pl.records_dir, "unanchored"))

    def test_the_nodes_own_pps_counters_survive_so_the_row_stays_recoverable(self, pl, tmp_path,
                                                                            adapter):
        """`utc_us == 0` says the node could not NAME the edge; `pps_n`/`us_since_pps` still say
        WHICH edge. Without the pair an unanchored row cannot be placed even in principle."""
        adapter.run(pl, tmp_path, "unanchored", [Det(sketch_frame(21), utc_us=0)])
        rec = _one(pl)
        assert rec["pps_n"] == "42" and rec["us_since_pps"] == "597174"

    def test_a_node_row_does_not_answer_the_phones_trust_question(self, pl, tmp_path, adapter):
        """`utc_trusted` is the phone's audio-path question. A node's clock trust is a different
        measurement entirely, and answering True here would be a claim nothing made."""
        adapter.run(pl, tmp_path, "anchored", [Det(sketch_frame(22))])
        assert C.utc_trusted_of(_one(pl)) is None

    def test_a_wall_clock_fallback_is_anchored_but_not_trusted(self, pl, tmp_path):
        """The gotchi adapter's `wall` tier is a 50 ms wall-clock reading -- about 17 m at
        343 m/s. It carries a stamp, so `anchored` is true; it is not an arrival, so the trust
        question must answer False rather than inheriting the stamp's existence."""
        frame = sketch_frame(23)
        p = tmp_path / "phone.jsonl"
        p.write_text(json.dumps({
            "topic": "dama/phone-w/acoustic_sketch",
            "payload": {"sketch_b64": base64.b64encode(frame).decode(),
                        "ts_utc_ms": 1788763952189, "clock_tier": "wall"}}))
        pl.ingest_mqtt_jsonl(str(p))
        rec = _one(pl)
        assert rec["anchored"] is True
        assert C.utc_trusted_of(rec) is False

    def test_an_unrecognised_clock_tier_is_refused_rather_than_assumed_good(self, pl, tmp_path):
        """A tier this version has never heard of is a producer we do not understand."""
        frame = sketch_frame(24)
        p = tmp_path / "phone.jsonl"
        p.write_text(json.dumps({
            "topic": "dama/phone-x/acoustic_sketch",
            "payload": {"sketch_b64": base64.b64encode(frame).decode(),
                        "ts_utc_ms": 1788763952189, "clock_tier": "satellite-ish"}}))
        pl.ingest_mqtt_jsonl(str(p))
        assert C.utc_trusted_of(_one(pl)) is False


# =================================== 4. missing sample-rate / profile metadata

class TestCase4MissingRateMetadata:
    """A record must be able to say "I do not know what rate this was", and say who told it."""

    def test_the_legacy_profile_states_no_rate_and_the_record_does_not_invent_one(self, pl,
                                                                                  tmp_path,
                                                                                  adapter):
        """Profile 0 is 20x8 with the rate and layout UNSTATED, which is what every frame already
        sent under it actually means. With no column either, `fs_hz` is None -- not a default."""
        assert WR.profile_geometry(0).fs_hz is None
        adapter.run(pl, tmp_path, "norate", [Det(legacy_profile0_frame(30), fs_hz=None)])
        rec = _one(pl)
        assert rec["fs_hz"] is None
        assert rec["fs_stated_by"] is None

    def test_a_rate_the_column_states_is_kept_and_attributed_to_the_column(self, pl, tmp_path,
                                                                          adapter):
        """The node's running estimate is evidence, and it is weaker evidence than the frame's
        own declaration -- so it is stored under who said it, not merged into one field."""
        adapter.run(pl, tmp_path, "csvrate", [Det(legacy_profile0_frame(31),
                                                  fs_hz="16000.000")])
        rec = _one(pl)
        assert rec["fs_hz"] == 16000.0
        assert rec["fs_stated_by"] == "csv"
        assert rec["fs_csv_hz"] == 16000.0

    def test_the_frame_outranks_the_column_when_the_profile_states_a_rate(self, pl, tmp_path,
                                                                         adapter):
        """Profile 1 IS 48 kHz. A disagreeing column is the node's estimate drifting, and the
        column's value stays visible instead of being overwritten."""
        adapter.run(pl, tmp_path, "framerate", [Det(sketch_frame(32, profile_id=1),
                                                    fs_hz="16000.000")])
        rec = _one(pl)
        assert rec["fs_hz"] == 48000.0
        assert rec["fs_stated_by"] == "frame"
        assert rec["fs_csv_hz"] == 16000.0

    def test_a_profile_this_build_does_not_know_is_refused_and_not_defaulted(self):
        """4 bits is 16 ids and this build knows five. An unknown one is a firmware this reader
        cannot interpret, and guessing its geometry relabels a measurement."""
        with pytest.raises(ValueError):
            WR.profile_geometry(9)
        with pytest.raises(ValueError):
            WR.profile_shape(9)

    def test_a_new_frame_may_not_claim_the_rate_unstated_profile(self):
        """Read-only is the whole point of keeping 0: a frame that cannot say its own rate is the
        defect this registry closed, so it must not be selectable going forward."""
        assert 0 in WR.LEGACY_PROFILES
        assert 0 not in WR.NEW_PROFILE_IDS
        assert WR.DEFAULT_PROFILE in WR.NEW_PROFILE_IDS
        assert WR.profile_geometry(WR.DEFAULT_PROFILE).fs_hz is not None


# ============================ 5. transport loss, retry and idempotency

class TestCase5TransportLossRetryIdempotency:
    """Every delivery here is at-least-once, so acceptance has to be idempotent by construction."""

    def test_the_same_body_delivered_twice_adds_one_event(self, pl, tmp_path, adapter):
        dets = [Det(sketch_frame(40), sample="1"), Det(sketch_frame(41), sample="2")]
        first = adapter.run(pl, tmp_path, "run1", dets)
        second = adapter.run(pl, tmp_path, "run2", dets)
        assert first["added"] == 2
        assert second["added"] == 0 and second["duplicate"] == 2
        assert pl.stats()["records"] == 2

    def test_an_overlapping_retry_adds_only_what_is_new(self, pl, tmp_path, adapter):
        """Two drains overlap by design -- the tail is re-fetched every run so a gap cannot open.
        The overlap must cost nothing, which is what makes draining more often always safe."""
        a = [Det(sketch_frame(42), sample="1"), Det(sketch_frame(43), sample="2")]
        b = [Det(sketch_frame(43), sample="2"), Det(sketch_frame(44), sample="3")]
        adapter.run(pl, tmp_path, "run1", a)
        second = adapter.run(pl, tmp_path, "run2", b)
        assert second["added"] == 1 and second["duplicate"] == 1
        assert pl.stats()["records"] == 3

    def test_every_delivery_attempt_is_ledgered_with_the_bytes_it_carried(self, pl, tmp_path,
                                                                         adapter):
        """A duplicate is not a non-event: "we fetched this twice" and "we fetched it once" are
        different operational facts, and only the ledger can tell them apart afterwards."""
        dets = [Det(sketch_frame(45), sample="1")]
        adapter.run(pl, tmp_path, "run1", dets)
        adapter.run(pl, tmp_path, "run2", dets)
        led = pl.ledger()
        assert len(led) == 2
        assert led[0]["sha256"] == led[1]["sha256"]
        assert led[0]["added"] == 1 and led[1]["added"] == 0

    def test_a_delivery_that_arrives_out_of_order_still_converges(self, pl, tmp_path, adapter):
        """Retries reorder. The store is content-addressed, so the union is the same set whatever
        order the pieces land in."""
        one = [Det(sketch_frame(46), sample="1")]
        two = [Det(sketch_frame(47), sample="2")]
        adapter.run(pl, tmp_path, "late", two)
        adapter.run(pl, tmp_path, "early", one)
        adapter.run(pl, tmp_path, "both", one + two)
        assert pl.stats()["records"] == 2

    def test_the_cursor_watermark_never_moves_backwards_inside_one_boot(self, tmp_path):
        """⚠️THE CURSOR IS THE ONLY THING STANDING BETWEEN A RETRY AND A HOLE. A page that names
        an earlier position must not un-read rows already committed to the pool."""
        root = str(tmp_path / "drainroot")
        os.makedirs(root, exist_ok=True)
        wm: Dict[str, Any] = {}
        node_wm: Dict[str, Any] = {}
        ahead = {"contract": "cursor-v1", "boot_id": BOOT_ID,
                 "next_cursor": HD.live_ring_cursor(BOOT_ID, 50)}
        behind = {"contract": "cursor-v1", "boot_id": BOOT_ID,
                  "next_cursor": HD.live_ring_cursor(BOOT_ID, 20)}
        HD._write_live_ring_watermark(root, wm, NODE, node_wm, ahead, 100, 1788763952)
        after_ahead = dict(HD.read_watermarks(root)[NODE]["live_ring"])
        HD._write_live_ring_watermark(root, wm, NODE, node_wm, behind, 100, 1788763953)
        after_behind = HD.read_watermarks(root)[NODE]["live_ring"]
        assert HD.parse_live_cursor(after_ahead["next_cursor"])[1] == 50
        assert HD.parse_live_cursor(after_behind["next_cursor"])[1] == 50
        assert after_behind["last_i"] == 49

    def test_a_cursor_is_refused_rather_than_coerced_when_it_is_malformed(self):
        """An unparseable cursor resuming at 0 would re-ingest a whole boot; resuming at "latest"
        would skip one. Neither is a recovery, so neither is offered."""
        for bad in ("", "nonsense", "%s:-1" % BOOT_ID, "zz:" + "0", 17, None):
            with pytest.raises(ValueError):
                HD.parse_live_cursor(bad)

    def test_a_cursor_belongs_to_the_boot_that_minted_it(self):
        """Ring indices are meaningless across a reboot. The boot id is in the cursor so a stale
        one is a NAMED reboot gap instead of a position in a namespace that no longer exists."""
        assert HD.parse_live_cursor(HD.live_ring_cursor(BOOT_ID, 9)) == (BOOT_ID, 9)
        with pytest.raises(ValueError):
            HD.live_ring_cursor("not-a-boot-id", 9)


# ============================================ 6. cross-profile event identity

class TestCase6CrossProfileIdentity:
    """One detection has one identity; two measurements do not get to share one."""

    def test_one_detection_has_one_identity_across_two_transports(self, pl, tmp_path):
        """⚠️THE CROSS-ADAPTER CASE, AND THE REASON THIS FILE IS PARAMETRISED. Tonight's fleet
        drains the same event off a card and out of a live ring. If the two transports keyed it
        differently the corpus would hold one event twice and every rate computed from it would
        be wrong by however many nodes carry a card.
        """
        det = Det(sketch_frame(50), sample="909")
        first = pl.ingest_dets(_write_dets_csv(tmp_path, "card.csv", [det]), default_node=NODE)
        second = pl.ingest_detections_json(_write_live_ring(tmp_path, "ring.json", [det]),
                                           default_node=NODE)
        assert first["added"] == 1
        assert second["added"] == 0 and second["duplicate"] == 1
        assert pl.stats()["records"] == 1

    def test_two_frames_that_differ_only_in_what_their_bytes_mean_stay_two_events(self, pl,
                                                                                  tmp_path,
                                                                                  adapter):
        """Profiles 1 and 2 share a shape and disagree about the rate: 48 kHz against 16 kHz over
        identical geometry. Same node, same stamp, same counter -- and they are NOT the same
        measurement, so the store must not collapse them.
        """
        rng = np.random.default_rng(51)
        q, ref = SK.sketch(rng.normal(0, 1000, 4096), 48000.0)
        common = dict(us_of_day=50_000_123, node_id=202, seq=7, ref_db=ref, peak=1500, q=q)
        at48 = WR.pack_v2(profile_id=1, **common)
        at16 = WR.pack_v2(profile_id=2, **common)
        entry = adapter.run(pl, tmp_path, "profiles",
                            [Det(at48, sample="1"), Det(at16, sample="1")])
        assert entry["added"] == 2
        rates = sorted(r["fs_hz"] for r in pl.raw())
        assert rates == [16000.0, 48000.0]

    def test_the_profile_the_frame_claimed_is_carried_into_the_record(self, pl, tmp_path,
                                                                      adapter):
        """The id is what makes the frame self-describing. A record that drops it cannot say
        later which geometry its bytes were measured under."""
        adapter.run(pl, tmp_path, "profile", [Det(sketch_frame(52, profile_id=2))])
        rec = _one(pl)
        assert rec["profile_id"] == 2
        assert rec["bands"] == WR.profile_shape(2)[0]

    def test_a_row_the_fetch_disagrees_with_is_refused_rather_than_refiled(self, pl, tmp_path):
        """⚠️THE MIS-FILING GUARD IS PART OF IDENTITY, NOT BESIDE IT. A row filed under the wrong
        node is worse than a dropped one: it puts one microphone's arrival at another
        microphone's position, and the solver then answers for a source that was never there.
        The file's own id wins the naming and then has to AGREE with the node that was fetched.
        """
        det = Det(sketch_frame(53))
        path = _write_dets_csv(tmp_path, "mismatch.csv", [det])
        rows = open(path).read().splitlines()
        # The header keeps `node_id`; only the row's own id is changed, which is what a
        # mis-flashed board looks like on a card drained from the node it is not.
        rows[1] = "mach," + rows[1].split(",", 1)[1]
        open(path, "w").write("\n".join(rows) + "\n")
        entry = pl.ingest_dets(path, default_node=NODE)
        assert entry["added"] == 0
        assert entry["skip_reasons"].get("node_mismatch") == 1
        assert entry["node_mismatch"] == {"mach": 1}


# ================================== 7. the recoupling import boundary is green

class TestCase7RecouplingBoundary:
    """The merge-blocking gate is part of the suite, not a job beside it.

    Its own behaviour is tested in tests/test_recoupling_guard.py. What belongs HERE is the one
    assertion that makes a re-coupled core fail the conformance suite too: an adapter that has to
    import Redis to pass case 5 has not passed case 5.
    """

    def test_the_core_declares_no_transport_cloud_or_shared_volume_dependency(self):
        bad, stale = RG.check()
        assert bad == [], "\n".join(v.render() for v in bad)
        assert stale == [], stale
