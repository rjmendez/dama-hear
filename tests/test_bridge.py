"""The node-to-hugbot bridge: what the timestamp survives, what the vector means, whether the
retrieval pointer dereferences, and that no audio leaves."""
import json
import os
import struct
import sys
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import detsfile as DF        # noqa: E402
from hear import sketch as SK          # noqa: E402
from hear import wire as WR            # noqa: E402
from tools import hear_bridge as BR    # noqa: E402

# The retrieved capture. Absent, every test that needs it skips -- the capture is not in the repo,
# the same arrangement tests/test_node.py:18-19 makes for DAMA_HEAR_REAL_WAV.
CAPTURE = os.environ.get("DAMA_HEAR_CAPTURE", os.path.expanduser("~/dama-hear-capture-2026-09-07"))
DETS = os.path.join(CAPTURE, "dets.csv")
HAVE_CAPTURE = os.path.exists(DETS)
needs_capture = pytest.mark.skipif(
    not HAVE_CAPTURE, reason="set DAMA_HEAR_CAPTURE to a retrieved node capture to run this")

# One real detection from that capture, row 2 of dets.csv. Odd microseconds on purpose: the
# stamp is the thing under test and a round number would hide a rounding bug.
UTC_US = 1_788_763_952_189_911
NODE = "hear-01"


def _q(bands=20, frames=8, seed=3):
    r = np.random.RandomState(seed)
    return np.clip(r.normal(-30, 12, (bands, frames)), -128, 0).round().astype(np.int8)


def _row(utc_us=UTC_US, q=None, ref_db=52.5, peak=818, flags=0, sample=2222, fs_hz=16000.169):
    q = _q() if q is None else q
    return {"utc_us": utc_us, "uptime_s": 12, "sample": sample, "pps_n": 11,
            "us_since_pps": 189911, "trigger": peak, "flags": flags, "fs_hz": fs_hz,
            "frame_hex": SK.pack(189911, ref_db, peak, q, flags=flags).hex(),
            "src": "dets.csv"}


# A /scene.csv row in the firmware's own format: 20 bands x 4 slices of BARE int8 half-dB steps,
# band-major, no wire header, geometry and reference in the row's own columns. No scene capture
# exists yet (the 2026-09-07 capture predates the feature), so this is synthesised to the format
# hear_node.ino's scene_emit() writes rather than lifted from a file.
def _scene_q(bands=20, slices=4, seed=7):
    r = np.random.RandomState(seed)
    return np.clip(r.normal(-24, 10, (bands, slices)), -128, 0).round().astype(np.int8)


def _scene_row(utc_us=UTC_US, q=None, ref_db4=-166, sample=163840, span_ms=1024):
    q = _scene_q() if q is None else q
    return {"utc_us": utc_us, "uptime_s": 300, "sample": sample,
            "bands": q.shape[0], "slices": q.shape[1], "span_ms": span_ms,
            "ref_db4": ref_db4, "frames": 64, "fft_us": 41200,
            "mel_hex": q.astype(np.int8).tobytes().hex(), "src": "scene.csv"}


def _scene_csv(rows):
    out = [",".join(BR.SCENE_COLUMNS)]
    for r in rows:
        out.append(",".join(str(r[c]) for c in BR.SCENE_COLUMNS))
    return "\n".join(out) + "\n"


def _capture_rows():
    with open(DETS, encoding="utf-8") as fh:
        return BR.parse_dets_csv(fh.read())


def _lists(o):
    """Every list anywhere in a record, so a test can prove which arrays a record carries."""
    if isinstance(o, dict):
        return [x for v in o.values() for x in _lists(v)]
    if isinstance(o, list):
        return [o] + [x for v in o for x in _lists(v)]
    return []


def _qs(url):
    u = urlparse(url)
    return u.path, parse_qs(u.query)


class TestDetsCsv:
    def test_a_headerless_file_is_refused_not_guessed_at(self):
        # The firmware can create dets.csv with no header at all -- File::size() reads
        # uninitialised memory on a freshly created file. DictReader would take that first
        # detection as the column names and every later row would parse into plausible nonsense.
        body = "\n".join(open(DETS).read().splitlines()[1:3]) if HAVE_CAPTURE else \
            "%d,12,2222,11,189911,818,0,16000.000,%s" % (UTC_US, _row()["frame_hex"])
        with pytest.raises(ValueError):
            BR.parse_dets_csv(body)

    def test_a_trailing_column_is_carried_rather_than_rejected(self):
        # A column added to the firmware later arrives here without a change.
        hdr = ",".join(BR.DETS_COLUMNS + ["some_future_column"])
        r = _row()
        line = "%d,12,2222,11,189911,818,0,16000.169,%s,%s" % (UTC_US, r["frame_hex"], "42")
        rows = BR.parse_dets_csv(hdr + "\n" + line)
        assert rows[0]["some_future_column"] == "42"

    def test_reads_every_header_hear_detsfile_knows_about(self):
        # Built from hear.detsfile.GENERATIONS directly rather than retyped here, so the two
        # tables cannot drift apart again the way DETS_COLUMNS (G1 only) drifted from the real,
        # node_id-first header every node has written since G4.
        r = _row()
        for gen in DF.GENERATIONS:
            if gen.broken_header:
                continue  # G3: covered separately below, its declared header is not its rows
            values = {
                "node_id": "3", "sketch_back": "192", "clip": "", "clip_why": "",
                "sync_sigma_ns": "35000",
            }
            values.update({k: str(r[k]) for k in
                          ("utc_us", "uptime_s", "sample", "pps_n", "us_since_pps",
                           "trigger", "flags", "fs_hz", "frame_hex")})
            hdr = ",".join(gen.declared)
            line = ",".join(values[c] for c in gen.declared)
            rows = BR.parse_dets_csv(hdr + "\n" + line)
            assert len(rows) == 1, gen.name
            assert int(rows[0]["utc_us"]) == UTC_US, gen.name
            assert rows[0]["frame_hex"] == r["frame_hex"], gen.name

    def test_g3s_lying_header_is_read_by_position_not_by_name(self):
        # G3's declared header says `node,utc_us,...`; the firmware never wrote that column, so
        # every real row is one field narrower than its own header. A straight DictReader (keyed
        # off the header's names) shifts utc_us into the `node` slot; parse_dets_csv must not.
        r = _row()
        hdr = ",".join(DF.G3.declared)
        line = ",".join(str(r[k]) for k in
                        ("utc_us", "uptime_s", "sample", "pps_n", "us_since_pps",
                         "trigger", "flags", "fs_hz", "frame_hex")) + ",,"
        rows = BR.parse_dets_csv(hdr + "\n" + line)
        assert len(rows) == 1
        assert int(rows[0]["utc_us"]) == UTC_US
        assert "node" not in rows[0]


class TestSceneCsv:
    """The scene feature is a SEPARATE file in a DIFFERENT format: /scene.csv, a bare 80-byte
    quantised array with no wire header. It is not a column on dets.csv and it does not decode
    through hear.wire."""

    def test_the_scene_header_is_checked_like_the_detections_header(self):
        with pytest.raises(ValueError):
            BR.parse_scene_csv(_scene_csv([_scene_row()]).replace("mel_hex", "mel"))
        rows = BR.parse_scene_csv(_scene_csv([_scene_row()]))
        assert rows[0]["src"] == "scene.csv" and len(rows) == 1

    def test_mel_hex_is_a_bare_array_and_will_not_go_through_the_wire_decoder(self):
        r = BR.parse_scene_csv(_scene_csv([_scene_row()]))[0]
        with pytest.raises(ValueError):
            BR.decode_feature(r["mel_hex"])          # 80 B, no header: hear.wire cannot read it
        dec = BR.decode_scene_row(r)
        assert dec["q"].shape == (20, 4)
        assert dec["ref_db"] == -166 / 4.0           # ref_db4 is quarter-dB, from the row itself

    def test_the_layout_is_band_major_as_the_firmware_writes_it(self):
        # scene_emit fills scene_db[b * SCENE_SLICES + slice], so byte i is band i//slices.
        q = np.arange(80, dtype=np.int8).reshape(20, 4) - 100
        dec = BR.decode_scene_row(_scene_row(q=q))
        assert np.array_equal(dec["q"], q)
        assert dec["q"][3, 2] == q[3, 2]

    def test_a_truncated_row_is_refused_not_reshaped(self):
        # Same reason /detections' frame_len is checked: a short line is otherwise a smaller
        # descriptor, which is a different embed_dim.
        r = _scene_row()
        r["mel_hex"] = r["mel_hex"][:-8]
        res = BR.convert([r], NODE)
        assert res["records"] == []
        assert res["rejected"][0]["reason"] == "undecodable_feature"

    def test_a_row_with_no_span_is_refused_rather_than_pointed_at_a_default_window(self):
        # span_ms IS the retrieval window for a scene record. Emitted as 0, the node reads the
        # URL as having no dur at all and serves its own default 5 s instead.
        r = _scene_row()
        r["span_ms"] = ""
        res = BR.convert([r], NODE)
        assert res["records"] == [] and res["rejected"][0]["reason"] == "undecodable_feature"

    def test_a_scene_record_carries_the_rows_own_geometry_and_span(self):
        rec = BR.to_record(_scene_row(), NODE, frame_key="mel_hex")
        f = rec["feature"]
        assert rec["embed_dim"] == 80 == len(rec["embed"]) == f["bands"] * f["slices"]
        assert f["kind"] == "mel_scene"
        assert f["bands"] == 20 and f["slices"] == 4
        assert f["span_ms"] == 1024.0                     # the node's own number, not derived here
        assert f["frames_summed"] == 64                   # FFT frames behind the 4 slices
        assert f["wire_version"] is None                  # it is not a wire frame
        assert f["peak"] is None                          # not gated: there is no trigger peak
        assert float(np.linalg.norm(rec["embed"])) == pytest.approx(1.0, abs=1e-5)

    def test_the_second_axis_is_slices_and_frames_never_names_two_things(self):
        # Defect: the record called the descriptor's second axis `frames` (4, the CSV's `slices`
        # column) while the CSV's own `frames` column (64) travelled as `frames_summed`. Two
        # numbers, one name, two files -- documented, and still one substitution away from a
        # descriptor read as 64 slices or an average read as 4 frames.
        row = _scene_row()
        f = BR.to_record(row, NODE, frame_key="mel_hex")["feature"]
        assert "frames" not in f, "a scene record has slices; only a sketch record has frames"
        assert f["slices"] == int(row["slices"]) == 4     # the CSV's `slices` column
        assert f["frames_summed"] == int(row["frames"]) == 64   # the CSV's `frames` column
        assert f["slices"] != f["frames_summed"], "the two the old name conflated"
        # and the sketch path keeps `frames`, because there the axis really is frames.
        g = BR.to_record(_row(), NODE)["feature"]
        assert g["frames"] == 8 and "slices" not in g

    def test_the_scene_geometry_is_read_from_the_row_not_from_a_constant(self):
        # The scene CSV's banding is firmware's to change. Nothing in the bridge may pin it, so a
        # row of another shape must convert, not be measured with this build's numbers.
        q = _scene_q(bands=32, slices=8)
        rec = BR.to_record(_scene_row(q=q), NODE, frame_key="mel_hex")
        assert rec["feature"]["bands"] == 32 and rec["feature"]["slices"] == 8
        assert rec["embed_dim"] == 256 == len(rec["embed"])
        assert rec["feature"]["span_ms"] == 1024.0, "still the row's own span, not one derived"

    def test_the_scene_pointer_is_the_rows_own_window(self):
        rec = BR.to_record(_scene_row(), NODE, frame_key="mel_hex",
                           base_url="http://node.invalid")
        a = rec["audio"]
        assert a["utc_us_from"] == UTC_US                  # the row starts at its own stamp
        assert a["dur_s"] == pytest.approx(1.024, abs=1e-6)
        assert a["sample_from"] == 163840
        _, q = _qs(a["url"])
        assert int(q["from"][0]) == UTC_US and float(q["dur"][0]) == pytest.approx(1.024, abs=1e-6)

    def test_scene_rows_are_tagged_scene_until_edges_are_measured(self):
        # No scene capture exists to derive bucket edges from, and inventing four numbers is what
        # the house rule forbids. The single name is honest and the operator can supply edges.
        rec = BR.to_record(_scene_row(), NODE, frame_key="mel_hex")
        assert [t["name"] for t in rec["top_tags"]] == ["scene"]
        rec2 = BR.to_record(_scene_row(), NODE, frame_key="mel_hex",
                            scene_ref_edges=[-60.0, -50.0, -40.0])
        assert rec2["top_tags"][0]["name"].startswith("ref_")
        assert rec2["top_tags"][-1]["name"] == "scene"

    def test_an_unanchored_scene_row_is_refused_like_an_unanchored_detection(self):
        res = BR.convert([_scene_row(utc_us=0)], NODE)
        assert res["records"] == [] and res["rejected"][0]["reason"] == "unanchored_time"


class TestDetectionsJson:
    def test_frame_len_disagreeing_with_the_hex_is_refused(self):
        # A truncated JSON body is otherwise indistinguishable from a genuinely smaller sketch,
        # and a smaller sketch is a different embed_dim.
        d = {"i": 1, "utc_us": UTC_US, "uptime_s": 12, "sample": 2222, "pps_n": 11,
             "us_since_pps": 189911, "trigger": 818, "flags": 0, "fs_hz": 16000.169,
             "frame_len": 172, "frame": _row()["frame_hex"][:-4]}
        with pytest.raises(ValueError):
            BR.rows_from_detections([d])

    def test_the_live_ring_and_the_card_produce_the_same_record(self):
        # /detections and dets.csv are two views of one Det struct; they must not disagree.
        r = _row()
        j = {"i": 1, "utc_us": r["utc_us"], "uptime_s": r["uptime_s"], "sample": r["sample"],
             "pps_n": r["pps_n"], "us_since_pps": r["us_since_pps"], "trigger": r["trigger"],
             "flags": r["flags"], "fs_hz": r["fs_hz"], "frame_len": 172, "frame": r["frame_hex"]}
        a = BR.to_record(r, NODE)
        b = BR.to_record(BR.rows_from_detections([j])[0], NODE)
        a.pop("src"), b.pop("src")
        assert a == b


class TestTimestamp:
    def test_the_microsecond_is_not_rounded_away(self):
        rec = BR.to_record(_row(), NODE)
        assert rec["utc_us"] == UTC_US, "the node's integer stamp travels verbatim"
        # hugbot's own producer ships round(t, 3), which quantises onto a 1 ms grid. Measured on
        # this stamp rather than asserted: the round MOVES it 89.17 us, 30.6 mm at 343 m/s, and
        # destroys the 911 us of sub-millisecond position it carried -- from a receiver whose
        # tacc_ns ran 22-26 over the whole capture.
        assert round(rec["ts"], 3) != rec["ts"]
        moved_us = abs(round(rec["ts"], 3) - rec["ts"]) * 1e6
        assert moved_us == pytest.approx(89.17, abs=0.01)
        assert moved_us * 1e-6 * 343.0 == pytest.approx(0.0306, abs=1e-4)   # metres
        assert UTC_US % 1000 == 911, "the sub-millisecond position a 1 ms grid cannot hold"
        assert 0.001 * 343.0 == 0.343, "and the grid itself is 343 mm of range"

    def test_ts_round_trips_the_integer_it_came_from(self):
        # float64 spacing at this epoch is 238 ns, finer than the 1 us the stamp is quantised to,
        # so `ts` recovers `utc_us` exactly. It cannot carry the 22 ns tAcc; utc_us is the field.
        rec = BR.to_record(_row(), NODE)
        assert int(round(rec["ts"] * 1e6)) == rec["utc_us"]

    @needs_capture
    def test_every_stamp_in_the_capture_round_trips(self):
        res = BR.convert(_capture_rows(), NODE)
        bad = [r for r in res["records"] if int(round(r["ts"] * 1e6)) != r["utc_us"]]
        assert not bad, "ts is the compatibility field; it must not lose a microsecond"

    def test_a_stamp_survives_json_serialisation(self):
        # The shard line is what actually leaves. A repr that dropped digits would be invisible
        # until the far end tried to associate two nodes.
        rec = BR.to_record(_row(), NODE)
        back = json.loads(BR.to_shard_line(rec))
        assert back["utc_us"] == UTC_US
        assert int(round(back["ts"] * 1e6)) == UTC_US


class TestUnanchoredTime:
    def test_zero_utc_is_refused_not_shipped_as_epoch_zero(self):
        # The firmware writes 0 when the GPS anchor was not trusted. Shipped, it buckets into
        # telem_0.jsonl and stretches cluster_audio_embed's coverage span across 56 years.
        res = BR.convert([_row(utc_us=0)], NODE)
        assert res["records"] == []
        assert res["rejected"][0]["reason"] == "unanchored_time"

    @needs_capture
    def test_the_capture_rejects_exactly_its_pre_lock_rows_by_the_frame_not_the_column(self):
        rows = _capture_rows()
        res = BR.convert(rows, NODE)
        assert res["n_input"] == 59
        assert len(res["records"]) == 48
        assert len(res["rejected"]) == 11
        assert {r["reason"] for r in res["rejected"]} == {"unanchored_time"}
        # Measured coincidence worth pinning, and pinned in BOTH directions so it cannot be half
        # true: the 11 rows with no trusted clock are exactly the rows whose sketch predates a
        # filled ring. Read where production reads it -- through frame_flags() on the DECODED
        # frame, not off the csv `flags` column, which production does not consult at all.
        def ctx(row):
            return BR.frame_flags(BR.decode_feature(row["frame_hex"]))[1]
        assert all(ctx(r["row"]) is False for r in res["rejected"])
        assert all(ctx(r) is True for r in rows if int(r["utc_us"]) > 0)
        assert all(rec["feature"]["context_ok"] is True for rec in res["records"])


class TestFrameFlags:
    """Defect: `flags` was read as a raw word with v1's bit meanings whatever the wire version."""

    def test_v1_bits_mean_what_the_firmware_writes(self):
        a = BR.to_record(_row(flags=BR.V1_FLAG_NO_CONTEXT), NODE)
        assert a["feature"]["context_ok"] is False
        assert a["top_tags"][0]["name"] == "no_context"
        b = BR.to_record(_row(flags=BR.V1_FLAG_RETRIGGER), NODE)
        assert b["feature"]["retrigger"] is True and b["feature"]["context_ok"] is True

    def test_a_v2_profile_id_is_not_an_insufficient_context_bit(self, monkeypatch):
        # v2 packs seq<<8 | version<<5 | profile<<1 | retrigger, so bit 1 is profile-id bit 0.
        # profile 1 with seq 3 gives flags 0x0342, which has v1's "no context" bit set while the
        # frame has full context. Profile 1 is not in this build's table, so it is added for the
        # length of this test -- the bit layout is what is under test, not the profile list.
        monkeypatch.setitem(WR.PROFILES, 1, (20, 8))
        monkeypatch.setitem(WR._SHAPES, (20, 8), 0)
        frame = WR.pack_v2(189911, 7, 3, 52.5, 818, _q(), retrigger=False, profile_id=1)
        assert struct.unpack_from("<H", frame, 11)[0] == 0x0342
        assert 0x0342 & BR.V1_FLAG_NO_CONTEXT, "this is the bit the old reading masked"
        row = _row()
        row["frame_hex"] = frame.hex()
        rec = BR.to_record(row, NODE)
        assert rec["feature"]["wire_version"] == 2
        # Unknown, not False: v2's flags word is fully allocated and has no context bit at all.
        assert rec["feature"]["context_ok"] is None
        assert "no_context" not in [t["name"] for t in rec["top_tags"]]
        assert rec["feature"]["retrigger"] is False

    def test_a_v2_retrigger_comes_from_the_decoder(self, monkeypatch):
        monkeypatch.setitem(WR.PROFILES, 1, (20, 8))
        frame = WR.pack_v2(189911, 7, 3, 52.5, 818, _q(), retrigger=True)
        row = _row()
        row["frame_hex"] = frame.hex()
        rec = BR.to_record(row, NODE)
        assert rec["feature"]["retrigger"] is True
        assert [t["name"] for t in rec["top_tags"]][-1] == "retrigger"

    def test_the_csv_flags_column_is_not_consulted(self):
        # It was dead code behind the frame's own word, and a disagreement must resolve to the
        # frame: that is what the vector was built from.
        row = _row(flags=0)
        row["flags"] = BR.V1_FLAG_NO_CONTEXT           # column says one thing, frame says another
        assert BR.to_record(row, NODE)["feature"]["context_ok"] is True


class TestConservation:
    def test_nothing_is_dropped_silently(self):
        rows = [_row(), _row(utc_us=0), {"utc_us": UTC_US, "sample": 1, "frame_hex": ""},
                {"utc_us": UTC_US, "sample": 1, "frame_hex": "zz"}]
        res = BR.convert(rows, NODE)
        assert len(res["records"]) + len(res["rejected"]) == res["n_features"] == res["n_input"]
        assert {r["reason"] for r in res["rejected"]} == {
            "unanchored_time", "no_feature", "undecodable_feature"}

    def test_a_row_with_no_utc_us_is_rejected_not_an_aborted_run(self):
        # to_record used to index row["utc_us"] unguarded: a /detections body missing the field
        # raised KeyError out of convert() and lost every other row in the batch.
        good = _row()
        bad = dict(_row())
        del bad["utc_us"]
        res = BR.convert([bad, good, bad], NODE)
        assert len(res["records"]) == 1
        assert len(res["rejected"]) == 2
        assert {r["reason"] for r in res["rejected"]} == {"malformed_row"}
        assert len(res["records"]) + len(res["rejected"]) == res["n_features"]

    def test_an_unparseable_number_is_one_rejection_not_an_exception(self):
        bad = _row()
        bad["sample"] = "twenty"
        res = BR.convert([bad], NODE)
        assert res["rejected"][0]["reason"] == "malformed_row"
        assert "sample" in res["rejected"][0]["why"]

    def test_two_features_on_one_row_are_two_records_not_two_vectors(self):
        # A record can only have one `embed`; conservation is counted in features, not rows.
        r = _row()
        r["mel_hex"] = _scene_row()["mel_hex"]
        for k in ("bands", "slices", "span_ms", "ref_db4", "frames"):
            r[k] = _scene_row()[k]
        res = BR.convert([r], NODE)
        assert res["n_input"] == 1 and res["n_features"] == 2
        assert len(res["records"]) + len(res["rejected"]) == res["n_features"]
        assert sorted(x["embed_dim"] for x in res["records"]) == [80, 160]
        assert len({x["utc_us"] for x in res["records"]}) == 1, "one row, one stamp"

    def test_a_rejection_reason_outside_the_vocabulary_raises(self):
        with pytest.raises(ValueError):
            BR._row({}, "it_looked_wrong", "")
        with pytest.raises(ValueError):
            BR.Reject("it_looked_wrong", "")


class TestConsumerCompatibility:
    """Defect: the module claimed embed_dim was a routing key. It is not -- every consumer keeps
    only len(embed) == 1024 and discards or zero-scores the rest."""

    def test_a_hear_width_is_flagged_not_advertised_as_routed(self):
        note = BR.consumer_note([160, 80])
        assert note is not None
        assert "160" in note and "80" in note and "1024" in note
        assert "DISCARD" in note
        for cited in ("cluster_audio_embed.py", "audio_anomaly_train.py",
                      "correlate_audio_sources.py", "audio_anomaly_score.py"):
            assert cited in note

    def test_the_only_width_that_passes_is_yamnets(self):
        assert BR.consumer_note([BR.CONSUMER_EMBED_DIM]) is None
        assert BR.CONSUMER_EMBED_DIM == 1024

    def test_the_warning_is_printed_by_a_run_not_left_in_a_docstring(self, tmp_path, capsys):
        p = tmp_path / "dets.csv"
        p.write_text(",".join(BR.DETS_COLUMNS) + "\n" +
                     "%d,12,2222,11,189911,818,0,16000.169,%s\n" % (UTC_US, _row()["frame_hex"]))
        BR.main(["--dets-csv", str(p), "--out", str(tmp_path / "sh"), "--node", NODE])
        err = capsys.readouterr().err
        assert "160" in err and "1024" in err


class TestFeatureVector:
    def test_the_dimension_is_derived_from_the_frame_not_declared(self):
        # embed_dim is the label that tells the two hear features apart in a shard, and the only
        # thing that distinguishes a 20x8 impulse from a longer descriptor once it is a list.
        a = BR.to_record(_row(), NODE)
        b = BR.to_record(_row(q=_q(12, 15)), NODE)
        assert a["embed_dim"] == 160 == len(a["embed"])
        assert b["embed_dim"] == 180 == len(b["embed"])

    def test_an_unknown_geometry_gets_no_span_rather_than_a_wrong_one(self):
        # hop is not on the wire, so span is only knowable for a shape this build recognises.
        assert BR.to_record(_row(), NODE)["feature"]["span_ms"] == 44.0
        assert BR.to_record(_row(q=_q(12, 15)), NODE)["feature"]["span_ms"] is None

    def test_the_vector_is_unit_norm(self):
        # audio_anomaly_train's EPS ridge and its p99.5 threshold are calibrated for unit vectors.
        v = np.asarray(BR.to_record(_row(), NODE)["embed"])
        assert float(np.linalg.norm(v)) == pytest.approx(1.0, abs=1e-5)

    def test_level_stays_out_of_the_vector_and_beside_it(self):
        # Absolute level is the strongest feature this project has measured, which is exactly why
        # it must not sit inside a cosine distance: it would collapse every cluster onto loudness.
        q = _q()
        a = BR.to_record(_row(q=q, ref_db=52.5, peak=818), NODE)
        b = BR.to_record(_row(q=q, ref_db=91.0, peak=2709), NODE)
        assert a["embed"] == b["embed"], "same shape, different level: the vector is the shape"
        assert a["feature"]["ref_db"] != b["feature"]["ref_db"]
        assert a["feature"]["peak"] != b["feature"]["peak"]

    def test_a_flat_sketch_is_zeros_not_nan(self):
        # Every cell at the reference is a real sketch of silence, not an error. A NaN here would
        # poison a covariance fit for the whole capture.
        rec = BR.to_record(_row(q=np.zeros((20, 8), np.int8)), NODE)
        assert rec["embed"] == [0.0] * 160
        assert not any(np.isnan(rec["embed"]))


class TestTags:
    def test_no_context_leads_because_the_shape_is_an_artefact(self):
        # A v1 sketch taken before the ring filled is all-equal bands. Naming it by its level
        # would seed a real-looking cluster out of an artefact.
        t = BR.level_tags(818, retrigger=False, context_ok=False)
        assert t[0]["name"] == "no_context"

    def test_unknown_context_adds_no_tag_either_way(self):
        t = BR.level_tags(818, retrigger=False, context_ok=None)
        assert [x["name"] for x in t] == ["lvl_p0_p25"]

    def test_a_retrigger_is_a_qualifier_not_the_cluster_name(self):
        t = BR.level_tags(818, retrigger=True)
        assert t[0]["name"].startswith("lvl_")
        assert t[-1]["name"] == "retrigger"

    def test_scores_are_not_posteriors(self):
        # There is no classifier on the node. A score that varied would be read as one.
        assert all(t["score"] == 1.0 for t in BR.level_tags(2709))

    @needs_capture
    def test_the_default_edges_are_this_captures_own_quartiles(self):
        # The house rule: a number in a comment is one that was measured. These are the numpy
        # linear quartiles of abs(trigger) over the capture's clock-anchored detections.
        pk = sorted(abs(int(r["trigger"])) for r in _capture_rows() if int(r["utc_us"]) > 0)
        assert len(pk) == 48
        assert tuple(np.percentile(pk, [25, 50, 75])) == BR.DET_PEAK_QUARTILES
        assert (pk[0], pk[-1]) == (796, 2709)

    @needs_capture
    def test_the_minimum_peak_is_below_the_gate_floor(self):
        # The comment used to claim every detection is above the gate floor of 800 "by
        # construction". It is not, twice over: the floor thresholds a 16-sample envelope while
        # `trigger` is the single DC-blocked sample at the crossing, and the measured minimum is
        # 796.
        pk = [abs(int(r["trigger"])) for r in _capture_rows() if int(r["utc_us"]) > 0]
        assert min(pk) < 800

    @needs_capture
    def test_the_bands_actually_separate_in_this_capture(self):
        # cluster_audio_embed names a cluster by top_tags[0] and sorts "?" into silence/ambient,
        # so a single-valued label makes the report meaningless. The detections' own quartiles
        # split them 12/12/12/12.
        res = BR.convert(_capture_rows(), NODE)
        names = [r["top_tags"][0]["name"] for r in res["records"]]
        assert len(set(names)) == 4
        assert min(names.count(n) for n in set(names)) >= 10

    def test_operator_edges_reach_the_tags(self):
        # level_tags' docstring says to pass your own edges; there must be a way to.
        hot = BR.to_record(_row(peak=818), NODE, peak_edges=[100.0, 200.0, 300.0])
        assert hot["top_tags"][0]["name"] == "lvl_p75_up"


class TestAudioPointer:
    def test_no_audio_is_in_the_record_only_a_way_to_ask_for_it(self):
        rec = BR.to_record(_row(), NODE, base_url="http://node.invalid")
        assert rec["audio"]["pushed"] is False
        # The only numeric array a record carries is the embedding. 2 s of int16 at 16 kHz is
        # 64000 B before any encoding, so this also bounds what could be hiding in the line.
        arrays = [l for l in _lists(rec) if l and all(isinstance(x, float) for x in l)]
        assert arrays == [rec["embed"]]
        assert len(BR.to_shard_line(rec)) < 4096

    def test_the_url_is_the_nodes_documented_contract(self):
        # GET /audio?from=<utc_us>&dur=<seconds>. `from` is MICROSECONDS SINCE THE EPOCH and
        # there is no `n` parameter: a sample index passed as `from` dereferences to 1970 and
        # comes back 400.
        rec = BR.to_record(_row(sample=1_000_000), NODE, base_url="http://node.invalid",
                           pre_s=1.0, post_s=1.0)
        a = rec["audio"]
        path, q = _qs(a["url"])
        assert path == "/audio"
        assert set(q) == {"from", "dur"}
        assert "n" not in q
        assert int(q["from"][0]) == a["utc_us_from"] > 1_700_000_000_000_000
        assert float(q["dur"][0]) == pytest.approx(a["dur_s"], abs=1e-6)

    def test_the_url_parses_back_to_this_records_own_timestamps(self):
        rec = BR.to_record(_row(sample=1_000_000), NODE, base_url="http://node.invalid",
                           pre_s=1.0, post_s=2.0)
        a = rec["audio"]
        _, q = _qs(a["url"])
        frm, dur = int(q["from"][0]), float(q["dur"][0])
        fs = a["fs_hz"]
        # The window starts one second before the detection and runs three: `from` is the stamp
        # minus the pre-roll, to the microsecond the sample count works out to.
        assert frm == rec["utc_us"] - int(round(int(round(1.0 * fs)) / fs * 1e6))
        assert rec["utc_us"] - frm == pytest.approx(1e6, abs=100)
        assert dur == pytest.approx(3.0, abs=1e-3)
        # and it agrees with the sample range, which is the same two numbers times fs.
        assert a["n_samples"] == pytest.approx(dur * fs, abs=1)
        assert a["utc_us_to"] - a["utc_us_from"] == pytest.approx(dur * 1e6, abs=2)

    @needs_capture
    def test_every_pointer_in_the_capture_dereferences(self):
        # The first record of this capture is the one that used to emit from=0 -> epoch 0 -> 400.
        res = BR.convert(_capture_rows(), NODE, base_url="http://node.invalid")
        lo = min(r["utc_us"] for r in res["records"])
        hi = max(r["utc_us"] for r in res["records"])
        for r in res["records"]:
            _, q = _qs(r["audio"]["url"])
            frm, dur = int(q["from"][0]), float(q["dur"][0])
            assert frm > 0, "a from= of 0 is 1970 and the node answers 400"
            assert lo - 2_000_000 <= frm <= hi, "inside the capture it came from"
            assert 0 < dur <= BR.NODE_MAX_DUR_S
            assert r["audio"]["sample_from"] >= 0

    def test_a_window_before_boot_is_clamped_in_time_and_samples_together(self):
        # sample resets on boot; a negative ring index is meaningless. Both ranges must describe
        # the SAME window -- the bug was a sample range of 1.139 s under a UTC range of 2 s.
        a = BR.audio_pointer(NODE, UTC_US, sample=2222, fs_hz=16000.0, pre_s=1.0, post_s=1.0)
        assert a["sample_from"] == 0
        assert a["clamped_to_boot"] is True
        assert a["n_samples"] == 2222 + 16000
        assert a["dur_s"] == pytest.approx(a["n_samples"] / 16000.0, abs=1e-6)
        assert a["utc_us_to"] - a["utc_us_from"] == pytest.approx(a["dur_s"] * 1e6, abs=2)
        assert UTC_US - a["utc_us_from"] == pytest.approx(2222 / 16000.0 * 1e6, abs=2)

    def test_a_row_with_no_sample_still_points_at_the_right_seconds(self):
        # The pointer is addressed by TIME, so a missing ring index costs the sample range and
        # nothing else. It must not be replaced by 0, which is a real position in the ring.
        row = _row()
        del row["sample"]
        rec = BR.to_record(row, NODE, base_url="http://node.invalid")
        assert rec["clock"]["sample"] is None
        a = rec["audio"]
        assert a["sample_from"] is None and a["clamped_to_boot"] is None
        assert a["n_samples"] == 2 * int(round(16000.169))
        _, q = _qs(a["url"])
        assert int(q["from"][0]) == a["utc_us_from"]

    def test_a_window_with_no_pre_roll_says_unknown_not_false(self):
        # Defect: on the scene path pre_s is 0, so `sample - 0 < 0` could never be True and every
        # scene record shipped clamped_to_boot: false -- a check reported as passed that was never
        # run. None is the third state: the question was not asked.
        a = BR.audio_pointer(NODE, UTC_US, sample=0, fs_hz=16000.0, pre_s=0.0, post_s=1.024)
        assert a["clamped_to_boot"] is None
        assert a["sample_from"] == 0, "the sample range is still exact; only the clamp is unasked"
        # sample 0 is the tightest case there is, and even there the scene path cannot clamp.
        rec = BR.to_record(_scene_row(sample=0), NODE, frame_key="mel_hex")
        assert rec["audio"]["clamped_to_boot"] is None
        assert rec["audio"]["sample_from"] == 0
        # A detection, which does have a pre-roll, still answers True or False.
        det = BR.to_record(_row(sample=0), NODE)["audio"]
        assert det["clamped_to_boot"] is True
        assert BR.audio_pointer(NODE, UTC_US, 10 ** 7, 16000.0)["clamped_to_boot"] is False

    def test_sample_zero_is_a_position_not_an_absence(self):
        rec = BR.to_record(_row(sample=0), NODE)
        assert rec["clock"]["sample"] == 0
        assert rec["audio"]["sample_from"] == 0 and rec["audio"]["clamped_to_boot"] is True

    def test_a_window_longer_than_the_node_serves_is_clamped_visibly(self):
        # The node caps dur at AUDIO_MAX_S and would return a shorter file than the pointer
        # promised; better to promise what it will actually serve.
        a = BR.audio_pointer(NODE, UTC_US, sample=10_000_000, fs_hz=16000.0,
                             pre_s=20.0, post_s=30.0)
        assert a["clamped_to_max_dur"] is True
        assert a["dur_s"] == pytest.approx(BR.NODE_MAX_DUR_S, abs=1e-6)
        assert a["n_samples"] == int(BR.NODE_MAX_DUR_S * 16000.0)

    def test_retention_is_absent_unless_the_operator_measured_it(self):
        # The ring exists now, but its span is chosen at boot from whatever PSRAM was contiguous
        # (80 s down to 30 s). Only that boot's /status knows which.
        a = BR.audio_pointer(NODE, UTC_US, 2222, 16000.0)
        assert a["retention_s"] is None and a["expires_utc_us"] is None
        b = BR.audio_pointer(NODE, UTC_US, 2222, 16000.0, retention_s=180.0)
        assert b["expires_utc_us"] == b["utc_us_to"] + 180_000_000

    def test_no_base_url_gives_no_url_rather_than_a_guessed_host(self):
        assert BR.audio_pointer(NODE, UTC_US, 2222, 16000.0)["url"] is None

    def test_a_nonsense_rate_falls_back_to_nominal(self):
        # The firmware substitutes the nominal itself pre-lock. At the measured +10.6 ppm a 2 s
        # window differs by 21 us, a third of a sample: not a pointer.
        a = BR.audio_pointer(NODE, UTC_US, 32000, fs_hz=0.0)
        assert a["fs_hz"] == BR.NOMINAL_FS


class TestShards:
    def test_bucket_and_name_match_the_archiver(self):
        assert BR.shard_bucket(1788763952.189911, 60) == 1788763920
        assert BR.shard_name(1788763920) == "telem_1788763920.jsonl"

    def test_a_zero_shard_length_does_not_divide_by_zero(self):
        assert BR.shard_bucket(100.0, 0) == 100

    def test_a_line_carries_the_one_field_the_ingest_api_requires(self):
        # to_ingest_line's whole contract: a JSON object with a time under `t` or `ts`. Missing,
        # the API quarantines the entire message.
        line = BR.to_shard_line(BR.to_record(_row(), NODE))
        d = json.loads(line)
        assert isinstance(d, dict) and isinstance(d["ts"], float)
        assert line.endswith("\n") and line.count("\n") == 1

    def test_buckets_follow_the_recording_not_the_hour_it_was_converted(self):
        rows = [_row(utc_us=UTC_US), _row(utc_us=UTC_US + 3_600_000_000)]
        sh = BR.group_into_shards(BR.convert(rows, NODE)["records"])
        assert len(sh) == 2, "an 11 h capture must not collapse into one shard"

    def test_write_leaves_no_partial_behind(self, tmp_path):
        sh = BR.group_into_shards(BR.convert([_row()], NODE)["records"])
        BR.write_shards(str(tmp_path), sh)
        names = sorted(p.name for p in tmp_path.iterdir())
        assert names == list(sh), "log_forwarder globs telem_*.jsonl and posts what it finds"

    def test_a_second_run_refuses_rather_than_duplicating_a_capture(self, tmp_path):
        sh = BR.group_into_shards(BR.convert([_row()], NODE)["records"])
        BR.write_shards(str(tmp_path), sh)
        with pytest.raises(FileExistsError):
            BR.write_shards(str(tmp_path), sh)


class TestCommandLine:
    def _dets(self, tmp_path, rows):
        p = tmp_path / "dets.csv"
        body = [",".join(BR.DETS_COLUMNS)]
        for r in rows:
            body.append(",".join(str(r[c]) for c in BR.DETS_COLUMNS))
        p.write_text("\n".join(body) + "\n")
        return str(p)

    def test_peak_edges_are_reachable_from_the_command_line(self, tmp_path):
        # The documented escape hatch has to exist as a flag, or it is not an escape hatch.
        d = self._dets(tmp_path, [_row(peak=818)])
        out = tmp_path / "sh"
        assert BR.main(["--dets-csv", d, "--out", str(out), "--node", NODE,
                        "--peak-edges", "100,200,300"]) == 0
        rec = json.loads(next(out.iterdir()).read_text().splitlines()[0])
        assert rec["top_tags"][0]["name"] == "lvl_p75_up"

    def test_default_edges_are_this_repos_capture(self, tmp_path):
        d = self._dets(tmp_path, [_row(peak=818)])
        out = tmp_path / "sh"
        BR.main(["--dets-csv", d, "--out", str(out), "--node", NODE])
        rec = json.loads(next(out.iterdir()).read_text().splitlines()[0])
        assert rec["top_tags"][0]["name"] == "lvl_p0_p25"

    def test_unusable_edges_are_refused_not_sorted_for_you(self, tmp_path):
        d = self._dets(tmp_path, [_row()])
        with pytest.raises(SystemExit):
            BR.main(["--dets-csv", d, "--out", str(tmp_path / "sh"), "--node", NODE,
                     "--peak-edges", "300,200,100"])

    def test_a_scene_file_is_a_source_in_its_own_right(self, tmp_path):
        p = tmp_path / "scene.csv"
        p.write_text(_scene_csv([_scene_row(), _scene_row(utc_us=UTC_US + 1_024_000)]))
        out = tmp_path / "sh"
        assert BR.main(["--scene-csv", str(p), "--out", str(out), "--node", NODE]) == 0
        recs = [json.loads(l) for f in out.iterdir() for l in f.read_text().splitlines()]
        assert len(recs) == 2 and {r["embed_dim"] for r in recs} == {80}


class TestRejectIsTyped:
    """to_record's docstring promises Reject, a ValueError carrying .reason, on any row that must
    not ship. Defect: `float(row.get("fs_hz") or 0.0)` let a non-numeric fs_hz out as a BARE
    ValueError. convert()'s backstop renamed it malformed_row so the batch contract held, but a
    direct caller got the untyped error Reject exists to remove."""

    def test_a_non_numeric_fs_hz_is_a_reject_with_a_reason(self):
        row = _row()
        row["fs_hz"] = "sixteen thousand"
        with pytest.raises(BR.Reject) as e:
            BR.to_record(row, NODE)
        assert e.value.reason == "malformed_row"
        assert "fs_hz" in e.value.why
        assert isinstance(e.value, ValueError), "an `except ValueError` caller still catches it"

    def test_every_reject_from_to_record_carries_a_reason(self):
        # Whatever a caller feeds a row that cannot ship, the error names why in the vocabulary.
        bad_fs, no_utc, unanchored, junk = _row(), dict(_row()), _row(utc_us=0), _row()
        bad_fs["fs_hz"] = "fast"
        del no_utc["utc_us"]
        junk["frame_hex"] = "zz"
        no_sample = _row()
        no_sample["sample"] = "twenty"
        for row in (bad_fs, no_utc, unanchored, junk, no_sample):
            with pytest.raises(BR.Reject) as e:
                BR.to_record(row, NODE)
            assert e.value.reason in BR.REASONS

    def test_a_blank_fs_hz_is_still_a_missing_value_not_a_rejection(self):
        # Missing stays missing; only garbage is refused.
        row = _row()
        row["fs_hz"] = ""
        rec = BR.to_record(row, NODE)
        assert rec["clock"]["fs_hz"] is None
        assert rec["audio"]["fs_hz"] == BR.NOMINAL_FS


class TestEmptyIsNotSuccess:
    """Defect: _parse_csv returned [] for an empty file, write_shards wrote nothing for an empty
    dict, and main printed "0 row(s) -> 0 record(s) in 0 shard(s)" and returned 0. A zero-byte or
    wiped dets.csv -- the exact SD failure DETS_COLUMNS exists to catch -- read as a completed
    run."""

    def test_a_zero_byte_file_is_refused_not_read_as_a_quiet_period(self):
        for body in ("", "   ", "\n\n", "\n \t\n"):
            with pytest.raises(ValueError, match="empty"):
                BR.parse_dets_csv(body)
            with pytest.raises(ValueError, match="empty"):
                BR.parse_scene_csv(body)

    def test_a_header_with_no_rows_is_a_real_empty_run(self):
        # The other case, and it is legitimate: the node made the file, wrote the header, and had
        # nothing to append. It must stay distinguishable from the one above.
        assert BR.parse_dets_csv(",".join(BR.DETS_COLUMNS) + "\n") == []
        assert BR.parse_scene_csv(",".join(BR.SCENE_COLUMNS) + "\n") == []

    def test_write_shards_refuses_to_report_success_for_writing_nothing(self, tmp_path):
        out = tmp_path / "sh"
        with pytest.raises(ValueError):
            BR.write_shards(str(out), {})
        assert not out.exists(), "an empty --out looks exactly like a forwarded capture"

    def test_main_on_a_wiped_dets_csv_fails_loudly(self, tmp_path):
        p = tmp_path / "dets.csv"
        p.write_text("")
        with pytest.raises(ValueError, match="empty"):
            BR.main(["--dets-csv", str(p), "--out", str(tmp_path / "sh"), "--node", NODE])

    def test_main_writes_nothing_and_returns_nonzero_when_there_are_no_records(
            self, tmp_path, capsys):
        p = tmp_path / "dets.csv"
        p.write_text(",".join(BR.DETS_COLUMNS) + "\n")
        out = tmp_path / "sh"
        assert BR.main(["--dets-csv", str(p), "--out", str(out), "--node", NODE]) == 1
        assert not out.exists()
        cap = capsys.readouterr()
        assert "NOTHING WAS WRITTEN" in cap.err
        assert "0 record(s)" not in cap.out, "stdout must not report a completed run"

    def test_a_capture_that_was_entirely_rejected_is_not_a_completed_run(self, tmp_path, capsys):
        # Every row unanchored: rows were read, nothing shippable came out. log_forwarder would
        # glob an --out that does not exist, so the exit status has to say so.
        body = [",".join(BR.DETS_COLUMNS)]
        r = _row(utc_us=0)
        body.append(",".join(str(r[c]) for c in BR.DETS_COLUMNS))
        p = tmp_path / "dets.csv"
        p.write_text("\n".join(body) + "\n")
        out = tmp_path / "sh"
        assert BR.main(["--dets-csv", str(p), "--out", str(out), "--node", NODE]) == 1
        assert not out.exists()
        err = capsys.readouterr().err
        assert "1 row(s) -> 0 record(s)" in err
        assert "unanchored_time" in err, "the zero is explained, not just reported"


class TestFirmwareConstantsDoNotDrift:
    """NODE_BANDS/FRAMES and NODE_MAX_DUR_S are COPIES of firmware constants, and a copy drifts.
    Bands and frames are on the wire, so a frame of another shape decodes to another shape and
    span_ms goes honestly null. Hop and nfft are NOT copied here any more (D8): feature_span_ms
    reads SK.HOP_S/SK.NFFT straight from hear.sketch, so there is nothing left in this module for
    a firmware edit to leave stale -- the risk that remains is passing feature_span_ms the WRONG
    fs, and that is what test_the_span_... below proves against, for both rates the fleet has.

    ⚠️THIS CLASS USED TO POINT AT firmware/hear_node/mel16.h, WHICH IS GONE (D1: the hear_node
    bank was renamed mel_impulse.h / MELIMP_ when it moved to 48 kHz, precisely so a stale
    reference to the old name fails loudly -- FileNotFoundError -- instead of reading a real,
    valid, WRONG-RATE header and passing green. That FileNotFoundError firing here, once, on this
    change, was this guard doing its job; the fix is to point it at the new file and check the
    new rate, not to make it tolerant of either."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    IMPULSE = os.path.join(ROOT, "firmware", "hear_node", "mel_impulse.h")
    PATH_TEST = os.path.join(ROOT, "firmware", "path_test", "mel16.h")
    BOARD = os.path.join(ROOT, "firmware", "boards", "xiao_s3_sense.h")
    INO = os.path.join(ROOT, "firmware", "hear_node", "hear_node.ino")

    @staticmethod
    def _define(path, name):
        import re
        m = re.search(r"^#define\s+%s\s+([^\s/]+)" % re.escape(name),
                      open(path, encoding="utf-8", errors="replace").read(), re.M)
        assert m, "%s is not #defined in %s any more; this copy is now unanchored" % (name, path)
        return m.group(1).rstrip("f")

    def test_the_impulse_geometry_still_matches_the_compiled_bank(self):
        d = lambda n: int(self._define(self.IMPULSE, n))          # noqa: E731
        assert (BR.NODE_BANDS, BR.NODE_FRAMES) == (d("MELIMP_BANDS"), d("MELIMP_FRAMES"))
        # NOMINAL_FS converts a DECIMATED `sample` index; LEGACY_NODE_FS guesses a rate-unstated
        # sketch's rate. Both are the board's FS_NOMINAL -- two names, one number, on purpose (D8).
        fs_nominal = float(self._define(self.BOARD, "FS_NOMINAL"))
        assert BR.NOMINAL_FS == fs_nominal
        assert BR.LEGACY_NODE_FS == fs_nominal
        # And the impulse bank itself must NOT be at that rate any more -- it is the one thing
        # this whole change moves.
        assert float(self._define(self.IMPULSE, "MELIMP_FS")) == fs_nominal * int(
            self._define(self.INO, "DECIM"))

    def test_the_span_this_module_reports_is_the_span_the_firmware_fetches(self):
        # hear_node.ino: `#define SKETCH_SPAN (MELIMP_NFFT + (MELIMP_FRAMES - 1) * MELIMP_HOP)`.
        # No +32 guard term any more -- the ring fetch IS the span (D2/D4 replaced the old
        # over-fetch with an exact readiness test) -- so this is the rate-agnostic property with
        # nothing subtracted off either side, checked against the header that is actually built
        # into a hear_node: mel_impulse.h, at its own (48 kHz) rate.
        nfft = int(self._define(self.IMPULSE, "MELIMP_NFFT"))
        hop = int(self._define(self.IMPULSE, "MELIMP_HOP"))
        frames = int(self._define(self.IMPULSE, "MELIMP_FRAMES"))
        fs = float(self._define(self.IMPULSE, "MELIMP_FS"))
        span_samples = nfft + (frames - 1) * hop
        assert BR.feature_span_ms(frames, fs) == pytest.approx(
            span_samples / fs * 1000.0, abs=1e-9)

        # THE SAME CALL, both rates the fleet actually writes: a legacy/16 kHz frame (path_test's
        # bank, and every node before this move) and a current 48 kHz one. Fails against a version
        # of feature_span_ms that re-pins its formula to one configuration -- e.g. hardcoding the
        # 48 kHz numbers this test's first assertion just checked -- because that would still pass
        # the first assertion and fail one of these two.
        assert BR.feature_span_ms(BR.NODE_FRAMES, 16000.0) == 44.0
        assert BR.feature_span_ms(BR.NODE_FRAMES, 48000.0) == pytest.approx(33.333, abs=0.001)
        # path_test's own compiled bank names the legacy rate explicitly, rather than trusting the
        # literal 16000.0 above to still be what that file says.
        assert BR.feature_span_ms(BR.NODE_FRAMES, float(
            self._define(self.PATH_TEST, "MEL16_FS"))) == 44.0

        # A row whose frame does not state a rate (predates the fs code, or SK.fs_code() cannot
        # name it) falls back to LEGACY_NODE_FS -- still 44.0 ms, which is the whole fleet's
        # history before this move.
        assert BR.to_record(_row(), NODE)["feature"]["span_ms"] == 44.0

    def test_the_column_and_the_frame_agree_on_four_milliseconds(self):
        """D6: dets.csv's `sketch_back` column is NOT renamed when its unit changes (64 decimated
        samples on a legacy row, 192 acquisition samples on a current one) -- tools/hear_bridge.py
        does not even read that column (see DETS_COLUMNS), so the unit has to be recoverable from
        the rest of the SAME row instead: the row's own frame_hex states its rate (flags bits
        8-11), and sketch_back / that rate is SKETCH_BACK_S regardless of which row it is.

        Proved against the firmware's own SKETCH_BACK_S and the value hear_node.ino currently
        writes to the column (SKETCH_BACK, derived per sketch_domain.h's sk_back_acq_len -- see
        tests/test_firmware_sketch_domain.py::test_the_back_off_is_derived_from_time_not_from_the_hop
        for that derivation itself), plus the legacy pairing every stored row before this move
        used. Fails against a firmware that writes an acquisition-domain sketch_back but a frame
        whose flags still claim the decimated rate, or vice versa -- exactly the mismatch a G6
        column rename would have been catching, without paying for a G6 column rename."""
        back_s = float(self._define(self.INO, "SKETCH_BACK_S"))
        fs_nominal = float(self._define(self.BOARD, "FS_NOMINAL"))
        fs_acq = fs_nominal * int(self._define(self.INO, "DECIM"))
        # SKETCH_BACK == MELIMP_HOP at this rate is a coincidence (D5), not asserted equal in C
        # on purpose -- so this is derived from SKETCH_BACK_S x fs_acq, the way the firmware does.
        sketch_back_acq = round(back_s * fs_acq)

        for sketch_back, fs in ((sketch_back_acq, fs_acq),   # what a row writes today
                                (64, fs_nominal)):            # what every row wrote before
            q = _q()
            raw = SK.pack(0, 0.0, 0, q, fs=fs, layout=SK.LAYOUT_FIXED)
            u = SK.unpack(raw)
            assert u["fs_hz"] == fs, "SK.pack/unpack did not round-trip this rate"
            assert sketch_back / u["fs_hz"] == pytest.approx(back_s, abs=1e-12)

    def test_the_pointer_clamp_is_the_limit_the_node_actually_serves(self):
        # A pointer promising more than AUDIO_MAX_S comes back short with no error.
        assert BR.NODE_MAX_DUR_S == float(self._define(self.INO, "AUDIO_MAX_S"))


@needs_capture
class TestMeasuredNumbers:
    """The house rule, enforced: every number quoted in tools/hear_bridge.py's comments is
    recomputed here from ~/dama-hear-capture-2026-09-07, with the recipe the comment states."""

    def _health(self):
        import csv as _csv
        with open(os.path.join(CAPTURE, "health.csv"), encoding="utf-8") as fh:
            return list(_csv.DictReader(fh))

    def test_the_capture_is_the_run_the_module_docstring_describes(self):
        h = self._health()
        up = [int(r["uptime_s"]) for r in h]
        assert (len(h), len(_capture_rows())) == (1360, 59)
        assert (min(up), max(up)) == (28, 40819)
        assert (max(up) - min(up)) / 3600.0 == pytest.approx(11.33, abs=0.005)

    def test_the_clock_discipline_quoted_for_the_timestamp(self):
        h = self._health()
        tacc = [int(r["tacc_ns"]) for r in h]
        spread = [float(r["spread_us"]) for r in h]
        assert {r["fix"] for r in h} == {"3"}
        assert (min(tacc), max(tacc)) == (22, 26)
        assert (min(spread), max(spread), np.median(spread)) == (2.0, 15.0, 10.0)
        assert int(h[-1]["pps"]) == 40817 and max(int(r["pps_bad"]) for r in h) == 0

    def test_float64_at_this_epoch_holds_the_microsecond_but_not_the_nanosecond(self):
        import math
        assert math.ulp(UTC_US / 1e6) == pytest.approx(2.384e-07, rel=1e-3)   # 238 ns
        assert math.ulp(UTC_US / 1e6) * 1e9 > 26, "coarser than the tAcc the receiver reported"

    def test_the_nominal_rate_fallback_is_the_last_health_rows_own_numbers(self):
        last = self._health()[-1]
        assert float(last["fs_clean_hz"]) == 16000.1690
        assert int(last["fs_win_s"]) == 3030
        assert float(last["fs_cum_hz"]) == 15991.4821
        ppm = (float(last["fs_clean_hz"]) - BR.NOMINAL_FS) / BR.NOMINAL_FS * 1e6
        assert ppm == pytest.approx(10.5625, abs=1e-4)
        assert 2.0 * ppm == pytest.approx(21.1, abs=0.1)        # us over a 2 s window
        assert 1e6 / BR.NOMINAL_FS == 62.5                      # one sample

    def test_the_gate_and_ambient_numbers_beside_det_peak_quartiles(self):
        h = self._health()
        gt = [float(r["gate_thr"]) for r in h]
        assert (min(gt), max(gt)) == (800.0, 1715.0)
        assert sum(1 for x in gt if x == 800.0) == 1328
        ep = [float(r["env_peak_win"]) for r in h]
        # Defect: the comment claimed median 368.9 and p95 692.0. Neither reproduces on either
        # snapshot of this capture; these are what the column actually holds.
        assert np.median(ep) == 380.0
        assert np.percentile(ep, 95) == 708.0
        pk = [abs(int(r["trigger"])) for r in _capture_rows() if int(r["utc_us"]) > 0]
        assert np.percentile(ep, 95) < min(pk), "banding on ambient puts all 48 in one bucket"

    def test_the_final_snapshot_is_a_different_capture_with_different_edges(self):
        # The warning beside DET_PEAK_QUARTILES: same run, longer snapshot, three more detections
        # and three different quartiles. Quoting one file's number against the other is the
        # unreproducible-number failure in its purest form.
        import csv as _csv
        f = os.path.join(CAPTURE, "final", "dets.csv")
        if not os.path.exists(f):
            pytest.skip("final/ snapshot not retrieved beside this capture")
        with open(f, encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
        pk = sorted(abs(int(r["trigger"])) for r in rows if int(r["utc_us"]) > 0)
        assert (len(rows), len(pk)) == (62, 51)
        assert tuple(np.percentile(pk, [25, 50, 75])) == (860.5, 965.0, 1283.5)
        assert tuple(np.percentile(pk, [25, 50, 75])) != BR.DET_PEAK_QUARTILES

    def test_the_unanchored_share_the_comment_quotes(self):
        rows = _capture_rows()
        assert sum(1 for r in rows if int(r["utc_us"]) == 0) == 11
        assert len(rows) == 59
        assert 1788763952 / 86400 / 365.25 == pytest.approx(56.7, abs=0.05)   # "a 56-year event"

    def test_the_ring_arithmetic_in_the_pointer_docstring(self):
        assert 80 * 48000 * 2 / 1e6 == 7.68         # MB of int16 for the 80 s the node asks for


@needs_capture
class TestTheRetrievedCapture:
    def test_the_whole_capture_converts_to_one_dimension(self):
        res = BR.convert(_capture_rows(), NODE, node_id=1, base_url="http://node.invalid")
        assert {r["embed_dim"] for r in res["records"]} == {160}
        assert all(r["feature"]["bands"] == 20 and r["feature"]["frames"] == 8
                   for r in res["records"])

    def test_the_shards_span_the_capture_and_every_line_parses(self):
        res = BR.convert(_capture_rows(), NODE)
        sh = BR.group_into_shards(res["records"])
        rows = [json.loads(l) for lines in sh.values() for l in lines]
        assert len(rows) == len(res["records"])
        span_s = max(r["ts"] for r in rows) - min(r["ts"] for r in rows)
        assert span_s > 11 * 3600, "the capture is 11.33 h of unbroken run"

    def test_the_message_cap_binds_before_the_byte_cap(self):
        # log_forwarder chunks at 400 messages (:46) or 4_500_000 body bytes (:47). The widest
        # line this capture produces with a pointer URL is measured here rather than quoted, and
        # 400 of anything that size is well under the byte cap.
        res = BR.convert(_capture_rows(), NODE, base_url="http://node.invalid")
        widest = max(len(BR.to_shard_line(r)) for r in res["records"])
        assert widest < 4096
        assert 400 * widest < 4_500_000
