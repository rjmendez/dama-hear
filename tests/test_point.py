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
# ⚠️Every fixture runs on a real epoch, not a tidy t0=1000. `arrivals` are absolute seconds and
# the result key is t0_utc_s, so a small t0 tests a regime no caller is ever in -- and it is
# precisely where the float64-cancellation bug pinned at the bottom of this file hides.
T0_UTC = 1_757_000_000.0
SQUARE = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
LINE = [(0.0, 0.0), (50.0, 0.0), (100.0, 0.0), (150.0, 0.0)]


def _rot(pts, deg):
    th = math.radians(deg)
    ca, sa = math.cos(th), math.sin(th)
    return [(e * ca - n * sa, e * sa + n * ca) for e, n in pts]


# The same fence line at a bearing. LINE is axis-aligned, which lets any north-spread proxy stand
# in for PL.linearity and pass the whole file; this one is degenerate on neither axis.
LINE_30 = _rot(LINE, 30.0)


CRACK_ARRAY = [(-200.0, -200.0), (200.0, 200.0), (175.0, -150.0), (-150.0, 175.0)]
CRACK_BEARING_DEG, CRACK_OFFSET_M, CRACK_V_MPS = 20.0, 8.0, 900.0


def _arrivals(P, source, t0=T0_UTC, temp_c=T):
    c = SW.sound_speed(temp_c)
    s = np.asarray(source, float)[:2]
    return [t0 + float(np.linalg.norm(s - np.asarray(p, float)[:2])) / c for p in P]


def _crack_fitted_as_a_blast(P):
    """Shock arrivals from a real M855 track, mislabelled. Returns the fit and the track point
    nearest the array -- the closest thing to a 'right answer' a point model could have given."""
    c = SW.sound_speed(T)
    br = math.radians(CRACK_BEARING_DEG)
    t = [T0_UTC + SW.shock_time(p, br, CRACK_OFFSET_M, CRACK_V_MPS, c) for p in P]
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
        assert got["t0_utc_s"] == pytest.approx(T0_UTC, abs=0.005)

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

    # Deterministic +/-0.2 ms of clock disagreement -- an RNG here would make a geometry test
    # flaky for no gain.
    JITTER_S = (0.0002, -0.00015, 0.0001, -0.0002)

    @pytest.mark.parametrize("src", [(260.0, 40.0), (263.7, 41.9)])
    def test_node_order_still_does_not_matter_when_the_arrivals_disagree(self, src):
        """The test above cannot fail for the regression it names. Noise-free arrivals are exactly
        consistent, so `return (r - r[0])[1:]` -- a reference node creeping back into _residual --
        shares the same exact zero minimum as the marginalised cost and passes it. Inconsistent
        data is what separates the two. Run here: that mutant moves the fit 3.64 m (on-grid src)
        and 3.92 m (off-grid) across these rotations; the shipped code moves 8.8e-7 m."""
        c = SW.sound_speed(T)
        t = [T0_UTC + math.hypot(src[0] - p[0], src[1] - p[1]) / c + j
             for p, j in zip(SQUARE, self.JITTER_S)]
        base = PT.solve(SQUARE, t, "blast", temp_c=T)
        assert base["rms_residual_ms"] > 0.1, "arrivals must disagree, or this proves nothing"
        for k in range(1, len(SQUARE)):
            got = PT.solve(SQUARE[k:] + SQUARE[:k], t[k:] + t[:k], "blast", temp_c=T)
            assert got["east_m"] == pytest.approx(base["east_m"], abs=1e-3)
            assert got["north_m"] == pytest.approx(base["north_m"], abs=1e-3)
        rev = PT.solve(list(reversed(SQUARE)), list(reversed(t)), "blast", temp_c=T)
        assert rev["east_m"] == pytest.approx(base["east_m"], abs=1e-3)
        assert rev["north_m"] == pytest.approx(base["north_m"], abs=1e-3)

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

    def test_a_fence_line_at_a_bearing_is_refused_too(self):
        """The verdict must come from PL.linearity's SVD, not from a proxy that only happens to
        agree on an axis-aligned fixture. `obs = P[:, 1].std() > 1e-9` -- no relation to the
        contract at all -- passes every other test in this file, and would then fit a fence line
        at any bearing but due east confidently: the exact failure this class exists to prevent."""
        assert np.asarray(LINE_30)[:, 1].std() > 1.0, "if it were axis-aligned this proves nothing"
        got = PT.solve(LINE_30, _arrivals(LINE_30, self.SRC), "blast", temp_c=T)
        assert got["position_observable"] is False
        assert got["east_m"] is None and got["north_m"] is None
        assert "UNOBSERVABLE" in got["note"]
        assert got["linearity"] < PL.COLLINEAR_LINEARITY

    def test_the_mirror_really_is_indistinguishable(self):
        """Pins the reason for the refusal. If this ever fails, the verdict is over-cautious."""
        # t0=0: this is a statement about ranges, and at a real epoch float64 spacing (2.4e-7 s)
        # would swamp abs=1e-9 and make the tolerance a lie.
        a = _arrivals(LINE, self.SRC, t0=0.0)
        b = _arrivals(LINE, self.MIRROR, t0=0.0)
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
        Four nodes at least make the residual scream -- 188 ms, run here."""
        got, closest = _crack_fitted_as_a_blast(CRACK_ARRAY)
        assert got["position_observable"] is True, "it does not hesitate; that is the problem"
        fitted = np.array([got["east_m"], got["north_m"]])
        assert float(np.linalg.norm(fitted - closest)) > 50.0
        assert got["residual_is_meaningful"] is True and got["rms_residual_ms"] > 10.0

    def test_at_three_nodes_the_same_lie_is_silent(self):
        """The worst case in the repo, and it is arithmetic, not bad luck: the same crack fed to
        three nodes lands 145 m off the track (run here) at rms_residual_ms 0.00001. Two
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
    for t0 in (1000.0, T0_UTC):
        arr = [t0 + math.hypot(src[0] - n[0], src[1] - n[1]) / c for n in nodes]
        r = PT.solve(nodes, arr, "blast", temp_c=20.0)
        errs[t0] = math.hypot(r["east_m"] - src[0], r["north_m"] - src[1])
        assert r["t0_utc_s"] == pytest.approx(t0, abs=1e-3), "t0 must survive recentring"
    assert errs[T0_UTC] < 0.1, "real-epoch error %.3f m" % errs[T0_UTC]
    assert errs[T0_UTC] == pytest.approx(errs[1000.0], abs=0.05), \
        "accuracy must not depend on the epoch the operator happens to run at"
