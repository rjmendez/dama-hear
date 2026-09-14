"""Regression seams between synthetic TDoA data and the production solvers."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import math

import numpy as np
import pytest

from hear.sim import TDoAEvaluationMetrics, simulate_forward_model
from hear.solve import point, shockwave


def _point_arrivals(nodes, source, temp_c=20.0, t0_s=1000.0):
    c = shockwave.sound_speed(temp_c)
    return [t0_s + np.linalg.norm(np.asarray(node, float) - source) / c for node in nodes]


def test_collinear_array_reports_unobservable_point_and_infinite_gdop():
    nodes = np.array([(0., 0.), (40., 0.), (80., 0.), (120., 0.)])
    source = np.array([50., 30.])
    result = point.solve(nodes, _point_arrivals(nodes, source), "blast")
    geometry = TDoAEvaluationMetrics().gdop(source, nodes)
    assert not result["position_observable"]
    assert result["east_m"] is result["north_m"] is None
    assert "UNOBSERVABLE" in result["note"]
    assert math.isinf(result["dop"])
    assert math.isinf(geometry["gdop"])


@pytest.mark.parametrize("source_z", [35.0, -35.0])
def test_coplanar_array_flags_height_mirror_for_either_source_side(source_z):
    nodes = np.array([(0., 0., 0.), (100., 0., 0.), (100., 100., 0.), (0., 100., 0.)])
    source = np.array([140., 30., source_z])
    result = point.solve(nodes, _point_arrivals(nodes, source), "blast")
    assert result["position_observable"]
    assert not result["up_observable"]
    assert result["up_mirror_m"] == pytest.approx(-result["up_m"], abs=1e-3)
    assert abs(result["up_m"]) == pytest.approx(abs(source_z), abs=1.0)
    assert "HEIGHT is unobservable" in result["note"]



def test_mach_cone_forward_model_recovers_trajectory_bearing_and_offset():
    nodes = np.array([(-30., 0.), (25., 12.), (5., -28.), (-8., 30.)])
    bearing_deg, offset_m, speed_mps, temp_c = 20.0, 8.0, 900.0, 23.0
    forward = simulate_forward_model(
        [0., 0.], nodes, np.ones(8), t0_s=1000.0, add_clock_noise=False,
        propagation_mode="mach_cone", trajectory_bearing_deg=bearing_deg,
        trajectory_offset_m=offset_m, projectile_speed_mps=speed_mps,
        temperature_c=temp_c,
    )
    recovered = shockwave.solve(nodes, forward.arrival_times_s, v_mps=speed_mps, temp_c=temp_c)
    bearing_error = abs((recovered["bearing_deg"] - bearing_deg + 180.0) % 360.0 - 180.0)
    assert bearing_error < 1.0
    assert recovered["offset_observable"]
    assert recovered["offset_m"] == pytest.approx(offset_m, abs=1.0)


def test_mach_cone_forward_model_requires_physical_trajectory_inputs():
    nodes = np.array([(0., 0.), (10., 0.), (0., 10.)])
    with pytest.raises(ValueError, match="trajectory_bearing_deg"):
        simulate_forward_model([0., 0.], nodes, np.ones(4), add_clock_noise=False,
                               propagation_mode="mach_cone")
    with pytest.raises(ValueError, match="supersonic"):
        simulate_forward_model([0., 0.], nodes, np.ones(4), add_clock_noise=False,
                               propagation_mode="mach_cone", trajectory_bearing_deg=0,
                               projectile_speed_mps=300.0)
