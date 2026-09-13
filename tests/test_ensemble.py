"""Multi-node spatial consensus fusion: `hear.ensemble`, tested against known truth.

Node names throughout are the array's own -- `nyquist`, `mach`, `rankine`, `gold`, `ageev`,
`kasami` -- but nothing here depends on which six strings they are; the module is generic over
node identity. Scores are synthetic sigmoid-shaped probabilities in the tag store's own scale
(`tools/hear_tag.py`'s `scores` dict), not raw logits.
"""
import pytest

from hear import ensemble as ENS

C_20C = 331.3 + 0.606 * 20.0  # hear.solve.shockwave.sound_speed(20.0), restated to avoid a
                              # circular dependency on the module under test for the expected value


def obs(node, t, scores, snr_db=None):
    return ENS.NodeObservation(node=node, t_utc_s=t, scores=dict(scores), snr_db=snr_db)


class TestSigmoidLogit:
    def test_round_trips(self):
        for p in (0.001, 0.1, 0.5, 0.9, 0.999):
            assert ENS.sigmoid(ENS.logit(p)) == pytest.approx(p, abs=1e-6)

    def test_logit_of_half_is_zero(self):
        assert ENS.logit(0.5) == pytest.approx(0.0, abs=1e-9)

    def test_sigmoid_never_overflows(self):
        assert ENS.sigmoid(1e6) == pytest.approx(1.0)
        assert ENS.sigmoid(-1e6) == pytest.approx(0.0)


class TestTransitWindow:
    def test_scales_with_diameter(self):
        small = ENS.transit_window_s(10.0, temp_c=20.0, margin_s=0.0)
        large = ENS.transit_window_s(300.0, temp_c=20.0, margin_s=0.0)
        assert large > small
        assert small == pytest.approx(10.0 / C_20C, rel=1e-6)

    def test_margin_is_additive(self):
        base = ENS.transit_window_s(30.0, temp_c=20.0, margin_s=0.0)
        margined = ENS.transit_window_s(30.0, temp_c=20.0, margin_s=0.05)
        assert margined == pytest.approx(base + 0.05, rel=1e-9)


class TestSnrWeight:
    def test_monotonic_in_snr(self):
        assert ENS.snr_weight(20.0) > ENS.snr_weight(6.0) > ENS.snr_weight(-20.0)

    def test_none_is_floored(self):
        assert ENS.snr_weight(None) == ENS.snr_weight(ENS.SNR_FLOOR_DB)

    def test_very_low_snr_is_floored_not_negative_or_zero(self):
        assert ENS.snr_weight(-1000.0) > 0.0
        assert ENS.snr_weight(-1000.0) == ENS.snr_weight(ENS.SNR_FLOOR_DB)


class TestTwoNodeConsensus:
    """Two nodes hearing the same event within the transit window."""

    def test_two_nodes_agreeing_are_fused_and_corroborated(self):
        window = ENS.transit_window_s(12.0, margin_s=0.03)
        observations = [
            obs("nyquist", 100.000, {"Dog": 0.9}, snr_db=20.0),
            obs("mach", 100.000 + window * 0.3, {"Dog": 0.85}, snr_db=18.0),
        ]
        out = ENS.fuse_predictions(observations, diameter_m=12.0)
        assert len(out) == 1
        c = out[0]
        assert c.class_name == "Dog"
        assert c.n_nodes == 2
        assert set(c.nodes) == {"nyquist", "mach"}
        assert c.corroborated is True
        assert c.coincidence_bonus_db > 0.0
        # Two nodes each near-certain and agreeing must fuse to MORE confident than either alone.
        assert c.fused_score > max(0.9, 0.85)
        assert c.mean_snr_db == pytest.approx(19.0)

    def test_outside_the_window_does_not_fuse(self):
        window = ENS.transit_window_s(12.0, margin_s=0.03)
        observations = [
            obs("nyquist", 100.0, {"Dog": 0.9}, snr_db=20.0),
            obs("mach", 100.0 + window * 5.0, {"Dog": 0.9}, snr_db=20.0),
        ]
        out = ENS.fuse_predictions(observations, diameter_m=12.0)
        assert len(out) == 2
        assert all(c.n_nodes == 1 for c in out)
        assert all(c.corroborated is False for c in out)

    def test_explicit_window_overrides_computed_one(self):
        observations = [
            obs("nyquist", 100.0, {"Dog": 0.9}),
            obs("mach", 100.5, {"Dog": 0.9}),
        ]
        wide = ENS.fuse_predictions(observations, window_s=1.0)
        narrow = ENS.fuse_predictions(observations, window_s=0.1)
        assert wide[0].n_nodes == 2
        assert len(narrow) == 2 and all(c.n_nodes == 1 for c in narrow)


class TestThreeNodeConsensus:
    def test_three_agreeing_nodes_outscore_two(self):
        window = ENS.transit_window_s(15.0, margin_s=0.03)
        two = [
            obs("nyquist", 100.0, {"Owl": 0.8}, snr_db=15.0),
            obs("mach", 100.0 + window * 0.2, {"Owl": 0.8}, snr_db=15.0),
        ]
        three = two + [obs("rankine", 100.0 + window * 0.4, {"Owl": 0.8}, snr_db=15.0)]
        fused_two = ENS.fuse_predictions(two, diameter_m=15.0)[0]
        fused_three = ENS.fuse_predictions(three, diameter_m=15.0)[0]
        assert fused_three.n_nodes == 3
        assert fused_three.fused_score > fused_two.fused_score
        assert fused_three.coincidence_bonus_db > fused_two.coincidence_bonus_db

    def test_mixed_classes_split_into_separate_groups(self):
        observations = [
            obs("nyquist", 100.0, {"Dog": 0.9}, snr_db=20.0),
            obs("mach", 100.01, {"Owl": 0.9}, snr_db=20.0),
            obs("rankine", 100.02, {"Dog": 0.85}, snr_db=20.0),
        ]
        out = ENS.fuse_predictions(observations, window_s=1.0)
        by_class = {c.class_name: c for c in out}
        assert by_class["Dog"].n_nodes == 2
        assert by_class["Owl"].n_nodes == 1
        assert by_class["Owl"].corroborated is False


class TestSixNodeConsensus:
    """The full array: nyquist, mach, rankine, gold, ageev, kasami."""

    NODES = ("nyquist", "mach", "rankine", "gold", "ageev", "kasami")

    def test_all_six_agree(self):
        window = ENS.transit_window_s(40.0, margin_s=0.03)
        observations = [
            obs(name, 200.0 + i * (window / 6.0), {"Vehicle": 0.7 + 0.02 * i}, snr_db=12.0 + i)
            for i, name in enumerate(self.NODES)
        ]
        out = ENS.fuse_predictions(observations, diameter_m=40.0)
        assert len(out) == 1
        c = out[0]
        assert c.n_nodes == 6
        assert set(c.nodes) == set(self.NODES)
        assert c.corroborated is True
        assert c.agreement_count == 6
        # Bonus is capped even with many agreeing nodes.
        assert c.coincidence_bonus_db == pytest.approx(ENS.MAX_COINCIDENCE_BONUS_DB)
        assert c.fused_score > 0.9

    def test_one_node_disagrees_below_agree_floor_still_pools_but_does_not_bonus_for_it(self):
        window = ENS.transit_window_s(40.0, margin_s=0.03)
        observations = [
            obs(name, 200.0 + i * (window / 6.0), {"Vehicle": 0.85}, snr_db=15.0)
            for i, name in enumerate(self.NODES[:5])
        ]
        observations.append(obs(self.NODES[5], 200.0 + 5 * (window / 6.0), {"Vehicle": 0.1},
                                snr_db=15.0))
        out = ENS.fuse_predictions(observations, diameter_m=40.0)
        assert len(out) == 1
        c = out[0]
        assert c.n_nodes == 6
        assert c.agreement_count == 5
        assert c.coincidence_bonus_db == pytest.approx(
            ENS.coincidence_bonus_db(5))


class TestSnrWeightedPooling:
    def test_high_snr_node_dominates_the_fused_score(self):
        quiet = obs("nyquist", 100.0, {"Dog": 0.2}, snr_db=-15.0)
        loud = obs("mach", 100.01, {"Dog": 0.95}, snr_db=25.0)
        out = ENS.fuse_predictions([quiet, loud], window_s=1.0, method="linear")
        c = out[0]
        assert c.fused_score > 0.7  # dominated by the high-SNR node, not a 0.2/0.95 flat average

    def test_equal_snr_linear_pool_is_the_plain_mean(self):
        a = obs("nyquist", 100.0, {"Dog": 0.4}, snr_db=10.0)
        b = obs("mach", 100.01, {"Dog": 0.8}, snr_db=10.0)
        # Equal weights before any coincidence bonus is applied on top.
        raw = ENS.fuse_linear([a, b], "Dog")
        assert raw == pytest.approx(0.6)

    def test_logodds_weight_by_snr_outvotes_a_quiet_disagreement(self):
        quiet_low = obs("nyquist", 100.0, {"Dog": 0.05}, snr_db=-15.0)
        loud_high = obs("mach", 100.01, {"Dog": 0.97}, snr_db=25.0)
        fused = ENS.fuse_logodds([quiet_low, loud_high], "Dog")
        assert fused > 0.8


class TestUncorrelatedNoiseSuppression:
    """A single node's spike, with nothing at any other node, must not read as a corroborated
    consensus event -- the whole point of spatial coincidence."""

    def test_lone_spike_is_not_corroborated(self):
        observations = [obs("nyquist", 100.0, {"Owl": 0.97}, snr_db=8.0)]
        out = ENS.fuse_predictions(observations, window_s=1.0)
        assert len(out) == 1
        c = out[0]
        assert c.n_nodes == 1
        assert c.corroborated is False
        assert c.coincidence_bonus_db == 0.0
        # Unelevated: fused score for a lone node is exactly its own score.
        assert c.fused_score == pytest.approx(0.97)

    def test_five_quiet_nodes_pull_down_one_noisy_spike(self):
        window = ENS.transit_window_s(30.0, margin_s=0.03)
        spike = obs("nyquist", 100.0, {"Vehicle": 0.95}, snr_db=6.0)
        quiet = [obs(name, 100.0 + i * (window / 6.0), {"Vehicle": 0.02}, snr_db=6.0)
                for i, name in enumerate(("mach", "rankine", "gold", "ageev", "kasami"), start=1)]
        out = ENS.fuse_predictions([spike] + quiet, diameter_m=30.0)
        c = out[0]
        assert c.n_nodes == 6
        # Averaged with five near-zero independent readings, the lone spike is suppressed well
        # below its own raw score.
        assert c.fused_score < 0.5
        assert c.agreement_count == 1
        assert c.coincidence_bonus_db == 0.0

    def test_uncorrelated_spikes_on_different_nodes_do_not_corroborate_each_other(self):
        # Two nodes each spike, but on DIFFERENT classes -- no shared class, no coincidence.
        observations = [
            obs("nyquist", 100.0, {"Owl": 0.9}, snr_db=8.0),
            obs("mach", 100.01, {"Dog": 0.9}, snr_db=8.0),
        ]
        out = ENS.fuse_predictions(observations, window_s=1.0)
        assert len(out) == 2
        assert all(c.n_nodes == 1 and not c.corroborated for c in out)


class TestFuseGroupMethodValidation:
    def test_unknown_method_refused(self):
        with pytest.raises(ValueError):
            ENS.fuse_group([obs("nyquist", 100.0, {"Dog": 0.5})], "Dog", method="bogus")


class TestConsensusPredictionToDict:
    def test_to_dict_has_the_documented_keys(self):
        observations = [
            obs("nyquist", 100.0, {"Dog": 0.9}, snr_db=20.0),
            obs("mach", 100.01, {"Dog": 0.85}, snr_db=18.0),
        ]
        c = ENS.fuse_predictions(observations, window_s=1.0)[0]
        d = c.to_dict()
        for key in ("class", "t_utc_s", "fused_score", "method", "n_nodes", "nodes",
                   "per_node_scores", "mean_snr_db", "spread_s", "coincidence_bonus_db",
                   "agreement_count", "corroborated", "window_s"):
            assert key in d
        assert d["nodes"] == sorted(d["nodes"])
