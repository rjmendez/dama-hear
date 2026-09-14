import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pytest

from hear.sim.placement_optimizer import PlacementOptimizer


def test_2d_heatmap_reports_surface_and_percentiles():
    optimizer = PlacementOptimizer((0, 100, 0, 100), grid_shape=(5, 7))
    result = optimizer.heatmap([(0, 0), (100, 0), (100, 100), (0, 100)])

    assert result["gdop"].shape == (7, 5)
    assert result["hdop"].shape == (7, 5)
    assert np.isfinite(result["stats"]["mean"])
    assert result["stats"]["p95"] >= result["stats"]["mean"]
    assert result["stats"]["finite_fraction"] > 0.8


def test_3d_heatmap_and_vertical_diagnostics_flag_flat_array():
    optimizer = PlacementOptimizer((0, 100, 0, 100), grid_shape=(4, 4), z_bounds=(-20, 20))
    nodes = [(0, 0, 0), (100, 0, 0), (100, 100, 0), (0, 100, 0)]
    result = optimizer.heatmap(nodes, dimensions=3)
    diagnostic = optimizer.vertical_observability(nodes)

    assert result["gdop"].shape == (4, 4)
    assert "vdop" in result and np.any(~np.isfinite(result["vdop"]))
    assert diagnostic["coplanar"] is True
    assert diagnostic["singular_fraction"] > 0
    assert diagnostic["recommended_z_offsets_m"] == (-10.0, 10.0)


def test_optimizer_preserves_bounds_and_minimum_separation():
    optimizer = PlacementOptimizer((0, 100, 0, 100), grid_shape=(4, 4), min_separation=20)
    initial = [(10, 10), (90, 10), (90, 90), (10, 90)]
    result = optimizer.optimize(initial, iterations=2, step_fraction=0.05)
    nodes = result["nodes"]

    assert np.all(nodes[:, 0] >= 0) and np.all(nodes[:, 0] <= 100)
    assert np.all(nodes[:, 1] >= 0) and np.all(nodes[:, 1] <= 100)
    distances = np.linalg.norm(nodes[:, None, :] - nodes[None, :, :], axis=2)
    assert np.all(distances[np.triu_indices(4, 1)] >= 20)
    assert result["objective"]["objective"] <= optimizer.objective(initial)["objective"]


def test_invalid_layout_constraints_are_rejected():
    optimizer = PlacementOptimizer((0, 10, 0, 10), min_separation=5)
    with pytest.raises(ValueError, match="minimum-separation"):
        optimizer.heatmap([(0, 0), (1, 1), (10, 0)])
