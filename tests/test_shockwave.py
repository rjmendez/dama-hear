"""Shockwave solver. The observability tests are the point of this file."""
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.solve import shockwave as SW  # noqa: E402

V = 900.0
T = 23.0


def _arrivals(P, bearing_deg, offset, t0=1000.0):
    c = SW.sound_speed(T)
    br = math.radians(bearing_deg)
    return [t0 + SW.shock_time(p, br, offset, V, c) for p in P]


class TestPhysics:
    def test_sound_speed_matches_the_field_value(self):
        assert SW.sound_speed(23.0) == pytest.approx(345.238, abs=1e-3)

    def test_mach_angle_for_m855(self):
        # M855 at ~900 m/s and 23 C -> M 2.61, half-angle 22.6 deg, so the crack arrives
        # 67.4 deg off the trajectory. That offset is why aiming a bearing at the shooter fails.
        assert SW.mach_angle_deg(900.0, SW.sound_speed(23.0)) == pytest.approx(22.6, abs=0.1)

    def test_subsonic_has_no_cone(self):
        assert SW.mach_angle_deg(300.0, SW.sound_speed(23.0)) is None

    def test_solver_refuses_a_subsonic_round(self):
        P = [(0, 0), (20, 5), (-10, 18)]
        with pytest.raises(ValueError, match="subsonic"):
            SW.solve(P, [0, 1, 2], v_mps=300.0, temp_c=T)


class TestRecovery:
    @pytest.mark.parametrize("bearing,offset,P", [
        (20.0, 8.0, [(-30, 0), (25, 12), (5, -28), (-8, 30)]),
        (354.0, -1.5, [(-30, 0), (25, 12), (5, -28), (-8, 30)]),
        # 120 deg needs its own ring: the set above sits entirely on one side of that track,
        # which the solver correctly refuses rather than guessing.
        (120.0, -25.0, [(-40, -40), (40, 40), (35, -30), (-30, 35)]),
    ])
    def test_straddling_nodes_recover_both_parameters(self, bearing, offset, P):
        got = SW.solve(P, _arrivals(P, bearing, offset), v_mps=V, temp_c=T)
        assert got["offset_observable"], "test geometry does not straddle this track"
        d = abs(((got["bearing_deg"] - bearing + 180) % 360) - 180)
        assert d < 1.0, "bearing off by %.2f deg" % d
        assert got["offset_m"] == pytest.approx(offset, abs=1.0)


class TestObservability:
    """THE finding. Same-side geometry makes the offset unrecoverable, at zero residual."""

    def test_same_side_nodes_report_offset_unobservable(self):
        P = [(10, 0), (14, 20), (18, -15)]          # all east of a northbound track
        got = SW.solve(P, _arrivals(P, 0.0, 0.0), v_mps=V, temp_c=T)
        assert not got["offset_observable"]
        assert got["offset_m"] is None, "must refuse to quote an unobservable offset"
        assert "UNOBSERVABLE" in got["note"]

    def test_bearing_survives_even_when_offset_does_not(self):
        P = [(10, 0), (14, 20), (18, -15)]
        got = SW.solve(P, _arrivals(P, 35.0, 4.0), v_mps=V, temp_c=T)
        d = abs(((got["bearing_deg"] - 35.0 + 180) % 360) - 180)
        assert d < 1.5, "bearing must still be recovered: off by %.2f" % d

    def test_shifting_a_track_past_same_side_nodes_changes_no_tdoa(self):
        # the mechanism itself: measured 0.000 ms across +/-6 m of offset
        P = [(10, 0), (14, 20), (18, -15)]
        base = None
        for off in (-6.0, -3.0, 0.0, 3.0, 6.0):
            a = _arrivals(P, 0.0, off)
            dt = [a[i] - a[0] for i in (1, 2)]
            if base is None:
                base = dt
            assert max(abs(x - y) for x, y in zip(dt, base)) < 1e-9

    def test_shifting_a_track_past_straddling_nodes_does_change_tdoa(self):
        # the control: the same shift IS visible when the nodes straddle
        P = [(-9, 0), (12, 20), (15, -15)]
        a0 = _arrivals(P, 0.0, -6.0)
        a1 = _arrivals(P, 0.0, 6.0)
        d0 = [a0[i] - a0[0] for i in (1, 2)]
        d1 = [a1[i] - a1[0] for i in (1, 2)]
        assert max(abs(x - y) for x, y in zip(d0, d1)) > 0.05


class TestResidualHonesty:
    def test_three_nodes_flagged_as_exactly_determined(self):
        # 2 equations, 2 unknowns: the residual is ~0 by construction and proves nothing
        P = [(-30, 0), (25, 12), (5, -28)]
        got = SW.solve(P, _arrivals(P, 40.0, 6.0), v_mps=V, temp_c=T)
        assert got["n_equations"] == 2
        assert not got["residual_is_meaningful"]

    def test_four_nodes_give_a_meaningful_residual(self):
        P = [(-30, 0), (25, 12), (5, -28), (-8, 30)]
        got = SW.solve(P, _arrivals(P, 40.0, 6.0), v_mps=V, temp_c=T)
        assert got["n_equations"] == 3
        assert got["residual_is_meaningful"]

    def test_too_few_nodes_is_refused(self):
        with pytest.raises(ValueError, match="3 nodes"):
            SW.solve([(0, 0), (10, 10)], [0.0, 0.1], v_mps=V, temp_c=T)


class TestShockCoefficientIsPhysical:
    """The Mach-cone coefficient was wrong for a long time and the suite could not see it, because
    solve() reuses the same k as shock_time() and a round-trip agrees with itself however wrong the
    constant is. These check it against physics instead of against itself."""

    V, M = 900.0, 100.0

    def _brute(self, v, c, m, a=0.0):
        """Arrival minimised over the emission point, from first principles: the bullet reaches
        along-track x at x/v, sound then covers the hypotenuse. No shared algebra with the module."""
        x = np.linspace(-3000.0, a, 3_000_001)
        return float(np.min(x / v + np.sqrt((a - x) ** 2 + m * m) / c))

    def test_matches_a_brute_force_minimisation_over_the_emission_point(self):
        c = SW.sound_speed(23.0)
        got = SW.shock_time((0.0, self.M), math.radians(90.0), 0.0, self.V, c)
        assert got == pytest.approx(self._brute(self.V, c, self.M), abs=2e-5)

    def test_a_shock_cannot_arrive_later_than_plain_sound(self):
        """The bound that would have caught it on day one. The old coefficient put a 100 m miss at
        0.4876 s against 0.2897 s for sound over the same distance -- a front doing 205 m/s."""
        c = SW.sound_speed(23.0)
        for m in (1.0, 10.0, 100.0, 300.0):
            t = SW.shock_time((0.0, m), math.radians(90.0), 0.0, self.V, c)
            assert t < m / c, "miss %.0f m: shock %.4f s vs sound %.4f s" % (m, t, m / c)

    def test_arrival_is_bounded_by_the_mach_cone_geometry(self):
        """t = a/V + m*sqrt(V^2-c^2)/(V*c), so the miss term must scale linearly in m and its slope
        must be 1/(V*tan(theta)) for cone half-angle theta = asin(c/V)."""
        c = SW.sound_speed(23.0)
        t1 = SW.shock_time((0.0, 50.0), math.radians(90.0), 0.0, self.V, c)
        t2 = SW.shock_time((0.0, 150.0), math.radians(90.0), 0.0, self.V, c)
        theta = math.asin(c / self.V)
        assert (t2 - t1) / 100.0 == pytest.approx(1.0 / (self.V * math.tan(theta)), rel=1e-9)


# --------------------------------------------------------- weighted least squares
# The same MEASURED corpus figures test_point.py uses: median and maximum of the 8,720 phone
# rows that state sync_sigma_ns in the 2026-09-11 pool snapshot.
SIGMA_PHONE_MEDIAN_S = 106076.28471886144e-9
SIGMA_PHONE_MAX_S = 3233133158.913254e-9
SIGMA_PPS_S = 40e-9

# Four straddling receivers, so the offset is observable and n_eq = 3 over n_unk = 2.
STRADDLE = [(-30.0, 0.0), (25.0, 12.0), (5.0, -28.0), (-8.0, 30.0)]
BEARING, OFFSET = 20.0, 8.0


class TestEqualSigmasChangeNothing:
    """⚠️FIRST. shockwave.solve is a grid search, so a weight that is not exactly 1.0 can move
    the argmin by a whole grid cell -- a quarter degree of bearing -- not by an ulp. sigma_weights
    returns None for equal sigmas precisely so the arithmetic below is the arithmetic that ran
    before it existed."""

    # The sigma-derived keys are the new capability and are None when nothing was stated; every
    # OTHER key is the answer this array has been publishing and must not move at all.
    SIGMA_KEYS = {"chi2", "chi2_reduced", "sigma_s", "equation_weights"}

    @pytest.mark.parametrize("sigma", [None, SIGMA_PPS_S, SIGMA_PHONE_MEDIAN_S, 1.0])
    def test_the_fit_is_bit_for_bit_the_unweighted_one(self, sigma):
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        base = SW.solve(STRADDLE, t, v_mps=V, temp_c=T)
        kw = {} if sigma is None else {"sigmas": [sigma] * 4}
        got = SW.solve(STRADDLE, t, v_mps=V, temp_c=T, **kw)
        assert set(got) == set(base), "the key set must not depend on whether sigmas were given"
        for k, want in base.items():
            if k in self.SIGMA_KEYS:
                continue
            a, b = got[k], want
            if isinstance(a, float) and isinstance(b, float):
                assert a.hex() == b.hex(), "%s drifted: %r vs %r" % (k, a, b)
            else:
                assert a == b, k

    def test_an_unweighted_call_states_no_sigma_rather_than_a_default(self):
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        got = SW.solve(STRADDLE, t, v_mps=V, temp_c=T)
        assert got["sigma_s"] is None and got["equation_weights"] is None
        assert got["chi2"] is None and got["chi2_reduced"] is None
        assert got["reference_node_index"] == 0


class TestTheReferenceReceiver:
    """⚠️THE WEAKNESS OF THE DIFFERENCED FORM, made harmless rather than hidden. This solver
    differences against one receiver instead of marginalising t0 out, so that receiver's clock
    error enters EVERY equation and no per-equation weight can remove it. Differencing against
    the worst-stated clock in the group is therefore not a detail."""

    def test_an_unweighted_call_still_differences_against_the_first_receiver(self):
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        assert SW.solve(STRADDLE, t, v_mps=V, temp_c=T)["reference_node_index"] == 0

    def test_equal_sigmas_still_difference_against_the_first_receiver(self):
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        got = SW.solve(STRADDLE, t, v_mps=V, temp_c=T, sigmas=[SIGMA_PPS_S] * 4)
        assert got["reference_node_index"] == 0, "a tie must not reorder anything"

    def test_the_best_stated_clock_becomes_the_reference(self):
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        got = SW.solve(STRADDLE, t, v_mps=V, temp_c=T,
                       sigmas=[SIGMA_PHONE_MAX_S, SIGMA_PHONE_MEDIAN_S,
                               SIGMA_PPS_S, SIGMA_PHONE_MEDIAN_S])
        assert got["reference_node_index"] == 2

    def test_a_bad_reference_clock_is_not_allowed_to_poison_every_equation(self):
        """Receiver 0 is 4 ms late. Listed first it is the reference, so the error is in all
        three differences at once; naming its sigma moves the reference off it."""
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        t[0] += 0.004
        blind = SW.solve(STRADDLE, t, v_mps=V, temp_c=T)
        told = SW.solve(STRADDLE, t, v_mps=V, temp_c=T,
                        sigmas=[0.004] + [SIGMA_PHONE_MEDIAN_S] * 3)
        truth = SW.solve(STRADDLE, _arrivals(STRADDLE, BEARING, OFFSET), v_mps=V, temp_c=T)

        def berr(r):
            return abs((r["bearing_deg"] - truth["bearing_deg"] + 180.0) % 360.0 - 180.0)

        assert told["reference_node_index"] != 0
        assert berr(told) < berr(blind), \
            "declaring the bad clock must beat differencing against it (%.3f vs %.3f deg)" \
            % (berr(told), berr(blind))


class TestTheExtremes:
    def test_a_huge_sigma_is_weighted_out_rather_than_averaged_in(self):
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        t[3] += 0.050                                   # 50 ms of nonsense on one receiver
        three = SW.solve(STRADDLE[:3], t[:3], v_mps=V, temp_c=T)
        four = SW.solve(STRADDLE, t, v_mps=V, temp_c=T,
                        sigmas=[SIGMA_PHONE_MEDIAN_S] * 3 + [SIGMA_PHONE_MAX_S])
        assert four["bearing_deg"] == pytest.approx(three["bearing_deg"], abs=0.5)
        assert four["equation_weights"][2] < 1e-4

    def test_a_weighted_out_receiver_stops_counting_toward_determinacy(self):
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        got = SW.solve(STRADDLE, t, v_mps=V, temp_c=T,
                       sigmas=[SIGMA_PHONE_MEDIAN_S] * 3 + [SIGMA_PHONE_MAX_S])
        assert got["n_equations"] == 3 and got["n_unknowns"] == 2
        assert got["n_counting_equations"] == 2
        assert got["residual_is_meaningful"] is False, \
            "2 counting equations against 2 unknowns is exactly determined"

    def test_all_four_at_the_same_class_keep_every_equation(self):
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        got = SW.solve(STRADDLE, t, v_mps=V, temp_c=T, sigmas=[SIGMA_PHONE_MEDIAN_S] * 4)
        assert got["n_counting_equations"] == 3
        assert got["n_effective_equations"] == pytest.approx(3.0, abs=1e-12)
        assert got["residual_is_meaningful"] is True

    def test_three_receivers_are_exactly_determined_weighted_or_not(self):
        t = _arrivals(STRADDLE[:3], BEARING, OFFSET)
        for kw in ({}, {"sigmas": [SIGMA_PHONE_MEDIAN_S] * 3}):
            got = SW.solve(STRADDLE[:3], t, v_mps=V, temp_c=T, **kw)
            assert got["n_equations"] == 2 and got["residual_dof"] == 0
            assert got["residual_is_meaningful"] is False
            assert got["chi2_reduced"] is None


class TestASigmaIsNeverInvented:
    def test_a_partially_stated_vector_raises(self):
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        with pytest.raises(ValueError, match="will not invent one"):
            SW.solve(STRADDLE, t, v_mps=V, temp_c=T,
                     sigmas=[SIGMA_PPS_S, None, SIGMA_PPS_S, SIGMA_PPS_S])

    def test_a_wrong_length_vector_raises(self):
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        with pytest.raises(ValueError, match="one entry per receiver"):
            SW.solve(STRADDLE, t, v_mps=V, temp_c=T, sigmas=[SIGMA_PPS_S] * 3)

    @pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
    def test_a_sigma_that_is_not_a_positive_duration_raises(self, bad):
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        with pytest.raises(ValueError, match="finite and > 0"):
            SW.solve(STRADDLE, t, v_mps=V, temp_c=T,
                     sigmas=[SIGMA_PPS_S, bad, SIGMA_PPS_S, SIGMA_PPS_S])


class TestTheSharedHelpers:
    """sigma_weights and the two counts live here and point.py imports them, so one definition
    serves both solvers and they cannot drift apart about what a stated sigma means."""

    def test_equal_sigmas_collapse_to_no_weighting_at_all(self):
        """This is the mechanism requirement 1 rests on: not "the weights are close to one" but
        "there are no weights". Checked at four magnitudes because x/x == 1.0 exactly for every
        finite non-zero float, which a mean-normalised weight would not give."""
        for s in (1e-9, 4e-8, SIGMA_PHONE_MEDIAN_S, 3.23):
            sig, w = SW.sigma_weights([s] * 5, 5)
            assert w is None, "%r produced weights %r" % (s, w)
            assert list(sig) == [s] * 5

    def test_the_weights_are_normalised_to_the_best_stated_sigma(self):
        sig, w = SW.sigma_weights([2e-4, 1e-4, 4e-4], 3)
        assert list(w) == [0.5, 1.0, 0.25]

    def test_sigma_none_means_none(self):
        assert SW.sigma_weights(None, 4) == (None, None)

    def test_effective_n_is_exact_for_equal_weights(self):
        for n in range(2, 12):
            assert SW.effective_n(None, n) == float(n)
            assert SW.effective_n(np.ones(n), n) == float(n)

    def test_effective_n_collapses_toward_one_as_a_single_weight_dominates(self):
        assert SW.effective_n(np.array([1.0, 1e-9, 1e-9, 1e-9]), 4) == pytest.approx(1.0, abs=1e-9)

    def test_counting_n_is_the_receivers_that_still_carry_a_vote(self):
        assert SW.counting_n(None, 5) == 5
        assert SW.counting_n(np.array([1.0, 1.0, 0.5, 1e-6]), 4) == 3
        assert SW.counting_n(np.array([1.0, SW.WEIGHT_FLOOR / 2.0]), 2) == 1


class TestThePairVariance:
    """⚠️A DIFFERENCE OF TWO RECEIVERS IS NOISIER THAN EITHER. Each equation here is
    t_i - t_ref, so its variance is sigma_i^2 + sigma_ref^2, and dropping the reference's share
    would over-state how much every equation is worth. That mutation survived every other test
    in this file -- it leaves the weight ORDERING unchanged, so the grid picks the same cell --
    which is why this one checks the number rather than the answer."""

    def test_equal_sigmas_give_each_equation_sqrt_two_times_the_receiver_sigma(self):
        """chi2 is computed against the PAIR sigma, so a known discrepancy has a predictable
        chi-square: d/(s*sqrt(2)) per equation, not d/s."""
        s = SIGMA_PHONE_MEDIAN_S
        t = _arrivals(STRADDLE[:3], BEARING, OFFSET)
        # a residual the grid cannot fit away: push receiver 0, the reference, by a known amount
        t = list(t)
        t[1] += 40.0 * s
        got = SW.solve(STRADDLE[:3], t, v_mps=V, temp_c=T, sigmas=[s] * 3)
        # r = sum(d^2) over the differenced equations; chi2 must be r / (2 s^2), i.e. the raw
        # sum of squares divided by the PAIR variance and not by s^2.
        r_s2 = (got["rms_residual_ms"] / 1000.0) ** 2 * got["n_equations"]
        assert got["chi2"] == pytest.approx(r_s2 / (2.0 * s * s), rel=1e-9)
        assert got["chi2"] == pytest.approx(0.5 * r_s2 / (s * s), rel=1e-9), \
            "halving is the sqrt(2): ignoring the reference's own sigma would double chi2"

    def test_a_worse_reference_makes_every_equation_worth_less(self):
        """rho_ref enters all of them, so raising it must lower every weight together rather
        than leaving them at 1.0."""
        t = _arrivals(STRADDLE, BEARING, OFFSET)
        # receiver 2 is the best, so it becomes the reference; the other three are 4x worse
        got = SW.solve(STRADDLE, t, v_mps=V, temp_c=T,
                       sigmas=[4e-4, 4e-4, 1e-4, 4e-4])
        assert got["reference_node_index"] == 2
        # rho = [4, 4, 1, 4]; each equation pairs a rho-4 receiver with the rho-1 reference,
        # so rho_pair = sqrt((16 + 1) / 2) and the weight is its reciprocal.
        want = 1.0 / math.sqrt((16.0 + 1.0) / 2.0)
        assert got["equation_weights"] == pytest.approx([want] * 3, rel=1e-12)
        assert all(w < 1.0 for w in got["equation_weights"]), \
            "every equation is worse than the best receiver alone"
