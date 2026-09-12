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

    def test_the_phone_timestamp_uses_the_frames_microseconds_not_the_ms_bucket(self):
        """⚠️THE CROSS-REPO TIMING CONTRACT. dama-gotchi puts the absolute second in
        `ts_utc_ms` and the frame's own microseconds-within-that-second in `node_us`. Reading the
        millisecond field alone quantises the sketch onto a 1 ms grid and loses the onset
        precision the frame already carries.

        Pick the near-rollover case because it proves both halves at once: 999.600 ms rounds to
        the next integer millisecond, so the absolute second still has to come from `ts_utc_ms`
        while the within-second digits come from the frame.
        """
        true_utc_us = 1788700000999600
        frame, q, ref = _frame(fs=48000.0)
        frame = SK.pack(true_utc_us % 1_000_000, ref, 500, q, fs=48000.0, layout=SK.LAYOUT_FIXED)
        r = C.from_phone({"sketch_b64": base64.b64encode(frame).decode(),
                          "ts_utc_ms": round(true_utc_us / 1000),
                          "clock_tier": "gnss"})
        assert r.node_us == 999600
        assert r.ts_utc_s == pytest.approx(true_utc_us / 1e6, abs=1e-12)

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

    def test_the_records_band_edges_are_the_frames_own_and_not_the_nyquist_axis(self):
        """⚠️Record.band_edges_hz() is the accessor a caller uses to decide whether two frames are
        comparable, so it has to answer from the axis the frame states. Fails against
        `SK.band_edges_hz(self.fs_hz, self.bands)` -- the layout argument defaulted, which gives
        a fixed-layout 16 kHz node frame a 7840.0 Hz top edge against its own frame's 20000.0 and
        so makes two frames on ONE axis compare as two.

        16 kHz is the rate that reproduces it: at 48 kHz the two layouts coincide, which is why
        every frame the node emits from here on hides this and only the stored history shows it.
        """
        for fs in (16000.0, 48000.0):
            frame = SK.pack(123456, -20.0, 500,
                            SK.sketch(np.random.default_rng(5).normal(0, 1000, 4096), fs)[0],
                            fs=fs, layout=SK.LAYOUT_FIXED)
            rec = C.from_node(frame, "n")
            assert rec.extra["layout"] == SK.LAYOUT_FIXED
            assert np.allclose(rec.band_edges_hz(), SK.unpack(frame)["band_edges_hz"]), fs
        # and the two rates are then the same axis, which is the question this accessor answers
        r16 = C.from_node(SK.pack(1, -20.0, 500, SK.sketch(
            np.random.default_rng(5).normal(0, 1000, 4096), 16000.0)[0],
            fs=16000.0, layout=SK.LAYOUT_FIXED), "n")
        r48 = C.from_node(SK.pack(1, -20.0, 500, SK.sketch(
            np.random.default_rng(5).normal(0, 1000, 4096), 48000.0)[0],
            fs=48000.0, layout=SK.LAYOUT_FIXED), "p")
        assert np.allclose(r16.band_edges_hz(), r48.band_edges_hz())


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


class TestCrossRateAlignment:
    """⚠️Band k is only the same frequency at two rates under the FIXED layout. These pin that."""

    @staticmethod
    def _rec(fs, seed=0, layout=SK.LAYOUT_FIXED, node="n"):
        q, ref = SK.sketch(np.random.default_rng(seed).normal(0, 1000, 4096), fs, layout=layout)
        return C.from_node(SK.pack(1, ref, 5, q, fs=fs, layout=layout), node)

    def test_the_two_layouts_are_the_same_bytes_at_48k(self):
        """The whole fleet's phones are above LAYOUT_EQUIVALENT_ABOVE_HZ, so turning the fixed
        layout on changes not one byte they send. It changes everything a 16 kHz node sends."""
        x = np.random.default_rng(4).normal(0, 1000, 4096)
        a, ra = SK.sketch(x, 48000.0, layout=SK.LAYOUT_NYQUIST)
        b, rb = SK.sketch(x, 48000.0, layout=SK.LAYOUT_FIXED)
        assert np.array_equal(a, b) and ra == rb
        c, _ = SK.sketch(x, 16000.0, layout=SK.LAYOUT_NYQUIST)
        d, _ = SK.sketch(x, 16000.0, layout=SK.LAYOUT_FIXED)
        assert not np.array_equal(c, d), "16 kHz must differ, or the fix does nothing"

    def test_band_centres_agree_across_rates_under_the_fixed_layout(self):
        e48 = SK.band_edges_hz(48000.0, layout=SK.LAYOUT_FIXED)
        e16 = SK.band_edges_hz(16000.0, layout=SK.LAYOUT_FIXED)
        assert np.allclose(e48, e16)
        # and disagree under the shipped one, which is the defect
        assert not np.allclose(SK.band_edges_hz(48000.0), SK.band_edges_hz(16000.0))

    def test_a_slow_node_leaves_its_top_bands_empty(self):
        q, _ = SK.sketch(np.random.default_rng(2).normal(0, 1000, 4096), 16000.0,
                         layout=SK.LAYOUT_FIXED)
        k = SK.valid_bands(16000.0)
        assert k == 15
        assert (q[k:] == q.min()).all(), "bands above Nyquist must sit at the floor"

    def test_the_matrix_is_as_wide_as_the_narrowest_sensor(self):
        recs = [self._rec(48000.0, 0), self._rec(48000.0, 1), self._rec(16000.0, 2)]
        X, kept, info = C.aligned_matrix(recs)
        assert X.shape == (3, 15 * SK.FRAMES)
        assert info["bands"] == 15 and info["rates_hz"] == [16000.0, 48000.0]
        assert info["dropped_bands"] == 5

    def test_all_fast_sensors_keep_every_band(self):
        X, _, info = C.aligned_matrix([self._rec(48000.0, i) for i in range(3)])
        assert info["bands"] == SK.MEL_BANDS and X.shape[1] == SK.MEL_BANDS * SK.FRAMES

    def test_it_refuses_a_rescaled_record_rather_than_aligning_fiction(self):
        recs = [self._rec(48000.0, 0), self._rec(16000.0, 1, layout=SK.LAYOUT_NYQUIST)]
        with pytest.raises(ValueError, match="nyquist"):
            C.aligned_matrix(recs)

    def test_a_record_that_states_no_axis_is_refused_not_assumed_onto_the_shared_one(self):
        """The refusal above reads `extra["layout"]`, so what an ABSENT key means decides whether
        an axis-less record is checked at all. Fails against `extra.get("layout",
        SK.LAYOUT_FIXED)`, which walks it straight through the one refusal this function exists
        for. Nothing in-tree builds such a Record today -- all three constructors set the key --
        which is exactly why the default has to be the refusing one."""
        r = self._rec(48000.0, 0)
        r.extra.pop("layout")
        with pytest.raises(ValueError, match="nyquist"):
            C.aligned_matrix([r])

    def test_an_unstated_rate_is_dropped_because_its_empty_bands_are_unknown(self):
        q, ref = SK.sketch(np.zeros(4096), 16000.0, layout=SK.LAYOUT_FIXED)
        nofs = C.from_node(SK.pack(1, ref, 0, q, layout=SK.LAYOUT_FIXED), "nofs")
        X, kept, _ = C.aligned_matrix([self._rec(48000.0, 0), nofs])
        assert len(kept) == 1

    def test_strict_drops_the_partially_covered_top_band(self):
        assert SK.valid_bands(16000.0, strict=True) < SK.valid_bands(16000.0, strict=False)
        recs = [self._rec(48000.0, 0), self._rec(16000.0, 1)]
        loose = C.aligned_matrix(recs)[2]["bands"]
        tight = C.aligned_matrix(recs, strict=True)[2]["bands"]
        assert tight < loose

    def test_empty_input_is_empty_not_an_error(self):
        X, kept, info = C.aligned_matrix([])
        assert X.shape == (0, 0) and kept == [] and info["bands"] == 0


class TestUtcTrusted:
    """⚠️`utc_trusted` is DERIVED from `clock_tier`, which the phone has published all along.

    hear/backend/associate.py refuses arrivals on a `utc_trusted` key and nothing publishes one.
    The obvious fix -- add the boolean to the phone payload -- is the wrong one: a new key is
    ABSENT on every row recorded before its rollout, and associate.arrival_is_usable reads absent
    as usable, so exactly the wall-clock stamps the flag exists to refuse would pass. Deriving it
    from a field already on the wire answers correctly for history too.
    """

    def test_a_disciplined_tier_is_trusted(self):
        for tier in ("gnss", "location"):
            p, _, _ = _phone_payload(clock_tier=tier)
            assert C.from_phone(p).utc_trusted is True, tier

    def test_the_wall_clock_fallback_is_not(self):
        p, _, _ = _phone_payload(clock_tier="wall")
        r = C.from_phone(p)
        assert r.utc_trusted is False
        # ...and it still carries a timestamp, which is the whole trap: the stamp exists.
        assert r.ts_utc_s is not None

    def test_network_is_not_a_gps_anchor_and_is_refused(self):
        """⚠️NOT a naming quibble. "network" is a NETWORK_PROVIDER fallback, not a GPS anchor.
        GPSTimingSync.java:612-616 calls it deliberately NOT clock-trustworthy, same as "wall";
        dama-gotchi's own GOOD_CLOCK_TIERS is {gnss, location}; EskfFusion.kt:318 lists it in
        BAD_PEER_CLOCK_TIERS. Its declared sigma is 25 ms -- 8.6 m at 343 m/s. Admitting it would
        make dama-hear disagree with the producer fleet about the same string.
        """
        p, _, _ = _phone_payload(clock_tier="network")
        assert C.from_phone(p).utc_trusted is False
        assert "network" not in C.TRUSTED_CLOCK_TIERS

    def test_a_tier_this_version_never_heard_of_is_not_trusted(self):
        p, _, _ = _phone_payload(clock_tier="ptp")
        assert C.from_phone(p).utc_trusted is False

    def test_an_unstated_tier_is_not_stated_not_false(self):
        """None means the producer did not say, and associate.arrival_is_usable treats that as
        usable BY DESIGN. Returning False here would refuse every pre-clock_tier phone row."""
        p, _, _ = _phone_payload()
        del p["clock_tier"]
        assert C.from_phone(p).utc_trusted is None

    def test_an_undated_onset_is_refused_at_any_tier(self):
        """The guard against an older build publishing a tier without the `stamp != null`
        coupling AcousticRangingCollector.kt:3479-3500 gives it today."""
        for tier in ("gnss", "location", "wall"):
            p, _, _ = _phone_payload(clock_tier=tier, onset_dated=False)
            assert C.from_phone(p).utc_trusted is False, tier

    def test_a_dated_onset_does_not_promote_a_wall_stamp(self):
        p, _, _ = _phone_payload(clock_tier="wall", onset_dated=True)
        assert C.from_phone(p).utc_trusted is False

    def test_a_node_is_none_because_its_clock_is_a_different_measurement(self):
        """A node's time comes from PPS lock and tAcc, not from clock_tier. Asserting node trust
        off a field nodes never set would be inventing a measurement."""
        frame, _, _ = _frame(fs=16000.0)
        assert C.from_node(frame, "nyquist", second_utc_s=1788700000).utc_trusted is None

    def test_a_producer_stating_it_outright_outranks_the_derivation(self):
        p, _, _ = _phone_payload(clock_tier="gnss", utc_trusted=False)
        assert C.from_phone(p).utc_trusted is False

    def test_it_is_read_only_on_the_record(self):
        """A property, not a dataclass field, so it cannot be reassigned on an instance.

        ⚠️That is ALL this proves. It does not prove the value can never disagree with the tier:
        a producer stating `utc_trusted` outright still outranks the derivation, by design --
        see test_a_producer_stating_it_outright_outranks_the_derivation. An earlier docstring
        here claimed the stronger property and was simply wrong.
        """
        p, _, _ = _phone_payload(clock_tier="wall")
        r = C.from_phone(p)
        with pytest.raises(AttributeError):
            r.utc_trusted = True

    def test_a_statement_does_not_defeat_the_source_guard(self):
        # A node's clock trust is a different measurement; a stated flag must not import this
        # scale onto it. ⚠️Nodes DO publish clock_tier ("pps"/"free"), neither of which is in
        # TRUSTED_CLOCK_TIERS, so the guard tests the SOURCE and not the field's presence.
        assert C.utc_trusted_of({"source": "node", "clock_tier": "pps",
                                 "utc_trusted": True}) is None

    def test_a_statement_does_not_defeat_the_undated_guard(self):
        # The same producer already said it could not date this onset. A flag contradicting its
        # own refusal is not new information, and the conservative reading wins.
        assert C.utc_trusted_of({"source": "phone", "clock_tier": "gnss",
                                 "onset_dated": False, "utc_trusted": True}) is False

    @pytest.mark.parametrize("stated", [0, 1, "false", "true", "", []])
    def test_a_non_bool_statement_is_refused_not_discarded(self, stated):
        """⚠️THE REGRESSION THAT POINTED THE WRONG WAY.

        `isinstance(stated, bool)` alone let a non-bool fall through to the tier derivation, so a
        producer publishing `utc_trusted: 0` -- saying do not trust me -- came back TRUE off its
        own good tier. A statement this version cannot parse is a producer it does not
        understand, which is the case the unknown-tier rule already refuses.
        """
        assert C.utc_trusted_of({"source": "phone", "clock_tier": "gnss",
                                 "utc_trusted": stated}) is False

    def test_onset_found_reaches_the_record_at_all(self):
        """⚠️REGRESSION. from_phone dropped `onset_found` on the floor, and it is the OTHER key
        associate._QUALITY_FLAGS refuses on -- a Record could not answer the gate's question."""
        p, _, _ = _phone_payload(onset_found=False)
        assert C.from_phone(p).extra["onset_found"] is False
        p2, _, _ = _phone_payload(onset_found=True)
        assert C.from_phone(p2).extra["onset_found"] is True

    def test_the_shared_function_is_what_the_property_calls(self):
        """One definition, so a second reader (pool.stats) cannot answer differently."""
        assert C.utc_trusted_of({"source": "phone", "clock_tier": "gnss"}) is True
        assert C.utc_trusted_of({"source": "phone", "clock_tier": "wall"}) is False
        assert C.utc_trusted_of({"source": "phone"}) is None
        assert C.utc_trusted_of({"source": "node", "clock_tier": "gnss"}) is None
