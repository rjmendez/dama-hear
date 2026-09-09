"""hear/solve/rangediff.py -- what two nodes CAN say, and the proof that it is not a position.

The thing being defended: the 2-node Fisher matrix is rank 1 for every geometry, so nothing on
this path may emit a coordinate, a DOP, a CEP or a confidence. These tests pin the rank result
against the code that computes it (not against a comment), pin the closed form |g| = 2sin(th/2)/c
that explains the field's geometry-dominance measurement, and pin that `cue()` cannot leak a key
a fix consumer reads.
"""
import json
import math
import os

import numpy as np
import pytest

from hear.solve import rangediff as RD

C = 345.238

# survey.json as deployed: nyquist at the origin, mach 16.873 m away with a NOMINAL 3.0 m storey
# height. The sigmas are the file's own, and they are what makes the direction survey-limited.
NY = (0.0, 0.0, 0.0)
MA = (-16.602, -0.272, 3.0)
SIG_H = (0.717, 0.521)
SIG_U = (0.0, 1.0)
B = RD.baseline_m(NY, MA)          # 16.8731 m, computed so it cannot drift from survey.json


class TestRankOne:
    """The literature result, computed rather than asserted."""

    def test_the_fisher_matrix_is_rank_one_for_every_geometry_tried(self):
        rng = np.random.default_rng(20260908)
        for _ in range(400):
            p1 = rng.uniform(-50, 50, 3)
            p2 = p1 + rng.uniform(-40, 40, 3)
            if np.linalg.norm(p2 - p1) < 1.0:
                continue
            s = rng.uniform(-300, 300, 3)
            if min(np.linalg.norm(s - p1), np.linalg.norm(s - p2)) < 1.0:
                continue
            f = RD.fisher(p1, p2, s, c=C, sigma_tau_s=1e-4)
            assert f["rank"] == 1
            assert len(f["null_directions"]) == 2

    def test_a_longer_baseline_and_a_better_clock_do_not_raise_the_rank(self):
        """The point of the result: it is not a conditioning problem that better hardware fixes."""
        for base in (1.0, 16.873, 1000.0, 100000.0):
            for sig in (1.0, 1e-3, 1e-9):
                f = RD.fisher((0, 0, 0), (base, 0, 0), (30.0, 40.0, 0.0), c=C, sigma_tau_s=sig)
                assert f["rank"] == 1

    def test_moving_the_source_along_a_null_direction_does_not_change_the_tdoa(self):
        """Zero Fisher information means EXACTLY zero, not 'small'. Measured against the
        observable direction on the same geometry, which moves it four orders more."""
        s = np.array([30.0, 30.0, 0.0])
        f = RD.fisher(NY, MA, s, c=C, sigma_tau_s=2.7e-5)
        a, b = np.array(NY), np.array(MA)

        def tdoa(x):
            return (np.linalg.norm(x - b) - np.linalg.norm(x - a)) / C

        base = tdoa(s)
        moved = [abs(tdoa(s + 1e-4 * np.array(d)) - base) for d in f["null_directions"]]
        obs = abs(tdoa(s + 1e-4 * np.array(f["observable_direction"])) - base)
        assert max(moved) < 1e-12
        assert obs > 1e-8
        assert obs / max(max(moved), 1e-300) > 1e4

    def test_the_gradient_matches_the_closed_form_that_explains_the_field_result(self):
        """|u2 - u1| = 2 sin(theta/2). This is the mechanism behind 'same-side nodes see nothing'
        and it has to hold in the code, not only in the docstring."""
        for src in [(200.0, 200.0, 0.0), (-8.3, 0.5, 0.0), (0.0, 60.0, 0.0), (-40.0, -3.0, 2.0)]:
            f = RD.fisher(NY, MA, src, c=C, sigma_tau_s=1.0)
            th = math.radians(f["subtended_angle_deg"])
            assert f["gradient_norm_per_m"] == pytest.approx(2.0 * math.sin(th / 2.0) / C,
                                                             rel=1e-12)

    def test_same_side_geometry_loses_the_gradient_and_straddling_keeps_it(self):
        far = RD.fisher(NY, MA, (200.0, 200.0, 0.0), c=C, sigma_tau_s=1.0)
        mid = RD.fisher(NY, MA, (-8.3, 0.5, 0.0), c=C, sigma_tau_s=1.0)
        assert far["subtended_angle_deg"] < 5.0
        assert mid["subtended_angle_deg"] > 150.0
        assert mid["gradient_norm_per_m"] / far["gradient_norm_per_m"] > 40.0


class TestTheCueIsNotAFix:

    def _cue(self, tau):
        return RD.cue(NY, MA, tau, c=C, sigma_tau_s=math.sqrt(2) * 19.3e-6,
                      sigma_pos_m=SIG_H, sigma_up_m=SIG_U)

    def test_it_carries_no_key_a_fix_consumer_reads(self):
        """`hear/backend/pipeline.to_dama_event` copies east_m/north_m/position_observable/range_m
        straight onto the fleet payload when they are present. If one ever appeared here a
        downstream consumer would read a locus as a position."""
        for tau in (-0.04, -0.01, 0.0, 0.01, 0.04):
            assert not (RD.FIX_KEYS & set(self._cue(tau)))

    def test_it_says_outright_that_there_is_no_fix(self):
        r = self._cue(-0.02)
        assert r["fix"] is False
        assert r["observable_dof"] == 1 and r["unobservable_dof"] == 2
        assert r["mirror_ambiguous"] is True
        assert r["locus"] == "hyperboloid_sheet"

    def test_a_delay_past_the_plane_wave_bound_is_refused_not_clamped(self):
        r = RD.cue(NY, MA, 0.060, c=C, sigma_tau_s=2e-5, sigma_pos_m=SIG_H, sigma_up_m=SIG_U)
        assert r["usable"] is False and "impossible" in r["reason"]

    def test_a_delay_with_no_sigma_is_refused(self):
        with pytest.raises(ValueError):
            RD.cue(NY, MA, -0.01, sigma_tau_s=0.0, sigma_pos_m=SIG_H, sigma_up_m=SIG_U)

    def test_three_dimensional_nodes_demand_a_vertical_sigma(self):
        """The mach height is a nominal storey, +/-1.0 m assumed. Dropping it understates the
        axis term by the largest single contributor to it."""
        with pytest.raises(ValueError):
            RD.cue(NY, MA, -0.01, sigma_tau_s=2e-5, sigma_pos_m=SIG_H)

    def test_the_angle_keeps_the_sign_of_the_range_difference(self):
        """arccos(|Delta|/B) folds 175 deg onto 5 deg and loses which end of the baseline the
        source is toward. The signed form round-trips."""
        for want in (10.0, 30.0, 90.0, 150.0, 175.0):
            tau = -B * math.cos(math.radians(want)) / C
            r = self._cue(tau)
            assert r["asymptote_angle_deg"] == pytest.approx(want, abs=1e-6)


class TestTheUncertaintyIsSurveyLimited:
    """The measured headline: on this pair the node survey beats the clock by two orders."""

    def _terms(self, theta_deg, sigma_e_s):
        tau = -B * math.cos(math.radians(theta_deg)) / C
        return RD.cue(NY, MA, tau, c=C, sigma_tau_s=math.sqrt(2) * sigma_e_s,
                      sigma_pos_m=SIG_H, sigma_up_m=SIG_U)["direction_sigma_terms_deg"]

    def test_survey_beats_timing_at_the_onset_pickers_own_median_sigma(self):
        for th in (30.0, 60.0, 90.0):
            t = self._terms(th, 19.3e-6)
            survey = math.hypot(t["baseline_length_deg"], t["axis_orientation_deg"])
            assert survey / t["timing_deg"] > 50.0

    def test_only_the_chunk_quantised_fallback_makes_timing_the_binding_term(self):
        """~20% of picks fall back to raw chunk quantisation at ~5.25 ms, flat across 80 dB of
        SNR. That is the one regime where the clock path matters more than the tape measure."""
        t = self._terms(60.0, 5.25e-3)
        assert t["timing_deg"] > math.hypot(t["baseline_length_deg"], t["axis_orientation_deg"])

    def test_the_axis_term_never_vanishes_and_the_length_term_does(self):
        """They are different errors and a single combined sigma would hide it: a perpendicular
        survey error rotates the cone axis at every angle, an along-baseline one only scales the
        cone and so drops out at broadside."""
        t90 = self._terms(90.0, 19.3e-6)
        assert t90["baseline_length_deg"] == pytest.approx(0.0, abs=1e-12)
        assert t90["sound_speed_deg"] == pytest.approx(0.0, abs=1e-12)
        assert t90["axis_orientation_deg"] > 3.0

    def test_endfire_returns_infinity_rather_than_a_small_looking_number(self):
        r = RD.cue(NY, MA, -B / C * 0.9999, c=C, sigma_tau_s=2e-5,
                   sigma_pos_m=SIG_H, sigma_up_m=SIG_U)
        assert r["endfire_degenerate"] is True
        assert not math.isfinite(r["asymptote_angle_sigma_deg"])
        assert math.isfinite(r["range_difference_m"]), "the range difference is still measured"


class TestTheLocusItself:

    def test_the_asymptote_is_approached_from_inside_as_range_grows(self):
        d = -8.0
        asym = RD.asymptote_angle_deg(d, B)
        near = RD.direction_at_range(d, NY, MA, 30.0)
        far = RD.direction_at_range(d, NY, MA, 500.0)
        assert near < far < asym
        assert asym - far < asym - near
        assert asym - far < 0.01

    def test_a_point_on_the_sheet_reproduces_the_range_difference(self):
        d = -8.0
        for R in (20.0, 50.0, 200.0):
            phi = math.radians(RD.direction_at_range(d, NY, MA, R))
            a, b = np.array(NY), np.array(MA)
            u = (b - a) / B
            w = np.array([0.0, 0.0, 1.0])
            w = w - np.dot(w, u) * u
            w /= np.linalg.norm(w)
            s = 0.5 * (a + b) + R * (math.cos(phi) * u + math.sin(phi) * w)
            got = np.linalg.norm(s - b) - np.linalg.norm(s - a)
            assert got == pytest.approx(d, abs=1e-6)

    def test_the_range_difference_can_never_exceed_the_baseline(self):
        rng = np.random.default_rng(20260908)
        a, b = np.array(NY), np.array(MA)
        for _ in range(2000):
            s = rng.uniform(-500, 500, 3)
            d = np.linalg.norm(s - b) - np.linalg.norm(s - a)
            assert abs(d) <= B + 1e-9

    def test_the_survey_file_still_describes_the_pair_these_numbers_were_measured_on(self):
        """If someone re-surveys the nodes, the docstring's sigma table stops being true and this
        fails rather than the table quietly going stale."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "survey.json")
        nodes = {n["name"]: n for n in json.load(open(path))["nodes"]}
        assert B == pytest.approx(16.8731, abs=1e-3)
        assert nodes["nyquist"]["sigma_m"] == pytest.approx(SIG_H[0])
        assert nodes["mach"]["sigma_m"] == pytest.approx(SIG_H[1])
        assert nodes["mach"]["sigma_u_m"] == pytest.approx(SIG_U[1])


class TestTheApiRefuses:

    def test_two_nodes_at_one_point_have_no_baseline(self):
        with pytest.raises(ValueError):
            RD.baseline_m((0, 0, 0), (0, 0, 0))

    def test_a_source_on_a_node_has_no_gradient(self):
        with pytest.raises(ValueError):
            RD.fisher(NY, MA, NY, c=C)

    def test_mixed_arity_positions_are_refused(self):
        with pytest.raises(ValueError):
            RD.baseline_m((0, 0), (1, 2, 3))
