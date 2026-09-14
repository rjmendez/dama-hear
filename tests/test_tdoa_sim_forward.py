import numpy as np

from hear import nodeclass
from hear.sim import SimNode, simulate, simulate_forward_model


def test_forward_model_matches_geometry_and_timestamps_without_noise():
    source = np.array([0.0, 0.0])
    nodes = [[3.0, 4.0], [0.0, 10.0]]
    result = simulate_forward_model(
        source, nodes, np.array([1.0, 0.5, -0.25]),
        node_classes=[nodeclass.get("xiao-s3-pps")] * 2,
        t0_s=12.0, add_clock_noise=False,
    )
    np.testing.assert_allclose(result.distances_m, [5.0, 10.0])
    np.testing.assert_allclose(result.propagation_delays_s, [5 / 343, 10 / 343])
    np.testing.assert_allclose(result.arrival_times_s, 12.0 + result.propagation_delays_s)
    expected_n = 3 + int(np.ceil(result.propagation_delays_s[0] * result.sample_rates_hz[0]))
    assert result.audio[0].shape == (1, expected_n)
    assert result.audio[0].max() > result.audio[1].max()
    assert result.audio[0].max() < result.audio[1].max() * 2.5


def test_capture_bias_is_applied_to_arrivals():
    cls = nodeclass.get("xiao-s3-pps")
    result = simulate(
        [0.0, 0.0], [[1.0, 0.0]], np.ones(8),
        node_classes=[cls], capture_path_bias_s=[0.01],
        rng=np.random.default_rng(4), add_clock_noise=False,
    )
    np.testing.assert_allclose(result.arrival_times_s, [1 / 343 + 0.01])


def test_simulation_supports_3d_nodes_and_node_specific_sample_rates():
    nodes = [
        SimNode([0.0, 0.0, 1.0], nodeclass.get("esp32s3-cam-mains")),
        SimNode([0.0, 0.0, 2.0], nodeclass.get("xiao-s3-pps")),
    ]
    result = simulate_forward_model([0.0, 0.0, 0.0], [n.position for n in nodes], np.ones(16),
                                    node_classes=nodes, add_clock_noise=False)
    assert tuple(result.sample_rates_hz) == (16000.0, 48000.0)
    assert result.audio[0].shape[0] == result.audio[1].shape[0] == 1
    assert result.audio[1].shape[1] > result.audio[0].shape[1]
