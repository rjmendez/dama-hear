"""Point-source solver. The cone gate and the collinear verdict are the point of this file."""
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.solve import placement as PL  # noqa: E402
from hear.solve import point as PT  # noqa: E402
from hear.solve import shockwave as SW  # noqa: E402

T = 23.0
SQUARE = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
LINE = [(0.0, 0.0), (50.0, 0.0), (100.0, 0.0), (150.0, 0.0)]


CRACK_ARRAY = [(-200.0, -200.0), (200.0, 200.0), (175.0, -150.0), (-150.0, 175.0)]
CRACK_BEARING_DEG, CRACK_OFFSET_M, CRACK_V_MPS = 20.0, 8.0, 900.0


def _arrivals(P, source, t0=1000.0, temp_c=T):
    c = SW.sound_speed(temp_c)
    s = np.asarray(source, float)[:2]
    return [t0 + float(np.linalg.norm(s - np.asarray(p, float)[:2])) / c for p in P]


def _crack_fitted_as_a_blast(P):
    """Shock arrivals from a real M855 track, mislabelled. Returns the fit and the track point
    nearest the array -- the closest thing to a 'right answer' a point model could have given."""
    c = SW.sound_speed(T)
    br = math.radians(CRACK_BEARING_DEG)
    t = [1000.0 + SW.shock_time(p, br, CRACK_OFFSET_M, CRACK_V_MPS, c) for p in P]
    u, n = SW._axes(br)
    w = np.asarray(P, float).mean(axis=0) - n * CRACK_OFFSET_M
    closest = n * CRACK_OFFSET_M + u * float(np.dot(w, u))
    return PT.solve(P, t, "blast", temp_c=T), closest


class TestRecovery:
    SRC = (260.0, 40.0)

    def test_a_blast_outside_the_array_is_recovered(self):
        t = _arrivals(SQUARE, self.SRC)
        got = PT.solve(SQUARE, t, "blast", temp_c=T)
        assert got["position_observable"] is True
        assert got["east_m"] == pytest.approx(self.SRC[0], abs=1.0)
        assert got["north_m"] == pytest.approx(self.SRC[1], abs=1.0)
        assert got["t0_utc_s"] == pytest.approx(1000.0, abs=0.005)

    def test_does_not_depend_on_which_node_is_called_first(self):
        """t0 is marginalised, not differenced against node 0. Regression against a reference
        node creeping back in -- the same property test_placement pins for dop()."""
        t = _arrivals(SQUARE, self.SRC)
        base = PT.solve(SQUARE, t, "blast", temp_c=T)
        for k in range(1, len(SQUARE)):
            got = PT.solve(SQUARE[k:] + SQUARE[:k], t[k:] + t[:k], "blast", temp_c=T)
            assert got["east_m"] == pytest.approx(base["east_m"], rel=1e-6)
            assert got["north_m"] == pytest.approx(base["north_m"], rel=1e-6)
        rev = PT.solve(list(reversed(SQUARE)), list(reversed(t)), "blast", temp_c=T)
        assert rev["east_m"] == pytest.approx(base["east_m"], rel=1e-6)
        assert rev["north_m"] == pytest.approx(base["north_m"], rel=1e-6)

    def test_a_three_vector_position_is_sliced_not_rejected(self):
        """Fact 1: the solver is 2D. Pinned rather than left implicit in shockwave.py:88's [:2]."""
        P3 = [(e, n, 7.5) for e, n in SQUARE]
        t = _arrivals(SQUARE, self.SRC)
        a, b = PT.solve(P3, t, "blast", temp_c=T), PT.solve(SQUARE, t, "blast", temp_c=T)
        assert a["east_m"] == pytest.approx(b["east_m"], rel=1e-9)
        assert a["north_m"] == pytest.approx(b["north_m"], rel=1e-9)

    def test_c_comes_from_shockwave_not_a_literal(self):
        t = _arrivals(SQUARE, self.SRC)
        got = PT.solve(SQUARE, t, "blast", temp_c=23.0)
        assert got["sound_speed_mps"] == pytest.approx(345.238, abs=1e-3)


class TestRedundancy:
    """Three nodes give two equations for two unknowns. The residual is zero BY CONSTRUCTION and
    proves nothing -- which is exactly why a perfect-looking residual there is dangerous."""

    SRC = (260.0, 40.0)

    def test_three_nodes_look_perfect_and_mean_nothing(self):
        P = SQUARE[:3]
        got = PT.solve(P, _arrivals(P, self.SRC), "blast", temp_c=T)
        assert got["n_equations"] == 2
        assert got["residual_is_meaningful"] is False
        # abs=1e-3 ms == 1 us: noise-free synthetic arrivals, so anything above the refine's own
        # convergence floor would mean the model is wrong, not that the data is.
        assert got["rms_residual_ms"] == pytest.approx(0.0, abs=1e-3)

    def test_four_nodes_are_where_a_residual_starts_meaning_something(self):
        got = PT.solve(SQUARE, _arrivals(SQUARE, self.SRC), "blast", temp_c=T)
        assert got["n_equations"] == 3
        assert got["residual_is_meaningful"] is True


class TestCollinear:
    """Reflect a source across the line its nodes sit on and every range is unchanged. Both fit
    at zero residual, so a coordinate here is a coin toss dressed as a measurement."""

    SRC = (60.0, 80.0)
    MIRROR = (60.0, -80.0)

    def test_collinear_nodes_are_refused_not_fitted(self):
        got = PT.solve(LINE, _arrivals(LINE, self.SRC), "blast", temp_c=T)
        assert got["position_observable"] is False
        assert got["east_m"] is None and got["north_m"] is None
        assert "UNOBSERVABLE" in got["note"]
        assert got["linearity"] < PL.COLLINEAR_LINEARITY

    def test_the_mirror_really_is_indistinguishable(self):
        """Pins the reason for the refusal. If this ever fails, the verdict is over-cautious."""
        a = _arrivals(LINE, self.SRC)
        b = _arrivals(LINE, self.MIRROR)
        assert a == pytest.approx(b, abs=1e-9)

    def test_an_off_line_node_makes_it_observable_again(self):
        P = LINE[:3] + [(75.0, 40.0)]
        got = PT.solve(P, _arrivals(P, self.SRC), "blast", temp_c=T)
        assert got["position_observable"] is True
        assert got["east_m"] == pytest.approx(self.SRC[0], abs=1.0)


class TestSourceClassGate:
    @pytest.mark.parametrize("cls", sorted(PT.CONE_CLASSES))
    def test_a_cone_class_is_refused_outright(self, cls):
        with pytest.raises(ValueError, match="Mach cone"):
            PT.solve(SQUARE, _arrivals(SQUARE, (260.0, 40.0)), cls, temp_c=T)

    def test_an_unknown_class_is_refused_rather_than_assumed_to_be_a_point(self):
        with pytest.raises(ValueError, match="unknown source class"):
            PT.solve(SQUARE, _arrivals(SQUARE, (260.0, 40.0)), "banana", temp_c=T)

    def test_is_point_source_splits_the_two_sets(self):
        assert PT.is_point_source("blast") is True
        assert PT.is_point_source("crack") is False
        with pytest.raises(ValueError):
            PT.is_point_source("banana")

    def test_feeding_it_a_crack_as_a_blast_costs_this_much(self):
        """The gate cannot catch a caller who lies about the class, so pin the damage. Arrivals
        from a real M855 track, labelled 'blast': the fit is confident and far off the track.
        Four nodes at least make the residual scream -- 299 ms, run here."""
        got, closest = _crack_fitted_as_a_blast(CRACK_ARRAY)
        assert got["position_observable"] is True, "it does not hesitate; that is the problem"
        fitted = np.array([got["east_m"], got["north_m"]])
        assert float(np.linalg.norm(fitted - closest)) > 50.0
        assert got["residual_is_meaningful"] is True and got["rms_residual_ms"] > 10.0

    def test_at_three_nodes_the_same_lie_is_silent(self):
        """The worst case in the repo, and it is arithmetic, not bad luck: the same crack fed to
        three nodes lands 302 m off the track (run here) at rms_residual_ms 0.00000. Two
        equations, two unknowns -- the residual CANNOT report the model is wrong."""
        got, closest = _crack_fitted_as_a_blast(CRACK_ARRAY[:3])
        fitted = np.array([got["east_m"], got["north_m"]])
        assert float(np.linalg.norm(fitted - closest)) > 50.0
        assert got["residual_is_meaningful"] is False
        assert got["rms_residual_ms"] == pytest.approx(0.0, abs=1e-3)


class TestPreconditions:
    def test_two_nodes_raise(self):
        with pytest.raises(ValueError, match="3 nodes"):
            PT.solve(SQUARE[:2], [0.0, 1.0], "blast", temp_c=T)

    def test_mismatched_lengths_raise(self):
        with pytest.raises(ValueError, match="3 nodes"):
            PT.solve(SQUARE, [0.0, 1.0, 2.0], "blast", temp_c=T)

    def test_a_non_finite_arrival_raises_rather_than_returning_nan(self):
        t = _arrivals(SQUARE, (260.0, 40.0))
        t[2] = float("nan")
        with pytest.raises(ValueError, match="non-finite"):
            PT.solve(SQUARE, t, "blast", temp_c=T)


def test_solves_at_a_real_utc_epoch_not_just_a_small_t0():
    """Regression: arrivals are absolute epoch seconds, where float64 spacing is 2.4e-7 s. The
    2-point Jacobian perturbs a position by ~4e-9 s of range, which vanishes in that rounding, so
    least_squares saw an exactly-zero Jacobian and returned the raw grid cell. Measured before the
    fix: 0.001 m error at t0=1e3, 6.58 m at real UTC. A test that only ever uses a small t0 cannot
    see it -- which is why this one uses both."""
    c = SW.sound_speed(20.0)
    nodes = [(0., 0.), (200., 0.), (200., 200.), (0., 200.), (100., 100.)]
    src = (263.7, 41.9)                     # deliberately off the 10 m grid
    errs = {}
    for t0 in (1000.0, 1_757_000_000.0):
        arr = [t0 + math.hypot(src[0] - n[0], src[1] - n[1]) / c for n in nodes]
        r = PT.solve(nodes, arr, "blast", temp_c=20.0)
        errs[t0] = math.hypot(r["east_m"] - src[0], r["north_m"] - src[1])
        assert r["t0_utc_s"] == pytest.approx(t0, abs=1e-3), "t0 must survive recentring"
    assert errs[1_757_000_000.0] < 0.1, "real-epoch error %.3f m" % errs[1_757_000_000.0]
    assert errs[1_757_000_000.0] == pytest.approx(errs[1000.0], abs=0.05), \
        "accuracy must not depend on the epoch the operator happens to run at"
