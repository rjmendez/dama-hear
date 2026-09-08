"""Burst association against KNOWN truth.

⚠️These are synthetic. The field data cannot test this: over a 15.0 h overlap the 2026-09-08 node
pair shared only 3 bursts with 3+ events on both sides, and 30% of one node's events sat in a
burst the other never heard -- its two largest (12 and 23 events) drew zero co-activity. So the
mechanism is pinned here against truth, and stays field-unvalidated until a loud impulsive source
both nodes hear turns up.
"""
import numpy as np
import pytest

from hear.solve import burstassoc as BA

BOUND = 0.049          # 16.873 m / 343 m/s, the real node pair
TOL = 0.003


def _burst(n=5, tau=0.031, jitter=0.0004, seed=1, spacing=0.085):
    rng = np.random.default_rng(seed)
    a = np.arange(n) * spacing + rng.normal(0, 0.002, n)
    a.sort()
    b = np.sort(a + tau + rng.normal(0, jitter, n))
    return a, b


class TestItRecoversTheTruth:
    def test_a_clean_burst(self):
        a, b = _burst()
        m = BA.associate_burst(a, b, BOUND, TOL)
        assert m and m.n_pairs == 5
        assert abs(m.tau_s - 0.031) < 0.001
        assert [i for i, _ in m.pairs] == [0, 1, 2, 3, 4]

    def test_a_negative_tau(self):
        a, b = _burst(tau=-0.040)
        m = BA.associate_burst(a, b, BOUND, TOL)
        assert m and abs(m.tau_s + 0.040) < 0.001

    def test_echoes_are_rejected_when_their_delay_VARIES_by_more_than_the_tolerance(self):
        """⚠️THE CASE PER-EVENT MATCHING LOSES, and the condition under which this wins it.

        Each echo is individually inside the bound, so nearest-neighbour can pair with one. The
        shared tau rules them out ONLY when the echo delay varies by more than the tolerance
        across the burst -- then no single shifted tau explains them all, while the direct
        arrivals share one exactly.
        """
        rng = np.random.default_rng(21)
        a, b = _burst()
        e1 = b + rng.uniform(0.008, 0.030, len(b))       # spread 22 ms >> 3 ms tolerance
        m = BA.associate_burst(a, np.sort(np.concatenate([b, e1])), BOUND, TOL)
        assert m, m.reason
        assert abs(m.tau_s - 0.031) < 0.001
        assert m.n_pairs == 5

    def test_a_CONSTANT_echo_delay_is_an_UNRESOLVABLE_tie_and_is_refused(self):
        """⚠️A LIMIT OF THE PROBLEM, NOT A DEFECT, AND THE REALISTIC CASE.

        A stationary source in a fixed room echoes at a near-CONSTANT delay: same paths, same
        geometry, every event. The echo train is then exactly the direct train shifted, so tau
        and tau+echo explain the arrivals equally well and NOTHING in the timing separates them.

        The discriminators that would are outside this function: the geometric bound (tau+echo
        may exceed d/c, and is then never proposed -- see the bound test below), and AMPLITUDE,
        which the 172-byte frame already carries as `peak` and this does not yet use. Until one
        of those is applied, refusing is the only honest answer.

        Found by a fixture that accidentally built exactly this case.
        """
        a, b = _burst()
        m = BA.associate_burst(a, np.sort(np.concatenate([b, b + 0.012])), BOUND, TOL)
        assert not m
        assert "ambiguous" in m.reason
        assert abs(m.runner_up_tau_s - (m.tau_s + 0.012)) < 0.002

    def test_an_echo_beyond_the_geometry_is_never_a_candidate(self):
        """The bound does break the tie when the echo is long enough: tau+echo outside d/c is
        not a hypothesis at all."""
        a, b = _burst(tau=0.020)
        m = BA.associate_burst(a, np.sort(np.concatenate([b, b + 0.045])), BOUND, TOL)
        assert m, m.reason                       # 20+45 = 65 ms > the 49 ms bound
        assert abs(m.tau_s - 0.020) < 0.001

    def test_a_node_that_missed_events_still_resolves(self):
        a, b = _burst(n=7)
        m = BA.associate_burst(a, np.delete(b, [2, 5]), BOUND, TOL)
        assert m and m.n_pairs == 5
        assert abs(m.tau_s - 0.031) < 0.001

    def test_the_assignment_is_order_preserving(self):
        """Both sensors hear one source's events in the same order; a crossing pairing
        describes a reordering of time, which no geometry produces."""
        a, b = _burst(n=6)
        m = BA.associate_burst(a, b, BOUND, TOL)
        ia = [i for i, _ in m.pairs]
        ib = [j for _, j in m.pairs]
        assert ia == sorted(ia) and ib == sorted(ib)


class TestItRefuses:
    def test_unrelated_events_are_refused(self):
        rng = np.random.default_rng(4)
        a = np.sort(rng.uniform(0, 0.4, 5))
        b = np.sort(rng.uniform(0, 0.4, 5))
        m = BA.associate_burst(a, b, BOUND, TOL, min_pairs=4)
        assert not m

    def test_a_genuine_tie_is_refused_not_picked(self):
        """⚠️Two taus explaining the same number of pairs is the 13-of-55 field case. Picking
        one is how a plausible wrong association enters a solve."""
        a = np.array([0.0, 0.100, 0.200])
        b = np.sort(np.concatenate([a + 0.020, a + 0.040]))
        m = BA.associate_burst(a, b, BOUND, TOL, min_pairs=3)
        assert not m
        assert "ambiguous" in m.reason
        assert m.runner_up_tau_s is not None

    def test_too_few_events_is_refused_with_the_counts(self):
        m = BA.associate_burst([0.0, 0.1], [0.03, 0.13], BOUND, TOL)
        assert not m and "too few events" in m.reason

    def test_a_tolerance_as_wide_as_the_bound_is_rejected_outright(self):
        """A tolerance that admits everything makes the fit meaningless -- the same trap as
        crackblast.interval_consistent's bound with no tolerance."""
        a, b = _burst()
        with pytest.raises(ValueError, match="not tighter than the bound"):
            BA.associate_burst(a, b, BOUND, BOUND)

    def test_a_tau_outside_the_geometry_is_never_proposed(self):
        a, b = _burst(tau=0.200)          # far outside d/c
        m = BA.associate_burst(a, b, BOUND, TOL)
        assert not m


class TestAgainstANull:
    def test_it_beats_matched_density_noise(self):
        """The control the field analysis needed: same event COUNT in the same window, no shared
        source. A method that 'resolves' those is fitting noise."""
        rng = np.random.default_rng(11)
        real = sum(bool(BA.associate_burst(*_burst(seed=s), BOUND, TOL)) for s in range(40))
        null = 0
        for _ in range(40):
            a = np.sort(rng.uniform(0, 0.4, 5))
            b = np.sort(rng.uniform(0, 0.4, 5))
            null += bool(BA.associate_burst(a, b, BOUND, TOL))
        assert real >= 35, "only %d/40 real bursts resolved" % real
        assert null <= 8, "%d/40 random pairs 'resolved' -- fitting noise" % null


class TestSplitBursts:
    def test_it_splits_on_the_gap_and_nothing_else(self):
        t = [0.0, 0.08, 0.16, 5.0, 5.09]
        assert BA.split_bursts(t, gap_s=1.0) == [[0, 1, 2], [3, 4]]

    def test_empty_input(self):
        assert BA.split_bursts([]) == []
