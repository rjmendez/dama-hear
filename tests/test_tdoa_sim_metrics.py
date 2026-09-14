import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import numpy as np
import pytest
from hear.sim.metrics import TDoAEvaluationMetrics, TDoAReport


def test_spatial_errors_include_2d_3d_radial_and_bearing():
    metrics = TDoAEvaluationMetrics()
    nodes = np.array([[0., 0., 0.], [10., 0., 0.], [0., 10., 0.]])
    result = metrics.spatial_errors([6., 5., 4.], [3., 4., 1.], nodes)
    assert result["error_2d_m"] == pytest.approx(np.sqrt(10.))
    assert result["error_3d_m"] == pytest.approx(np.sqrt(19.))
    assert result["bearing_error_deg"] > 0.


def test_timing_residuals_use_reference_node_and_estimated_ranges():
    metrics = TDoAEvaluationMetrics(sound_speed_mps=100.)
    nodes = np.array([[0., 0.], [100., 0.], [0., 100.]])
    estimate = np.array([30., 40.])
    arrivals = np.linalg.norm(nodes - estimate, axis=1) / 100. + 7.
    assert np.allclose(metrics.timing_residuals(arrivals, estimate, nodes)["residuals_s"], 0.)


def test_gdop_and_hdop_handle_good_and_singular_geometry():
    metrics = TDoAEvaluationMetrics()
    truth = np.array([2., 3., 4.])
    good = np.array([[0., 0., 0.], [10., 0., 0.], [0., 10., 0.], [0., 0., 10.]])
    result = metrics.gdop(truth, good)
    assert np.isfinite(result["gdop"]) and np.isfinite(result["hdop"])
    assert metrics.gdop([.5, 1.], [[0., 0.], [1., 0.]])["gdop"] == np.inf


def test_confidence_check_uses_covariance_ellipse_or_sphere():
    metrics = TDoAEvaluationMetrics()
    assert metrics.confidence_check([.5, 0.], [0., 0.], np.eye(2))["inside"]
    assert not metrics.confidence_check([4., 0.], [0., 0.], np.eye(2))["inside"]


def test_node_gate_compliance_reports_non_admissible_rates():
    arrivals = [{"admissible": True, "accepted": True}, {"admissible": True, "accepted": False},
                {"admissible": False, "accepted": True}, {"admissible": False, "accepted": False},
                {"admissible": False, "accepted": False}]
    result = TDoAEvaluationMetrics.node_gate_compliance(arrivals)
    assert result.non_admissible_rejection_rate == pytest.approx(2 / 3)
    assert result.non_admissible_false_accept_rate == pytest.approx(1 / 3)


def test_report_has_json_and_comparison_table():
    metrics = TDoAEvaluationMetrics()
    run = metrics.evaluate(estimated_position=[2, 3], true_position=[2, 3],
                           node_positions=[[0, 0], [10, 0], [0, 10], [10, 10]])
    report = TDoAReport({"baseline": run, "repeat": run})
    assert set(json.loads(report.to_json())["runs"]) == {"baseline", "repeat"}
    assert "scenario" in report.summary_table() and "baseline" in report.summary_table()
