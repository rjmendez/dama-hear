"""Tests for `modules/supersonic/validate_sketch.py`.

No test reaches outside this checkout: the real label corpus lives at
`~/analysis_20260905/label_items.json`, off the repo, and is never read here. Every fixture is
built in `tmp_path` (real WAV bytes through `train_sketch.load`, unmodified) or as plain arrays.
The module's docstring numbers (22.4s lag, 91.7% / 77.9% leakage, the native/lag_merged Jaccard
table) were checked by hand against that external file during development and are not re-checked
by CI; what IS checked here is that the module's own arithmetic is internally correct, using
independent reference implementations where the point is to catch a bug in that arithmetic.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from modules.supersonic import validate_sketch as VS   # noqa: E402
from hear import validate as V                          # noqa: E402


# --------------------------------------------------------------------------- fixtures

def _make_clip(rng, fs=48000, dur_s=0.5, onset_s=0.06, shot=True):
    """A 0.5 s clip with a synthetic onset ~60 ms in -- the shape train_sketch's `SEARCH_S`
    expects. `shot` clips get a louder, higher-frequency burst; not a physical rifle report,
    just a signal the classifier fixtures below can learn to separate."""
    n = int(dur_s * fs)
    x = rng.normal(scale=0.01, size=n).astype(np.float32)
    i0 = int(onset_s * fs)
    burst = int(0.02 * fs)
    amp = 0.8 if shot else 0.3
    freq = 3000.0 if shot else 800.0
    tt = np.arange(burst) / fs
    x[i0:i0 + burst] += (amp * np.sin(2 * np.pi * freq * tt)).astype(np.float32)
    return x, fs


def _wav_uri(x, fs):
    buf = io.BytesIO()
    sf.write(buf, x, fs, format="WAV", subtype="PCM_16")
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _make_fixture(tmp_path, specs, seed=0):
    """`specs`: [(id, utc, shot, label), ...]. Writes an items.json + one label file per event
    in `tmp_path`, in exactly the shape `train_sketch.load` expects. Returns (items_path,
    labels_glob)."""
    rng = np.random.default_rng(seed)
    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    items = []
    for eid, utc, shot, label in specs:
        x, fs = _make_clip(rng, shot=shot)
        items.append({"id": eid, "utc": utc, "uri": _wav_uri(x, fs)})
        (labels_dir / (eid + ".json")).write_text(json.dumps({"id": eid, "label": label}))
    items_path = tmp_path / "label_items.json"
    items_path.write_text(json.dumps(items))
    return str(items_path), str(labels_dir / "*.json")


def _toy_xtg(n_groups=24, per=3, group_gap=50.0, seed=0):
    """`(X, t, g)`: `n_groups` evenly time-spaced groups of `per` events each, `group_s=3.0`
    already honoured by construction (each group's events land inside one 3 s bucket, groups
    themselves `group_gap` seconds apart -- far more than any lag this fixture could measure)."""
    rng = np.random.default_rng(seed)
    n = n_groups * per
    t = np.repeat(np.arange(n_groups) * group_gap, per) + rng.uniform(0, 1.0, size=n)
    g = (t // VS.GROUP_S).astype(np.int64)
    X = rng.normal(size=(n, 4))
    return X, t, g, rng


def _toy_xtg_uneven(n_groups=21, group_gap=50.0, seed=0):
    """Like `_toy_xtg`, but group SIZE varies with `rank % 3` (round-robin part), so the three
    `alarm_reproducibility` parts end up with different total row counts -- `n_a != n_b`, which
    `_toy_xtg`'s constant `per` cannot produce and which `test_d_and_n_over_d_are_recorded`
    needs in order to tell `n_a_over_d` and `n_b_over_d` apart at all."""
    rng = np.random.default_rng(seed)
    sizes = [2 + (i % 3) for i in range(n_groups)]      # part 0 -> 2, part 1 -> 3, part 2 -> 4
    t_list, g_list = [], []
    for gi, size in enumerate(sizes):
        center = gi * group_gap
        t_list.extend((center + rng.uniform(0, 1.0, size=size)).tolist())
        g_list.extend([gi] * size)
    t = np.array(t_list)
    g = np.array(g_list, dtype=np.int64)
    X = rng.normal(size=(t.size, 4))
    return X, t, g, rng


def _reference_leak(t, g, lag_s):
    """A DELIBERATELY separate, brute-force reference for `group_leakage`'s two fractions --
    plain nested loops, not vectorised, so it cannot share a bug with the module's own
    searchsorted-based implementation."""
    t = np.asarray(t)
    g = np.asarray(g)
    n = t.size
    cross = 0
    for i in range(n):
        hit = False
        for j in range(n):
            if j == i:
                continue
            if abs(float(t[j]) - float(t[i])) <= lag_s and g[j] != g[i]:
                hit = True
                break
        if hit:
            cross += 1
    frac_cross = cross / n

    uniq = sorted(set(g.tolist()))
    if len(uniq) >= 2:
        centers = [float(np.mean([t[k] for k in range(n) if g[k] == gg])) for gg in uniq]
        gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        frac_adj = sum(1 for gp in gaps if gp < lag_s) / len(gaps)
    else:
        frac_adj = None
    return frac_cross, frac_adj


def _hash_seed(Xf):
    h = hashlib.sha256(np.ascontiguousarray(Xf).tobytes()).digest()
    return int.from_bytes(h[:8], "little") % (2**32 - 1)


def _noise_fit(Xf, yf):
    """A `FitFn` that trains on NOTHING: the returned model is a random projection whose seed
    depends only on which rows were handed to it. Two independent fits get two independent
    projections, so downstream scores behave like noise uncorrelated between fits -- the low
    arm for `TestSketchAlarmReproducibility`, the supervised-scorer analogue of hugbot's
    pure-noise scorer."""
    return np.random.default_rng(_hash_seed(Xf)).normal(size=Xf.shape[1])


_TRUE_W = np.array([1.0, -1.0, 0.5, 0.0])


def _stable_fit(Xf, yf):
    """A `FitFn` that ignores its rows and always returns the SAME direction -- the high arm:
    two independent fits that happen to learn the identical thing."""
    return _TRUE_W


def _linear_score(model, Xs):
    return Xs @ model


def _const_fit(Xf, yf):
    return None


# --------------------------------------------------------------------------- loading

class TestLoadSketchCorpus:
    def test_utc_lines_up_with_ids_in_whatever_order_load_returns(self, tmp_path):
        specs = [("e0", 1000.0, True, "crack"), ("e1", 1000.5, False, "echo"),
                 ("e2", 2000.0, True, "both"), ("e3", 3000.0, False, "not")]
        items_path, labels_glob = _make_fixture(tmp_path, specs)
        corpus = VS.load_sketch_corpus(items_path, labels_glob)
        want_utc = {"e0": 1000.0, "e1": 1000.5, "e2": 2000.0, "e3": 3000.0}
        want_y = {"e0": 1, "e1": 0, "e2": 1, "e3": 0}
        assert set(corpus.ids) == set(want_utc)
        for eid, tv, yv, gv in zip(corpus.ids, corpus.t, corpus.y, corpus.g):
            assert tv == pytest.approx(want_utc[eid])
            assert int(yv) == want_y[eid]
            assert int(gv) == int(want_utc[eid] // VS.GROUP_S)

    def test_x_t_g_ids_are_all_the_same_length(self, tmp_path):
        specs = [("e%d" % i, 1000.0 + i * 4.0, i % 2 == 0, "crack" if i % 2 == 0 else "not")
                 for i in range(6)]
        items_path, labels_glob = _make_fixture(tmp_path, specs)
        corpus = VS.load_sketch_corpus(items_path, labels_glob)
        n = len(corpus.ids)
        assert corpus.X.shape[0] == n
        assert corpus.y.size == n
        assert corpus.g.size == n
        assert corpus.t.size == n
        assert corpus.X.shape[1] == 160        # 20 bands x 8 frames, the LAYOUT_FIXED default

    def test_unsure_labels_are_excluded_same_as_train_sketch(self, tmp_path):
        specs = [("e0", 1000.0, True, "crack"), ("e1", 1000.2, True, "unsure")]
        items_path, labels_glob = _make_fixture(tmp_path, specs)
        corpus = VS.load_sketch_corpus(items_path, labels_glob)
        assert corpus.ids == ["e0"]

    def test_utc_for_ids_refuses_an_id_load_returned_that_items_does_not_have(self, tmp_path):
        items_path = tmp_path / "items.json"
        items_path.write_text(json.dumps([{"id": "a", "utc": 1.0}]))
        with pytest.raises(V.Refused):
            VS._utc_for_ids(str(items_path), ["a", "b"])

    def test_it_is_reproducible(self, tmp_path):
        specs = [("e0", 1000.0, True, "crack"), ("e1", 1050.0, False, "not")]
        items_path, labels_glob = _make_fixture(tmp_path, specs)
        c1 = VS.load_sketch_corpus(items_path, labels_glob)
        c2 = VS.load_sketch_corpus(items_path, labels_glob)
        assert np.array_equal(c1.X, c2.X)
        assert np.array_equal(c1.t, c2.t)
        assert c1.ids == c2.ids


# --------------------------------------------------------------------------- group_leakage

class TestGroupLeakage:
    def test_fractions_match_an_independent_brute_force_reference(self):
        X, t, g, _ = _toy_xtg(n_groups=16, per=4, group_gap=0.6, seed=2)
        leak = VS.group_leakage(X, t, g)
        ref_cross, ref_adj = _reference_leak(t, g, leak["lag_s"])
        assert leak["frac_events_with_cross_group_neighbor"] == pytest.approx(ref_cross)
        assert leak["frac_adjacent_groups_within_lag"] == pytest.approx(ref_adj)

    def test_sufficient_is_true_when_group_s_covers_the_measured_lag(self):
        X, t, g, _ = _toy_xtg(seed=3)
        leak = VS.group_leakage(X, t, g, group_s=1e9)
        assert leak["sufficient"] is True

    def test_sufficient_is_false_when_group_s_is_tiny(self):
        X, t, g, _ = _toy_xtg(seed=3)
        leak = VS.group_leakage(X, t, g, group_s=1e-6)
        assert leak["sufficient"] is False
        # boundary is exactly lag_s <= group_s, not a fuzzy comparison
        assert leak["lag_s"] > 1e-6

    def test_a_single_group_has_no_adjacent_fraction(self):
        rng = np.random.default_rng(4)
        n = 40
        t = np.linspace(0.0, 20.0, n) + rng.uniform(-0.05, 0.05, size=n)
        g = np.zeros(n, dtype=np.int64)
        X = rng.normal(size=(n, 4))
        leak = VS.group_leakage(X, t, g)
        assert leak["frac_adjacent_groups_within_lag"] is None
        # every event's neighbours are all in the SAME group, so none is a cross-group hit
        assert leak["frac_events_with_cross_group_neighbor"] == 0.0

    def test_it_is_reproducible(self):
        X, t, g, _ = _toy_xtg(seed=5)
        a = VS.group_leakage(X, t, g)
        b = VS.group_leakage(X, t, g)
        assert a["lag_s"] == b["lag_s"]
        assert a["frac_events_with_cross_group_neighbor"] == b["frac_events_with_cross_group_neighbor"]


# --------------------------------------------------------------------------- block schemes

class TestNativeGroupBlocks:
    def test_blocks_equal_the_groups_exactly(self):
        X, t, g, _ = _toy_xtg(seed=6)
        b = VS.native_group_blocks(t, g, lag_s=5.0)
        assert np.array_equal(b.block, g.astype(np.int64))
        assert b.n_blocks == np.unique(g).size

    def test_row_accounting_is_trivial(self):
        X, t, g, _ = _toy_xtg(n_groups=10, per=2, seed=7)
        b = VS.native_group_blocks(t, g, lag_s=5.0)
        assert b.n_in == b.row.size == t.size
        assert (b.dropped_unanchored, b.dropped_short_block, b.dropped_guard) == (0, 0, 0)


class TestLagMergedBlocks:
    def test_close_groups_merge_far_ones_do_not(self):
        t = np.array([0.0, 0.1, 2.0, 2.1, 100.0, 100.1])
        g = np.array([0, 0, 1, 1, 2, 2])
        b = VS.lag_merged_blocks(t, g, lag_s=5.0)
        assert b.n_blocks == 2
        assert b.block[0] == b.block[2]     # group 0 and group 1 (gap 2.0s < 5s) merge
        assert b.block[0] != b.block[4]     # group 2 (gap ~98s) stays separate

    def test_a_smaller_lag_keeps_all_three_groups_separate(self):
        t = np.array([0.0, 0.1, 2.0, 2.1, 100.0, 100.1])
        g = np.array([0, 0, 1, 1, 2, 2])
        b = VS.lag_merged_blocks(t, g, lag_s=1.0)
        assert b.n_blocks == 3

    def test_merging_is_independent_of_input_order(self):
        t_ordered = np.array([0.0, 0.1, 2.0, 2.1, 100.0, 100.1])
        g_ordered = np.array([0, 0, 1, 1, 2, 2])
        b1 = VS.lag_merged_blocks(t_ordered, g_ordered, lag_s=5.0)

        t_shuffled = np.array([100.0, 100.1, 0.0, 0.1, 2.0, 2.1])
        g_shuffled = np.array([2, 2, 0, 0, 1, 1])
        b2 = VS.lag_merged_blocks(t_shuffled, g_shuffled, lag_s=5.0)

        assert b2.n_blocks == 2
        assert b2.block[2] == b2.block[4]   # (g=0, t=0.0) and (g=1, t=2.0) merged
        assert b2.block[0] != b2.block[2]   # (g=2, t=100.0) is not
        assert b1.n_blocks == b2.n_blocks

    def test_row_accounting_is_trivial(self):
        X, t, g, _ = _toy_xtg(n_groups=10, per=2, seed=8)
        b = VS.lag_merged_blocks(t, g, lag_s=5.0)
        assert b.n_in == b.row.size == t.size
        assert (b.dropped_unanchored, b.dropped_short_block, b.dropped_guard) == (0, 0, 0)

    def test_merged_blocks_never_outnumber_native_ones(self):
        X, t, g, _ = _toy_xtg(n_groups=20, per=3, group_gap=0.5, seed=9)
        native = VS.native_group_blocks(t, g, lag_s=5.0)
        merged = VS.lag_merged_blocks(t, g, lag_s=5.0)
        assert merged.n_blocks <= native.n_blocks


# --------------------------------------------------------------------------- fit / score

class TestSketchFitScore:
    def test_fit_refuses_rows_with_no_labels(self):
        fit = VS._sketch_fit(C=1.0)
        with pytest.raises(V.Refused):
            fit(np.zeros((5, 3)), None)

    def test_fit_refuses_a_single_class(self):
        fit = VS._sketch_fit(C=1.0)
        X = np.random.default_rng(0).normal(size=(10, 3))
        with pytest.raises(V.Refused):
            fit(X, np.zeros(10))

    def test_it_fits_and_scores_a_separable_toy_sensibly(self):
        rng = np.random.default_rng(11)
        n, d = 40, 5
        y = (rng.random(n) < 0.5).astype(int)
        X = rng.normal(size=(n, d))
        X[y == 1] += 3.0
        model = VS._sketch_fit(C=3.0)(X, y)
        s = VS._sketch_score(model, X)
        assert s.shape == (n,)
        assert s[y == 1].mean() > s[y == 0].mean()

    def test_it_is_reproducible(self):
        rng = np.random.default_rng(12)
        X = rng.normal(size=(30, 4))
        y = (rng.random(30) < 0.5).astype(int)
        m1 = VS._sketch_fit(C=2.0)(X, y)
        m2 = VS._sketch_fit(C=2.0)(X, y)
        assert np.array_equal(VS._sketch_score(m1, X), VS._sketch_score(m2, X))


# --------------------------------------------------------------------------- alarm reproducibility

class TestSketchAlarmReproducibility:
    def test_a_noise_fit_agrees_with_itself_far_less_than_a_stable_one(self):
        """The low/high arms. Two fits that learn nothing in common (`_noise_fit`) versus two
        fits that happen to learn the identical thing (`_stable_fit`), scored the same way, on
        the same blocks. Measured on this fixture at pctl=75: noise jaccard 0.154, stable
        jaccard 0.7 -- the gap this whole module exists to detect, reproduced with a scorer
        whose ground truth is known by construction rather than fitted."""
        X, t, g, rng = _toy_xtg(n_groups=24, per=3, group_gap=50.0, seed=7)
        y = (rng.random(X.shape[0]) < 0.4).astype(int)
        blocks = VS.native_group_blocks(t, g, lag_s=1.0)

        noise = VS.sketch_alarm_reproducibility(X, y, blocks, fit=_noise_fit,
                                                 score=_linear_score, pctl=75.0,
                                                 parts=3, min_blocks_part=4)
        stable = VS.sketch_alarm_reproducibility(X, y, blocks, fit=_stable_fit,
                                                  score=_linear_score, pctl=75.0,
                                                  parts=3, min_blocks_part=4)
        assert noise["jaccard"] < 0.3
        assert stable["jaccard"] > 0.5
        assert stable["jaccard"] > noise["jaccard"]

    def test_it_is_reproducible(self):
        X, t, g, rng = _toy_xtg(n_groups=24, per=3, group_gap=50.0, seed=7)
        y = (rng.random(X.shape[0]) < 0.4).astype(int)
        blocks = VS.native_group_blocks(t, g, lag_s=1.0)
        a = VS.sketch_alarm_reproducibility(X, y, blocks, fit=_noise_fit, score=_linear_score,
                                            pctl=75.0, parts=3, min_blocks_part=4)
        b = VS.sketch_alarm_reproducibility(X, y, blocks, fit=_noise_fit, score=_linear_score,
                                            pctl=75.0, parts=3, min_blocks_part=4)
        assert a["jaccard"] == b["jaccard"]
        assert a["rate_a"] == b["rate_a"]

    def test_a_scorer_that_never_alarms_is_refused_not_scored_a_perfect_one(self):
        X, t, g, rng = _toy_xtg(n_groups=20, per=3, group_gap=50.0, seed=3)
        y = (rng.random(X.shape[0]) < 0.3).astype(int)
        blocks = VS.native_group_blocks(t, g, lag_s=1.0)

        def zero_score(model, Xs):
            return np.zeros(Xs.shape[0])

        with pytest.raises(V.Refused):
            VS.sketch_alarm_reproducibility(X, y, blocks, fit=_const_fit, score=zero_score,
                                            pctl=99.9, parts=3, min_blocks_part=4)

    def test_pctl_defaults_to_100_minus_prevalence_percent(self):
        X, t, g, rng = _toy_xtg(n_groups=20, per=3, group_gap=50.0, seed=3)
        y = (rng.random(X.shape[0]) < 0.3).astype(int)
        blocks = VS.native_group_blocks(t, g, lag_s=1.0)
        rep = VS.sketch_alarm_reproducibility(X, y, blocks, fit=_const_fit,
                                              score=lambda m, Xs: Xs[:, 0],
                                              parts=3, min_blocks_part=4)
        assert rep["pctl"] == pytest.approx(100.0 * (1.0 - float(y.mean())))

    def test_an_explicit_pctl_is_not_overridden_by_prevalence(self):
        X, t, g, rng = _toy_xtg(n_groups=20, per=3, group_gap=50.0, seed=3)
        y = (rng.random(X.shape[0]) < 0.3).astype(int)
        blocks = VS.native_group_blocks(t, g, lag_s=1.0)
        rep = VS.sketch_alarm_reproducibility(X, y, blocks, fit=_const_fit,
                                              score=lambda m, Xs: Xs[:, 0], pctl=50.0,
                                              parts=3, min_blocks_part=4)
        assert rep["pctl"] == 50.0

    def test_prevalence_of_0_or_1_is_refused_when_pctl_is_not_given(self):
        X, t, g, rng = _toy_xtg(n_groups=20, per=3, group_gap=50.0, seed=3)
        y = np.zeros(X.shape[0])
        blocks = VS.native_group_blocks(t, g, lag_s=1.0)
        with pytest.raises(V.Refused):
            VS.sketch_alarm_reproducibility(X, y, blocks, fit=_const_fit,
                                            score=lambda m, Xs: Xs[:, 0],
                                            parts=3, min_blocks_part=4)

    def test_d_and_n_over_d_are_recorded(self):
        # uneven groups so n_a != n_b: a test built on _toy_xtg's constant `per` cannot tell
        # n_a_over_d from n_b_over_d apart, since both would be numerically identical either way.
        X, t, g, rng = _toy_xtg_uneven(n_groups=21, group_gap=50.0, seed=3)
        y = (rng.random(X.shape[0]) < 0.3).astype(int)
        blocks = VS.native_group_blocks(t, g, lag_s=1.0)
        rep = VS.sketch_alarm_reproducibility(X, y, blocks, fit=_const_fit,
                                              score=lambda m, Xs: Xs[:, 0],
                                              parts=3, min_blocks_part=4)
        assert rep["n_a"] != rep["n_b"]
        assert rep["d"] == X.shape[1]
        assert rep["n_a_over_d"] == pytest.approx(rep["n_a"] / rep["d"])
        assert rep["n_b_over_d"] == pytest.approx(rep["n_b"] / rep["d"])

    def test_the_real_classifier_wires_up_end_to_end_on_a_synthetic_corpus(self):
        """No stubs: the default `_sketch_fit`/`_sketch_score` (real sklearn LogisticRegression,
        `C=DEFAULT_C`), against a synthetic corpus built the same way `main()`'s fallback
        builds one. Not a claim about the real corpus's numbers -- only that the wiring
        (native blocks -> supervised fit -> Jaccard) runs to completion and returns the shape
        `format_report` expects."""
        corpus = VS.synthetic_corpus(n_events=180, n_groups=60, d=20, seed=5)
        leak = VS.group_leakage(corpus.X, corpus.t, corpus.g)
        blocks = VS.native_group_blocks(corpus.t, corpus.g, lag_s=leak["lag_s"])
        rep = VS.sketch_alarm_reproducibility(corpus.X, corpus.y, blocks, C=3.0,
                                              parts=3, min_blocks_part=4)
        assert 0.0 <= rep["jaccard"] <= 1.0
        assert rep["n_a"] > 0 and rep["n_b"] > 0
        assert rep["d"] == 20


# --------------------------------------------------------------------------- format_report

def _leak(sufficient, lag_s=22.4, group_s=3.0, frac_cross=0.9, frac_adj=0.8, censored=False,
          lag045=22.4, lag055=15.8):
    return {"lag_s": lag_s, "censored": censored, "group_s": group_s, "sufficient": sufficient,
            "frac_events_with_cross_group_neighbor": frac_cross,
            "frac_adjacent_groups_within_lag": frac_adj, "n_events": 228, "n_groups": 69,
            "lag_at_rho_045": lag045, "lag_at_rho_055": lag055,
            "lag_swing_x": max(lag045, lag055) / min(lag045, lag055), "n_pairs_at_lag": 10}


def _rep(jaccard, rate_a, rate_b, designed, n_a, n_b, d, blocks_a=5, blocks_b=5,
        blocks_holdout=5, pearson=0.5, separated=True, gap=30.0, lag_s=22.4):
    ra, rb = rate_a, rate_b
    chance = (ra * rb) / (ra + rb - ra * rb) if (ra + rb - ra * rb) > 0 else None
    return {"jaccard": jaccard, "rate_a": rate_a, "rate_b": rate_b, "designed": designed,
            "n_a": n_a, "n_b": n_b, "d": d, "n_a_over_d": n_a / d, "n_b_over_d": n_b / d,
            "blocks_a": blocks_a, "blocks_b": blocks_b, "blocks_holdout": blocks_holdout,
            "pearson": pearson, "jaccard_chance": chance,
            "jaccard_excess": (None if chance is None else jaccard - chance),
            "separated": separated, "min_cross_block_gap_s": gap, "lag_s": lag_s}


class TestFormatReport:
    def test_warn_on_insufficient_grouping_absent_when_sufficient(self):
        report_bad = VS.format_report(_leak(sufficient=False), {})
        report_good = VS.format_report(_leak(sufficient=True), {})
        assert "WARN" in report_bad and "SHORTER than the measured lag" in report_bad
        assert "WARN" not in report_good

    def test_warn_on_underdetermined_fit(self):
        under = VS.format_report(_leak(True), {"native": _rep(0.8, 0.4, 0.4, 0.4, 5, 5, 160)})
        over = VS.format_report(_leak(True), {"native": _rep(0.8, 0.4, 0.4, 0.4, 200, 200, 160)})
        assert "underdetermined" in under
        assert "underdetermined" not in over

    def test_warn_on_large_design_deviation(self):
        bad = VS.format_report(_leak(True), {"m": _rep(0.8, 0.05, 0.07, 0.44, 200, 200, 20)})
        good = VS.format_report(_leak(True), {"m": _rep(0.8, 0.42, 0.46, 0.44, 200, 200, 20)})
        assert "does not generalise" in bad
        assert "does not generalise" not in good

    def test_warn_on_low_jaccard(self):
        low = VS.format_report(_leak(True), {"m": _rep(0.1, 0.4, 0.4, 0.4, 200, 200, 20)})
        high = VS.format_report(_leak(True), {"m": _rep(0.8, 0.4, 0.4, 0.4, 200, 200, 20)})
        assert "mostly decided by which" in low
        assert "mostly decided by which" not in high

    def test_a_clean_report_carries_no_warn(self):
        clean = VS.format_report(_leak(sufficient=True),
                                 {"m": _rep(0.9, 0.41, 0.43, 0.4, 300, 300, 20)})
        assert "WARN" not in clean

    def test_pearson_none_is_printed_as_n_a(self):
        rep = _rep(0.8, 0.4, 0.4, 0.4, 200, 200, 20)
        rep["pearson"] = None
        report = VS.format_report(_leak(True), {"m": rep})
        assert "pearson n/a" in report


# --------------------------------------------------------------------------- synthetic fallback

class TestSyntheticCorpus:
    def test_it_is_reproducible_given_a_seed(self):
        c1 = VS.synthetic_corpus(n_events=100, n_groups=30, d=8, seed=42)
        c2 = VS.synthetic_corpus(n_events=100, n_groups=30, d=8, seed=42)
        assert np.array_equal(c1.X, c2.X)
        assert np.array_equal(c1.t, c2.t)
        assert np.array_equal(c1.y, c2.y)

    def test_a_different_seed_gives_a_different_corpus(self):
        c1 = VS.synthetic_corpus(n_events=100, n_groups=30, d=8, seed=1)
        c2 = VS.synthetic_corpus(n_events=100, n_groups=30, d=8, seed=2)
        assert not np.array_equal(c1.X, c2.X)

    def test_shape_matches_the_request(self):
        c = VS.synthetic_corpus(n_events=150, n_groups=50, d=12, seed=0)
        assert c.X.shape == (150, 12)
        assert c.y.size == c.t.size == c.g.size == 150

    def test_group_leakage_runs_to_completion_on_it(self):
        """The machinery stays exercisable even with no real corpus on the box -- this is what
        `main()`'s fallback buys."""
        c = VS.synthetic_corpus(n_events=120, n_groups=40, d=16, seed=9)
        leak = VS.group_leakage(c.X, c.t, c.g)
        assert leak["lag_s"] > 0.0
        assert 0.0 <= leak["frac_events_with_cross_group_neighbor"] <= 1.0


# --------------------------------------------------------------------------- the naive scheme

class TestTheNativeSchemeIsLabelledNotSmuggled:
    """`alarm_reproducibility` re-checked nothing about the `Blocks` it was handed, so `native`
    shipped `block_s=3.0` against `lag_s=22.4` with `guard_s=0.0` -- the exact split
    `hear.validate.make_blocks` refuses by name, with the guard band its own docstring calls the
    thing that makes interleaving legitimate set to zero -- and was scored without objection."""

    def _corpus(self, seed=21, n_groups=24, per=4, group_gap=50.0):
        X, t, g, _ = _toy_xtg(n_groups=n_groups, per=per, group_gap=group_gap, seed=seed)
        y = (np.arange(t.size) % 2).astype(int)
        return X, y, t, g

    def test_the_naive_split_is_refused_by_default(self):
        X, y, t, g = self._corpus()
        b = VS.native_group_blocks(t, g, lag_s=60.0)
        with pytest.raises(V.Refused, match="blocks are not separated"):
            VS.sketch_alarm_reproducibility(X, y, b, fit=_stable_fit, score=_linear_score,
                                            pctl=50.0)

    def test_it_is_scored_only_under_the_explicit_escape(self):
        X, y, t, g = self._corpus()
        b = VS.native_group_blocks(t, g, lag_s=60.0)
        rep = VS.sketch_alarm_reproducibility(X, y, b, fit=_stable_fit, score=_linear_score,
                                              pctl=50.0, allow_unseparated=True)
        assert rep["separated"] is False
        assert "UNSEPARATED" in VS.format_report(_leak(False), {"native": rep})

    def test_the_merged_scheme_needs_no_escape(self):
        # The arm: the honest scheme clears the same check without asking for an exemption.
        X, y, t, g = self._corpus()
        b = VS.lag_merged_blocks(t, g, lag_s=1.0)
        rep = VS.sketch_alarm_reproducibility(X, y, b, fit=_stable_fit, score=_linear_score,
                                              pctl=50.0)
        assert rep["separated"] is True
        assert "UNSEPARATED" not in VS.format_report(_leak(True), {"lag_merged": rep})


class TestMergingUsesTheEventGapNotTheGroupCentres:
    """The merge condition tested group CENTRES while the leakage is between EVENTS, and an event
    sits up to `group_s`/2 off its bucket's centre. Two blocks kept apart on a centre gap just
    over the lag could have their nearest events up to 3 s closer than it -- the guarantee was
    asserted, not enforced. It happened to hold on the real corpus, with 2.39 s of margin."""

    def test_the_constructed_counterexample_now_merges(self):
        # Two buckets whose centres are 26.3 s apart (> a 22.4 s lag) but whose nearest events
        # are 21.2 s apart (< it). The centre rule kept them separate; the event rule does not.
        t = np.array([1190.0, 1195.0, 1205.87, 1216.4, 1226.5, 1237.0])
        g = np.array([0, 0, 0, 1, 1, 1])
        assert abs(np.mean(t[g == 1]) - np.mean(t[g == 0])) > 22.4      # centres: far apart
        assert (t[g == 1].min() - t[g == 0].max()) < 22.4               # events: not
        b = VS.lag_merged_blocks(t, g, lag_s=22.4)
        assert b.n_blocks == 1

    def test_a_genuinely_separated_pair_still_stays_apart(self):
        t = np.array([0.0, 1.0, 2.0, 100.0, 101.0, 102.0])
        g = np.array([0, 0, 0, 1, 1, 1])
        b = VS.lag_merged_blocks(t, g, lag_s=22.4)
        assert b.n_blocks == 2
        assert b.min_cross_block_gap_s >= 22.4

    def test_every_kept_boundary_clears_the_lag_by_construction(self):
        # Two pairs of close groups and one far one: the merge has to happen twice and stop
        # twice, or this asserts nothing.
        t = np.array([0.0, 1.0, 5.0, 6.0, 40.0, 41.0, 45.0, 46.0, 100.0, 101.0])
        g = np.array([0, 0, 1, 1, 2, 2, 3, 3, 4, 4])
        b = VS.lag_merged_blocks(t, g, lag_s=10.0)
        assert b.n_blocks == 3
        assert b.min_cross_block_gap_s > 10.0


class TestTheLagSensitivityIsPublished:
    def test_the_bracket_and_the_swing_are_reported(self):
        X, t, g, _ = _toy_xtg(n_groups=20, per=4, seed=33)
        leak = VS.group_leakage(X, t, g)
        assert leak["lag_at_rho_045"] >= leak["lag_at_rho_055"]
        assert leak["lag_swing_x"] >= 1.0
        assert leak["n_pairs_at_lag"] is None or leak["n_pairs_at_lag"] >= V.MIN_PAIRS_PER_LAG

    def test_the_report_prints_it(self):
        X, t, g, _ = _toy_xtg(n_groups=20, per=4, seed=33)
        out = VS.format_report(VS.group_leakage(X, t, g), {})
        assert "lag at rho 0.45 / 0.55" in out and "swing" in out


class TestCensoringIsCarriedNotDeclared:
    def test_both_schemes_carry_the_flag_they_are_given(self):
        # Both builders hard-coded `lag_censored=False` -- a field asserting a measurement they
        # never made. A block length resting on a search bound rather than on a crossing has to
        # say so wherever it travels.
        X, t, g, _ = _toy_xtg(n_groups=12, per=3, seed=35)
        assert VS.native_group_blocks(t, g, lag_s=5.0, lag_censored=True).lag_censored is True
        assert VS.lag_merged_blocks(t, g, lag_s=5.0, lag_censored=True).lag_censored is True

    def test_the_default_is_still_false(self):
        X, t, g, _ = _toy_xtg(n_groups=12, per=3, seed=35)
        assert VS.native_group_blocks(t, g, lag_s=5.0).lag_censored is False
        assert VS.lag_merged_blocks(t, g, lag_s=5.0).lag_censored is False


class TestChanceCorrectedAgreement:
    def test_the_report_carries_the_chance_baseline_beside_the_jaccard(self):
        out = VS.format_report(_leak(True), {"m": _rep(0.62, 0.573, 0.354, 0.439, 75, 71, 160)})
        assert "chance jaccard at those rates 0.280" in out

    def test_agreement_that_is_only_chance_is_called_out(self):
        # Two flaggers at 57.3% and 35.4% overlap at 0.280 by chance; a Jaccard of 0.30 on those
        # rates is not agreement, and the raw number alone reads as though it were.
        bad = VS.format_report(_leak(True), {"m": _rep(0.30, 0.573, 0.354, 0.439, 75, 71, 160)})
        good = VS.format_report(_leak(True), {"m": _rep(0.62, 0.573, 0.354, 0.439, 75, 71, 160)})
        assert "only" in bad and "not agreement" in bad
        assert "not agreement" not in good
