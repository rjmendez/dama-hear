import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pytest

from hear.sim import CalibrationRefusal, LatencyCalibrator


def _calibrator():
    return LatencyCalibrator(
        {
            "xiao-s3-pps": (0.0, 0.0, 0.0),
            "esp32s3-speaker": (10.0, 0.0, 0.0),
        },
        reference_nodes=("xiao-s3-pps",),
    )


def test_uncalibrated_node_is_refused_then_admitted_within_30_us():
    calibrator = _calibrator()
    assert calibrator.status("esp32s3-speaker") == "refused"
    with pytest.raises(CalibrationRefusal):
        calibrator.require_arrival("esp32s3-speaker")

    true_bias = 18e-6
    calibrator.simulate_impulses(
        [(2.0, 4.0, 0.0), (8.0, 3.0, 0.0), (4.0, -2.0, 0.0)],
        emission_times_s=(100.0, 200.0, 300.0),
        capture_biases_s={"esp32s3-speaker": true_bias},
    )
    estimate = calibrator.calibrate("esp32s3-speaker")

    assert estimate.bias_s == pytest.approx(true_bias, abs=1e-12)
    assert estimate.sigma_b_s <= 30e-6
    assert calibrator.status("esp32s3-speaker") == "admissible"
    assert calibrator.require_arrival("esp32s3-speaker") == estimate


def test_noisy_multi_position_calibration_reports_confidence_and_residual():
    calibrator = _calibrator()
    true_bias = 24e-6
    calibrator.simulate(
        [(-4.0, 1.0, 0.0), (3.0, 7.0, 0.0), (12.0, -1.0, 0.0), (5.0, 2.0, 0.0)],
        emission_times_s=(10.0, 20.0, 30.0, 40.0),
        capture_biases_s={"esp32s3-speaker": true_bias},
        timing_noise_s=2e-6,
        seed=7,
    )
    estimate = calibrator.calibrate("esp32s3-speaker")

    assert estimate.n_events == 4
    assert estimate.bias_s == pytest.approx(true_bias, abs=5e-6)
    assert estimate.sigma_b_s < 30e-6
    assert estimate.residual_rms_s < 5e-6


def test_reference_bias_and_geometry_are_included_in_residual_model():
    calibrator = LatencyCalibrator(
        {"ref": (0.0, 0.0), "target": (100.0, 0.0)},
        reference_nodes=("ref",),
        reference_bias_s={"ref": 7e-6},
        sound_speed_mps=100.0,
    )
    calibrator.ingest([
        {"event_id": "one", "node_id": "ref", "timestamp_s": 1.0,
         "calibration_position": (0.0, 100.0)},
        {"event_id": "one", "node_id": "target", "timestamp_s": 2.0001,
         "calibration_position": (0.0, 100.0)},
    ])
    estimate = calibrator.calibrate("target")
    expected = 1.0001 + 7e-6 - (np.sqrt(20000.0) - 100.0) / 100.0
    assert estimate.bias_s == pytest.approx(expected)
