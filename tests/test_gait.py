import json
import math
import numpy as np
import pytest

from hear.gait import (
    BiomechanicalFeatures,
    GaitBiomechanicalClassifier,
    GaitClassificationResult,
    GAIT_AMBIENT_NOISE,
    GAIT_RUN,
    GAIT_STATIONARY,
    GAIT_STOMP,
    GAIT_WALK,
    compute_crest_factor,
    compute_dual_peak_and_flight_fractions,
    compute_grf_proxy,
    compute_impact_rise_time_ms,
    compute_spectral_centroid,
    compute_spectral_flatness,
    compute_z_kurtosis,
    detect_footstep_impacts,
    estimate_cadence_and_cv,
)
from hear.live_runner_solver import PhoneTelemetry


def test_crest_factor_computation():
    # Constant signal -> crest factor = 1.0
    const_sig = np.ones(100) * 2.0
    assert compute_crest_factor(const_sig) == pytest.approx(1.0, rel=1e-3)

    # Pure impulse spike in quiet background -> high crest factor
    impulse_sig = np.zeros(1000)
    impulse_sig[500] = 10.0
    # RMS = sqrt(100 / 1000) = sqrt(0.1) = 0.3162 -> peak / RMS = 10 / 0.3162 = 31.62
    cf = compute_crest_factor(impulse_sig)
    assert cf > 25.0

    # Zero signal -> 0.0
    assert compute_crest_factor(np.zeros(10)) == 0.0


def test_spectral_centroid_and_flatness():
    fs = 100.0  # 100 Hz IMU
    t = np.arange(0, 2.0, 1.0 / fs)

    # 10 Hz pure sine wave
    sine_10hz = np.sin(2 * np.pi * 10.0 * t)
    centroid = compute_spectral_centroid(sine_10hz, fs=fs, f_min=0.5, f_max=50.0)
    assert centroid == pytest.approx(10.0, abs=1.0)

    flatness_sine = compute_spectral_flatness(sine_10hz, fs=fs, f_min=0.5, f_max=50.0)
    assert flatness_sine < 0.1  # Highly peaked / tonal

    # White noise -> high spectral flatness
    np.random.seed(42)
    white_noise = np.random.randn(2000)
    flatness_noise = compute_spectral_flatness(white_noise, fs=1000.0)
    assert flatness_noise > 0.45  # Flat noise distribution


def test_z_kurtosis_and_grf_proxy():
    # Gaussian noise -> kurtosis ~ 3.0, excess kurtosis ~ 0.0
    np.random.seed(42)
    noise = np.random.randn(5000) + 9.81
    kurt, excess = compute_z_kurtosis(noise)
    assert kurt == pytest.approx(3.0, abs=0.4)
    assert excess == pytest.approx(0.0, abs=0.4)

    # Heavy impact spikes -> high kurtosis
    spikes = np.random.randn(1000) * 0.1 + 9.81
    spikes[100] = 50.0
    spikes[500] = 45.0
    kurt_spk, excess_spk = compute_z_kurtosis(spikes)
    assert kurt_spk > 8.0
    assert excess_spk > 5.0

    # GRF proxy (in m/s^2)
    grf_peak, grf_mean, g_scale = compute_grf_proxy(spikes)
    assert g_scale == pytest.approx(9.80665, rel=1e-3)
    assert grf_peak == pytest.approx(50.0 / 9.80665, rel=0.05)


def test_impact_rise_time_discrimination():
    fs = 400.0  # 400 Hz IMU
    dt = 1.0 / fs

    # 1. Fast stomp impact: rise from floor to peak in 2 ms (< 5 ms)
    stomp_sig = np.ones(400) * 1.0  # 1g baseline
    # Stomp at index 200: rises from 1g to 5g in 1 sample (2.5 ms)
    stomp_sig[199] = 1.0
    stomp_sig[200] = 5.0
    stomp_sig[201:220] = np.linspace(5.0, 1.0, 19)

    rise_stomp = compute_impact_rise_time_ms(stomp_sig, fs=fs, peak_indices=[200])
    assert 0.1 <= rise_stomp <= 5.0

    # 2. Normal walking footstep: progressive heel strike rising over 20 ms (8 samples at 400 Hz)
    step_sig = np.ones(400) * 1.0
    step_sig[190:200] = np.linspace(1.0, 1.6, 10)  # 10 samples = 25 ms rise
    step_sig[200:220] = np.linspace(1.6, 1.0, 20)

    rise_step = compute_impact_rise_time_ms(step_sig, fs=fs, peak_indices=[200])
    assert 10.0 <= rise_step <= 30.0


def test_cadence_and_inter_impact_interval_cv():
    # Rhythmic walking: 1.8 Hz cadence (step every ~0.555 s) -> 108 SPM
    times = [0.0, 0.555, 1.110, 1.665, 2.220, 2.775]
    cad_hz, cad_spm, cv = estimate_cadence_and_cv(times)
    assert cad_hz == pytest.approx(1.8, rel=0.02)
    assert cad_spm == pytest.approx(108.0, rel=0.02)
    assert cv < 0.05  # Highly periodic

    # Sporadic stomp intervals
    stomp_times = [0.0, 1.85]
    cad_hz_s, cad_spm_s, cv_s = estimate_cadence_and_cv(stomp_times)
    assert cad_hz_s == pytest.approx(1.0 / 1.85, rel=0.05)


def test_dual_peak_and_flight_fraction():
    fs = 200.0  # 200 Hz

    # Create synthetic walking stride with dual peak (heel-strike + toe-off)
    walk_stride = np.ones(500) * 1.0
    # Stride 1: impact at 100, heel peak 1.5g at 100, trough 1.1g at 120, toe peak 1.45g at 140
    walk_stride[100] = 1.5
    walk_stride[101:120] = np.linspace(1.5, 1.1, 19)
    walk_stride[120] = 1.1
    walk_stride[121:140] = np.linspace(1.1, 1.45, 19)
    walk_stride[140] = 1.45
    walk_stride[141:170] = np.linspace(1.45, 1.0, 29)

    dual_frac, flight_frac = compute_dual_peak_and_flight_fractions(
        walk_stride, fs=fs, peak_indices=[100], g_scale=1.0
    )
    assert dual_frac == 1.0
    assert flight_frac < 0.05

    # Create synthetic running stride with single peak and flight phase (< 0.35g)
    run_stride = np.ones(500) * 0.1  # airborne flight phase
    # Stance impact at 100: sharp single peak to 2.8g
    run_stride[95:100] = np.linspace(0.1, 2.8, 5)
    run_stride[100:110] = np.linspace(2.8, 0.1, 10)

    dual_frac_run, flight_frac_run = compute_dual_peak_and_flight_fractions(
        run_stride, fs=fs, peak_indices=[100], g_scale=1.0
    )
    assert dual_frac_run == 0.0
    assert flight_frac_run > 0.40


def test_classify_stationary():
    classifier = GaitBiomechanicalClassifier(imu_fs=100.0)
    # Static gravity reading with negligible noise
    np.random.seed(42)
    static_z = np.ones(250) * 9.81 + np.random.randn(250) * 0.02

    result = classifier.classify(static_z)
    assert result.label == GAIT_STATIONARY
    assert result.confidence > 0.70
    assert result.features.grf_peak_bw == pytest.approx(1.0, abs=0.05)


def test_classify_stomp():
    classifier = GaitBiomechanicalClassifier(imu_fs=200.0, audio_fs=16000.0)
    # Quiet baseline with one heavy stomp impact (> 3.5x BW, < 5ms rise)
    np.random.seed(42)
    z = np.ones(500) * 9.81 + np.random.randn(500) * 0.1
    # Sharp stomp impact at sample 250
    z[250] = 42.0  # > 4.2x BW
    z[251:280] = 9.81 + 30.0 * np.exp(-np.linspace(0, 5, 29))

    # Acoustic burst
    audio = np.random.randn(500 * 80) * 0.01
    audio[250 * 80: 260 * 80] = np.random.randn(10 * 80) * 0.8

    result = classifier.classify(z, audio=audio)
    assert result.label == GAIT_STOMP
    assert result.confidence > 0.60
    assert result.features.grf_peak_bw > 3.5
    assert result.features.impact_rise_time_ms < 6.0


def test_classify_walk():
    classifier = GaitBiomechanicalClassifier(imu_fs=200.0)
    # Generate walking signal: cadence 1.9 Hz (step every 0.526s = ~105 samples)
    fs = 200.0
    t = np.arange(0, 3.0, 1.0 / fs)
    z = np.ones_like(t) * 9.81

    # Add dual-peak footsteps at 0.5s, 1.02s, 1.55s, 2.08s, 2.61s
    step_times = [0.5, 1.02, 1.55, 2.08, 2.61]
    for st in step_times:
        idx = int(st * fs)
        # Heel strike
        z[idx - 4: idx + 1] = np.linspace(9.81, 14.5, 5)  # ~1.48 BW
        z[idx: idx + 15] = np.linspace(14.5, 11.0, 15)
        # Toe off
        z[idx + 15: idx + 30] = np.linspace(11.0, 14.0, 15)
        z[idx + 30: idx + 45] = np.linspace(14.0, 9.81, 15)

    result = classifier.classify(z)
    assert result.label == GAIT_WALK
    assert result.confidence > 0.60
    assert 1.5 <= result.features.cadence_hz <= 2.2
    assert 90.0 <= result.features.cadence_spm <= 135.0
    assert result.features.dual_peak_fraction >= 0.40


def test_classify_run():
    classifier = GaitBiomechanicalClassifier(imu_fs=200.0)
    # Generate running signal: cadence 3.0 Hz (step every 0.333s = ~67 samples)
    fs = 200.0
    t = np.arange(0, 3.0, 1.0 / fs)
    z = np.ones_like(t) * 2.0  # Flight phase between steps (~0.2 BW)

    step_times = [0.3, 0.63, 0.96, 1.29, 1.62, 1.95, 2.28, 2.61]
    for st in step_times:
        idx = int(st * fs)
        # Sharp single impact ~2.6 BW (25.5 m/s^2)
        z[idx - 2: idx + 1] = np.linspace(2.0, 25.5, 3)
        z[idx: idx + 12] = np.linspace(25.5, 2.0, 12)

    result = classifier.classify(z)
    assert result.label == GAIT_RUN
    assert result.confidence > 0.60
    assert result.features.cadence_hz >= 2.5
    assert result.features.cadence_spm >= 150.0
    assert result.features.dual_peak_fraction < 0.35


def test_streaming_ingestion_and_evaluation():
    classifier = GaitBiomechanicalClassifier(imu_fs=100.0, window_s=2.0, hop_s=0.5)
    classifier.reset_streaming()

    # Ingest 3 seconds of walking frame by frame
    fs = 100.0
    results = []
    t_vals = np.arange(0, 3.0, 1.0 / fs)
    for t in t_vals:
        # Simple step wave
        z = 9.81 + 4.0 * np.sin(2 * np.pi * 1.8 * t)
        res = classifier.ingest_imu_frame(t_s=t, z=z)
        if res is not None:
            results.append(res)

    # Should produce streaming evaluations as window fills
    assert len(results) >= 2
    for r in results:
        assert isinstance(r, GaitClassificationResult)
        assert r.label in (GAIT_WALK, GAIT_RUN, GAIT_AMBIENT_NOISE, GAIT_STATIONARY, GAIT_STOMP)


def test_phone_telemetry_and_mqtt_ingestion():
    classifier = GaitBiomechanicalClassifier(imu_fs=100.0)

    # PhoneTelemetry compatibility
    phone = PhoneTelemetry(phone_id="phone-test", ip="192.168.1.50")
    phone.imu_z = list(np.ones(300) * 9.81)
    phone.audio = list(np.zeros(3000))

    res = classifier.classify_phone_telemetry(phone)
    assert res.label == GAIT_STATIONARY

    # MQTT payload ingestion
    mqtt_res = classifier.ingest_mqtt_payload(
        "dama/phone-01/imu_stream", [9.81] * 250
    )
    # Ingesting block may produce result if window size is met
    assert mqtt_res is None or isinstance(mqtt_res, GaitClassificationResult)


def test_schema_tag_export():
    classifier = GaitBiomechanicalClassifier()
    z = np.ones(200) * 9.81
    result = classifier.classify(z)

    tag_dict = result.to_tag_dict(node="mach", clip_key="clip-123", ts_utc_s=1726000000.0)
    assert tag_dict["schema_version"] == 2
    assert tag_dict["provenance"] == "model"
    assert tag_dict["clip_key"] == "clip-123"
    assert tag_dict["node"] == "mach"
    assert len(tag_dict["predictions"]) == 5
    assert tag_dict["predictions"][0]["rank"] == 0

    tag_record = result.to_tag_record(clip_key="clip-123", node="mach")
    assert tag_record is not None
