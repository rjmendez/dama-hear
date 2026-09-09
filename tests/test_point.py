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


# Five nodes, not four. In 2D, four was where a residual began to carry information; the third
# unknown moved that to five, so the "the residual screams" test needs one more node than it did
# to make the same point. The original four are unchanged so the geometry is comparable.
CRACK_ARRAY = [(-200.0, -200.0), (200.0, 200.0), (175.0, -150.0), (-150.0, 175.0), (0.0, -220.0)]
CRACK_BEARING_DEG, CRACK_OFFSET_M, CRACK_V_MPS = 20.0, 8.0, 900.0


def _p3(v):
    """Pad to (e, n, u); a 2-vector means ground level. The helper used to slice [:2] instead,
    which quietly made every fixture's height zero AND made the test blind to a solver that
    ignored height -- the assertion and the thing it was checking shared the same bug."""
    a = np.asarray(v, float)
    return np.concatenate([a, np.zeros(3 - len(a))]) if len(a) < 3 else a[:3]


def _arrivals(P, source, t0=T0_UTC, temp_c=T):
    """TRUE 3D slant ranges. Sound travels through the air, not across a map."""
    c = SW.sound_speed(temp_c)
    s = _p3(source)
    return [t0 + float(np.linalg.norm(s - _p3(p))) / c for p in P]


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

    def test_differential_node_height_changes_the_answer_because_it_is_no_longer_sliced(self):
        """The inverse of what this test used to assert. It previously pinned that a 3-vector was
        SLICED -- a deliberate statement that the solver was 2D. Height is a distance now.

        It has to be DIFFERENTIAL height. Lifting the whole array uniformly is invisible in the
        horizontal: it lengthens every slant range by the same amount and _residual subtracts the
        mean, so the common part cancels exactly. Nodes at DIFFERENT heights do not cancel, and
        that is the case where slicing [:2] throws away real information.
        """
        P3 = [(e, n, u) for (e, n), u in zip(SQUARE, (0.0, 12.0, 3.0, 25.0))]
        t = _arrivals(P3, self.SRC)                     # arrivals from the real, uneven array
        uneven = PT.solve(P3, t, "blast", temp_c=T)
        flat = PT.solve(SQUARE, t, "blast", temp_c=T)   # same times, heights thrown away
        assert uneven["east_m"] == pytest.approx(self.SRC[0], abs=0.5)
        assert uneven["north_m"] == pytest.approx(self.SRC[1], abs=0.5)
        moved = math.hypot(uneven["east_m"] - flat["east_m"], uneven["north_m"] - flat["north_m"])
        assert moved > 1.0, "ignoring node height would have to move the answer to matter"

    def test_a_broken_node_plane_makes_height_observable(self):
        """Coplanar nodes cannot separate a source above the plane from its reflection below.
        Spread the nodes in height and they can -- which is the whole reason for carrying u."""
        src = (260.0, 40.0, 55.0)
        flat = [(e, n, 0.0) for e, n in SQUARE]
        broken = [(e, n, u) for (e, n), u in zip(SQUARE, (0.0, 30.0, 5.0, 45.0))]
        a = PT.solve(flat, _arrivals(flat, src), "blast", temp_c=T)
        assert a["up_observable"] is False
        assert a["up_mirror_m"] == pytest.approx(-a["up_m"], abs=0.5)
        assert "HEIGHT is unobservable" in a["note"]
        b = PT.solve(broken, _arrivals(broken, src), "blast", temp_c=T)
        assert b["up_observable"] is True
        assert b["up_m"] == pytest.approx(55.0, abs=1.0)
        assert b["up_mirror_m"] is None
        assert b["note"] is None

    def test_c_comes_from_shockwave_not_a_literal(self):
        t = _arrivals(SQUARE, self.SRC)
        got = PT.solve(SQUARE, t, "blast", temp_c=23.0)
        assert got["sound_speed_mps"] == pytest.approx(345.238, abs=1e-3)


class TestRedundancy:
    """FOUR nodes give three equations for three unknowns. The residual is zero BY CONSTRUCTION
    and proves nothing -- which is exactly why a perfect-looking residual there is dangerous.

    These counts moved by one when the solver became 3D. In 2D it was three nodes that fitted
    exactly and four that carried information; the third unknown costs one more node at both
    ends. The hazard is unchanged and so is what this class is guarding.
    """

    SRC = (260.0, 40.0, 0.0)

    def test_four_nodes_look_perfect_and_mean_nothing(self):
        got = PT.solve(SQUARE, _arrivals(SQUARE, self.SRC), "blast", temp_c=T)
        assert got["n_equations"] == 3
        assert got["n_unknowns"] == 3
        assert got["residual_is_meaningful"] is False
        # abs=1e-3 ms == 1 us: noise-free synthetic arrivals, so anything above the refine's own
        # convergence floor would mean the model is wrong, not that the data is.
        assert got["rms_residual_ms"] == pytest.approx(0.0, abs=1e-3)

    def test_five_nodes_are_where_a_residual_starts_meaning_something(self):
        P = SQUARE + [(75.0, 40.0)]
        got = PT.solve(P, _arrivals(P, self.SRC), "blast", temp_c=T)
        assert got["n_equations"] == 4
        assert got["residual_is_meaningful"] is True

    def test_three_nodes_are_refused_outright(self):
        """Underdetermined in 3D: two equations, three unknowns. Refusing beats returning a
        confident coordinate off a fit that cannot constrain one of its own axes."""
        P = SQUARE[:3]
        with pytest.raises(ValueError, match="4 nodes"):
            PT.solve(P, _arrivals(P, self.SRC), "blast", temp_c=T)


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
        Five nodes at least make the residual scream -- measured here."""
        got, closest = _crack_fitted_as_a_blast(CRACK_ARRAY)
        assert got["position_observable"] is True, "it does not hesitate; that is the problem"
        fitted = np.array([got["east_m"], got["north_m"]])
        assert float(np.linalg.norm(fitted - closest)) > 50.0
        assert got["residual_is_meaningful"] is True and got["rms_residual_ms"] > 10.0

    def test_at_the_minimum_node_count_the_lie_is_no_longer_silent(self):
        """This was the worst case in the repo. In 2D, a crack fed to the minimum node count
        landed 145 m off the track at rms_residual_ms 0.00001: two equations, two unknowns, and
        nothing in the result said the model was wrong.

        Going 3D removed that particular silence. The third unknown is not free -- a real source
        has a real height, and the refine is bounded to the region the grid actually searched --
        so cone arrivals can no longer be absorbed by sliding the fit somewhere convenient. The
        solver now pins at the edge of the search box and says so twice: `at_search_bound` and a
        160 ms residual.

        This is NOT a claim that a minimum-count fit is now trustworthy in general. Data the model
        CAN produce still fits exactly at four nodes with a meaningless residual -- that hazard is
        unchanged and is pinned in TestRedundancy. What changed is that this specific silent
        failure now announces itself.
        """
        got, closest = _crack_fitted_as_a_blast(CRACK_ARRAY[:4])
        fitted = np.array([got["east_m"], got["north_m"]])
        assert float(np.linalg.norm(fitted - closest)) > 50.0
        assert got["at_search_bound"] is True
        assert got["rms_residual_ms"] > 100.0
        assert "pinned at the edge" in got["note"]


class TestPreconditions:
    def test_two_nodes_raise(self):
        with pytest.raises(ValueError, match="4 nodes"):
            PT.solve(SQUARE[:2], [0.0, 1.0], "blast", temp_c=T)

    def test_mismatched_lengths_say_so_rather_than_blaming_the_node_count(self):
        """4 positions and 3 arrivals is a length bug, not a "you need more nodes" bug. Reporting
        it as the latter -- "need >= 4 nodes ... got 4" -- sends the reader to count nodes they
        already have enough of."""
        with pytest.raises(ValueError, match="same length"):
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


class TestDopSaysWhichEstimationProblemItPriced:
    """⚠️`dop` and `pdop` are dilutions of DIFFERENT fits and sat unlabelled in one dict.

    Each number is recomputed here from the geometry, never asserted from memory."""

    NODES = [(0., 0., 0.), (40., 0., 0.), (0., 40., 0.), (40., 40., 0.)]
    SRC = (30., 30., 2.)

    def _solve(self, **kw):
        c = SW.sound_speed(T)
        arr = [T0_UTC + math.dist(self.SRC, n) / c for n in self.NODES]
        return PT.solve(self.NODES, arr, "blast", temp_c=T, **kw)

    def test_the_short_name_reads_far_better_than_the_fit_it_sits_beside(self):
        """THE DEFECT, measured. On a ground array the vertical is the weak axis, so a figure
        that never priced it is optimistic by two orders of magnitude -- and it is the figure
        whose name a consumer reaches for first."""
        r = self._solve()
        assert r["dop"] < 2.0                      # ~1.04: looks excellent
        assert r["pdop"] > 100.0                   # ~107.2: what this solve actually cost
        assert r["pdop"] / r["dop"] > 50.0, "the gap is the whole reason for the labels"
        # The labels are what make the two comparable at all.
        assert r["dop_unknowns"] == 2
        assert r["pdop_unknowns"] == 3
        assert r["n_unknowns"] == r["pdop_unknowns"], "this fit solved three unknowns"

    def test_dop_prices_this_fit_is_false_when_the_height_was_estimated(self):
        r = self._solve()
        assert r["dop_prices_this_fit"] is False
        assert r["up_assumed_m"] is None

    def test_dop_prices_this_fit_is_true_only_when_the_height_was_declared(self):
        """A DECLARED height really does leave two unknowns, and that is the one case where the
        2-unknown figure describes the estimate."""
        r = self._solve(fixed_up_m=2.0)
        assert r["dop_prices_this_fit"] is True
        assert r["n_unknowns"] == r["dop_unknowns"] == 2

    def test_the_singular_flag_names_its_own_quantity(self):
        """⚠️At three nodes `dop` is finite while the old `dop_singular` was True -- it was
        reporting the THREE-unknown fit's refusal under a name that said `dop`. A consumer
        checking it before trusting `dop` was wrong in both directions."""
        c = SW.sound_speed(T)
        n3 = self.NODES[:3]
        arr = [T0_UTC + math.dist(self.SRC, n) / c for n in n3]
        r = PT.solve(n3, arr, "blast", temp_c=T, fixed_up_m=2.0)
        assert math.isfinite(r["dop"]), "2 unknowns, 3 nodes: this is a real number"
        assert r["dop3_singular"] is True, "3 unknowns is what refused"
        # The correctly-named key and the legacy alias still agree, so nothing silently changed
        # meaning; only the name became honest.
        assert r["dop3_singular"] == r["dop_singular"]
        assert r["dop3_dof"] == r["dop_dof"]

    def test_every_dilution_key_carries_an_unknown_count(self):
        """A dilution figure with no dimension is the thing this class exists to prevent, so a
        future key added without one fails here."""
        r = self._solve()
        for k in ("dop", "pdop"):
            assert "%s_unknowns" % k in r, "%s reports no unknown count" % k
        assert {r["dop_unknowns"], r["pdop_unknowns"]} == {2, 3}
