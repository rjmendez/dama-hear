"""Tests for acoustic harmonic Doppler speed verification."""
import numpy as np
import pytest

from hear.doppler_verifier import HarmonicTrack, KinematicDopplerVerifier


def synthetic_tracks(verifier, f0=440.0, velocity=25.0, t_cpa=0.7, d_perp=30.0):
    times = np.linspace(-2.0, 3.0, 41)
    tracks = []
    for receiver, x_rec in (("west", -20.0), ("east", 20.0), ("center", 0.0)):
        freq = verifier.doppler_frequency(times, f0, velocity, t_cpa, d_perp, x_rec)
        tracks.append(HarmonicTrack(times, freq, np.ones_like(times), receiver))
    return tracks


def test_doppler_model_has_s_curve_and_correct_sign():
    verifier = KinematicDopplerVerifier()
    t = np.array([-2.0, 0.0, 2.0])
    f = verifier.doppler_frequency(t, 500.0, 20.0, 0.0, 25.0)
    assert f[0] > 500.0
    assert f[2] < 500.0
    assert f[1] == pytest.approx(500.0)


def test_joint_fit_recovers_carrier_speed_cpa_and_lateral_distance():
    verifier = KinematicDopplerVerifier()
    fit = verifier.fit(synthetic_tracks(verifier), {"west": -20.0, "east": 20.0, "center": 0.0},
                       initial=(430.0, 22.0, 0.5, 25.0))
    assert fit.success
    assert fit.f0_hz == pytest.approx(440.0, abs=0.01)
    assert fit.velocity_mps == pytest.approx(25.0, abs=0.01)
    assert fit.t_cpa_s == pytest.approx(0.7, abs=0.01)
    assert fit.d_perp_m == pytest.approx(30.0, abs=0.02)
    assert fit.residual_rms_hz < 1e-6


def test_velocity_cross_verification_uses_mean_tdoa_speed():
    verifier = KinematicDopplerVerifier()
    fit = verifier.fit(synthetic_tracks(verifier), {"west": -20.0, "east": 20.0, "center": 0.0})
    check = verifier.cross_verify_velocity(fit, [24.0, 25.0, 26.0], tolerance_mps=1.1)
    assert check.consistent
    assert check.tdoa_velocity_mps == pytest.approx(25.0)
    assert check.difference_mps == pytest.approx(0.0)


def test_harmonic_track_rejects_invalid_data():
    with pytest.raises(ValueError):
        HarmonicTrack([0.0, 0.0], [100.0, 101.0], [1.0, 1.0])


def test_stft_phase_vocoder_extracts_a_moving_harmonic():
    verifier = KinematicDopplerVerifier(n_fft=512, hop_length=128, peak_prominence=0.001)
    fs = 8000.0
    duration = 4.0
    t = np.arange(int(fs * duration)) / fs
    # A slowly varying source is sufficient to exercise phase-vocoder interpolation and ridge linking.
    instantaneous = 600.0 + 35.0 * np.sin(2.0 * np.pi * 0.25 * t)
    phase = 2.0 * np.pi * np.cumsum(instantaneous) / fs
    audio = np.sin(phase)
    tracks = verifier.extract_harmonic_tracks(audio, fs, fmin_hz=400.0, fmax_hz=800.0,
                                              max_tracks=2, min_track_length=5)
    assert tracks
    strongest = tracks[0]
    assert len(strongest.times_s) >= 5
    assert np.median(strongest.frequencies_hz) == pytest.approx(600.0, abs=15.0)
    assert np.ptp(strongest.frequencies_hz) > 20.0


def test_invalid_fit_input_is_rejected():
    verifier = KinematicDopplerVerifier()
    track = HarmonicTrack([0.0, 1.0], [400.0, 401.0], [1.0, 1.0], receiver_id="missing")
    with pytest.raises(ValueError):
        verifier.fit([track], {"other": 0.0})
