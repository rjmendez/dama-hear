import numpy as np
import pytest

from hear.geometry_opt import ArrayGeometryOptimizer


def test_geometry_classes_cover_collinear_triangle_star_and_l_shape():
    opt = ArrayGeometryOptimizer()
    assert opt.classify_geometry([(0, 0), (5, 0), (10, 0)])["classification"] == "collinear"
    assert opt.classify_geometry([(0, 0), (10, 0), (0, 10)])["classification"] == "triangle"
    assert opt.classify_geometry([(0, 0), (10, 0), (0, 10), (5, 5)])["classification"] == "star"
    assert opt.classify_geometry([(0, 0), (10, 0), (0, 10), (0, 5)])["classification"] == "L-shape"


def test_gdop_grid_reports_2d_and_3d_metrics():
    opt = ArrayGeometryOptimizer()
    receivers = [(0, 0, 0), (100, 0, 0), (100, 100, 8), (0, 100, 0)]
    result = opt.compute_gdop_grid(receivers, (0, 0, 100, 100), resolution=50)
    assert result["grid"].shape == (3, 3)
    assert np.isfinite(result["mean_gdop"])
    assert np.isfinite(result["mean_area_gdop"])
    assert np.isfinite(result["hdop"]).any()
    assert np.isfinite(result["vdop"]).any()


def test_collinear_array_has_exact_mirror_and_requires_off_axis_sensor():
    opt = ArrayGeometryOptimizer(timing_sigma_s=1e-4, target_side_resolution_m=0.5)
    line = [(0, 0), (50, 0), (100, 0)]
    result = opt.evaluate_mirror_resolvability(line, [(75, 10)])
    point = result["points"][0]
    assert result["classification"] == "collinear"
    assert point["mirror_tdoa_max_ms"] == pytest.approx(0.0)
    assert point["resolvable"] is False
    assert result["minimum_off_axis_baseline_m"] > 0
    assert result["recommended_sensor"][1] != pytest.approx(0.0)


def test_fourth_noncollinear_sensor_resolves_mirror_side():
    opt = ArrayGeometryOptimizer(timing_sigma_s=1e-4, target_side_resolution_m=0.5)
    receivers = [(0, 0), (50, 0), (100, 0), (1.5, 2.0)]
    result = opt.evaluate_mirror_resolvability(receivers, [(75, 10)])
    point = result["points"][0]
    assert result["classification"] != "collinear"
    assert point["mirror_tdoa_max_ms"] > 0
    assert point["resolvable"] is True


def test_evaluate_geometry_exposes_hdop_vdop_and_pdop():
    opt = ArrayGeometryOptimizer()
    result = opt.evaluate_geometry([(0, 0, 0), (100, 0, 0), (0, 100, 0), (100, 100, 20)], [(50, 50, 5)])
    row = result["points"][0]
    assert row["gdop"] > 0
    assert row["hdop"] is not None and row["vdop"] is not None and row["pdop"] is not None


def test_placement_adds_requested_sensors_and_improves_area_metric():
    opt = ArrayGeometryOptimizer(placement_resolution_m=25)
    existing = [(0, 0), (100, 0), (100, 100)]
    before = opt.compute_gdop_grid(existing, (0, 0, 100, 100), 25)["mean_area_gdop"]
    result = opt.optimize_placement(existing, 1, (0, 0, 100, 100))
    assert len(result["placements"]) == 1
    assert len(result["receivers"]) == 4
    assert result["mean_area_gdop"] < before


def test_mapping_coordinates_and_invalid_inputs_are_handled():
    opt = ArrayGeometryOptimizer()
    receivers = [{"e_m": 0, "n_m": 0, "u_m": 0}, {"e_m": 10, "n_m": 0}, {"e_m": 0, "n_m": 10}]
    assert opt.classify_geometry(receivers)["classification"] == "triangle"
    with pytest.raises(ValueError):
        opt.compute_gdop_grid(receivers, (0, 0, 10, 10), 0)
    with pytest.raises(ValueError):
        opt.optimize_placement(receivers, 0, (0, 0, 10, 10))
