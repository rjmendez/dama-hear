"""The classifier that scores what the fleet actually transmits."""
import sys, os
import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                "modules", "supersonic"))
import classify as CL                       # noqa: E402
from hear import sketch as SK               # noqa: E402


def _frame(fs, seed=0, layout=SK.LAYOUT_FIXED, amp=3000.0):
    q, ref = SK.sketch(np.random.default_rng(seed).normal(0, amp, 4096), fs, layout=layout)
    return SK.unpack(SK.pack(1, ref, 900, q, fs=fs, layout=layout))


@pytest.fixture(scope="module")
def m20():
    return CL.load_model(CL.DEFAULT_SKETCH_MODEL)


@pytest.fixture(scope="module")
def m15():
    return CL.load_model(CL.FLEET_SKETCH_MODEL)


class TestShippedModels:
    def test_both_models_declare_what_they_can_be_applied_to(self, m20, m15):
        assert m20["bands"] == 20 and m20["min_fs_hz"] == 32000.0
        assert m15["bands"] == 15 and m15["min_fs_hz"] == 16000.0
        for m in (m20, m15):
            assert m["layout"] == SK.LAYOUT_FIXED
            assert len(m["w"]) == m["bands"] * m["frames"]
            assert m["n_train"] == 228

    def test_the_reported_auc_is_the_nested_one(self, m20):
        """Not the number a C was chosen against. This project has shipped one of those."""
        assert "auc_nested_grouped_cv" in m20
        assert 0.90 < m20["auc_nested_grouped_cv"] < 0.99


class TestScoring:
    def test_it_scores_a_48k_frame_with_either_model(self, m20, m15):
        f = _frame(48000.0, 1)
        for m in (m20, m15):
            p = CL.score_sketch(f, m)
            assert 0.0 <= p <= 1.0

    def test_raw_bytes_and_the_unpacked_dict_agree(self, m20):
        q, ref = SK.sketch(np.random.default_rng(5).normal(0, 3000, 4096), 48000.0,
                           layout=SK.LAYOUT_FIXED)
        raw = SK.pack(1, ref, 900, q, fs=48000.0, layout=SK.LAYOUT_FIXED)
        assert CL.score_sketch(raw, m20) == CL.score_sketch(SK.unpack(raw), m20)

    def test_the_same_shape_louder_scores_higher(self, m20, m15):
        """sum(w) is +0.31, so level raises the score at fixed spectral shape -- amplitude alone
        was worth AUC 0.90 on this corpus and the model has not thrown that away.

        ⚠️NOT tested with a synthetic loud burst. A Gaussian impulse at 12,000 int16 scores
        1.1e-6 while quiet noise scores 2.6e-5: the model is not a level gate, and a white
        impulse does not look like a crack (measured centroid ~7.9 kHz, 1% of energy under
        500 Hz). That is the detector it replaces, working as intended."""
        f = _frame(48000.0, 9)
        for m in (m20, m15):
            lo = CL.score_sketch(dict(f, ref_db=f["ref_db"] - 12.0), m)
            hi = CL.score_sketch(dict(f, ref_db=f["ref_db"] + 12.0), m)
            assert hi > lo


class TestRefusals:
    def test_a_16k_frame_is_refused_by_the_20_band_model(self, m20):
        """⚠️Its top five bands are empty BY CONSTRUCTION. Padding them costs 3.3 points of AUC
        (0.9141 against 0.9473), and nothing in the score would look wrong."""
        with pytest.raises(CL.SketchMismatch, match="15 of this frame's bands"):
            CL.score_sketch(_frame(16000.0, 2), m20)

    def test_the_15_band_model_takes_it(self, m15):
        assert 0.0 <= CL.score_sketch(_frame(16000.0, 2), m15) <= 1.0

    def test_a_legacy_layout_frame_is_refused(self, m20):
        with pytest.raises(CL.SketchMismatch, match="layout"):
            CL.score_sketch(_frame(48000.0, 3, layout=SK.LAYOUT_NYQUIST), m20)

    def test_wrong_time_frame_count_is_refused(self, m20):
        f = _frame(48000.0, 4)
        f["q"] = f["q"][:, :4]
        with pytest.raises(CL.SketchMismatch, match="time frames"):
            CL.score_sketch(f, m20)

    def test_a_model_whose_weights_do_not_match_its_geometry_is_refused(self, m20):
        bad = dict(m20, w=m20["w"][:-1])
        with pytest.raises(CL.SketchMismatch, match="weights"):
            CL.score_sketch(_frame(48000.0, 6), bad)

    def test_a_frame_with_fewer_bands_than_the_model_is_refused(self, m20):
        f = _frame(48000.0, 7)
        f["q"] = f["q"][:10]
        with pytest.raises(CL.SketchMismatch, match="carries 10 bands"):
            CL.score_sketch(f, m20)


class TestTheReferenceIsHalfTheSignal:
    def test_dropping_ref_db_changes_the_score(self, m20):
        """A sketch scored without its reference is missing the strongest single term measured."""
        f = _frame(48000.0, 8)
        g = dict(f, ref_db=0.0)
        assert abs(CL.score_sketch(f, m20) - CL.score_sketch(g, m20)) > 1e-6
