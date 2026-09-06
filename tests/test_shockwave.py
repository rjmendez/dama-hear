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
