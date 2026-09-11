"""Point-source solver. The cone gate and the collinear verdict are the point of this file."""
import json
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


# --------------------------------------------------------- weighted least squares
# MEASURED over the 8,720 phone rows that state sync_sigma_ns in the 2026-09-11 pool snapshot
# (9,356 phone rows, 7,758 node rows). The maximum is why the degenerate case below is real data
# and not a hypothetical: 16.6% of stated phone sigmas are outside the 129.4 us arrival budget,
# and the worst single row states 3.23 s = 1110 m of arrival uncertainty.
SIGMA_PHONE_MEDIAN_S = 106076.28471886144e-9
SIGMA_PHONE_MAX_S = 3233133158.913254e-9
SIGMA_PPS_S = 40e-9                         # hear_tdoa.py:71, PPS is 25-40 ns

CORPUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                      "tdoa-solved-events-2026-09-11.json")

# The keys this change ADDS. They are None without sigmas and populated with them, so they are
# the one thing that legitimately differs between an unweighted call and an equal-sigma one.
NEW_KEYS = frozenset({"chi2", "chi2_reduced", "n_effective_nodes", "n_counting_nodes",
                      "weights_degenerate", "weight_note", "sigma_s", "relative_weights"})


def _corpus():
    with open(CORPUS) as fh:
        return json.load(fh)


def _unfloat(v):
    """The fixture stores non-finite floats as strings; bare Infinity is not valid JSON."""
    if isinstance(v, str):
        return {"inf": math.inf, "-inf": -math.inf, "nan": math.nan}.get(v, v)
    return v


def _same_bits(a, b):
    a, b = _unfloat(a), _unfloat(b)
    if isinstance(a, float) and isinstance(b, float):
        return a.hex() == b.hex() or (math.isnan(a) and math.isnan(b))
    return type(a) is type(b) and a == b


class TestEqualSigmasChangeNothing:
    """⚠️THE FIRST THING TO CHECK. A weighted solver that shifts the unweighted answer has
    changed every position this array ever published, and no new capability pays for that.
    Pinned on the REAL corpus events -- the two the hear-tdoa CronJob actually solved -- rather
    than a synthetic array, because a synthetic fixture can be built to agree with anything.
    """

    def test_the_fixture_is_the_configuration_the_cronjob_runs(self):
        """⚠️tools/hear_tdoa.py DERIVES margin_s from the array's own pair bounds and does NOT
        use associate.MARGIN_S. Measuring at the library default measures a configuration
        nothing runs. This pins which one produced the events below."""
        prov = _corpus()["provenance"]
        assert prov["margin_source"] == "derived"
        assert prov["margin_s"] == pytest.approx(0.00860753054387325, rel=1e-12)
        assert prov["margin_s"] < 0.030, "the library default is 3.5x this and is not what ran"

    def test_the_fixture_carries_real_three_node_events(self):
        evs = _corpus()["events"]
        assert len(evs) == 2
        for ev in evs:
            assert len(ev["node_ids"]) == 3 and len(ev["arrivals_utc_s"]) == 3
            assert ev["source_class"] == "blast"
            assert set(ev["node_names"]) == {"nyquist", "mach", "rankine"}

    @pytest.mark.parametrize("sigma", [None, SIGMA_PPS_S, SIGMA_PHONE_MEDIAN_S, 1.0])
    def test_equal_sigmas_are_bit_for_bit_the_unweighted_call(self, sigma):
        """THE requirement, at three equal sigmas spanning eight orders of magnitude. Compared by
        float.hex(), so one ulp fails.

        ⚠️BOTH SIDES ARE COMPUTED HERE, deliberately, rather than against the stored referent.
        A least_squares fit is bit-reproducible within an environment and NOT across BLAS
        builds -- this fixture records a one-ulp north_m difference between the machine that ran
        the CronJob and this one, on identical code -- and CI runs a second python/numpy pin set.
        A stored-bits assertion would fail there for a reason that is not a regression. What is
        environment-independent, and what this change is actually judged on, is that stating
        equal sigmas costs nothing relative to stating none in the SAME interpreter."""
        for ev in _corpus()["events"]:
            P = np.array(ev["positions_enu_m"], float)
            base = PT.solve(P, ev["arrivals_utc_s"], ev["source_class"],
                            temp_c=ev["temp_c"], fixed_up_m=ev["fixed_up_m"])
            kw = {} if sigma is None else {"sigmas": [sigma] * 3}
            got = PT.solve(P, ev["arrivals_utc_s"], ev["source_class"],
                           temp_c=ev["temp_c"], fixed_up_m=ev["fixed_up_m"], **kw)
            bad = [(k, got[k], v) for k, v in base.items()
                   if k not in NEW_KEYS and not _same_bits(got[k], v)]
            assert not bad, "%s drifted at sigma %r: %r" % (ev["event_key"], sigma, bad)

    def test_the_unweighted_answer_is_still_what_origin_main_published(self):
        """The other half: the arithmetic above could be self-consistently WRONG. This one checks
        the stored origin/main output -- but at a tolerance, for the BLAS reason in the test
        above. A real regression in this solver is metres (the grid step alone is 10 m); the
        cross-machine noise it must tolerate is an ulp, which is 1.4e-14 m here. Ten orders of
        magnitude separate the two, so there is no tolerance worth arguing about."""
        for ev in _corpus()["events"]:
            got = PT.solve(np.array(ev["positions_enu_m"], float), ev["arrivals_utc_s"],
                           ev["source_class"], temp_c=ev["temp_c"],
                           fixed_up_m=ev["fixed_up_m"])
            want = ev["solution_origin_main"]
            for k, v in want.items():
                a, b = got[k], _unfloat(v)
                if isinstance(b, float) and math.isfinite(b):
                    assert a == pytest.approx(b, rel=1e-9, abs=1e-9), "%s: %r vs %r" % (k, a, b)
                else:
                    # bools, counts, notes and the inf DOPs are exact or they are broken
                    assert _same_bits(a, v), "%s: %r vs %r" % (k, a, v)

    def test_the_new_keys_are_additive_and_never_replace_one(self):
        """A caller reading the old keys must not find one missing or renamed."""
        ev = _corpus()["events"][0]
        got = PT.solve(np.array(ev["positions_enu_m"], float), ev["arrivals_utc_s"],
                       ev["source_class"], temp_c=ev["temp_c"], fixed_up_m=ev["fixed_up_m"])
        assert set(ev["solution_origin_main"]).issubset(set(got))

    def test_an_unweighted_call_states_no_sigma_rather_than_a_default(self):
        ev = _corpus()["events"][0]
        got = PT.solve(np.array(ev["positions_enu_m"], float), ev["arrivals_utc_s"],
                       ev["source_class"], temp_c=ev["temp_c"], fixed_up_m=ev["fixed_up_m"])
        for k in ("sigma_s", "relative_weights", "chi2", "chi2_reduced",
                  "n_effective_nodes", "weights_degenerate", "weight_note"):
            assert got[k] is None, "%s must be None when no sigma was stated" % k


# Five nodes with the plane deliberately broken by the fifth, so a 3D fit is over-determined:
# n_eq = 4 against n_unk = 3. SQUARE alone is coplanar AND exactly determined, which is the two
# degeneracies this section exists to get past.
WEIGHT_ARRAY = [(0.0, 0.0, 0.0), (100.0, 0.0, 0.0), (100.0, 100.0, 0.0),
                (0.0, 100.0, 0.0), (50.0, -70.0, 12.0)]
WEIGHT_SRC = (130.0, 40.0, 0.0)


def _wa_arrivals(temp_c=T):
    c = SW.sound_speed(temp_c)
    s = _p3(WEIGHT_SRC)
    return [T0_UTC + float(np.linalg.norm(s - _p3(p))) / c for p in WEIGHT_ARRAY]


class TestResidualIsMeaningful:
    """⚠️THE WHOLE POINT. An exactly-determined fit drives its residual to ~0 BY CONSTRUCTION,
    so reading it as agreement is reading the arithmetic back. The flag says which it is, and
    chi2_reduced is what makes the number comparable to the uncertainties that produced it."""

    def test_exactly_determined_says_so_and_the_residual_is_zero_anyway(self):
        arr = _wa_arrivals()
        got = PT.solve(WEIGHT_ARRAY[:4], arr[:4], "blast", temp_c=T,
                       sigmas=[SIGMA_PHONE_MEDIAN_S] * 4)
        assert (got["n_equations"], got["n_unknowns"], got["residual_dof"]) == (3, 3, 0)
        assert got["residual_is_meaningful"] is False
        assert got["chi2_reduced"] is None, "there is no dof to divide by"
        # MEASURED 6.8e-07, and "~0" has to be said in chi-square rather than in milliseconds.
        assert got["chi2"] < 1e-3, got["chi2"]

    def test_chi2_has_a_numerical_floor_and_pps_is_under_it(self):
        """⚠️THE CAVEAT ON EVERYTHING ABOVE, and it is not a regression -- rms_residual_ms is
        bit-identical to origin/main here, the floor was always there and a chi-square is simply
        the first thing that made it visible.

        least_squares leaves ~5.07e-08 s rms on arrivals that are EXACT by construction: 17 um of
        range, nothing to do with the array. Squared against a stated sigma that is smaller than
        it, that convergence noise becomes the whole chi-square. MEASURED on this fixture:
        6.8e-07 at the corpus's 106 us phone median, 4.81 at a 40 ns PPS figure. So a PPS-class
        chi-square is a statement about scipy, and the crossover is near 88 ns."""
        arr = _wa_arrivals()
        plain = PT.solve(WEIGHT_ARRAY[:4], arr[:4], "blast", temp_c=T)
        floor_s = plain["rms_residual_ms"] / 1000.0
        assert floor_s == pytest.approx(5.067e-8, rel=0.2), \
            "the convergence floor moved: %r s" % floor_s

        at_pps = PT.solve(WEIGHT_ARRAY[:4], arr[:4], "blast", temp_c=T, sigmas=[SIGMA_PPS_S] * 4)
        assert at_pps["chi2"] > 1.0, \
            "a 40 ns sigma is below the floor, so chi2 must NOT look clean: %r" % at_pps["chi2"]
        at_phone = PT.solve(WEIGHT_ARRAY[:4], arr[:4], "blast", temp_c=T,
                            sigmas=[SIGMA_PHONE_MEDIAN_S] * 4)
        assert at_phone["chi2"] < 1e-3
        # chi2 scales as 1/sigma^2 over the same arrivals, which is what makes the crossover
        # predictable rather than a surprise.
        assert at_pps["chi2"] == pytest.approx(
            at_phone["chi2"] * (SIGMA_PHONE_MEDIAN_S / SIGMA_PPS_S) ** 2, rel=1e-6)

    def test_a_fifth_receiver_over_determines_it_and_the_residual_can_falsify(self):
        arr = _wa_arrivals()
        got = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T, sigmas=[SIGMA_PHONE_MEDIAN_S] * 5)
        assert (got["n_equations"], got["n_unknowns"], got["residual_dof"]) == (4, 3, 1)
        assert got["residual_is_meaningful"] is True
        assert got["chi2_reduced"] is not None

    def test_chi2_counts_sigmas_and_not_milliseconds(self):
        """The same millisecond error is damning at a PPS clock and unremarkable at a phone's.
        A raw rms cannot say that; this is the quantity that can."""
        arr = _wa_arrivals()
        arr[0] += 3.0 * SIGMA_PHONE_MEDIAN_S                 # push one receiver by 3 sigma
        loose = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T,
                         sigmas=[SIGMA_PHONE_MEDIAN_S] * 5)
        tight = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T,
                         sigmas=[SIGMA_PHONE_MEDIAN_S / 100.0] * 5)
        assert loose["rms_residual_ms"] == pytest.approx(tight["rms_residual_ms"], rel=1e-6), \
            "the arrivals are identical, so the raw ms cannot distinguish the two"
        assert tight["chi2_reduced"] == pytest.approx(1e4 * loose["chi2_reduced"], rel=1e-6), \
            "a 100x tighter claimed clock makes the same error 1e4x more surprising"
        # MEASURED 3.985 at dof 1. A 3-sigma disagreement that the fit cannot absorb lands near
        # 3^2, which is the whole reason this is worth reporting: 3.985 is a number an operator
        # can argue with, where "rms 0.10 ms" is not.
        assert 2.0 < loose["chi2_reduced"] < 9.0, loose["chi2_reduced"]

    def test_a_consistent_fit_lands_near_one_reduced_chi_square(self):
        """Sanity on the SCALE, not just the ordering: perturb every receiver by ~1 sigma and
        the reduced chi-square should be order 1 rather than order 1e6."""
        arr = _wa_arrivals()
        for i, k in enumerate((0.9, -1.1, 0.8, -0.7, 1.2)):
            arr[i] += k * SIGMA_PHONE_MEDIAN_S
        got = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T, sigmas=[SIGMA_PHONE_MEDIAN_S] * 5)
        assert 0.01 < got["chi2_reduced"] < 100.0, got["chi2_reduced"]

    def test_the_residual_only_falsifies_where_the_geometry_has_leverage(self):
        """⚠️A CHI-SQUARE IS NOT A UNIFORM ALARM, and a reader who takes a low one as "every
        receiver agrees" will be wrong on the weak axis. MEASURED on this array: the same
        3-sigma push scores 3.985 on receiver 0, which sits in the node plane, and 0.0015 on
        receiver 4, the only one off it -- 2,700x less. The fit absorbs receiver 4's error by
        moving `up` 1.39 m, because height is the axis this array barely observes. Over-determined
        means the residual CAN falsify, not that it will."""
        arr = _wa_arrivals()
        strong = list(arr); strong[0] += 3.0 * SIGMA_PHONE_MEDIAN_S
        weak = list(arr); weak[4] += 3.0 * SIGMA_PHONE_MEDIAN_S
        a = PT.solve(WEIGHT_ARRAY, strong, "blast", temp_c=T, sigmas=[SIGMA_PHONE_MEDIAN_S] * 5)
        b = PT.solve(WEIGHT_ARRAY, weak, "blast", temp_c=T, sigmas=[SIGMA_PHONE_MEDIAN_S] * 5)
        assert a["chi2_reduced"] > 100.0 * b["chi2_reduced"]
        assert abs(b["up_m"]) > 1.0, "the weak-axis error is absorbed into height, not reported"


class TestTheExtremes:
    """⚠️A receiver with an enormous sigma must not silently dominate or silently vanish. The
    corpus states sigmas up to 3.23 s, so both ends below are real numbers off the pool."""

    def test_a_huge_sigma_does_not_move_the_fit(self):
        """It is weighted out, not averaged in: the 5-receiver answer is the 4-receiver answer."""
        arr = _wa_arrivals()
        four = PT.solve(WEIGHT_ARRAY[:4], arr[:4], "blast", temp_c=T,
                        sigmas=[SIGMA_PHONE_MEDIAN_S] * 4)
        five = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T,
                        sigmas=[SIGMA_PHONE_MEDIAN_S] * 4 + [SIGMA_PHONE_MAX_S])
        assert five["east_m"] == pytest.approx(four["east_m"], abs=0.01)
        assert five["north_m"] == pytest.approx(four["north_m"], abs=0.01)
        assert five["relative_weights"][4] < 1e-4

    def test_a_huge_sigma_does_not_vanish_silently(self):
        """⚠️REGRESSION. Weighting a receiver to 3.3e-05 removes its equation, so a 5-receiver
        event is really a 4-receiver one and its residual is exactly determined again. Kish's
        effective count alone cannot carry this: it scores 4.000000002 here, and `n_eff - 1 > 3`
        is TRUE on that float noise. The counting rule is what refuses it."""
        arr = _wa_arrivals()
        got = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T,
                       sigmas=[SIGMA_PHONE_MEDIAN_S] * 4 + [SIGMA_PHONE_MAX_S])
        assert got["n_nodes"] == 5 and got["n_equations"] == 4
        assert got["n_effective_nodes"] == pytest.approx(4.0, abs=1e-6)
        assert got["n_counting_nodes"] == 4
        assert got["residual_is_meaningful"] is False, \
            "4 counting receivers against 3 unknowns is exactly determined"
        assert got["weight_note"] and "does not widen this fit" in got["weight_note"]

    def test_a_tiny_sigma_does_not_dominate_silently(self):
        """The mirror image: one receiver claims 40 ns beside four at 3.23 s, so the fit is
        effectively its alone. The position is still returned -- refusing is an admission
        decision, not a solver one -- but nothing about it reads as agreement."""
        arr = _wa_arrivals()
        got = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T,
                       sigmas=[SIGMA_PHONE_MAX_S] * 4 + [SIGMA_PPS_S])
        assert got["n_effective_nodes"] == pytest.approx(1.0, abs=1e-6)
        assert got["n_counting_nodes"] == 1
        assert got["weights_degenerate"] is True
        assert got["residual_is_meaningful"] is False
        assert "under-determined" in got["weight_note"]

    def test_a_merely_worse_receiver_keeps_its_vote(self):
        """⚠️The floor has to be checked from BOTH sides or it is a rule that only ever refuses.
        A receiver 5x worse than the best is still a receiver; one 20x worse is not counted.

        The crossing is NOT exact at 10.000x: 1/10.0 is 0x1.9999999999999p-4, one ulp under the
        literal 0.1, so a sigma ratio of exactly ten falls on the refusing side. Nothing real
        lands there and contorting the comparison to move it would be worse than saying so."""
        arr = _wa_arrivals()
        keeps = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T,
                         sigmas=[SIGMA_PHONE_MEDIAN_S] * 4 + [SIGMA_PHONE_MEDIAN_S * 5.0])
        assert keeps["relative_weights"][4] == pytest.approx(0.2, rel=1e-12)
        assert keeps["n_counting_nodes"] == 5
        assert keeps["residual_is_meaningful"] is True
        assert keeps["weight_note"] is None

        drops = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T,
                         sigmas=[SIGMA_PHONE_MEDIAN_S] * 4 + [SIGMA_PHONE_MEDIAN_S * 20.0])
        assert drops["n_counting_nodes"] == 4
        assert drops["residual_is_meaningful"] is False
        assert drops["weight_note"] is not None


class TestASigmaIsNeverInvented:
    """Requirement: absent is not zero and it is not the class figure. A stale class constant
    overriding what a detection said about itself is the defect this whole change answers, and
    re-introducing it inside the solver would be the same bug one layer down."""

    def test_a_partially_stated_vector_raises_rather_than_being_filled_in(self):
        arr = _wa_arrivals()
        sig = [SIGMA_PHONE_MEDIAN_S] * 5
        sig[2] = None
        with pytest.raises(ValueError, match="will not invent one"):
            PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T, sigmas=sig)

    def test_a_wrong_length_vector_raises(self):
        arr = _wa_arrivals()
        with pytest.raises(ValueError, match="one entry per receiver"):
            PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T, sigmas=[SIGMA_PPS_S] * 4)

    @pytest.mark.parametrize("bad", [0.0, -1e-6, float("nan"), float("inf")])
    def test_a_sigma_that_is_not_a_positive_duration_raises(self, bad):
        """Zero is the dangerous one: it means infinite confidence and would make that receiver
        the only one that counts, silently."""
        arr = _wa_arrivals()
        sig = [SIGMA_PHONE_MEDIAN_S] * 5
        sig[1] = bad
        with pytest.raises(ValueError, match="finite and > 0"):
            PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T, sigmas=sig)


class TestWeightingActuallyWeights:
    """Equal sigmas changing nothing is only half the claim; UNEQUAL sigmas must change the
    right thing, or this is an elaborate no-op."""

    def test_the_fit_moves_toward_the_receiver_that_claims_the_better_clock(self):
        arr = _wa_arrivals()
        arr[4] += 0.004                       # 4 ms = 1.37 m of error on one receiver
        flat = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T, sigmas=[SIGMA_PHONE_MEDIAN_S] * 5)
        # tell the solver that receiver 4 is the one with the bad clock
        told = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T,
                        sigmas=[SIGMA_PHONE_MEDIAN_S] * 4 + [0.004])
        clean = PT.solve(WEIGHT_ARRAY[:4], arr[:4], "blast", temp_c=T,
                         sigmas=[SIGMA_PHONE_MEDIAN_S] * 4)
        def err(r):
            return math.hypot(r["east_m"] - clean["east_m"], r["north_m"] - clean["north_m"])
        assert err(told) < err(flat), \
            "declaring which receiver is worse must pull the fit back toward the good ones"

    def test_node_order_still_does_not_matter_when_the_sigmas_differ(self):
        """⚠️t0 is marginalised with the WEIGHTED mean. Using the plain mean there leaves a t0
        the weighted cost does not want, and the answer starts depending on the listing order."""
        arr = _wa_arrivals()
        arr[4] += 0.004
        sig = [SIGMA_PHONE_MEDIAN_S] * 4 + [0.004]
        base = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T, sigmas=sig)
        for k in range(1, 5):
            rot = PT.solve(WEIGHT_ARRAY[k:] + WEIGHT_ARRAY[:k], arr[k:] + arr[:k], "blast",
                           temp_c=T, sigmas=sig[k:] + sig[:k])
            assert rot["east_m"] == pytest.approx(base["east_m"], abs=1e-3)
            assert rot["north_m"] == pytest.approx(base["north_m"], abs=1e-3)
            assert rot["chi2"] == pytest.approx(base["chi2"], rel=1e-6)

    def test_scaling_every_sigma_by_the_same_factor_leaves_the_position_alone(self):
        """Only the RATIOS can matter to where the source is; the absolute scale belongs to
        chi2, which must move as the square of it."""
        arr = _wa_arrivals()
        arr[4] += 0.004
        sig = [SIGMA_PHONE_MEDIAN_S] * 4 + [0.004]
        a = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T, sigmas=sig)
        b = PT.solve(WEIGHT_ARRAY, arr, "blast", temp_c=T, sigmas=[s * 7.0 for s in sig])
        assert b["east_m"] == pytest.approx(a["east_m"], rel=1e-9)
        assert b["north_m"] == pytest.approx(a["north_m"], rel=1e-9)
        assert b["chi2"] == pytest.approx(a["chi2"] / 49.0, rel=1e-9)

    def test_the_declared_height_path_weights_too(self):
        """fixed_up_m is the path the CronJob actually takes on a 3-node array, so the 2D branch
        cannot be the one that quietly ignores the argument."""
        ev = _corpus()["events"][1]
        P = np.array(ev["positions_enu_m"], float)
        skew = PT.solve(P, ev["arrivals_utc_s"], "blast", temp_c=ev["temp_c"],
                        fixed_up_m=ev["fixed_up_m"],
                        sigmas=[SIGMA_PPS_S, SIGMA_PPS_S, SIGMA_PHONE_MAX_S])
        assert skew["n_counting_nodes"] == 2
        assert skew["weights_degenerate"] is True     # a 2D fit needs 3
        assert skew["relative_weights"][2] < 1e-7


class TestTheMechanismsThatMakeEqualSigmasFree:
    """⚠️THE THREE TESTS ABOVE DO NOT COVER THESE, and mutation testing is how that was found.
    Both defects below survived the whole rest of this file: the corpus events have three
    receivers and the weighted arithmetic happens to round identically at that size."""

    # nyquist/mach/rankine plus five more, because the array this work exists for is not three
    # receivers. Six devices detect today (3 nodes + 3 phones), seven with hugbot.
    EIGHT = [(0.0, 0.0, 0.0), (-16.602, -0.272, 3.0), (-4.58, 10.48, 3.0),
             (-22.16, 8.98, 0.0), (12.4, -7.1, 2.5), (-9.3, -14.6, 0.0),
             (5.7, 19.2, 6.0), (-30.1, -5.4, 1.5)]

    def test_an_absent_weight_runs_the_original_arithmetic_and_not_ones(self):
        """⚠️THE CONTRACT origin/main equivalence RESTS ON, checked against arithmetic written
        out here rather than against another call to the same function. Comparing solve(no sigma)
        with solve(equal sigma) CANNOT see this: sigma_weights returns None for equal sigmas, so
        both sides take the same branch and a mutant that replaces the branch with a vector of
        ones moves both together. That is how this survived a mutation run.

        MEASURED: `q @ r` (BLAS ddot) and `r.mean()` (numpy pairwise) disagree in the last bit on
        1342/4000 random 8-vectors and 2491/4000 at n=32, so at the array sizes this project is
        heading for -- six devices detect today, seven with hugbot -- 'multiply by ones' is not a
        no-op."""
        c = SW.sound_speed(T)
        P = np.array(self.EIGHT, float)
        src = _p3((61.0, 53.0, 0.0))
        t = np.array([float(np.linalg.norm(src - _p3(p))) / c for p in self.EIGHT])
        s = np.array([30.0, 20.0, 1.0])

        r = t - np.linalg.norm(s[None, :] - P, axis=1) / c
        want = r - r.mean()
        got = PT._residual(s, P, t, c)
        assert got.tobytes() == want.tobytes(), "the unweighted residual is no longer r - r.mean()"

        d = s[None, :] - P
        J = -d / (np.linalg.norm(d, axis=1)[:, None] * c)
        wantJ = J - J.mean(axis=0, keepdims=True)
        assert PT._jacobian(s, P, t, c).tobytes() == wantJ.tobytes()

        # and the weighted path at w=ones must NOT be assumed equal to it -- that is the point
        ones = np.ones(len(P))
        assert PT._residual(s, P, t, c, ones).tobytes() != want.tobytes(), (
            "this fixture no longer demonstrates the divergence; pick another source, or the "
            "early return in _residual has stopped being load-bearing on this numpy/BLAS build")

    def test_equal_sigmas_are_free_at_eight_receivers_too(self):
        """⚠️REGRESSION, and the reason `_residual` returns early on `w is None` instead of
        multiplying by a vector of ones. numpy's mean uses PAIRWISE summation above 8 elements;
        `q @ r` is a BLAS ddot with its own blocking. MEASURED: the two disagree in the last bit
        on ~30% of random 8-vectors (0/4000 at n<8, 1342/4000 at n=8, 2491/4000 at n=32). Three
        receivers can never show it, so every other bit-identity test in this file passes with
        the early return deleted.

        ⚠️SWEEPS SOURCES ON PURPOSE. Whether the last bit survives depends on the residual
        vector, so a single source is a coin flip: with only _residual forced through the
        weighted path, 165 of 255 probed source positions moved and 90 did not. These six all
        move, and sweeping them is what keeps this test from passing by luck."""
        c = SW.sound_speed(T)
        for src in [(40.0, -68.0, 0.0), (40.0, 42.0, 0.0), (54.0, -2.0, 0.0),
                    (61.0, 53.0, 0.0), (75.0, 9.0, 0.0), (75.0, 86.0, 0.0)]:
            arr = [T0_UTC + float(np.linalg.norm(_p3(src) - _p3(p))) / c for p in self.EIGHT]
            base = PT.solve(self.EIGHT, arr, "blast", temp_c=T)
            for s in (SIGMA_PPS_S, SIGMA_PHONE_MEDIAN_S, 1.0):
                got = PT.solve(self.EIGHT, arr, "blast", temp_c=T, sigmas=[s] * 8)
                for k, want in base.items():
                    if isinstance(want, float) and isinstance(got[k], float):
                        assert got[k].hex() == want.hex(), \
                            "%s drifted at src %s sigma %r: %r vs %r" % (k, src, s, got[k], want)

    def test_the_weighted_cost_is_what_picks_the_grid_seed(self):
        """⚠️REGRESSION. The coarse scan seeds a NON-CONVEX cost, so scoring its cells
        unweighted while the refine minimises the weighted cost hands the optimiser a cell in
        the wrong basin. It is not a rounding difference: MEASURED over 300 random 5-receiver
        layouts with one receiver at sigma 1.0 s, the final position moved in 113 of them, worst
        687 m. This layout is one of them -- the weighted seed converges to the answer the four
        good receivers give, the unweighted one lands 273 m away."""
        P = [[-39.2008, 52.6664, 11.093], [1.7814, 27.0419, 5.2564], [10.148, -18.8075, 0.9067],
             [-32.5567, -35.36, 14.8263], [-3.7068, -26.5429, 3.6513]]
        t = [1757000000.7098563, 1757000000.7530756, 1757000000.7082083,
             1757000000.5774298, 1757000000.4438539]
        sig = [SIGMA_PHONE_MEDIAN_S] * 4 + [1.0]
        got = PT.solve(P, t, "blast", temp_c=23.0, sigmas=sig)
        four = PT.solve(P[:4], t[:4], "blast", temp_c=23.0, sigmas=[SIGMA_PHONE_MEDIAN_S] * 4)
        assert got["relative_weights"][4] < 1e-3, "receiver 4 must really be weighted out"
        moved = math.dist((got["east_m"], got["north_m"], got["up_m"]),
                          (four["east_m"], four["north_m"], four["up_m"]))
        assert moved < 1.0, \
            "weighting out receiver 4 must give the four-receiver answer, not one %.1f m away" \
            % moved
