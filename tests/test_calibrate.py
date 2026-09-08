"""hear/solve/calibrate.py -- estimating a per-node constant clock bias across many events.

The thing being defended: a fixed timestamp offset is not noise. It moves one node's range by
c*bias for every event alike, and in an exactly-determined fit it relocates the answer WITHOUT
disturbing the residual. Nothing downstream can see it. These tests pin that it is recoverable,
what it takes to recover it, and the two ways the problem is genuinely unsolvable.
"""
import math

import numpy as np
import pytest

from hear.solve import calibrate as CB
from hear.solve import shockwave as SW

T = 15.0
C = SW.sound_speed(T)
# 2 PPS nodes at the house, 3 phones out in the yard.
NODES = np.array([[0., 0., 0.], [-16.6, -0.3, 3.0], [25., 30., 0.], [-40., 25., 0.], [10., -45., 0.]])
PHONES = np.array([False, False, True, True, True])
TRUE_BIAS_MS = np.array([0., 0., 68.0, -41.0, 95.0])


def _sim(K, sigma_ms, seed=1, nodes=NODES, bias=TRUE_BIAS_MS, drop=None):
    rng = np.random.default_rng(seed)
    src = np.column_stack([rng.uniform(-60, 60, K), rng.uniform(-60, 60, K), np.zeros(K)])
    sig = np.where(PHONES[:len(nodes)], sigma_ms, 0.01)
    T_ = np.array([1.757e9 + np.linalg.norm(src[k] - nodes, axis=1) / C
                   + (bias + rng.normal(0, sig, len(nodes))) * 1e-3 for k in range(K)])
    if drop is not None:
        T_[drop] = np.nan
    return src, T_


def _err(src, res):
    return np.median([math.hypot(p[0] - src[k][0], p[1] - src[k][1])
                      for k, p in enumerate(res["positions_m"])])


class TestBiasIsRecoverable:
    def test_a_clean_fleet_recovers_all_three_biases(self):
        src, arr = _sim(40, 2.8, seed=9)
        r = CB.solve_multi(NODES, arr, PHONES, "blast", temp_c=T, fixed_up_m=0.0)
        got = np.array(r["bias_ms"])[PHONES]
        assert got == pytest.approx(TRUE_BIAS_MS[PHONES], abs=3.0)
        assert r["observable"] is True

    def test_removing_the_bias_is_what_buys_the_accuracy(self):
        """Same data, same nodes. The only difference is whether the offsets are estimated."""
        src, arr = _sim(40, 2.8, seed=9)
        ignored = np.array([False, False, False, False, True])   # one flag: keeps it solvable
        a = CB.solve_multi(NODES, arr, ignored, "blast", temp_c=T, fixed_up_m=0.0)
        b = CB.solve_multi(NODES, arr, PHONES, "blast", temp_c=T, fixed_up_m=0.0)
        assert _err(src, a) > 10.0
        assert _err(src, b) < 5.0

    def test_calibration_does_not_rescue_a_noisy_node_only_a_biased_one(self):
        """Accuracy, not precision. The 25 ms phone still cannot be made into a good node -- the
        bias comes out, the scatter stays, and the position error stays an order of magnitude
        worse than the same fleet with the timestamp fixed."""
        s1, a1 = _sim(40, 25.1, seed=4)
        s2, a2 = _sim(40, 2.8, seed=4)
        noisy = CB.solve_multi(NODES, a1, PHONES, "blast", temp_c=T, fixed_up_m=0.0)
        clean = CB.solve_multi(NODES, a2, PHONES, "blast", temp_c=T, fixed_up_m=0.0)
        assert np.array(noisy["bias_ms"])[PHONES] == pytest.approx(
            TRUE_BIAS_MS[PHONES], abs=20.0), "the bias still comes out, roughly"
        assert _err(s1, noisy) > 4 * _err(s2, clean)


class TestWhatItTakes:
    def test_one_event_cannot_separate_bias_from_position(self):
        """The core degeneracy. With a single event, 'the source was further that way' and 'that
        node's clock is late' stretch the same range and no data distinguishes them."""
        _, arr = _sim(1, 2.8)
        with pytest.raises(CB.CalibrationError, match="add events"):
            CB.solve_multi(NODES, arr, PHONES, "blast", temp_c=T, fixed_up_m=0.0)

    def test_it_asks_for_events_not_nodes(self):
        """The message matters: the instinct on a bad fit is to add hardware, and here that is
        exactly the wrong move. A bias is separable because the SOURCE moves and it does not."""
        _, arr = _sim(1, 2.8)
        with pytest.raises(CB.CalibrationError) as e:
            CB.solve_multi(NODES, arr, PHONES, "blast", temp_c=T, fixed_up_m=0.0)
        assert "not more nodes" in str(e.value) or "add events, not nodes" in str(e.value)

    def test_two_events_is_enough(self):
        src, arr = _sim(2, 2.8, seed=3)
        r = CB.solve_multi(NODES, arr, PHONES, "blast", temp_c=T, fixed_up_m=0.0)
        assert r["observable"] is True
        assert np.array(r["bias_ms"])[PHONES] == pytest.approx(TRUE_BIAS_MS[PHONES], abs=6.0)


class TestRefusals:
    def test_every_node_biased_is_refused_as_singular(self):
        """Rank deficient by exactly one at ANY number of events: adding the same constant to
        every clock is indistinguishable from moving t0, which is already marginalised out."""
        _, arr = _sim(10, 2.8)
        with pytest.raises(CB.CalibrationError, match="reference"):
            CB.solve_multi(NODES, arr, np.ones(len(NODES), bool), "blast", temp_c=T)

    def test_an_event_heard_by_too_few_nodes_is_refused(self):
        _, arr = _sim(10, 2.8)
        arr[3, 1:] = np.nan          # only one node heard event 3
        with pytest.raises(CB.CalibrationError, match=">= 3 nodes"):
            CB.solve_multi(NODES, arr, PHONES, "blast", temp_c=T, fixed_up_m=0.0)

    def test_a_cone_class_is_refused_here_too(self):
        _, arr = _sim(10, 2.8)
        with pytest.raises(ValueError, match="Mach cone"):
            CB.solve_multi(NODES, arr, PHONES, "crack", temp_c=T, fixed_up_m=0.0)

    def test_mismatched_shape_is_refused(self):
        _, arr = _sim(10, 2.8)
        with pytest.raises(CB.CalibrationError, match="match"):
            CB.solve_multi(NODES, arr[:, :3], PHONES, "blast", temp_c=T)


class TestPartialHearing:
    def test_a_node_that_missed_some_events_still_calibrates(self):
        """NaN means "did not hear it", not "heard it at zero". Real fleets have gaps."""
        src, arr = _sim(60, 2.8, seed=11)
        arr[::3, 4] = np.nan          # node 4 missed a third of the events
        r = CB.solve_multi(NODES, arr, PHONES, "blast", temp_c=T, fixed_up_m=0.0)
        assert r["observable"] is True
        assert np.array(r["bias_ms"])[PHONES] == pytest.approx(TRUE_BIAS_MS[PHONES], abs=5.0)


class TestReporting:
    def test_bias_is_reported_in_metres_as_well_as_milliseconds(self):
        """Milliseconds are the unit it is measured in; metres are the unit that says whether it
        matters. 68 ms is 23 m, which is larger than the whole nyquist-mach baseline."""
        src, arr = _sim(20, 2.8, seed=6)
        r = CB.solve_multi(NODES, arr, PHONES, "blast", temp_c=T, fixed_up_m=0.0)
        for ms, m in zip(r["bias_ms"], r["bias_range_m"]):
            assert m == pytest.approx(ms * 1e-3 * r["sound_speed_mps"], rel=1e-9)
        assert max(abs(v) for v in r["bias_range_m"]) > 16.6

    def test_the_declared_height_is_echoed_back(self):
        src, arr = _sim(20, 2.8, seed=6)
        r = CB.solve_multi(NODES, arr, PHONES, "blast", temp_c=T, fixed_up_m=2.5)
        assert r["up_assumed_m"] == 2.5
        assert all(p[2] == 2.5 for p in r["positions_m"])
