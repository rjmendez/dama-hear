"""The pool that makes node and phone sketches one dataset, and the drain that keeps it fed."""
import base64
import binascii
import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import detsfile as DF                                    # noqa: E402
from hear import pool as P                                         # noqa: E402
from hear import sketch as SK                                      # noqa: E402
from tools import hear_drain as HD                                 # noqa: E402


def _frame(fs=16000.0, seed=1, node_us=597174, flags=0):
    rng = np.random.default_rng(seed)
    q, ref = SK.sketch(rng.normal(0, 1000, 4096), fs)
    return SK.pack(node_us, ref, 1140, q, flags=flags, fs=fs)


def _dets(tmp_path, name, rows, header=None, utc0=1788763952189911, node="nyquist", seed0=1):
    """A G5 dets.csv with `rows` detections, each with its own frame and timestamp.

    `seed0` shifts the frames: two files built with the same seeds hold the SAME detections and
    the pool deduplicates them, which is correct and is not what a two-node fixture wants.
    """
    header = header or DF.G5.declared
    lines = [",".join(header)]
    for i in range(rows):
        fh = binascii.hexlify(_frame(seed=seed0 + i, node_us=597000 + i)).decode()
        lines.append("%s,%d,1234,%d,42,%d,1140,4608,16000.000,64,%s,,"
                     % (node, utc0 + i * 1_000_000, 5000000 + i, 597000 + i, fh))
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n")
    return str(p)


def _mqtt(tmp_path, name, rows, node="phone-a", ts0=1788763952189):
    lines = []
    for i in range(rows):
        frame = _frame(fs=48000.0, seed=100 + i, node_us=1000 + i)
        lines.append(json.dumps({
            "topic": "dama/%s/acoustic_sketch" % node,
            "payload": {"sketch_b64": base64.b64encode(frame).decode(),
                        "ts_utc_ms": ts0 + i * 1000, "clock_tier": "gnss",
                        "sync_sigma_ns": 105000.0, "clipped": False, "onset_found": True}}))
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n")
    return str(p)


class TestIdempotence:
    """Every drain re-serves detections an earlier drain already took. That must cost nothing."""

    def test_ingesting_the_same_file_twice_adds_nothing(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        f = _dets(tmp_path, "dets.csv", 5)
        assert pl.ingest_dets(f)["added"] == 5
        second = pl.ingest_dets(f)
        assert second["added"] == 0 and second["duplicate"] == 5
        assert len(list(pl.raw())) == 5

    def test_a_fresh_pool_object_sees_what_an_earlier_one_wrote(self, tmp_path):
        # The key cache is per-object; a timer runs a NEW process every tick, so the dedup has to
        # survive that or every tick re-appends the whole file.
        root = str(tmp_path / "pool")
        f = _dets(tmp_path, "dets.csv", 4)
        P.Pool(root).ingest_dets(f)
        assert P.Pool(root).ingest_dets(f)["added"] == 0
        assert len(list(P.Pool(root).raw())) == 4

    def test_a_rolled_file_overlapping_its_predecessor_adds_only_the_new(self, tmp_path):
        # dets-prev.csv holds what dets.csv held before the roll. The overlap is the normal case.
        pl = P.Pool(str(tmp_path / "pool"))
        prev = _dets(tmp_path, "dets-prev.csv", 6)
        pl.ingest_dets(prev)
        live = _dets(tmp_path, "dets.csv", 9)          # same first 6 detections, plus 3
        assert pl.ingest_dets(live)["added"] == 3
        assert len(list(pl.raw())) == 9

    def test_phone_ingest_is_idempotent_too(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        f = _mqtt(tmp_path, "sketches-2026-09-08.jsonl", 4)
        assert pl.ingest_mqtt_jsonl(f)["added"] == 4
        assert pl.ingest_mqtt_jsonl(f)["added"] == 0


class TestOneDataset:
    def test_both_sensors_land_in_one_pool_and_stay_distinguishable(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_dets(_dets(tmp_path, "dets.csv", 3))
        pl.ingest_mqtt_jsonl(_mqtt(tmp_path, "sketches-2026-09-08.jsonl", 2))
        s = pl.stats()
        assert s["records"] == 5
        assert s["by_source"] == {"node": 3, "phone": 2}
        recs = pl.records()
        assert {r.source for r in recs} == {"node", "phone"}
        assert {r.fs_hz for r in recs} == {16000.0, 48000.0}

    def test_records_come_back_as_corpus_records_the_matrix_code_already_reads(self, tmp_path):
        from hear import corpus as C
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_dets(_dets(tmp_path, "dets.csv", 3))
        recs = pl.records()
        assert all(isinstance(r, C.Record) for r in recs)
        X, kept = C.feature_matrix(recs, 16000.0)
        assert X.shape == (3, SK.MEL_BANDS * SK.FRAMES)

    def test_the_key_is_content_so_two_drains_of_one_detection_agree(self, tmp_path):
        frame = _frame()
        a = P.key("node", "nyquist", 1788763952189911, "5000000", frame)
        b = P.key("node", "nyquist", 1788763952189911, "5000000", frame)
        assert a == b

    def test_two_unanchored_detections_in_one_boot_do_not_collapse(self, tmp_path):
        # Both carry utc_us 0. Only `sample` and the frame separate them, which is why both are
        # in the key -- keying on time alone would store one and silently drop the other.
        k1 = P.key("node", "nyquist", 0, "100", _frame(seed=1))
        k2 = P.key("node", "nyquist", 0, "200", _frame(seed=2))
        assert k1 != k2


class TestNothingIsDroppedOnTheWayOut:
    """⚠️records() must not quietly lose a field the store held.

    `extra` was a fixed whitelist and was already dropping `onset_found` -- the flag
    hear/backend/associate.py refuses arrivals on. A phone honestly reporting that its own onset
    was not a measurement had that report deleted between the pool and the gate that exists to
    read it. A whitelist loses whatever a producer adds next; the remainder cannot.
    """

    def test_a_phone_quality_flag_survives_the_round_trip(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_mqtt_jsonl(_mqtt(tmp_path, "sketches-2026-09-08.jsonl", 2))
        r = pl.records(source="phone")[0]
        assert r.extra["onset_found"] is True

    def test_a_field_no_one_anticipated_still_arrives(self, tmp_path):
        # The property, not the instance: add a key to the store and it must reach extra with
        # no change to records().
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_dets(_dets(tmp_path, "dets.csv", 1))
        day = os.listdir(os.path.join(pl.records_dir))[0]
        p = os.path.join(pl.records_dir, day, "node.jsonl")
        rows = [json.loads(l) for l in open(p) if l.strip()]
        rows[0]["utc_trusted"] = False
        open(p, "w").write("\n".join(json.dumps(x, sort_keys=True) for x in rows) + "\n")
        assert P.Pool(str(tmp_path / "pool")).records()[0].extra["utc_trusted"] is False

    def test_the_fields_that_became_attributes_are_not_duplicated_into_extra(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_dets(_dets(tmp_path, "dets.csv", 1))
        r = pl.records()[0]
        for k in ("node", "source", "fs_hz", "ref_db", "frame_b64"):
            assert k not in r.extra, k
        # ...and the remainder is there. `layout` comes from the FRAME, not the CSV flags column,
        # so it is whatever the fixture packed -- the point here is that it survived at all.
        assert r.extra["layout"] == SK.unpack(base64.b64decode(
            next(iter(pl.raw()))["frame_b64"]))["layout"]
        assert "key" in r.extra and "no_context" in r.extra


class TestTime:
    def test_an_unanchored_row_is_kept_and_marked_not_dropped(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        fh = binascii.hexlify(_frame()).decode()
        text = ",".join(DF.G5.declared) + "\n" + \
            "nyquist,0,1234,5000000,42,597174,1140,4608,16000.000,64,%s,,\n" % fh
        p = tmp_path / "d.csv"
        p.write_text(text)
        assert pl.ingest_dets(str(p))["added"] == 1
        r = next(iter(pl.raw()))
        assert r["anchored"] is False and r["ts_utc_s"] is None
        assert pl.stats()["unanchored"] == 1

    def test_unanchored_rows_partition_separately_from_a_guessed_day(self, tmp_path):
        assert P._day(None) == "unanchored"
        assert P._day(1788763952.189911) == "2026-09-07"

    def test_anchored_only_filters_without_deleting(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_dets(_dets(tmp_path, "dets.csv", 3))
        fh = binascii.hexlify(_frame(seed=99)).decode()
        p = tmp_path / "u.csv"
        p.write_text(",".join(DF.G5.declared) + "\n" +
                     "nyquist,0,1,2,3,4,5,4608,16000.000,64,%s,,\n" % fh)
        pl.ingest_dets(str(p))
        assert len(pl.records()) == 4
        assert len(pl.records(anchored_only=True)) == 3


def _mqtt_payloads(tmp_path, name, payloads, node="phone-a", ts0=1788763952189):
    """One jsonl per payload override, each with its own frame so the pool does not dedup them."""
    lines = []
    for i, extra in enumerate(payloads):
        frame = _frame(fs=48000.0, seed=200 + i, node_us=2000 + i)
        p = {"sketch_b64": base64.b64encode(frame).decode(), "ts_utc_ms": ts0 + i * 1000}
        p.update(extra)
        lines.append(json.dumps({"topic": "dama/%s/acoustic_sketch" % node, "payload": p}))
    f = tmp_path / name
    f.write_text("\n".join(lines) + "\n")
    return str(f)


class TestAnchoredIsNotTrusted:
    """⚠️THE FALSE FRIEND. `anchored` answers "is there a stamp"; a solver wants "is the stamp a
    measurement". For a node those coincide -- `utc_us > 0` IS PPS lock. For a PHONE they do not:
    ingest sets `anchored = bool(ts)`, and GPSTimingSync publishes a `ts_utc_ms` on the "wall"
    tier too, at a declared 50 ms -- about 17 m at 343 m/s, which is not a TDoA arrival.

    The two are deliberately left disagreeing. `anchored` is content-addressed into every row
    already written and redefining it would change what those rows assert, so the trust question
    moved to `Record.utc_trusted` instead. Do not "fix" one to match the other.
    """

    def test_a_wall_tier_row_is_anchored_and_is_not_trusted(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_mqtt_jsonl(_mqtt_payloads(tmp_path, "w.jsonl", [{"clock_tier": "wall"}]))
        assert next(iter(pl.raw()))["anchored"] is True
        assert pl.records()[0].utc_trusted is False

    def test_a_gnss_row_is_both(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_mqtt_jsonl(_mqtt_payloads(tmp_path, "g.jsonl", [{"clock_tier": "gnss"}]))
        assert next(iter(pl.raw()))["anchored"] is True
        assert pl.records()[0].utc_trusted is True

    def test_onset_dated_survives_the_pool_so_the_guard_rung_still_works(self, tmp_path):
        """⚠️corpus.from_phone can evaluate the `onset_dated` rung; before this the pool could
        not, because ingest_mqtt_jsonl never stored the key. Two readers of one message giving
        two answers is the bug -- so the round trip is asserted, not assumed."""
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_mqtt_jsonl(_mqtt_payloads(tmp_path, "u.jsonl",
                                            [{"clock_tier": "gnss", "onset_dated": False}]))
        r = pl.records()[0]
        assert r.extra["onset_dated"] is False
        assert r.clock_tier == "gnss"
        assert r.utc_trusted is False

    def test_the_quality_flags_all_round_trip(self, tmp_path):
        """The stored-then-dropped leak, in both directions: what ingest wrote must reach the
        Record, or the gate meant to read it never sees the producer's own report."""
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_mqtt_jsonl(_mqtt_payloads(
            tmp_path, "q.jsonl",
            [{"clock_tier": "location", "onset_found": False, "onset_dated": True,
              "onset_offset_us": 4321}]))
        e = pl.records()[0].extra
        assert e["onset_found"] is False
        assert e["onset_dated"] is True
        assert e["onset_offset_us"] == 4321

    def test_stats_counts_the_wall_population_instead_of_hiding_it(self, tmp_path):
        """⚠️A selector that drops rows without saying how many is this pool's named failure --
        the G3 730 and the `flags & 2` 88. So the tiers are COUNTED."""
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_mqtt_jsonl(_mqtt_payloads(tmp_path, "m.jsonl", [
            {"clock_tier": "wall"}, {"clock_tier": "wall"}, {"clock_tier": "gnss"},
            {"clock_tier": "network"}, {},
        ]))
        s = pl.stats()
        assert s["by_clock_tier"] == {"wall": 2, "gnss": 1, "network": 1, "(unstated)": 1}
        # ...and the tiers total the phone rows: a breakdown that loses rows is the same bug.
        assert sum(s["by_clock_tier"].values()) == s["by_source"]["phone"]
        assert s["phone_utc_trusted"] == {"true": 1, "false": 3, "not_stated": 1}
        assert sum(s["phone_utc_trusted"].values()) == s["by_source"]["phone"]
        assert s["trusted_clock_tiers"] == ["gnss", "location"]

    def test_the_summary_agrees_with_the_records_it_summarises(self, tmp_path):
        """stats() asks corpus.utc_trusted_of the same question records() does. If it re-derived
        from by_clock_tier it would miss the onset_dated rung and quietly disagree."""
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_mqtt_jsonl(_mqtt_payloads(tmp_path, "a.jsonl", [
            {"clock_tier": "gnss"}, {"clock_tier": "gnss", "onset_dated": False},
            {"clock_tier": "wall"}, {},
        ]))
        got = pl.stats()["phone_utc_trusted"]
        recs = [r.utc_trusted for r in pl.records(source="phone")]
        assert got == {"true": sum(t is True for t in recs),
                       "false": sum(t is False for t in recs),
                       "not_stated": sum(t is None for t in recs)}
        assert got == {"true": 1, "false": 2, "not_stated": 1}

    def test_nodes_are_absent_from_the_tier_breakdown(self, tmp_path):
        """A node has no clock_tier -- its time is PPS. Folding it into a `None` bucket would
        read as a phone that failed to state one."""
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_dets(_dets(tmp_path, "dets.csv", 3))
        s = pl.stats()
        assert s["by_clock_tier"] == {}
        assert s["phone_utc_trusted"] == {"true": 0, "false": 0, "not_stated": 0}
        assert s["records"] == 3

    def test_a_node_record_has_no_opinion_about_utc_trust(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        pl.ingest_dets(_dets(tmp_path, "dets.csv", 1))
        assert pl.records()[0].utc_trusted is None

    def test_the_mqtt_arithmetic_still_closes_with_the_new_columns(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        f = _mqtt_payloads(tmp_path, "c.jsonl", [{"clock_tier": "wall"}, {"clock_tier": "gnss"}])
        e = pl.ingest_mqtt_jsonl(f)
        assert e["rows"] == e["added"] + e["duplicate"] + e["skipped"]
        assert e["added"] == 2


class TestLedger:
    def test_it_records_the_generation_and_the_skips_not_just_the_wins(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        fh = binascii.hexlify(_frame()).decode()
        p = tmp_path / "d.csv"
        p.write_text(",".join(DF.G5.declared) + "\n"
                     + "nyquist,1788763952189911,1,2,3,4,5,4608,16000.000,64,%s,,\n" % fh
                     + "nyquist,1788763952189912,1,2,3,4,5,4608,16000.000,64,dead,,\n")
        e = pl.ingest_dets(str(p))
        assert e["generation"] == "G5" and e["added"] == 1
        assert e["skip_reasons"] == {"frame_hex_len_4": 1}
        assert pl.stats()["skipped_at_ingest"] == {"frame_hex_len_4": 1}

    def test_stats_counts_files_by_content_so_a_recopied_drain_is_one_file(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        f = _dets(tmp_path, "dets.csv", 2)
        pl.ingest_dets(f)
        import shutil
        copy = str(tmp_path / "copy-of-dets.csv")
        shutil.copy(f, copy)
        pl.ingest_dets(copy)
        s = pl.stats()
        assert s["ingests"] == 2 and s["files_seen"] == 1 and s["records"] == 2


class TestTheArithmeticCloses:
    """⚠️rows == added + duplicate + skipped, for every file, always.

    A deploy of this tool printed "5 row(s) -> +0 new, 0 dup, 0 skipped" while discarding all
    five: `skipped` counted the reader's refusals but not the decoder's, so a decode failure was
    in no total at all. An invariant is the only version of this that cannot rot.
    """

    def _closed(self, e):
        assert e["rows"] == e["added"] + e["duplicate"] + e["skipped"], e

    def test_a_clean_dets_file_closes(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        self._closed(pl.ingest_dets(_dets(tmp_path, "dets.csv", 5)))

    def test_a_re_ingest_closes_through_duplicate(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        f = _dets(tmp_path, "dets.csv", 5)
        pl.ingest_dets(f)
        self._closed(pl.ingest_dets(f))

    def test_a_reader_refusal_closes_through_skipped(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        fh = binascii.hexlify(_frame()).decode()
        p = tmp_path / "d.csv"
        p.write_text(",".join(DF.G5.declared) + "\n"
                     + "nyquist,1788763952189911,1,2,3,4,5,4608,16000.000,64,%s,,\n" % fh
                     + "nyquist,1788763952189912,1,2,3,4,5,4608,16000.000,64,dead,,\n")
        e = pl.ingest_dets(str(p))
        self._closed(e)
        assert e["rows"] == 2 and e["added"] == 1 and e["skipped"] == 1

    def test_a_decoder_failure_is_counted_not_lost(self, tmp_path):
        # The exact regression: a row the READER accepts and the DECODER cannot use.
        import hear.pool as _P
        pl = P.Pool(str(tmp_path / "pool"))
        f = _dets(tmp_path, "dets.csv", 3)
        real = _P._record_from_node_row

        def boom(row):
            raise AttributeError("module 'hear.sketch' has no attribute 'FLAG_NO_CONTEXT'")
        _P._record_from_node_row = boom
        try:
            e = pl.ingest_dets(f)
        finally:
            _P._record_from_node_row = real
        self._closed(e)
        assert e["skipped"] == 3 and e["added"] == 0
        assert e["skip_reasons"] == {"decode_AttributeError": 3}

    def test_an_mqtt_file_closes_too(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        f = _mqtt(tmp_path, "sketches-2026-09-08.jsonl", 4)
        self._closed(pl.ingest_mqtt_jsonl(f))
        self._closed(pl.ingest_mqtt_jsonl(f))


class TestHeartbeat:
    def test_a_failed_run_does_not_overwrite_the_last_success(self, tmp_path):
        # ⚠️The whole point of the staleness check. If a failure stamped last_success with now,
        # the check would report the drainer's liveness, which is never the thing in doubt.
        root = str(tmp_path / "pool")
        os.makedirs(root, exist_ok=True)
        HD.write_heartbeat(root, [{"node": "nyquist", "ok": True, "added": 3, "errors": []}],
                           None, now=1000.0)
        HD.write_heartbeat(root, [{"node": "nyquist", "ok": False, "added": 0,
                                   "errors": ["status: timeout"]}], None, now=2000.0)
        hb = json.load(open(HD.heartbeat_path(root)))
        s = hb["sensors"]["nyquist"]
        assert s["last_success_s"] == 1000.0
        assert s["last_attempt_s"] == 2000.0
        assert s["last_error"] == "status: timeout"

    def test_check_fails_once_a_sensor_passes_the_staleness_bound(self, tmp_path):
        root = str(tmp_path / "pool")
        os.makedirs(root, exist_ok=True)
        HD.write_heartbeat(root, [{"node": "mach", "ok": True, "added": 1, "errors": []}],
                           None, now=1000.0)
        assert HD.check(root, max_stale_s=100, now=1050.0)[0] == 0
        code, lines = HD.check(root, max_stale_s=100, now=1200.0)
        assert code == 1 and "STALE" in lines[0]

    def test_check_fails_when_no_run_has_ever_completed(self, tmp_path):
        code, lines = HD.check(str(tmp_path / "nothing"))
        assert code == 1 and "never" in lines[0].lower()

    def test_a_sensor_that_has_only_ever_failed_is_a_failure_not_an_absence(self, tmp_path):
        root = str(tmp_path / "pool")
        os.makedirs(root, exist_ok=True)
        HD.write_heartbeat(root, [{"node": "puc", "ok": False, "added": 0,
                                   "errors": ["status: refused"]}], None, now=1000.0)
        code, lines = HD.check(root, max_stale_s=1e9, now=1001.0)
        assert code == 1 and "NEVER" in lines[0]


class TestBackfill:
    def test_a_directory_of_hand_made_drains_ingests_and_names_its_nodes(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        d = tmp_path / "drain"
        d.mkdir()
        _dets(d, "nyquist_dets.csv", 3, node="nyquist", seed0=1)
        _dets(d, "mach_dets.csv", 2, node="mach", seed0=50)
        entries = HD.backfill(pl, [str(d)])
        assert sum(e["added"] for e in entries) == 5
        assert pl.stats()["by_node"] == {"nyquist": 3, "mach": 2}

    def test_two_files_holding_the_same_detections_do_not_double_count(self, tmp_path):
        # The realistic hazard when backfilling a directory someone assembled by hand: the same
        # capture copied under two names.
        pl = P.Pool(str(tmp_path / "pool"))
        d = tmp_path / "drain"
        d.mkdir()
        _dets(d, "a_dets.csv", 4)
        _dets(d, "b_dets.csv", 4)
        HD.backfill(pl, [str(d)])
        assert pl.stats()["records"] == 4

    def test_an_unreadable_file_is_reported_not_skipped(self, tmp_path):
        pl = P.Pool(str(tmp_path / "pool"))
        bad = tmp_path / "junk_dets.csv"
        bad.write_text("when,what\n1,2\n")
        entries = HD.backfill(pl, [str(bad)])
        assert len(entries) == 1 and "error" in entries[0]
        assert "UnknownSchema" in entries[0]["error"]
