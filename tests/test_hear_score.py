"""hear-score: the consumer that finally reads the models this repo has been shipping.

⚠️NULL CONTROL FIRST, AND A STATED LIMIT ON WHAT IT PROVES. `TestNullControl` asserts the scorer
stays at the floor on a realistic quiet corpus before anything asserts it fires. That ordering is
this repo's discipline, but on its own it is a check that CANNOT FAIL: no labelled positive
exists in this checkout, the loudest of the nine shipped goldens scores 2.0e-02, and substituting
an all-zero weight vector scores 6.4e-21 -- both satisfy "stays quiet". `TestTheModelIsActually
Wired` is what closes that hole, by pinning exact values and then proving three real defects move
them. It proves the WIRING (weights, band-major order, the ref_db add-back), not detection.
"""
import base64
import json
import math
import os
import subprocess
import sys

import numpy as np
import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hear import sketch as SK                                   # noqa: E402
from hear import pool as P                                      # noqa: E402
from modules.supersonic import classify as CL                   # noqa: E402
from tools import hear_score as HS                              # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "deploy", "k8s"))
import gen_configmap as GC                                      # noqa: E402

MODEL_PATH = CL.FLEET_SKETCH_MODEL
GOLDEN = os.path.join(ROOT, "testdata", "sketch_golden.json")


# ----------------------------------------------------------------- fixtures

def _frame_bytes(fs=16000.0, layout=SK.LAYOUT_FIXED, amp=300.0, seed=0, node_us=1000,
                 state_fs=True):
    q, ref = SK.sketch(np.random.default_rng(seed).normal(0, amp, 4096), fs, layout=layout)
    return SK.pack(node_us, ref, 500, q, fs=(fs if state_fs else None), layout=layout)


def _row(frame: bytes, node="simnode", utc_us=1_700_000_000_000_000, sample="0", fs_csv=16000.0):
    return {"frame_hex": frame.hex(), "node": node, "utc_us": utc_us, "sample": sample,
            "schema": "G5", "fs_hz": fs_csv}


def build_pool(root, rows):
    pl = P.Pool(str(root))
    pl._append([P._record_from_node_row(r) for r in rows])
    return pl


def quiet_rows(n=24, **kw):
    return [_row(_frame_bytes(seed=i, **kw), utc_us=1_700_000_000_000_000 + i * 1_000_000,
                 sample=str(i)) for i in range(n)]


@pytest.fixture
def model():
    return CL.load_model(MODEL_PATH)


@pytest.fixture
def mb(model):
    return HS.model_block(model, MODEL_PATH, HS.model_sha256(MODEL_PATH))


def score(rec, model, mb, day="2023-11-14", **kw):
    return HS.score_record(rec, model, mb, day, **kw)


def one_record(frame, **kw):
    return P._record_from_node_row(_row(frame, **kw))


# ----------------------------------------------------------------- null control

class TestNullControl:
    """Quiet in, quiet out -- asserted BEFORE anything asserts the scorer fires."""

    def test_a_quiet_pool_scores_at_the_floor_and_refuses_nothing(self, tmp_path):
        build_pool(tmp_path, quiet_rows(24))
        t = HS.run(str(tmp_path), MODEL_PATH, write=False)
        assert t["scored"] == 24 and t["refused"] == 0 and t["unparseable"] == 0
        assert t["by_reason"] == {}
        d = t["observation_not_health"]["p_distribution"]
        assert d["above_0_5"] == 0, "a quiet corpus must not produce a positive"

    def test_the_class_distribution_is_not_an_input_to_any_gate(self, tmp_path):
        """A normal period is P at the floor. A check keyed on 'did anything score high' would
        read that correct result as a broken service, so it is published as an observation."""
        build_pool(tmp_path, quiet_rows(8))
        HS.run(str(tmp_path), MODEL_PATH)
        code, lines = HS.check(str(tmp_path))
        assert code == 0
        assert any("OBSERVATION, not a gate" in l for l in lines)
        hb = json.load(open(HS.heartbeat_path(str(tmp_path))))
        assert "p_distribution" in hb["runs"][-1]["observation_not_health"]


# ----------------------------------------------------------------- the model is wired

class TestTheModelIsActuallyWired:
    """⚠️WHAT THIS PROVES AND WHAT IT DOES NOT.

    It proves the weights are applied, in band-major order, with ref_db added back -- because
    each of those three defects MOVES a pinned value. It does NOT prove the model detects
    gunshots: this checkout carries no labelled positive, the nine shipped goldens are
    signal-processing fixtures (impulse/tone/noise/silence/clipped) and not class labels, and
    the loudest of them scores 2.0e-02. A threshold assertion over them ("stays below 0.5")
    would pass identically against an all-zero weight vector, which is why the values are
    pinned exactly instead.
    """

    @staticmethod
    def _golden(name):
        for c in json.load(open(GOLDEN))["cases"]:
            if c["name"] == name:
                return SK.unpack(base64.b64decode(c["frame_b64"]))
        raise AssertionError(name)

    def test_the_shipped_goldens_score_exactly_these_values(self, model):
        """Measured on this checkout, pinned to 1e-9 relative. Any of the three defects below
        moves every one of them, which is what a `< 0.5` assertion would not notice."""
        for name, want in (("noise_16k_fixed", 1.930663816381e-02),
                           ("impulse_16k_fixed", 4.876290010415e-13),
                           ("impulse_48k_fixed", 1.412061804705e-12)):
            got = CL.score_sketch(self._golden(name), model)
            assert got == pytest.approx(want, rel=1e-9), "%s = %.12e" % (name, got)

    def test_six_of_the_nine_goldens_are_refused_and_all_six_on_layout(self, model):
        """A fixed point for the refusal counter itself: the shipped fixture set is 2/3 legacy
        `nyquist` layout, so a change that started silently accepting those would show here."""
        refused = {}
        for c in json.load(open(GOLDEN))["cases"]:
            f = SK.unpack(base64.b64decode(c["frame_b64"]))
            try:
                CL.score_sketch(f, model)
            except CL.SketchMismatch:
                refused[c["name"]] = HS.classify_refusal(f, model)
        assert len(refused) == 6
        assert set(refused.values()) == {HS.R_LAYOUT_MISMATCH}

    def test_zeroing_the_weights_changes_them(self, model):
        """The pin above is only a check if a dead model fails it. It does."""
        dead = dict(model, w=[0.0] * len(model["w"]))
        base = CL.score_sketch(self._golden("noise_16k_fixed"), model)
        assert CL.score_sketch(self._golden("noise_16k_fixed"), dead) != pytest.approx(base,
                                                                                       rel=1e-6)

    def test_reversing_the_band_order_changes_them(self, model):
        """band-major vs band-minor is a silent bug: the score stays in [0,1] and looks fine."""
        rev = dict(model, w=list(reversed(model["w"])))
        f = self._golden("noise_16k_fixed")
        assert CL.score_sketch(f, rev) != pytest.approx(CL.score_sketch(f, model), rel=1e-6)

    def test_dropping_ref_db_changes_them(self, model):
        """ref_db is the single largest term this project has measured. Losing it is silent."""
        f = dict(self._golden("noise_16k_fixed"))
        f["ref_db"] = 0.0
        assert CL.score_sketch(f, model) != pytest.approx(
            CL.score_sketch(self._golden("noise_16k_fixed"), model), rel=1e-6)


class TestZIsTheLogitBehindP:
    def test_sigmoid_of_z_reproduces_score_sketch_exactly(self, model, mb):
        """z is published because P saturates and z does not -- a 28.4 dB inter-node offset is
        16.9 logits, invisible once P has printed 0.000000. It must be the SAME z."""
        for seed in range(12):
            rec = one_record(_frame_bytes(seed=seed), sample=str(seed))
            row = score(rec, model, mb)
            assert row["outcome"] == "scored"
            z = row["z"]
            assert 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z)))) == pytest.approx(
                row["p"], rel=1e-12)

    def test_the_terms_sum_to_z_and_level_is_ref_db_times_sum_w(self, model, mb):
        rec = one_record(_frame_bytes(seed=3))
        row = score(rec, model, mb)
        t = row["z_terms"]
        assert t["bias"] + t["level"] + t["shape"] == pytest.approx(row["z"], rel=1e-12)
        assert t["level"] == pytest.approx(
            row["frame"]["ref_db"] * HS.logits_per_db(model), rel=1e-12)


# ----------------------------------------------------------------- refusals

class TestARefusalIsCountedNotDropped:
    def test_a_legacy_layout_pool_refuses_every_row_and_counts_every_one(self, tmp_path):
        rows = quiet_rows(16, layout=SK.LAYOUT_NYQUIST)
        build_pool(tmp_path, rows)
        t = HS.run(str(tmp_path), MODEL_PATH, write=False)
        assert t["scored"] == 0
        assert t["refused"] == 16
        assert t["by_reason"] == {HS.R_LAYOUT_MISMATCH: 16}
        assert t["conservation_ok"]

    def test_a_refused_row_carries_the_reason_and_the_message_verbatim(self, model, mb):
        rec = one_record(_frame_bytes(layout=SK.LAYOUT_NYQUIST))
        row = score(rec, model, mb)
        assert row["outcome"] == "refused"
        assert row["refused_reason"] == HS.R_LAYOUT_MISMATCH
        assert "layout" in row["refused_detail"] and "fixed" in row["refused_detail"]

    def test_refusals_are_broken_out_by_node_and_by_day_partition(self, tmp_path):
        """⚠️A SCALAR HIDES THE BISECT. One real pool is 0% scorable in one day partition and
        100% in the next; one number describes neither. The unanchored partition -- where
        pre-PPS legacy frames collect -- has no derivable day at all, which is why the day comes
        from the directory name and not from ts_utc_s."""
        rows = quiet_rows(4, layout=SK.LAYOUT_NYQUIST)
        rows += [_row(_frame_bytes(layout=SK.LAYOUT_NYQUIST, seed=90 + i), node="other",
                      utc_us=0, sample="u%d" % i) for i in range(3)]
        build_pool(tmp_path, rows)
        t = HS.run(str(tmp_path), MODEL_PATH, write=False)
        buckets = t["by_node_day_reason"]
        assert any(b.startswith("simnode|2023-11-14|") for b in buckets), buckets
        assert any(b.startswith("other|unanchored|") for b in buckets), buckets
        assert sum(buckets.values()) == 7

    @pytest.mark.parametrize("mutate,want", [
        (lambda f, m: (dict(f, q=f["q"][:, :4]), m), HS.R_FRAMES),
        (lambda f, m: (dict(f, q=f["q"][:9]), m), HS.R_BANDS_SHORT),
        (lambda f, m: (dict(f, layout=None), m), HS.R_LAYOUT_UNSTATED),
        (lambda f, m: (dict(f, layout=SK.LAYOUT_NYQUIST), m), HS.R_LAYOUT_MISMATCH),
        (lambda f, m: (dict(f, valid_bands=9), m), HS.R_BANDS_FOR_RATE),
        (lambda f, m: (f, dict(m, w=list(m["w"]) + [0.0])), HS.R_MODEL_WEIGHTS),
    ])
    def test_every_score_sketch_guard_maps_to_its_own_reason(self, model, mutate, want):
        """⚠️SIX RAISE SITES, SIX REASONS, NO LUMPING. A layout refusal means legacy firmware; a
        bands refusal means a rate mismatch; they send an operator to opposite fixes. If
        score_sketch grows a seventh guard this parametrisation stops covering it and
        `classify_refusal` returns R_UNCLASSIFIED rather than a wrong bucket -- the next test
        asserts that fallback exists."""
        base = SK.unpack(_frame_bytes())
        f, m = mutate(base, model)
        with pytest.raises(CL.SketchMismatch):
            CL.score_sketch(f, m)
        assert HS.classify_refusal(f, m) == want
        assert want in HS.REFUSAL_REASONS

    def test_an_unrecognised_refusal_is_still_counted_rather_than_guessed(self, model):
        """A frame every known guard accepts classifies as UNCLASSIFIED, not as the last-checked
        reason. That is the fallback a seventh guard would land in."""
        assert HS.classify_refusal(SK.unpack(_frame_bytes()), model) == HS.R_UNCLASSIFIED

    def test_a_torn_line_is_a_counted_reason_not_a_crash(self, tmp_path):
        """⚠️`Pool.raw()`'s json.loads carries no try/except where `Pool.keys()`'s identical
        parse does, so one bad line aborts a whole scan with a column offset. hear-score walks
        the files itself and books the line."""
        build_pool(tmp_path, quiet_rows(5))
        day = next(iter(HS._partitions(HS.records_dir(str(tmp_path)))))[1]
        with open(day, "a") as fh:
            fh.write('{"key": "truncated", "frame_b6\n')
        t = HS.run(str(tmp_path), MODEL_PATH, write=False)
        assert t["unparseable"] == 1
        assert t["by_reason"][HS.R_LINE_UNPARSEABLE] == 1
        assert t["scored"] == 5
        assert t["conservation_ok"], "a torn line must be accounted for, not lost"

    def test_an_undecodable_frame_is_a_counted_reason(self, model, mb):
        rec = dict(one_record(_frame_bytes()), frame_b64=base64.b64encode(b"\x00" * 5).decode())
        row = score(rec, model, mb)
        assert row["outcome"] == "refused"
        assert row["refused_reason"] == HS.R_FRAME_UNDECODABLE

    def test_the_accounting_invariant_can_actually_break(self, tmp_path, monkeypatch):
        """⚠️`lines_read == scored + refused + unparseable + already_scored` would be a tautology
        if one loop produced both sides. `count_lines` is a SEPARATE pass over the same bytes, so
        making the two disagree flips the flag -- which is what makes it a check and not a
        restatement of the loop."""
        build_pool(tmp_path, quiet_rows(6))
        assert HS.run(str(tmp_path), MODEL_PATH, write=False)["conservation_ok"]
        monkeypatch.setattr(HS, "count_lines", lambda root: 99)
        t = HS.run(str(tmp_path), MODEL_PATH, write=False)
        assert not t["conservation_ok"]


class TestTheFsGuardScoreSketchSkips:
    """⚠️score_sketch's band guard is `if valid is not None and valid < want_b`, and
    `sketch.unpack` sets valid_bands=None whenever the frame states no rate -- so an fs-less
    frame is scored with empty bands padded in as measured silence, which is the exact 3.3-point
    padding failure score_sketch was written to prevent. hear-score closes it."""

    def test_score_sketch_alone_does_not_refuse_an_fs_less_frame(self, model):
        """The hole, demonstrated. If this ever starts raising, the guard below is redundant and
        should be deleted rather than left as decoration."""
        f = dict(SK.unpack(_frame_bytes()), fs_hz=None, valid_bands=None)
        assert 0.0 <= CL.score_sketch(f, model) <= 1.0

    def test_hear_score_refuses_it_and_names_it(self, model, mb):
        rec = one_record(_frame_bytes(state_fs=False))
        row = score(rec, model, mb)
        assert row["outcome"] == "refused"
        assert row["refused_reason"] == HS.R_FS_UNSTATED
        assert row["frame"]["frame_states_fs"] is False

    def test_the_more_specific_cause_wins_the_bucket(self, model, mb):
        """⚠️THE ORDERING BUG THIS TEST EXISTS FOR. On a real 1038-record pool, 955 rows are
        simultaneously legacy-layout AND state no rate in the frame. Checking fs first files all
        955 under `fs_unstated` -- "a producer stopped stating its sample rate" -- when the fix
        is "reflash the nodes still emitting the old bank". Same records, opposite conclusion,
        and the refusal census is this tool's headline output."""
        rec = one_record(_frame_bytes(layout=SK.LAYOUT_NYQUIST, state_fs=False))
        row = score(rec, model, mb)
        assert row["refused_reason"] == HS.R_LAYOUT_MISMATCH
        assert row["refused_reason"] != HS.R_FS_UNSTATED

    def test_hydration_is_off_by_default_and_marked_when_on(self, model, mb):
        """The pool records a rate the frame never stated (fs_stated_by='csv'). Scoring on it is
        a decision, so it is a flag, it is off, and a row scored that way says so."""
        rec = one_record(_frame_bytes(state_fs=False))
        assert score(rec, model, mb)["outcome"] == "refused"
        on = score(rec, model, mb, hydrate_fs=True)
        assert on["outcome"] == "scored"
        assert on["frame"]["fs_hydrated_from_pool"] is True
        assert on["frame"]["frame_states_fs"] is False

    def test_every_row_shape_is_json_serialisable(self, model, mb):
        """⚠️A numpy scalar leaking into a row raises only at WRITE time, i.e. in the cluster and
        not in a --census. `valid_bands` on the hydrate path comes straight from sketch.py."""
        for rec in (one_record(_frame_bytes()),
                    one_record(_frame_bytes(layout=SK.LAYOUT_NYQUIST)),
                    one_record(_frame_bytes(state_fs=False)),
                    dict(one_record(_frame_bytes()), frame_b64="!!not-base64!!")):
            for hyd in (False, True):
                json.dumps(score(rec, model, mb, hydrate_fs=hyd), sort_keys=True)

    def test_a_silent_frame_is_tagged_but_still_scored(self, model, mb):
        """ref_db at the 10*log10(1e-12) floor is an all-zero sketch: a producer defect, not a
        quiet period. It is scorable, so the refusal counter cannot see it -- hence its own tag,
        and hence `silent` is orthogonal to the buckets rather than one of them."""
        q, _ = SK.sketch(np.zeros(4096), 16000.0, layout=SK.LAYOUT_FIXED)
        rec = one_record(SK.pack(1, -120.0, 0, q, fs=16000.0, layout=SK.LAYOUT_FIXED))
        row = score(rec, model, mb)
        assert row["outcome"] == "scored" and row["silent_frame"] is True


# ----------------------------------------------------------------- resume

class TestARestartNeitherRescoresNorSkips:
    def test_a_second_run_emits_nothing_new(self, tmp_path):
        build_pool(tmp_path, quiet_rows(10))
        a = HS.run(str(tmp_path), MODEL_PATH)
        b = HS.run(str(tmp_path), MODEL_PATH)
        assert a["newly_scored"] == 10 and b["newly_scored"] == 0
        assert b["already_scored"] == 10
        assert len(HS.already_scored(str(tmp_path))) == 10

    def test_only_the_new_records_are_scored(self, tmp_path):
        pl = build_pool(tmp_path, quiet_rows(6))
        HS.run(str(tmp_path), MODEL_PATH)
        pl._keys = None
        pl._append([P._record_from_node_row(r) for r in
                    [_row(_frame_bytes(seed=100 + i), utc_us=1_700_000_500_000_000 + i * 10**6,
                          sample="n%d" % i) for i in range(4)]])
        t = HS.run(str(tmp_path), MODEL_PATH)
        assert t["newly_scored"] == 4 and t["already_scored"] == 6
        assert len(HS.already_scored(str(tmp_path))) == 10

    def test_a_backfill_into_an_already_scanned_partition_is_picked_up(self, tmp_path):
        """⚠️WHY THE RESUME IS A KEY SET AND NOT A WATERMARK. A later drain was measured
        appending 513 rows INTO an already-scanned partition file, creating no new partition, so
        every file/offset/partition/timestamp mark loses them permanently. Rows are not even
        time-ordered within one file."""
        build_pool(tmp_path, quiet_rows(5))
        HS.run(str(tmp_path), MODEL_PATH)
        path = next(iter(HS._partitions(HS.records_dir(str(tmp_path)))))[1]
        extra = P._record_from_node_row(
            _row(_frame_bytes(seed=555), utc_us=1_699_999_000_000_000, sample="back"))
        with open(path, "a") as fh:                 # older stamp, same already-read file
            fh.write(json.dumps(extra, sort_keys=True) + "\n")
        t = HS.run(str(tmp_path), MODEL_PATH)
        assert t["newly_scored"] == 1, "a backfilled row inside a scanned file must be scored"

    def test_the_store_holds_one_row_per_pool_key(self, tmp_path):
        build_pool(tmp_path, quiet_rows(7))
        for _ in range(3):
            HS.run(str(tmp_path), MODEL_PATH)
        keys = [json.loads(l)["key"]
                for _, p in HS._partitions(HS.scores_dir(str(tmp_path)))
                for l in open(p) if l.strip()]
        assert len(keys) == len(set(keys)) == 7

    def test_it_never_writes_the_record_store(self, tmp_path):
        """hear-drain owns <pool>/records, it has no locking, and a second writer's key cache
        would silently miss the first writer's rows."""
        build_pool(tmp_path, quiet_rows(4))
        before = {p: open(p).read() for _, p in HS._partitions(HS.records_dir(str(tmp_path)))}
        HS.run(str(tmp_path), MODEL_PATH)
        after = {p: open(p).read() for _, p in HS._partitions(HS.records_dir(str(tmp_path)))}
        assert before == after


# ----------------------------------------------------------------- health

class TestTheHealthCheckGoesUnhealthy:
    """⚠️EVERY GATE HERE IS ASSERTED TO FAIL ON ITS OWN CONDITION. A check that only ever passes
    is the failure this repo has shipped before; the healthy case is asserted last, so a
    check() that returned 1 unconditionally would not survive this class either."""

    def test_no_heartbeat_at_all_fails(self, tmp_path):
        code, lines = HS.check(str(tmp_path))
        assert code == 1 and "never completed a run" in lines[0]

    def test_a_heartbeat_with_no_runs_fails(self, tmp_path):
        HS._write_json_atomic(HS.heartbeat_path(str(tmp_path)), {"runs": []})
        assert HS.check(str(tmp_path))[0] == 1

    def test_an_unreadable_heartbeat_fails(self, tmp_path):
        p = HS.heartbeat_path(str(tmp_path))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("{not json")
        assert HS.check(str(tmp_path))[0] == 1

    def test_an_empty_read_fails_rather_than_passing_vacuously(self, tmp_path):
        """⚠️THE STATE THAT WOULD OTHERWISE REPORT HEALTHY WHILE SCORING NOTHING. Point --pool at
        /pool instead of /pool/corpus: 0 records, 0 refusals, accounting trivially consistent,
        and every growth-conditioned gate passes. tools/hear_drain.py:825 already solved this
        (`if not sensors: return 1`)."""
        HS.run(str(tmp_path), MODEL_PATH)                       # empty pool, writes a heartbeat
        code, lines = HS.check(str(tmp_path))
        assert code == 1
        assert any("EMPTY" in l for l in lines)

    def test_a_stale_scorer_fails_whatever_the_pool_looks_like(self, tmp_path):
        build_pool(tmp_path, quiet_rows(5))
        HS.run(str(tmp_path), MODEL_PATH, now=1000.0)
        assert HS.check(str(tmp_path), max_stale_s=100.0, now=1000.0 + 500)[0] == 1
        assert HS.check(str(tmp_path), max_stale_s=100.0, now=1000.0 + 50)[0] == 0

    def test_a_store_that_grew_with_nothing_emitted_fails(self, tmp_path):
        """The scorer stopped emitting while the drain kept feeding it."""
        HS._write_json_atomic(HS.heartbeat_path(str(tmp_path)), {
            "last_run_s": 1000.0,
            "runs": [{"at": 900.0, "records_seen": 10, "newly_scored": 0, "conservation_ok": True},
                     {"at": 1000.0, "records_seen": 60, "newly_scored": 0,
                      "conservation_ok": True}]})
        code, lines = HS.check(str(tmp_path), now=1000.0)
        assert code == 1 and any("STUCK" in l for l in lines)

    def test_a_torn_pool_line_in_the_window_fails(self, tmp_path):
        HS._write_json_atomic(HS.heartbeat_path(str(tmp_path)), {
            "last_run_s": 1000.0,
            "runs": [{"at": 1000.0, "records_seen": 5, "newly_scored": 5, "unparseable": 2,
                      "conservation_ok": True}]})
        code, lines = HS.check(str(tmp_path), now=1000.0)
        assert code == 1 and any("TORN" in l for l in lines)

    def test_a_broken_accounting_invariant_fails(self, tmp_path):
        HS._write_json_atomic(HS.heartbeat_path(str(tmp_path)), {
            "last_run_s": 1000.0,
            "runs": [{"at": 1000.0, "records_seen": 5, "newly_scored": 5,
                      "conservation_ok": False}]})
        code, lines = HS.check(str(tmp_path), now=1000.0)
        assert code == 1 and any("BROKEN" in l for l in lines)

    def test_a_refusal_reason_appearing_where_it_never_had_fails(self, tmp_path):
        """⚠️AN EVENT, NOT A RATE. valid_bands holds at 15 down to fs 13678 Hz and is 14 below
        it, so a refusal percentage reads 0% until it reads 100%; there is no threshold to tune.
        The first run is exempt because it establishes the census -- on one real pool that census
        is 92% refused and is CORRECT."""
        build_pool(tmp_path, quiet_rows(6))
        HS.run(str(tmp_path), MODEL_PATH, now=1000.0)
        assert HS.check(str(tmp_path), now=1000.0)[0] == 0
        pl = P.Pool(str(tmp_path))
        pl._append([P._record_from_node_row(
            _row(_frame_bytes(layout=SK.LAYOUT_NYQUIST, seed=77), sample="legacy"))])
        HS.run(str(tmp_path), MODEL_PATH, now=1100.0)
        code, lines = HS.check(str(tmp_path), now=1100.0)
        assert code == 1, lines
        assert any("NEW" in l and HS.R_LAYOUT_MISMATCH in l for l in lines)

    def test_the_first_run_is_exempt_from_the_new_bucket_gate(self, tmp_path):
        build_pool(tmp_path, quiet_rows(4, layout=SK.LAYOUT_NYQUIST))
        HS.run(str(tmp_path), MODEL_PATH, now=1000.0)
        code, lines = HS.check(str(tmp_path), now=1000.0)
        assert code == 0, lines
        assert json.load(open(HS.heartbeat_path(str(tmp_path))))["runs"][-1]["first_run"]

    def test_the_check_reads_a_window_of_the_ring_not_only_the_last_run(self, tmp_path):
        """The scorer runs 4x as often as the check, so a last-run read discards 3 of every 4
        measurements -- the trap the drain's reach-back ledger already hit."""
        HS._write_json_atomic(HS.heartbeat_path(str(tmp_path)), {
            "last_run_s": 1000.0,
            "runs": [{"at": 960.0, "records_seen": 5, "newly_scored": 5, "unparseable": 3,
                      "conservation_ok": True},
                     {"at": 1000.0, "records_seen": 5, "newly_scored": 5, "unparseable": 0,
                      "conservation_ok": True}]})
        assert HS.check(str(tmp_path), now=1000.0, window_s=600)[0] == 1
        assert HS.check(str(tmp_path), now=1000.0, window_s=10)[0] == 0

    def test_the_ring_is_bounded(self, tmp_path):
        build_pool(tmp_path, quiet_rows(2))
        for i in range(HS.RUN_RING + 5):
            HS.run(str(tmp_path), MODEL_PATH, now=1000.0 + i)
        assert len(json.load(open(HS.heartbeat_path(str(tmp_path))))["runs"]) == HS.RUN_RING

    def test_a_healthy_pool_passes(self, tmp_path):
        build_pool(tmp_path, quiet_rows(9))
        HS.run(str(tmp_path), MODEL_PATH, now=1000.0)
        code, lines = HS.check(str(tmp_path), now=1000.0)
        assert code == 0, lines


# ----------------------------------------------------------------- what a score says

class TestEveryRowCarriesTheModelAndItsCaveats:
    def test_the_model_identity_is_copied_from_the_file_not_retyped(self, mb):
        """⚠️Two live docstrings in this repo quote AUCs that match no shipped artifact. A row
        that retyped its model's numbers could do the same and nothing would catch it."""
        f = json.load(open(MODEL_PATH))
        for k in ("bands", "frames", "layout", "order", "kind", "min_fs_hz", "n_train",
                  "auc_nested_grouped_cv"):
            assert mb[k] == f[k], k
        assert mb["sha256"] == HS.model_sha256(MODEL_PATH)

    def test_the_auc_never_ships_bare(self, mb):
        """⚠️`auc_nested_grouped_cv` is a 48 kHz figure and the pool is 16 kHz. This repo has
        measured the number for exactly this operation -- 0.9473, hear/corpus.py:285 -- and the
        CV's 3 s grouping is 7.5x narrower than the corpus's 22.4 s decorrelation lag."""
        assert mb["auc_measured_at_fs_hz"] == 48000.0
        assert mb["auc_cross_rate_48k_to_16k"] == 0.9473
        assert mb["auc_is_optimistic"] is True

    def test_the_cross_rate_auc_is_the_one_this_repo_measured(self):
        """Sourced, not invented: it is written in hear/corpus.py's own docstring table."""
        txt = open(os.path.join(ROOT, "hear", "corpus.py")).read()
        assert "fixed bank, common 15 bands       %.4f" % HS.AUC_CROSS_RATE_48K_TO_16K in txt

    def test_a_refused_row_carries_the_model_too(self, model, mb):
        """Which model refused it is as much a fact as which model scored it."""
        row = score(one_record(_frame_bytes(layout=SK.LAYOUT_NYQUIST)), model, mb)
        assert row["outcome"] == "refused"
        assert row["model"]["sha256"] == mb["sha256"]
        assert row["model"]["auc_nested_grouped_cv"] == mb["auc_nested_grouped_cv"]

    def test_p_never_ships_without_its_five_disclaimers(self, model, mb):
        row = score(one_record(_frame_bytes()), model, mb)
        assert row["p"] is not None
        assert row["claim"] == {"is_general_classifier": False, "level_calibrated": False,
                                "comparable_across_nodes": False, "prior_applied": False,
                                "evidence_of_absence": False}

    def test_the_card_carries_the_prior_a_reader_needs_to_correct_p(self):
        """⚠️`prior_applied: false` without the training prevalence is like `level_calibrated:
        false` without ref_db -- a flag with no way to act on it. 43.9% over 100/128."""
        c = HS.model_card(CL.load_model(MODEL_PATH), MODEL_PATH, "x")
        assert c["train_pos"] == 100 and c["train_neg"] == 128
        assert c["train_prevalence"] == pytest.approx(0.4386, abs=5e-4)
        assert c["train_logit_offset"] == pytest.approx(math.log(100 / 128.0), rel=1e-12)
        assert c["evidence_of_absence"] is False
        assert c["node_ref_db_offset_db"] is None
        assert "never seen a single negative" in c["negatives_were"]

    def test_the_card_is_written_once_not_on_every_row(self, tmp_path):
        build_pool(tmp_path, quiet_rows(5))
        HS.run(str(tmp_path), MODEL_PATH)
        assert os.path.exists(os.path.join(HS.scores_dir(str(tmp_path)), "model_card.json"))
        row = json.loads(open(next(p for _, p in
                                   HS._partitions(HS.scores_dir(str(tmp_path))))).readline())
        assert row["model"]["card"] == "model_card.json"

    def test_the_row_carries_what_makes_the_refusal_reproducible(self, model, mb):
        row = score(one_record(_frame_bytes()), model, mb)
        for k in ("fs_hz", "valid_bands", "layout", "ref_db", "bands", "frames"):
            assert row["frame"][k] is not None, k
        for k in ("key", "node", "source", "day", "anchored", "retrigger"):
            assert k in row


class TestTheMqttLaneDegradesToZeroInput:
    """⚠️NOTHING PUBLISHES dama/+/acoustic_sketch TODAY. The phone lane is already wired THROUGH
    this store -- the corpus worker lands JSONL on the PVC and `hear-drain --phone-corpus`
    ingests it into the same pool -- so it degrades to zero input by construction rather than by
    a code path. What matters is that zero is reported as a NUMBER, not as an absence."""

    def test_a_node_only_pool_reports_the_phone_count_as_a_number(self, tmp_path):
        build_pool(tmp_path, quiet_rows(6))
        t = HS.run(str(tmp_path), MODEL_PATH, write=False)
        assert t["by_source"] == {"node": 6}
        assert t["by_source"].get("phone", 0) == 0

    def test_a_phone_row_is_scored_by_the_same_path(self, tmp_path):
        pl = P.Pool(str(tmp_path))
        rec = dict(P._record_from_node_row(_row(_frame_bytes())), source="phone", node="pixel")
        rec["key"] = "phone" + rec["key"][5:]
        pl._append([rec])
        t = HS.run(str(tmp_path), MODEL_PATH, write=False)
        assert t["by_source"] == {"phone": 1} and t["scored"] == 1


# ----------------------------------------------------------------- deploy

class TestTheDeployBundle:
    """⚠️NOTHING IN tests/ CHECKED A CODE ConfigMap AGAINST THE CHECKOUT BEFORE THIS. That is why
    a live hear-drain CronJob was found carrying an argument the checked-in file lacked."""

    @staticmethod
    def _cm(name):
        return yaml.safe_load(open(os.path.join(ROOT, "deploy", "k8s", "%s.yaml" % name)))

    @staticmethod
    def _regen(name):
        out = subprocess.run([sys.executable, os.path.join(ROOT, "deploy", "k8s",
                                                           "gen_configmap.py"), name],
                             capture_output=True, text=True, cwd=ROOT)
        assert out.returncode == 0, out.stderr
        return yaml.safe_load(out.stdout)

    @pytest.mark.parametrize("name", ["hear-drain-code", "hear-score-code"])
    def test_the_checked_in_configmap_matches_the_checkout(self, name):
        """⚠️COMPARES `data`, NEVER THE METADATA BLOCK. The commit annotation is
        `git rev-parse --short HEAD` plus a `-dirty` flag, so it changes on every commit and
        flips to -dirty in any tree with uncommitted work -- i.e. in the exact state a developer
        runs pytest in. A byte-equality test would fail on the first commit that added it."""
        assert self._cm(name)["data"] == self._regen(name)["data"]

    @pytest.mark.parametrize("name", ["hear-drain-code", "hear-score-code"])
    def test_the_commit_annotation_exists_and_is_not_empty(self, name):
        ann = self._cm(name)["metadata"]["annotations"]
        assert ann["dama-hear/commit"]
        assert ann["dama-hear/generated-by"] == "deploy/k8s/gen_configmap.py"

    def test_the_two_bundles_agree_on_the_files_they_share(self):
        """Separate ConfigMaps means duplicated keys, and duplicated keys can drift. This is the
        mitigation the separation is worth having."""
        a, b = self._cm("hear-drain-code")["data"], self._cm("hear-score-code")["data"]
        shared = set(a) & set(b)
        assert shared == {"hear__init__.py", "hear_sketch.py"}
        for k in shared:
            assert a[k] == b[k], k

    def test_every_mounted_subpath_is_a_key_the_configmap_has(self):
        """⚠️NO AST WALK CAN SEE A subPath TYPO. It generates cleanly and dies in the cluster."""
        keys = set(self._cm("hear-score-code")["data"])
        docs = [d for d in yaml.safe_load_all(
            open(os.path.join(ROOT, "deploy", "k8s", "hear-score.yaml"))) if d]
        assert docs, "hear-score.yaml parsed to nothing"
        for d in docs:
            c = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
            sub = {m["subPath"] for m in c["volumeMounts"] if "subPath" in m}
            assert sub == keys, d["metadata"]["name"]

    def test_the_pool_root_is_pinned_on_every_container(self):
        """⚠️`/pool` instead of `/pool/corpus` is a read of an empty directory, and every
        growth-conditioned health gate passes vacuously on one."""
        docs = [d for d in yaml.safe_load_all(
            open(os.path.join(ROOT, "deploy", "k8s", "hear-score.yaml"))) if d]
        for d in docs:
            c = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
            args = "\n".join(c["args"])
            assert "--pool /pool/corpus" in args, d["metadata"]["name"]

    def test_it_mounts_the_hear_pool_pvc_and_declares_no_new_one(self):
        """hear-pool is declared once, in hear-drain.yaml. Two manifests claiming one PVC is how
        they come to disagree about a storage request."""
        docs = [d for d in yaml.safe_load_all(
            open(os.path.join(ROOT, "deploy", "k8s", "hear-score.yaml"))) if d]
        assert not any(d["kind"] == "PersistentVolumeClaim" for d in docs)
        for d in docs:
            vols = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["volumes"]
            names = [v["persistentVolumeClaim"]["claimName"] for v in vols
                     if "persistentVolumeClaim" in v]
            assert names == ["hear-pool"]

    def test_it_asks_for_the_namespace_limitrange_minimum(self):
        """cpu 100m / memory 128Mi is the per-Container minimum; below it the namespace refuses
        the POD, which `kubectl apply --dry-run=server` does not show."""
        docs = [d for d in yaml.safe_load_all(
            open(os.path.join(ROOT, "deploy", "k8s", "hear-score.yaml"))) if d]
        for d in docs:
            r = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["resources"]
            assert r["requests"] == {"cpu": "100m", "memory": "128Mi"}

    def test_the_numpy_guard_asserts_a_version_not_a_directory(self):
        """⚠️/pool/pylib is SHARED with hear-drain. A `[ ! -d "$LIB/numpy" ]` test means whichever
        workload reaches an empty PVC first decides the version and the other silently uses what
        it finds -- both pins then read as discipline while enforcing nothing."""
        docs = [d for d in yaml.safe_load_all(
            open(os.path.join(ROOT, "deploy", "k8s", "hear-score.yaml"))) if d]
        args = "\n".join(docs[0]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                         ["containers"][0]["args"])
        assert 'numpy.__version__=="2.2.6"' in args
        assert '[ ! -d "$LIB/numpy" ]' not in args

    def test_the_check_container_exits_with_the_gate_not_the_census(self, tmp_path):
        """⚠️RUN, NOT GREPPED. A source-scanning guard for `rc=$?` would match the comment that
        explains why rc=$? has to be there. This executes the container's own shell against stub
        binaries and asserts the exit code follows the GATE even when the census that runs after
        it succeeds -- which is what `set -e` would have broken."""
        docs = [d for d in yaml.safe_load_all(
            open(os.path.join(ROOT, "deploy", "k8s", "hear-score.yaml"))) if d]
        chk = next(d for d in docs if d["metadata"]["name"] == "hear-score-check")
        script = "\n".join(chk["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                           ["containers"][0]["args"])
        stub = tmp_path / "bin"
        stub.mkdir()
        (stub / "python").write_text(
            '#!/bin/sh\ncase "$*" in *--check*) exit 3 ;; *) exit 0 ;; esac\n')
        (stub / "python").chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (stub, os.environ["PATH"]))
        r = subprocess.run(["/bin/sh", "-lc", script], env=env, capture_output=True, text=True)
        assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)

    def test_the_check_container_passes_when_the_gate_passes(self, tmp_path):
        docs = [d for d in yaml.safe_load_all(
            open(os.path.join(ROOT, "deploy", "k8s", "hear-score.yaml"))) if d]
        chk = next(d for d in docs if d["metadata"]["name"] == "hear-score-check")
        script = "\n".join(chk["spec"]["jobTemplate"]["spec"]["template"]["spec"]
                           ["containers"][0]["args"])
        stub = tmp_path / "bin"
        stub.mkdir()
        (stub / "python").write_text('#!/bin/sh\nexit 0\n')
        (stub / "python").chmod(0o755)
        env = dict(os.environ, PATH="%s:%s" % (stub, os.environ["PATH"]))
        r = subprocess.run(["/bin/sh", "-lc", script], env=env, capture_output=True, text=True)
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)


class TestTheGeneratorAudit:
    """⚠️THE AUDIT THAT DID NOT AUDIT. The original check() resolved only `hear/<name>.py` from
    three ImportFrom shapes -- no ast.Import branch, nothing outside the hear package, and
    nothing at all for a file a module OPENS. Both gaps are exactly the ones hear-score falls
    into, and both are asserted here by REMOVING a file and requiring generation to fail."""

    @staticmethod
    def _refusal(code, data):
        """`check()` calls sys.exit with the message, so the message is the SystemExit's arg."""
        with pytest.raises(SystemExit) as e:
            GC.check(code, data)
        return str(e.value)

    def test_dropping_the_classifier_fails_generation(self):
        """The gap that had no ast.Import branch and resolved nothing outside `hear`."""
        code = [e for e in GC.SCORE_CODE if e[1] != "modules/supersonic/classify.py"]
        msg = self._refusal(code, GC.SCORE_DATA)
        assert "modules/supersonic/classify.py" in msg and "imports" in msg

    def test_dropping_the_model_fails_generation(self):
        """⚠️AN IMPORT WALK CANNOT SEE A MODEL. classify.FLEET_SKETCH_MODEL is an os.path.join
        against __file__, so the import-only audit shipped a bundle whose pod died on
        FileNotFoundError."""
        data = [e for e in GC.SCORE_DATA if "model_sketch_15" not in e[1]]
        msg = self._refusal(GC.SCORE_CODE, data)
        assert "model_sketch_15.json" in msg and "opens" in msg

    def test_dropping_the_sketch_module_fails_generation(self):
        code = [e for e in GC.SCORE_CODE if e[1] != "hear/sketch.py"]
        assert "hear/sketch.py" in self._refusal(code, GC.SCORE_DATA)

    def test_a_listed_file_that_does_not_exist_fails_generation(self):
        msg = self._refusal(GC.SCORE_CODE + [("ghost.py", "tools/ghost.py")], GC.SCORE_DATA)
        assert "does not exist in the checkout" in msg

    def test_the_intact_bundles_pass_the_audit(self):
        """Asserted last, so an audit that refused everything would not survive this class."""
        for _, (_, code, data) in GC.BUNDLES.items():
            GC.check(code, data)

    def test_an_unknown_bundle_is_refused_rather_than_silently_defaulted(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "deploy", "k8s",
                                                         "gen_configmap.py"), "nope"],
                           capture_output=True, text=True, cwd=ROOT)
        assert r.returncode != 0 and "unknown bundle" in r.stderr

    def test_the_default_argument_still_emits_hear_drain_code(self):
        """deploy/k8s/README.md's no-argument command must keep working unchanged."""
        r = subprocess.run([sys.executable, os.path.join(ROOT, "deploy", "k8s",
                                                         "gen_configmap.py")],
                           capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0
        assert yaml.safe_load(r.stdout)["metadata"]["name"] == "hear-drain-code"

    def test_the_audit_resolves_a_plain_import_statement(self):
        """The branch that did not exist. `import modules.supersonic.classify` was invisible."""
        import ast
        t = ast.parse("import modules.supersonic.classify\nimport hear.sketch\n")
        assert GC._imported_paths(t) >= {"modules/supersonic/classify.py", "hear/sketch.py"}


class TestTheShippedDocsMatchTheShippedModels:
    """⚠️THIS ERROR HAS ALREADY BEEN MADE HERE. A consumer copies its numbers from the table it
    reads first, so the table has to agree with the artifact."""

    @pytest.mark.parametrize("f", ["model_sketch.json", "model_sketch_15.json"])
    def test_the_readme_table_quotes_the_auc_the_model_file_states(self, f):
        m = json.load(open(os.path.join(ROOT, "modules", "supersonic", f)))
        txt = open(os.path.join(ROOT, "modules", "supersonic", "README.md")).read()
        row = next(l for l in txt.splitlines() if l.startswith("| `%s`" % f))
        assert "%.4f" % m["auc_nested_grouped_cv"] in row, row
