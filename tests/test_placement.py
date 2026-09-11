"""Placement: DOP, redundancy, and whether a track offset is observable at all."""
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.solve import placement as PL   # noqa: E402

SQUARE = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]


class TestDop:
    def test_does_not_depend_on_which_node_is_called_first(self):
        """The differenced formulation invites a reference node; a geometry property must not
        depend on one. Regression against reintroducing that bookkeeping."""
        base = PL.dop(SQUARE, (50.0, 50.0))["dop"]
        for k in range(1, len(SQUARE)):
            rot = SQUARE[k:] + SQUARE[:k]
            assert PL.dop(rot, (50.0, 50.0))["dop"] == pytest.approx(base, rel=1e-9)
        assert PL.dop(list(reversed(SQUARE)), (50.0, 50.0))["dop"] == pytest.approx(base, rel=1e-9)

    def test_three_nodes_have_no_redundancy_however_good_the_dop(self):
        r = PL.dop(SQUARE[:3], (50.0, 50.0))
        assert r["dof"] == 0 and r["redundant"] is False
        assert math.isfinite(r["dop"]), "3 nodes still locate; they just cannot self-check"
        assert PL.dop(SQUARE, (50.0, 50.0))["redundant"] is True

    def test_collinear_nodes_are_singular_not_confident(self):
        line = [(0.0, 0.0), (50.0, 0.0), (100.0, 0.0), (150.0, 0.0)]
        assert PL.dop(line, (75.0, 0.0))["singular"] is True

    def test_inside_the_array_beats_far_outside_it(self):
        inside = PL.dop(SQUARE, (50.0, 50.0))["dop"]
        outside = PL.dop(SQUARE, (50.0, 3000.0))["dop"]
        assert inside < outside

    def test_a_distant_node_improves_dop_which_is_true_and_a_trap(self):
        """Counterintuitive but correct: TDoA likes long baselines, so geometry alone prefers a
        node far outside the array. It is a trap only because DOP says nothing about whether that
        node hears the event. Pinned so nobody 'fixes' the maths to match the intuition."""
        b = (-50.0, -50.0, 150.0, 150.0)
        mid = PL.dop_grid(SQUARE + [(50.0, 50.0)], b, step=25.0)["median"]
        far = PL.dop_grid(SQUARE + [(400.0, 400.0)], b, step=25.0)["median"]
        assert far < mid

    def test_grid_reports_a_usable_fraction(self):
        g = PL.dop_grid(SQUARE, (-50.0, -50.0, 150.0, 150.0), step=25.0)
        assert 0.0 <= g["usable_frac"] <= 1.0
        assert math.isfinite(g["median"])


class TestOffsetObservability:
    """The measured trap: +/-6 m of track shift moved same-side TDoAs by 0.000 ms and straddling
    ones by up to 117 ms. Timing quality was irrelevant; which side the nodes sat on was not."""

    SAME_SIDE = [(0.0, -20.0), (60.0, -25.0), (120.0, -30.0)]
    STRADDLE = [(0.0, -20.0), (60.0, 25.0), (120.0, -30.0)]

    def test_same_side_nodes_cannot_see_the_offset(self):
        r = PL.offset_sensitivity(self.SAME_SIDE, bearing_deg=90.0, delta_m=6.0)
        assert r["straddles"] is False
        assert r["max_tdoa_swing_ms"] == pytest.approx(0.0, abs=1e-9), \
            "a common delay added to every node cancels in TDoA -- by construction, not by noise"
        assert r["observable"] is False

    def test_straddling_nodes_can(self):
        r = PL.offset_sensitivity(self.STRADDLE, bearing_deg=90.0, delta_m=6.0)
        assert r["straddles"] is True
        assert r["max_tdoa_swing_ms"] > 10.0, "should be tens of ms, as measured"
        assert r["observable"] is True

    def test_a_track_outside_the_band_is_unobservable_however_good_the_clock(self):
        """The band is the whole story: inside it the nodes straddle, outside they cannot."""
        sp = PL.observable_span(self.SAME_SIDE, 90.0)
        outside = sp["offset_hi"] + 50.0
        assert PL.offset_sensitivity(self.SAME_SIDE, 90.0, offset_m=outside)["observable"] is False
        inside = 0.5 * (sp["offset_lo"] + sp["offset_hi"])
        assert PL.offset_sensitivity(self.SAME_SIDE, 90.0, offset_m=inside)["observable"] is True

    def test_a_nearly_collinear_layout_has_a_useless_band(self):
        """Nodes strung along one line can only measure offset for tracks threading that line."""
        w = PL.worst_bearing(self.SAME_SIDE)
        assert w["span_m"] < 15.0, "10 m of y-spread buys a 10 m window and nothing more"

    def test_a_square_is_thick_in_every_direction(self):
        assert PL.worst_bearing(SQUARE)["span_m"] >= 99.0


class TestBestAddition:
    def test_prefers_the_site_that_opens_the_blind_bearing(self):
        nodes = TestOffsetObservability.SAME_SIDE
        cands = [(60.0, 40.0), (60.0, -35.0)]        # crosses the track / stays same-side
        ranked = PL.best_addition(nodes, cands, (-50.0, -80.0, 170.0, 80.0), step=40.0)
        by_pos = {r["position"]: r for r in ranked}
        assert by_pos[(60.0, 40.0)]["worst_span_gain_m"] > \
               by_pos[(60.0, -35.0)]["worst_span_gain_m"]

    def test_adding_a_node_buys_redundancy(self):
        ranked = PL.best_addition(SQUARE[:3], [(50.0, 50.0)], (0.0, 0.0, 100.0, 100.0), step=50.0)
        assert ranked[0]["dof"] == 1


def test_map_reserves_at_sign_for_degenerate_cells_only():
    """The legend says '@' means degenerate. A saturated but finite DOP must not render as '@',
    or the map quietly lies about which cells are unsolvable. Found by Copilot on the port."""
    assert "@" not in PL._RAMP
    line = [(0.0, 0.0), (50.0, 0.0), (100.0, 0.0)]
    g = PL.dop_grid(line, (-25.0, -25.0, 125.0, 25.0), step=25.0)
    assert "@" in PL.render(g, line), "singular cells must still be marked"
    sq = PL.dop_grid(SQUARE, (0.0, 0.0, 100.0, 100.0), step=25.0)
    assert "@" not in PL.render(sq, SQUARE)


def test_cli_runs(capsys):
    assert PL.main(["--nodes", "0,0;100,0;100,100;0,100", "--step", "40"]) == 0
    out = capsys.readouterr().out
    assert "median DOP" in out and "thinnest bearing" in out


FLAT_SQUARE_3D = [(0.0, 0.0, 0.0), (100.0, 0.0, 0.0), (100.0, 100.0, 0.0), (0.0, 100.0, 0.0)]
RAISED_SQUARE = [(0.0, 0.0, 30.0), (100.0, 0.0, 0.0), (100.0, 100.0, 0.0), (0.0, 100.0, 0.0)]
# Square plus a mast in the middle: the only 5-node set here with real vertical extent.
TOWER = FLAT_SQUARE_3D + [(50.0, 50.0, 30.0)]
# Same square, mast collapsed to a kerb: horizontally identical, vertically ruined.
STUB_MAST = FLAT_SQUARE_3D + [(50.0, 50.0, 0.5)]
# Exactly coplanar and exactly VERTICAL -- coplanar without a mirror twin.
VERTICAL_PLANE = [(0.0, 0.0, 0.0), (100.0, 0.0, 0.0), (0.0, 0.0, 30.0), (100.0, 0.0, 30.0)]
# Exactly coplanar, sloped 10 m over 100 m. LAPACK hands back a DOWNWARD normal for this one.
RAMP = [(0.0, 0.0, 0.0), (100.0, 0.0, 10.0), (100.0, 100.0, 10.0), (0.0, 100.0, 0.0)]
# 9 nodes 0.4 m either side of their plane: rms 0.40 m, but raw s[2] is 1.19.
JITTER_GRID = [(i * 50.0, j * 50.0, 0.4 if (i + j) % 2 == 0 else -0.4)
               for i in range(3) for j in range(3)]


class TestVerticalObservability:
    """The same-side trap rotated into the vertical: a source raised above a flat array adds the
    same delay to every node and cancels in TDoA, exactly as +/-6 m of track shift gave 0.000 ms."""

    def test_a_flat_array_cannot_see_the_source_rise(self):
        r = PL.height_sensitivity(FLAT_SQUARE_3D, (50.0, 50.0, 0.0), delta_m=6.0)
        assert r["max_tdoa_swing_ms"] == pytest.approx(0.0, abs=1e-9), \
            "coplanar nodes see a common delay, which cancels -- by construction, not by noise"
        assert r["observable"] is False

    def test_one_raised_node_makes_it_observable(self):
        r = PL.height_sensitivity(RAISED_SQUARE, (50.0, 50.0, 0.0), delta_m=6.0)
        assert r["observable"] is True
        assert r["max_tdoa_swing_ms"] > PL.RESOLVABLE_SWING_MS

    def test_the_verdict_is_measured_not_inferred_from_planarity(self):
        """RAISED_SQUARE is far from coplanar AND observable; a flat one is neither. If
        `observable` were read off COPLANAR_RMS_M the two would agree for the wrong reason, so
        pin the swing itself as the thing that decides."""
        flat = PL.vertical_observability(FLAT_SQUARE_3D)
        up = PL.vertical_observability(RAISED_SQUARE, (50.0, 50.0, 0.0))
        assert flat["observable"] is False and "UNOBSERVABLE" in flat["note"]
        assert up["observable"] is True and up["note"] is None
        assert up["max_tdoa_swing_ms"] > 100.0 * flat["max_tdoa_swing_ms"] + 1.0

        # Both fixtures above have measurement and inference agreeing, so they cannot tell the two
        # apart. This one can: exactly coplanar, and observable anyway. Reading the verdict off
        # COPLANAR_RMS_M calls it UNOBSERVABLE, which is the wrong answer about a real geometry.
        off = PL.vertical_observability(FLAT_SQUARE_3D, (300.0, 300.0, 0.0))
        assert off["coplanar"] is True and off["planarity_rms_m"] == 0.0
        assert off["max_tdoa_swing_ms"] == pytest.approx(0.0618, abs=5e-4)
        assert off["observable"] is True, "the perturbation decides; planarity is not evidence"
        assert off["note"] is None
        assert off["mirror_ambiguous"] is True, \
            "still sees only the magnitude of elevation, never the sign -- a separate verdict"

    def test_the_resolvable_floor_is_a_number_and_both_directions_use_it(self):
        """0.05 ms is the verdict boundary of the entire block and the vertical and horizontal
        halves are meant to be the same test in two directions. Each pair straddles it -- source
        pushed to 300 m swings 0.0618 ms and to 350 m only 0.0424; 20 mm of node y-spread swings
        0.0538 ms and 10 mm only 0.0269. A floor anywhere outside (0.0424, 0.0538) flips a verdict
        here, and a second copy of the constant fails one pair while the other still passes."""
        assert PL.RESOLVABLE_SWING_MS == 0.05
        near = PL.height_sensitivity(FLAT_SQUARE_3D, (300.0, 300.0, 0.0))
        far = PL.height_sensitivity(FLAT_SQUARE_3D, (350.0, 350.0, 0.0))
        assert near["max_tdoa_swing_ms"] > PL.RESOLVABLE_SWING_MS > far["max_tdoa_swing_ms"]
        assert near["observable"] is True and far["observable"] is False

        for nodes, want in (([(0.0, 0.0), (60.0, 0.02), (120.0, 0.0)], True),
                            ([(0.0, 0.0), (60.0, 0.01), (120.0, 0.0)], False)):
            sp = PL.observable_span(nodes, 90.0)
            mid = 0.5 * (sp["offset_lo"] + sp["offset_hi"])
            r = PL.offset_sensitivity(nodes, 90.0, offset_m=mid)
            assert r["straddles"] is True, "both straddle -- the floor is the only thing deciding"
            assert r["observable"] is want
            assert (r["max_tdoa_swing_ms"] > PL.RESOLVABLE_SWING_MS) is want

    def test_a_horizontal_coplanar_array_is_mirror_ambiguous(self):
        assert PL.vertical_observability(FLAT_SQUARE_3D)["mirror_ambiguous"] is True
        assert PL.vertical_observability(RAISED_SQUARE)["mirror_ambiguous"] is False

    def test_a_coplanar_array_standing_on_edge_has_no_mirror_twin(self):
        """The twin is the reflection IN THE PLANE, so the plane has to be horizontal for one to
        exist. Four nodes in a vertical plane are exactly as coplanar and have no twin at all --
        and the source's height is plainly observable to them. Pins the near_horizontal half of
        the verdict, which coplanarity alone would get wrong."""
        r = PL.vertical_observability(VERTICAL_PLANE)
        assert r["coplanar"] is True, "coplanar alone cannot be the mirror verdict"
        assert abs(r["plane_normal"][2]) < 1e-9 and r["near_horizontal"] is False
        assert r["mirror_ambiguous"] is False
        assert r["observable"] is True and r["max_tdoa_swing_ms"] > 9.0

    def test_c_comes_from_shockwave(self):
        r = PL.height_sensitivity(RAISED_SQUARE, (50.0, 50.0, 0.0), temp_c=23.0)
        assert r["sound_speed_mps"] == pytest.approx(345.238, abs=1e-3)


class TestCoplanarity:
    def test_three_nodes_are_coplanar_by_construction_whatever_their_heights(self):
        """Three points define a plane, so s[2] is 0 exactly. Breaks if planarity is faked from
        vertical spread, which would call this layout wildly non-planar."""
        r = PL.coplanarity([(0.0, 0.0, 0.0), (10.0, 0.0, 5.0), (0.0, 10.0, -7.0)])
        assert r["coplanar"] is True
        assert r["planarity_rms_m"] == pytest.approx(0.0, abs=1e-9)
        assert r["vertical_spread_m"] == pytest.approx(12.0, abs=1e-9)

    def test_a_flat_array_has_an_upward_plane_normal(self):
        r = PL.coplanarity(FLAT_SQUARE_3D)
        assert r["near_horizontal"] is True
        assert r["plane_normal"][2] == pytest.approx(1.0, abs=1e-9)

    def test_the_plane_normal_is_forced_upward_not_left_to_lapack(self):
        """FLAT_SQUARE_3D passes the upward-normal test by SVD sign convention, not because the
        code does anything. RAMP is the case that separates them: LAPACK returns V[2] pointing
        DOWN, and one representative per plane means it still has to come out up."""
        C = np.asarray(RAMP, float)
        raw = np.linalg.svd(C - C.mean(axis=0), full_matrices=False)[2][2]
        assert raw[2] < -0.9, "fixture is worthless the day LAPACK's sign convention changes"
        r = PL.coplanarity(RAMP)
        assert r["plane_normal"][2] == pytest.approx(0.99504, abs=1e-5)
        assert r["near_horizontal"] is True and r["coplanar"] is True

    def test_planarity_is_an_rms_distance_not_a_raw_singular_value(self):
        """s[2] grows with node count; COPLANAR_RMS_M is a distance in metres, so they are only
        comparable after /sqrt(N). Drop the normalisation and this 9-node grid -- 0.4 m either
        side of its plane, which is survey noise -- gets reported non-planar at s[2] = 1.19."""
        r = PL.coplanarity(JITTER_GRID)
        C = np.asarray(JITTER_GRID, float)
        C = C - C.mean(axis=0)
        d = C @ np.asarray(r["plane_normal"], float)
        assert r["planarity_rms_m"] == pytest.approx(math.sqrt(float(np.mean(d ** 2))), rel=1e-12)
        assert r["planarity_rms_m"] == pytest.approx(0.3975, abs=1e-4)
        assert float(np.linalg.svd(C, compute_uv=False)[2]) > PL.COPLANAR_RMS_M
        assert r["coplanar"] is True

    def test_relief_breaks_coplanarity(self):
        r = PL.coplanarity(RAISED_SQUARE)
        assert r["coplanar"] is False and r["planarity_rms_m"] > PL.COPLANAR_RMS_M

    def test_two_vectors_are_read_as_ground_level(self):
        assert PL.coplanarity(SQUARE)["vertical_spread_m"] == 0.0


class TestDop3:
    def test_does_not_depend_on_which_node_is_called_first(self):
        base = PL.dop3(TOWER, (30.0, 20.0, 10.0))["vdop"]
        for k in range(1, len(TOWER)):
            rot = TOWER[k:] + TOWER[:k]
            assert PL.dop3(rot, (30.0, 20.0, 10.0))["vdop"] == pytest.approx(base, rel=1e-9)
        assert PL.dop3(list(reversed(TOWER)), (30.0, 20.0, 10.0))["vdop"] \
            == pytest.approx(base, rel=1e-9)

    def test_a_coplanar_layout_is_singular_in_the_vertical_while_2d_dop_is_fine(self):
        """The whole point of a separate dop3: the 2D Fisher matrix of this layout is perfectly
        well conditioned and says nothing about height."""
        r = PL.dop3(FLAT_SQUARE_3D, (50.0, 50.0, 0.0))
        assert r["singular"] is True and r["vdop"] == float("inf")
        assert math.isfinite(PL.dop(FLAT_SQUARE_3D, (50.0, 50.0))["dop"])

    def test_vdop_is_the_vertical_half_and_a_stub_mast_is_what_ruins_it(self):
        """vdop is the number this block exists to produce and nothing else says which half of the
        covariance it comes from -- swapping it with hdop is invisible on a symmetric layout.
        Values cross-checked against a covariance built from a finite-differenced Jacobian rather
        than the analytic unit vectors: hdop 1.131552, vdop 2.084582, identical to 6 dp. Collapse
        the 30 m mast to 0.5 m and vdop triples while hdop moves 4%: it is the vertical axis that
        the mast was buying."""
        src = (30.0, 20.0, 10.0)
        t = PL.dop3(TOWER, src)
        assert t["hdop"] == pytest.approx(1.13155, abs=1e-5)
        assert t["vdop"] == pytest.approx(2.08458, abs=1e-5)
        assert t["vdop"] > t["hdop"], "a 30 m mast on a 100 m square is still the weak axis"
        assert t["pdop"] == pytest.approx(math.hypot(t["hdop"], t["vdop"]), rel=1e-12)
        stub = PL.dop3(STUB_MAST, src)
        assert stub["vdop"] == pytest.approx(7.19376, abs=1e-5)
        assert stub["vdop"] > 3.0 * t["vdop"], "losing 29.5 m of mast is a vertical problem"
        assert stub["hdop"] == pytest.approx(t["hdop"], rel=0.05), "...and not a horizontal one"

    def test_dof_counts_three_unknowns_not_two(self):
        n = len(TOWER)
        assert PL.dop3(TOWER, (30.0, 20.0, 10.0))["dof"] == (n - 1) - 3
        assert PL.dop(TOWER, (30.0, 20.0))["dof"] == (n - 1) - 2

    def test_four_nodes_are_the_floor(self):
        with pytest.raises(ValueError, match="4 nodes"):
            PL.dop3(FLAT_SQUARE_3D[:3], (50.0, 50.0, 10.0))


class TestNodeCounts:
    """t0 cancels in TDoA, so N nodes give N-1 equations. Breaks if anyone 'simplifies' the
    table to unknowns+1, which would call an exactly-determined fit self-checking."""

    def test_the_table(self):
        assert PL.node_counts("trajectory", 3) == \
            {"model": "trajectory", "dims": 3, "unknowns": 4, "exact_n": 5, "meaningful_n": 6}
        assert PL.node_counts("point", 3) == \
            {"model": "point", "dims": 3, "unknowns": 3, "exact_n": 4, "meaningful_n": 5}
        for m in ("trajectory", "point"):
            assert PL.node_counts(m, 2) == \
                {"model": m, "dims": 2, "unknowns": 2, "exact_n": 3, "meaningful_n": 4}

    def test_an_unknown_model_is_refused_not_guessed(self):
        with pytest.raises(ValueError):
            PL.node_counts("banana", 2)
        with pytest.raises(ValueError):
            PL.node_counts("point", 4)


class TestLinearity:
    """hear/backend/survey.py and hear/solve/point.py import this and compare it against
    COLLINEAR_LINEARITY. Pin an exact value so the formula cannot drift under them."""

    def test_a_rectangle_reports_its_aspect_ratio(self):
        assert PL.linearity([(0.0, 0.0), (100.0, 0.0), (100.0, 20.0), (0.0, 20.0)]) \
            == pytest.approx(0.2, abs=1e-12)

    def test_a_square_is_isotropic_and_a_line_is_not(self):
        assert PL.linearity(SQUARE) > 0.5
        line = [(0.0, 0.0), (50.0, 0.0), (100.0, 0.0), (150.0, 0.0)]
        assert PL.linearity(line) == pytest.approx(0.0, abs=1e-12)
        assert PL.linearity(line) < PL.COLLINEAR_LINEARITY
        assert PL.dop(line, (75.0, 0.0))["singular"] is True

    def test_height_does_not_enter_it(self):
        """It is a HORIZONTAL measure -- the 2D solvers are what it advises."""
        assert PL.linearity(RAISED_SQUARE) == pytest.approx(PL.linearity(SQUARE), rel=1e-12)

    def test_three_nodes_are_the_floor(self):
        with pytest.raises(ValueError, match="3 nodes"):
            PL.linearity(SQUARE[:2])


class TestPlan3d:
    def test_reports_dop3_only_when_there_are_enough_nodes(self):
        assert PL.plan_3d(FLAT_SQUARE_3D[:3])["dop3"] is None
        assert PL.plan_3d(TOWER)["dop3"] is not None

    def test_a_flat_site_is_told_it_cannot_do_3d_and_what_3d_would_cost(self):
        p = PL.plan_3d(FLAT_SQUARE_3D)
        assert p["vertical"]["observable"] is False
        assert p["counts"]["trajectory_3d"]["meaningful_n"] == 6
        assert p["n_nodes"] == 4


class TestParsePoints:
    def test_arity_is_preserved_and_never_mixed(self):
        """np.asarray on a ragged list builds an object array and every downstream slice then
        lies about the geometry. Refused at the parse instead."""
        assert PL._parse_points("0,0,0;10,0,0") == [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)]
        assert PL._parse_points("0,0;10,0") == [(0.0, 0.0), (10.0, 0.0)]
        with pytest.raises(ValueError, match="mixed 2D and 3D"):
            PL._parse_points("0,0;10,0,5")


def test_cli_prints_the_vertical_block_for_3d_nodes(capsys):
    assert PL.main(["--nodes", "0,0,0;100,0,0;100,100,0;0,100,30", "--step", "40"]) == 0
    out = capsys.readouterr().out
    assert "median DOP" in out, "the 2D block must survive untouched"
    assert "vertical spread" in out and "VDOP" in out and "3D costs" in out


class TestPlacementForTheFleetPair:
    """nyquist/mach as surveyed. The weekend question is where receiver THREE goes, and the tool
    used to raise on it."""

    NYQ = (0.0, 0.0)
    MACH = (-16.602, -0.272)
    MID = (-8.301, -0.136)
    BOX = (-68.301, -60.136, 51.699, 59.864)

    def test_best_addition_answers_the_two_node_question(self):
        r = PL.best_addition([self.NYQ, self.MACH], [(-8.86, 33.86)], self.BOX, step=20.0)
        assert len(r) == 1
        assert r[0]["dof"] == 0                       # 3 nodes: exact fit, residual proves nothing
        assert r[0]["dop_gain"] is None, "a pair has no DOP, so there is no gain to quote"
        assert r[0]["worst_span_gain_m"] is None

    def test_the_baseline_extension_ranks_first_on_dop_and_is_unsolvable(self):
        """⚠️The measured trap. dop() is local and cannot see the mirror twin a collinear array
        has, so the extension site wins on median DOP while point.solve refuses it outright."""
        ext = (-42.3, -0.7)                           # 34 m past mach, on the axis
        perp = (-8.86, 33.86)                         # 34 m perpendicular of the midpoint
        ranked = PL.best_addition([self.NYQ, self.MACH], [ext, perp], self.BOX, step=20.0)
        by_pos = {r["position"]: r for r in ranked}
        assert by_pos[ext]["median_dop"] < by_pos[perp]["median_dop"], \
            "the unsolvable site really does score better on DOP -- that is the whole problem"
        assert by_pos[ext]["collinear"] is True and by_pos[perp]["collinear"] is False
        assert ranked[0]["position"] == perp, "collinear must sort last whatever its DOP says"

    def test_the_extension_collapses_the_observable_band(self):
        ext = PL.worst_bearing([self.NYQ, self.MACH, (-42.3, -0.7)])
        perp = PL.worst_bearing([self.NYQ, self.MACH, (-8.86, 33.86)])
        assert ext["span_m"] < 1.0
        assert perp["span_m"] > 15.0

    def test_cli_two_nodes_without_candidates_refuses(self, capsys):
        assert PL.main(["--nodes", "0,0;-16.602,-0.272"]) == 2
        out = capsys.readouterr().out
        assert "TWO NODES GIVE ONE TDoA" in out
        assert "pass --candidates" in out

    def test_cli_two_nodes_with_candidates_ranks_them(self, capsys):
        rc = PL.main(["--nodes", "0,0;-16.602,-0.272",
                      "--candidates=-42.3,-0.7;-8.86,33.86", "--step", "20"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "baseline" in out
        assert "COLLINEAR: unsolvable" in out, "the axis extension must still sort last"


class TestWorstBearingSwingIsNotAVerdict:
    """⚠️`observable` at the band midpoint is true by construction: the probe track straddles
    whatever the layout is, so the swing is a property of the cone and the shift. Pinning it so
    nobody reads it as a test of the array again."""

    def test_the_midband_swing_cannot_fail_for_any_layout(self):
        import numpy as _np
        rng = _np.random.default_rng(3)
        for _ in range(40):
            P = rng.uniform(-80.0, 80.0, size=(int(rng.integers(3, 7)), 2))
            w = PL.worst_bearing(P.tolist())
            assert w["observable"] is True
            assert w["max_tdoa_swing_ms"] > 4.0, \
                "never within two orders of the 0.05 ms floor -- a gate that cannot fail"

    def test_the_out_of_band_probe_is_the_one_that_discriminates(self):
        same_side = TestOffsetObservability.SAME_SIDE
        w = PL.worst_bearing(same_side)
        assert w["swing_outside_band_ms"] == pytest.approx(0.0, abs=1e-9)
        assert w["discriminating"] is True

    def test_the_swing_ceiling_is_the_cone_not_the_geometry(self):
        """2*delta*cos(theta_Mach)/c, reached by any layout whose extremes straddle squarely."""
        c = PL.SW.sound_speed(20.0)
        ceiling = 2.0 * 6.0 * math.cos(math.asin(c / 900.0)) / c * 1000.0
        w = PL.worst_bearing([(-30.0, -30.0), (30.0, -30.0), (30.0, 30.0), (-30.0, 30.0)])
        assert w["max_tdoa_swing_ms"] == pytest.approx(ceiling, rel=1e-9)
