import json

import numpy as np

from hear.sim.monte_carlo import MonteCarloSweep


def small_sweep():
    return MonteCarloSweep(
        node_positions=[[-10, -10], [10, -10], [10, 10], [-10, 10]],
        snr_db=[0, 40], timing_sigma_s=[1e-5, 1e-3], grid_step_m=10,
        outside_margin_m=10, trials_per_position=1, seed=7,
    )


def test_sweep_covers_inside_and_outside_and_emits_structured_metrics():
    result = small_sweep().run()
    assert {trial["region"] for trial in result["trials"]} == {"inside", "outside"}
    assert len(result["metrics"]) == 4
    assert {"rmse_m", "p50_m", "p90_m", "p99_m", "gdop_error_correlation", "solver_failure_rate"} <= set(result["metrics"][0])
    assert len(result["calibration_curves"]) == 4


def test_high_snr_low_timing_noise_has_sub_meter_bound():
    result = small_sweep().run()
    high_snr = next(row for row in result["metrics"] if row["snr_db"] == 40 and row["timing_sigma_s"] == 1e-5)
    assert high_snr["solver_failure_rate"] < 0.2
    assert high_snr["p90_m"] < 0.5


def test_json_output_is_serializable():
    payload = json.loads(small_sweep().run_json())
    assert payload["config"]["seed"] == 7
    assert isinstance(payload["trials"], list)


def test_position_grid_is_bounded_by_array_and_perimeter():
    sweep = MonteCarloSweep([[-1, -1], [1, -1], [1, 1], [-1, 1]], grid_step_m=1, outside_margin_m=1)
    positions = sweep.positions
    assert np.allclose(positions.min(axis=0), [-2, -2])
    assert np.allclose(positions.max(axis=0), [2, 2])
