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


def test_cli_runs(capsys):
    assert PL.main(["--nodes", "0,0;100,0;100,100;0,100", "--step", "40"]) == 0
    out = capsys.readouterr().out
    assert "median DOP" in out and "thinnest bearing" in out
