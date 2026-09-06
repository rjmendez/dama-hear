"""The ingest that makes phone and node sketches one corpus -- and refuses when they are not."""
import base64
import json

import numpy as np
import pytest

from hear import corpus as C
from hear import sketch as SK


def _frame(fs=48000.0, flags=0, seed=1, ref_peak=500):
    rng = np.random.default_rng(seed)
    q, ref = SK.sketch(rng.normal(0, 1000, 4096), fs)
    return SK.pack(123456, ref, ref_peak, q, flags=flags, fs=fs), q, ref


def _phone_payload(fs=48000.0, **kw):
    frame, q, ref = _frame(fs)
    p = {"sketch_b64": base64.b64encode(frame).decode(),
         "ref_db": ref, "peak_int16": 500, "retrigger": False,
         "bands": SK.MEL_BANDS, "frames": SK.FRAMES, "fs": fs,
         "ts_utc_ms": 1788700000123, "clock_tier": "gnss", "sync_sigma_ns": 105000.0,
         "clipped": False, "trigger_ts_utc_ms": 1788700000100}
    p.update(kw)
    return p, q, ref


class TestPhone:
    def test_round_trips_the_sketch_and_its_scale(self):
        p, q, ref = _phone_payload()
        r = C.from_phone(p, node_id="phone-a1")
        assert r.source == "phone" and r.node_id == "phone-a1"
        assert np.array_equal(r.q, q)
        assert r.ref_db == pytest.approx(ref, abs=0.25)      # ref travels quantised to 0.25 dB
        assert r.fs_hz == 48000.0
        assert r.ts_utc_s == pytest.approx(1788700000.123)

    def test_absolute_db_is_recoverable(self):
        p, q, ref = _phone_payload()
        r = C.from_phone(p)
        assert r.db.shape == (SK.MEL_BANDS, SK.FRAMES)
        assert r.db.max() == pytest.approx(r.ref_db, abs=1e-9)

    def test_a_skipped_onset_is_a_skip_not_an_empty_record(self):
        """The phone says WHY it could not sketch. Turning that into a zero row would train a
        model on silence labelled as an event."""
        with pytest.raises(C.SkipReason) as e:
            C.from_phone({"sketch_skipped": "capture_drop", "trigger_ts_utc_ms": 1})
        assert "capture_drop" in str(e.value)

    def test_geometry_disagreement_is_refused_not_reconciled(self):
        p, _, _ = _phone_payload()
        p["bands"] = 24                                       # json disagrees with the header
        with pytest.raises(C.SkipReason):
            C.from_phone(p)

    def test_short_frame_is_refused(self):
        with pytest.raises(C.SkipReason):
            C.from_phone({"sketch_b64": base64.b64encode(b"\x00" * 8).decode()})

    def test_truncated_body_is_refused(self):
        frame, _, _ = _frame()
        with pytest.raises(C.SkipReason):
            C.from_phone({"sketch_b64": base64.b64encode(frame[:-3]).decode()})

    def test_retrigger_flag_survives_the_wire(self):
        frame, _, ref = _frame(flags=1)
        r = C.from_phone({"sketch_b64": base64.b64encode(frame).decode()})
        assert r.retrigger is True


class TestNode:
    def test_node_us_alone_carries_no_absolute_time(self):
        """The second comes from the mesh clock and is NOT in the frame. Absent is absent."""
        frame, _, _ = _frame()
        r = C.from_node(frame, "node-7")
        assert r.ts_utc_s is None and r.node_us == 123456
        r2 = C.from_node(frame, "node-7", second_utc_s=1788700000)
        assert r2.ts_utc_s == pytest.approx(1788700000.123456)

    def test_a_16k_node_and_a_48k_phone_are_distinguishable_now(self):
        f16, _, _ = _frame(fs=16000.0, seed=3)
        f48, _, _ = _frame(fs=48000.0, seed=3)
        assert C.from_node(f16, "n").fs_hz == 16000.0
        assert C.from_node(f48, "n").fs_hz == 48000.0
        # and the bands genuinely mean different things
        e16 = C.from_node(f16, "n").band_edges_hz()
        e48 = C.from_node(f48, "n").band_edges_hz()
        assert e48[-1] == pytest.approx(20000.0)
        assert e16[-1] == pytest.approx(7840.0)


class TestFeatureMatrix:
    def test_it_will_not_stack_two_frequency_axes(self):
        """⚠️The whole reason this module exists. 20 bands span 300 Hz-20 kHz at 48 kHz and
        300 Hz-7.84 kHz at 16 kHz; stacking them trains on an axis that moves between rows."""
        recs = [C.from_node(_frame(fs=48000.0, seed=i)[0], "p%d" % i) for i in range(3)]
        recs += [C.from_node(_frame(fs=16000.0, seed=i)[0], "n%d" % i) for i in range(2)]
        X, kept = C.feature_matrix(recs, 48000.0)
        assert X.shape == (3, SK.MEL_BANDS * SK.FRAMES)
        assert all(r.fs_hz == 48000.0 for r in kept)
        X16, kept16 = C.feature_matrix(recs, 16000.0)
        assert X16.shape == (2, SK.MEL_BANDS * SK.FRAMES)

    def test_an_unstated_rate_is_excluded_not_assumed(self):
        q, ref = SK.sketch(np.zeros(4096), 48000.0)
        legacy = SK.pack(1, ref, 0, q)                        # no fs -> code 0
        recs = [C.from_node(legacy, "old"), C.from_node(_frame(fs=48000.0)[0], "new")]
        X, kept = C.feature_matrix(recs, 48000.0)
        assert len(kept) == 1 and kept[0].node_id == "new"

    def test_the_rate_is_required_and_never_inferred(self):
        with pytest.raises(TypeError):
            C.feature_matrix([], mode="db")                   # fs_hz has no default

    def test_empty_is_empty_not_an_error(self):
        X, kept = C.feature_matrix([], 48000.0)
        assert X.shape == (0, 0) and kept == []

    def test_q_mode_drops_the_level(self):
        recs = [C.from_node(_frame(seed=5)[0], "a")]
        Xdb, _ = C.feature_matrix(recs, 48000.0, mode="db")
        Xq, _ = C.feature_matrix(recs, 48000.0, mode="q")
        assert Xdb.max() == pytest.approx(recs[0].ref_db, abs=1e-9)
        assert Xq.max() == pytest.approx(0.0)                 # the reference is the zero point


class TestSummaryAndReader:
    def test_summary_splits_by_rate_because_a_total_would_hide_it(self):
        recs = [C.from_node(_frame(fs=48000.0, seed=i)[0], "p") for i in range(3)]
        recs += [C.from_node(_frame(fs=16000.0, seed=i)[0], "n") for i in range(2)]
        s = C.summarise(recs)
        assert s["records"] == 5
        assert s["by_fs_hz"] == {"48000.0": 3, "16000.0": 2}
        assert s["by_source"] == {"node": 5}

    def test_it_reads_the_corpus_workers_row_shape(self):
        """dama-gotchi realtime/sketch_corpus_worker.py writes node_id at the TOP level, taken
        from the topic -- the only place the broker guarantees it. A payload's own claim about
        which node it is is checked last, because that is the one a misconfigured device gets
        wrong."""
        p, _, _ = _phone_payload()
        row = {"node_id": "phone-a1", "topic": "dama/phone-a1/acoustic_sketch",
               "recv_utc_ms": 1788700001000, "payload": p}
        import tempfile, os
        d = tempfile.mkdtemp()
        f = os.path.join(d, "c.jsonl")
        with open(f, "w") as fh:
            fh.write(json.dumps(row) + "\n")
        recs, skips = C.read_mqtt_jsonl(f)
        assert not skips and recs[0].node_id == "phone-a1"

    def test_the_topic_wins_over_a_payloads_own_claim(self):
        p, _, _ = _phone_payload(node_id="not-me")
        row = {"topic": "dama/phone-a1/acoustic_sketch", "payload": p}
        import tempfile, os
        d = tempfile.mkdtemp()
        f = os.path.join(d, "c.jsonl")
        with open(f, "w") as fh:
            fh.write(json.dumps(row) + "\n")
        recs, _ = C.read_mqtt_jsonl(f)
        assert recs[0].node_id == "phone-a1"

    def test_reader_returns_its_skips_instead_of_swallowing_them(self, tmp_path):
        p, _, _ = _phone_payload()
        f = tmp_path / "cap.jsonl"
        f.write_text("\n".join([
            json.dumps({"topic": "dama/phone-a1/acoustic_sketch", "payload": p}),
            json.dumps({"topic": "dama/phone-b2/acoustic_sketch",
                        "payload": {"sketch_skipped": "ring_not_ready"}}),
            "{not json",
            "",
        ]) + "\n")
        recs, skips = C.read_mqtt_jsonl(str(f))
        assert len(recs) == 1 and recs[0].node_id == "phone-a1"
        assert len(skips) == 2
        assert any("ring_not_ready" in s for s in skips)
        assert any("bad json" in s for s in skips)
