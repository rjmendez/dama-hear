"""Joint near-field clap geometry + capture-latency calibration tests."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pytest

from hear.sim import (
    CalibrationRefusal,
    ClapCalibrationError,
    ClapCalibrator,
    ClapObservation,
)

# A five-node bench array: identifiability of the joint (position, t0, bias)
# solve needs K * (N - 4) > N - 1 in 3D, so N >= 5 nodes.
ARRAY = {
    "node-ref": (0.00, 0.00, 0.00),
    "esp32s3-speaker": (0.62, 0.00, 0.05),
    "esp32s3-box3": (0.00, 0.58, -0.03),
    "esp32s3-lora": (0.55, 0.52, 0.40),
    "xiao-s3-pps": (-0.41, 0.28, 0.22),
}
TRUE_BIASES = {
    "esp32s3-speaker": 180e-6,
    "esp32s3-box3": -95e-6,
    "esp32s3-lora": 40e-6,
    "xiao-s3-pps": -210e-6,
}
CLAPS = [
    (0.40, -0.50, 0.20),
    (-0.30, 0.40, -0.10),
    (0.70, 0.30, 0.50),
    (0.10, 0.10, -0.40),
    (-0.40, -0.20, 0.30),
    (0.20, 0.60, 0.10),
]
EMISSIONS = [10.0, 11.5, 13.2, 14.0, 15.7, 17.1]


def _calibrator(**kwargs):
    kwargs.setdefault("max_clap_radius_m", 1.0)
    return ClapCalibrator(ARRAY, reference_nodes=("node-ref",), **kwargs)


def _loaded(noise_s=0.0, seed=7, **kwargs):
    calibrator = _calibrator(**kwargs)
    calibrator.simulate_claps(
        CLAPS,
        emission_times_s=EMISSIONS,
        capture_biases_s=TRUE_BIASES,
        timing_noise_s=noise_s,
        seed=seed,
    )
    return calibrator


def test_noiseless_joint_solve_recovers_biases_positions_and_emission_times():
    result = _loaded().solve()

    assert result.converged
    assert result.n_observations == len(CLAPS) * len(ARRAY)
    # 6 claps x (3 coords + t0) + 4 unknown biases
    assert result.n_parameters == len(CLAPS) * 4 + len(TRUE_BIASES)
    assert result.degrees_of_freedom == result.n_observations - result.n_parameters
    assert result.residual_rms_s < 1e-9

    for node_id, truth in TRUE_BIASES.items():
        assert result.bias_s(node_id) == pytest.approx(truth, abs=1e-9)

    for clap, truth_xyz, truth_t0 in zip(result.claps, CLAPS, EMISSIONS):
        assert np.allclose(clap.position_m, truth_xyz, atol=1e-5)
        assert clap.emission_time_s == pytest.approx(truth_t0, abs=1e-7)
        assert clap.n_nodes == len(ARRAY)


def test_reference_node_bias_is_pinned_to_zero():
    result = _loaded(noise_s=5e-6).solve()

    assert result.bias_s("node-ref") == 0.0
    assert result.sigma_b_s("node-ref") == 0.0
    assert "node-ref.bias" not in result.parameter_names


def test_pinned_reference_bias_offsets_all_other_estimates():
    calibrator = ClapCalibrator(
        ARRAY,
        reference_nodes=("node-ref",),
        reference_bias_s={"node-ref": 25e-6},
        max_clap_radius_m=1.0,
    )
    calibrator.simulate_claps(
        CLAPS,
        emission_times_s=EMISSIONS,
        capture_biases_s=TRUE_BIASES,
    )
    result = calibrator.solve()

    assert result.bias_s("node-ref") == pytest.approx(25e-6)
    for node_id, truth in TRUE_BIASES.items():
        assert result.bias_s(node_id) == pytest.approx(truth, abs=1e-9)


def test_noisy_solve_recovers_biases_within_covariance_error_bars():
    result = _loaded(noise_s=5e-6, seed=11).solve()

    assert result.converged
    assert result.residual_rms_s < 2e-5
    for node_id, truth in TRUE_BIASES.items():
        sigma = result.sigma_b_s(node_id)
        assert sigma > 0.0
        assert abs(result.bias_s(node_id) - truth) < 5.0 * sigma


def test_covariance_is_square_symmetric_psd_and_named():
    result = _loaded(noise_s=5e-6).solve()
    cov = result.covariance

    assert cov.shape == (result.n_parameters, result.n_parameters)
    assert len(result.parameter_names) == result.n_parameters
    assert np.allclose(cov, cov.T, atol=1e-18)
    assert np.all(np.diag(cov) >= 0.0)
    assert np.min(np.linalg.eigvalsh(0.5 * (cov + cov.T))) > -1e-18
    assert result.parameter_names[-len(TRUE_BIASES):] == tuple(
        "%s.bias" % n for n in sorted(TRUE_BIASES)
    )


def test_sigma_b_scales_with_timing_noise():
    quiet = _loaded(noise_s=1e-6, seed=3).solve()
    loud = _loaded(noise_s=20e-6, seed=3).solve()

    for node_id in TRUE_BIASES:
        assert loud.sigma_b_s(node_id) > 3.0 * quiet.sigma_b_s(node_id)


def test_sigma_b_matches_monte_carlo_spread():
    samples = []
    for seed in range(40):
        result = _loaded(noise_s=5e-6, seed=seed).solve()
        samples.append(result.bias_s("esp32s3-speaker"))
    empirical = float(np.std(samples, ddof=1))
    predicted = _loaded(noise_s=5e-6, seed=0).solve().sigma_b_s("esp32s3-speaker")

    assert 0.4 < predicted / empirical < 2.5


def test_near_field_radius_constraint_is_enforced():
    calibrator = _calibrator(max_clap_radius_m=1.0)
    far = [(3.0, -3.0, 1.5), (-2.5, 2.0, -1.0), (4.0, 1.0, 2.0), (-3.0, -2.0, 0.5), (1.0, 4.0, -2.0), (2.0, 2.0, 2.0)]
    calibrator.simulate_claps(far, emission_times_s=EMISSIONS, capture_biases_s=TRUE_BIASES)
    result = calibrator.solve()

    for clap in result.claps:
        assert clap.radius_from_center_m <= 1.0 + 1e-3


def test_claps_inside_radius_are_reported_inside_radius():
    result = _loaded().solve()
    center = np.asarray(result.array_center_m)

    for clap, truth in zip(result.claps, CLAPS):
        assert clap.radius_from_center_m <= result.max_clap_radius_m + 1e-6
        assert clap.radius_from_center_m == pytest.approx(
            float(np.linalg.norm(np.asarray(truth) - center)), abs=1e-4
        )


def test_three_node_array_is_refused_as_under_determined():
    calibrator = ClapCalibrator(
        {k: ARRAY[k] for k in ("node-ref", "esp32s3-speaker", "esp32s3-box3")},
        reference_nodes=("node-ref",),
    )
    calibrator.simulate_claps(CLAPS, emission_times_s=EMISSIONS)

    with pytest.raises(ClapCalibrationError) as excinfo:
        calibrator.solve()
    assert "under-determined" in str(excinfo.value)


def test_pinned_clap_plane_makes_a_four_node_array_solvable():
    nodes = {k: ARRAY[k] for k in ("node-ref", "esp32s3-speaker", "esp32s3-box3", "esp32s3-lora")}
    biases = {k: v for k, v in TRUE_BIASES.items() if k in nodes}
    claps = [(x, y, 0.15) for x, y, _ in CLAPS]
    calibrator = ClapCalibrator(
        nodes,
        reference_nodes=("node-ref",),
        clap_plane_z_m=0.15,
        max_clap_radius_m=1.0,
    )
    calibrator.simulate_claps(claps, emission_times_s=EMISSIONS, capture_biases_s=biases)
    result = calibrator.solve()

    assert result.converged
    assert result.n_parameters == len(claps) * 3 + len(biases)
    for node_id, truth in biases.items():
        assert result.bias_s(node_id) == pytest.approx(truth, abs=1e-9)
    for clap, truth in zip(result.claps, claps):
        assert np.allclose(clap.position_m, truth, atol=1e-5)
        assert clap.sigma_position_m[2] == 0.0


def test_admissibility_gate_and_refusal():
    result = _loaded(noise_s=1e-7, seed=5).solve()

    assert result.is_admissible("esp32s3-speaker", 30e-6)
    assert result.require_arrival("esp32s3-speaker", 30e-6).bias_us == pytest.approx(180.0, abs=1.0)
    with pytest.raises(CalibrationRefusal):
        result.require_arrival("esp32s3-speaker", 1e-12)
    with pytest.raises(ClapCalibrationError):
        result.require_arrival("nope", 30e-6)


def test_as_dict_round_trips_estimates():
    result = _loaded(noise_s=2e-6).solve()
    payload = result.as_dict()

    assert payload["converged"] is True
    assert set(payload["biases_s"]) == set(ARRAY)
    assert len(payload["claps"]) == len(CLAPS)
    assert payload["claps"][0]["clap_id"] == "clap-0"
    assert len(payload["claps"][0]["position_m"]) == 3


def test_ingest_accepts_mappings_and_rejects_bad_records():
    calibrator = _calibrator()
    rendered = calibrator.simulate_claps(CLAPS, emission_times_s=EMISSIONS, ingest=False)
    calibrator.ingest(
        {"clap_id": o.clap_id, "node_id": o.node_id, "timestamp_s": o.timestamp_s} for o in rendered
    )
    assert len(calibrator.observations) == len(rendered)

    with pytest.raises(ClapCalibrationError):
        calibrator.ingest([{"clap_id": "c", "node_id": "node-ref"}])
    with pytest.raises(ClapCalibrationError):
        calibrator.ingest([ClapObservation("c", "ghost-node", 1.0)])
    with pytest.raises(ClapCalibrationError):
        calibrator.ingest([ClapObservation("c", "node-ref", float("nan"))])


def test_duplicate_and_lonely_observations_are_rejected():
    calibrator = _calibrator()
    calibrator.ingest([ClapObservation("c0", "node-ref", 1.0), ClapObservation("c0", "node-ref", 1.1)])
    with pytest.raises(ClapCalibrationError):
        calibrator.solve()

    lonely = _calibrator()
    lonely.ingest([ClapObservation("c0", "node-ref", 1.0)])
    with pytest.raises(ClapCalibrationError):
        lonely.solve()


def test_empty_solve_is_rejected():
    with pytest.raises(ClapCalibrationError):
        _calibrator().solve()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"nodes": {}},
        {"nodes": {"a": (0.0,), "b": (1.0,)}},
        {"nodes": {"a": (0.0, 0.0), "b": (1.0, 0.0, 0.0)}},
        {"nodes": {"a": (0.0, float("nan"), 0.0), "b": (1.0, 0.0, 0.0)}},
        {"nodes": ARRAY, "reference_nodes": ()},
        {"nodes": ARRAY, "reference_nodes": ("ghost",)},
        {"nodes": ARRAY, "sound_speed_mps": 0.0},
        {"nodes": ARRAY, "max_clap_radius_m": -1.0},
        {"nodes": ARRAY, "admissibility_tolerance_s": 0.0},
        {"nodes": ARRAY, "clap_plane_z_m": float("inf")},
        {"nodes": ARRAY, "reference_bias_s": {"ghost": 1.0}},
    ],
)
def test_constructor_validation(kwargs):
    kwargs = dict(kwargs)
    nodes = kwargs.pop("nodes")
    kwargs.setdefault("reference_nodes", ("node-ref",) if nodes is ARRAY else tuple(nodes)[:1])
    with pytest.raises(ClapCalibrationError):
        ClapCalibrator(nodes, **kwargs)


def test_simulation_input_validation():
    calibrator = _calibrator()
    with pytest.raises(ClapCalibrationError):
        calibrator.simulate_claps([])
    with pytest.raises(ClapCalibrationError):
        calibrator.simulate_claps([(0.0, 0.0)])
    with pytest.raises(ClapCalibrationError):
        calibrator.simulate_claps(CLAPS, emission_times_s=[0.0])
    with pytest.raises(ClapCalibrationError):
        calibrator.simulate_claps(CLAPS, timing_noise_s=-1.0)
    with pytest.raises(ClapCalibrationError):
        calibrator.simulate_claps(CLAPS, capture_biases_s={"ghost": 1.0})
    with pytest.raises(ClapCalibrationError):
        calibrator.simulate_claps(CLAPS, emission_times_s=[float("nan")] * len(CLAPS))


def test_analytic_jacobian_matches_finite_differences():
    from scipy.optimize._numdiff import approx_derivative

    calibrator = _loaded(noise_s=5e-6, seed=2)
    problem = calibrator.build_problem()
    rng = np.random.default_rng(0)
    theta = problem.theta0 + rng.normal(0.0, 1e-3, problem.theta0.size)

    analytic = problem.jacobian(theta)
    numeric = approx_derivative(problem.residuals, theta, method="3-point")

    assert analytic.shape == numeric.shape
    assert np.allclose(analytic, numeric, atol=1e-5)

    # structural identities: d r / d t0 = d r / d b = -c, ||d r / d x|| = 1
    speed = calibrator.sound_speed_mps
    data_rows = analytic[:problem.n_obs]
    assert np.allclose(data_rows[:, problem.t0_offset:problem.bias_offset].sum(axis=1), -speed)
    assert np.allclose(
        np.linalg.norm(data_rows[:, :problem.n_claps * problem.n_free], axis=1), 1.0, atol=1e-9
    )
    bias_block = data_rows[:, problem.bias_offset:]
    assert set(np.unique(np.round(bias_block, 9))) <= {0.0, round(-speed, 9)}


def test_build_problem_layout_matches_parameter_names():
    problem = _loaded().build_problem()

    assert problem.theta0.size == problem.n_params == len(problem.parameter_names)
    assert problem.residuals(problem.theta0).size == problem.n_obs + problem.n_claps
    assert problem.parameter_names[problem.t0_offset] == "clap-0.t0"
    assert problem.parameter_names[problem.bias_offset] == "esp32s3-box3.bias"
    lower, upper = problem.bounds
    assert np.all(lower[: problem.n_claps * problem.n_free] < upper[: problem.n_claps * problem.n_free])
