#!/usr/bin/env python3
"""Crack-blast interval: the model, its inverses, the detector, and the cross-mic bound."""
import math

import numpy as np
import pytest

from hear.solve.crackblast import (IntervalCheck, band_power, cone_limit_deg,
                                   crack_blast_interval, crack_onset, interval_consistent,
                                   max_interval, range_from_interval, range_lower_bound,
                                   range_uncertainty, rise_reference, second_arrival,
                                   theta_from_interval)

C = 345.238
M = 2.61
ESP_MICS = [(-0.1841, -0.2667, -0.0508), (-0.2223, -0.2667, -0.0508),
            (-0.1841, 0.2667, -0.0508), (-0.2223, 0.2667, -0.0508),
            (-0.8636, 0.0190, -0.1016), (-0.8636, -0.0190, -0.1016)]


# ---------------------------------------------------------------- model


def test_interval_is_largest_on_the_trajectory():
    on = crack_blast_interval(36.26, 0.0, mach=M, c=C)
    assert on == pytest.approx(max_interval(36.26, mach=M, c=C))
    for th in (1, 5, 10, 20, 40, 60):
        assert crack_blast_interval(36.26, th, mach=M, c=C) < on


def test_interval_falls_to_zero_at_the_cone_limit_and_vanishes_past_it():
    lim = cone_limit_deg(mach=M)
    assert lim == pytest.approx(90.0 - math.degrees(math.asin(1.0 / M)))
    assert crack_blast_interval(36.26, lim, mach=M, c=C) == pytest.approx(0.0, abs=1e-9)
    assert crack_blast_interval(36.26, lim + 1.0, mach=M, c=C) is None


def test_forward_and_inverse_round_trip():
    for R in (10.0, 36.26, 150.0):
        for th in (0.0, 3.0, 12.0, 45.0):
            dt = crack_blast_interval(R, th, mach=M, c=C)
            assert range_from_interval(dt, th, mach=M, c=C) == pytest.approx(R, rel=1e-12)
            assert theta_from_interval(dt, R, mach=M, c=C) == pytest.approx(th, abs=1e-9)


def test_lower_bound_is_never_violated_anywhere_in_the_cone():
    """The one claim that needs no angle: R >= c*dt/(1-1/M) for EVERY geometry."""
    for R in (5.0, 36.26, 200.0):
        for th in np.linspace(0.0, cone_limit_deg(mach=M) - 1e-6, 200):
            dt = crack_blast_interval(R, float(th), mach=M, c=C)
            assert range_lower_bound(dt, mach=M, c=C) <= R + 1e-9


def test_lower_bound_is_tight_only_on_the_trajectory():
    dt0 = crack_blast_interval(36.26, 0.0, mach=M, c=C)
    assert range_lower_bound(dt0, mach=M, c=C) == pytest.approx(36.26)
    dt = crack_blast_interval(36.26, 20.0, mach=M, c=C)
    assert range_lower_bound(dt, mach=M, c=C) < 0.6 * 36.26


def test_an_interval_above_the_ceiling_for_a_known_range_is_refused():
    """A measured interval larger than max_interval cannot be crack->blast from that shot."""
    assert theta_from_interval(max_interval(36.26, mach=M, c=C) * 1.05, 36.26, mach=M, c=C) is None


def test_subsonic_has_no_interval():
    with pytest.raises(ValueError):
        crack_blast_interval(36.26, 0.0, mach=0.9, c=C)
    with pytest.raises(ValueError):
        crack_blast_interval(36.26, 0.0, v_mps=300.0, c=C)


def test_exactly_one_of_speed_or_mach():
    with pytest.raises(ValueError):
        crack_blast_interval(36.26, 0.0, v_mps=900.0, mach=2.61, c=C)
    with pytest.raises(ValueError):
        crack_blast_interval(36.26, 0.0, c=C)


def test_measured_surveyed_interval_sits_under_its_own_ceiling():
    """2026-09-05 20:04:45, R = 36.26 m from RTK on both ends, dt = 55.3 ms measured."""
    assert 0.0553 < max_interval(36.26, mach=M, c=C)
    th = theta_from_interval(0.0553, 36.26, mach=M, c=C)
    assert th is not None and 0.0 < th < 12.0        # near the trajectory, as the flag line says


# ---------------------------------------------------------------- uncertainty


def test_fractional_interval_error_is_fractional_range_error():
    u = range_uncertainty(0.0538, 6.62, sigma_dt_s=0.000538, sigma_theta_deg=0.0,
                          sigma_v_mps=0.0, sigma_c_frac=0.0)
    assert u["interval"] == pytest.approx(0.01)
    assert u["total"] == pytest.approx(0.01)


def test_the_angle_dominates_at_the_small_angles_where_the_method_is_sharpest():
    u = range_uncertainty(0.0538, 6.62, sigma_dt_s=0.0015, sigma_theta_deg=5.0)
    assert u["theta"] > 4 * u["interval"]
    assert u["d_lnR_d_theta_per_deg"] == pytest.approx(0.0297, abs=0.002)


def test_sound_speed_enters_twice_so_its_coefficient_exceeds_one():
    u = range_uncertainty(0.0538, 6.62, sigma_dt_s=0.0, sigma_theta_deg=0.0)
    assert u["d_lnR_d_lnc"] > 1.5      # path AND Mach angle, not path alone


# ---------------------------------------------------------------- detector


def _click(fs, n, at_s, amp, f0, f1, dur_s, rng):
    x = np.zeros(n)
    i = int(at_s * fs)
    k = int(dur_s * fs)
    t = np.arange(k) / fs
    env = np.exp(-t / (dur_s / 4.0))
    sig = np.zeros(k)
    for f in np.linspace(f0, f1, 24):
        sig += np.sin(2 * math.pi * f * t + rng.uniform(0, 2 * math.pi))
    x[i:i + k] += amp * env * sig / 24.0
    return x


def test_band_power_measures_in_band_and_ignores_out_of_band():
    fs = 48000.0
    t = np.arange(int(0.2 * fs)) / fs
    _, p_in = band_power(np.sin(2 * math.pi * 400 * t), fs, (100.0, 1000.0))
    _, p_out = band_power(np.sin(2 * math.pi * 6000 * t), fs, (100.0, 1000.0))
    assert np.median(p_in) > 1e4 * np.median(p_out)


def test_band_power_does_not_leak_energy_backwards_beyond_half_a_frame():
    """A zero-phase IIR would light up long before the impulse.  This must not."""
    fs = 48000.0
    rng = np.random.default_rng(0)
    x = _click(fs, int(0.3 * fs), 0.150, 1.0, 200.0, 900.0, 0.003, rng)
    t, p = band_power(x, fs, (100.0, 1000.0), frame_s=0.008)
    early = p[t < 0.150 - 0.008]
    assert early.max() < 1e-6 * p.max()


def test_crack_onset_finds_the_shock_within_a_frame():
    fs = 48000.0
    rng = np.random.default_rng(1)
    x = _click(fs, int(0.4 * fs), 0.200, 1.0, 3000.0, 9000.0, 0.0006, rng)
    x += 1e-4 * rng.standard_normal(len(x))
    o = crack_onset(x, fs, (0.150, 0.260), floor_s=(0.0, 0.140))
    assert abs(o["t_s"] - 0.200) < 0.0015


def test_second_arrival_recovers_a_planted_separation():
    fs = 48000.0
    rng = np.random.default_rng(2)
    n = int(0.4 * fs)
    x = _click(fs, n, 0.100, 1.0, 3000.0, 9000.0, 0.0006, rng)          # crack
    x += _click(fs, n, 0.100 + 0.0538, 0.15, 150.0, 800.0, 0.004, rng)  # blast, 53.8 ms later
    x += 1e-4 * rng.standard_normal(n)
    o = crack_onset(x, fs, (0.060, 0.160), floor_s=(0.0, 0.050))
    s = second_arrival(x, fs, o["t_s"], frame_s=0.004)
    assert s is not None
    assert abs(s["dt_s"] - 0.0538) < 0.004
    assert s["rise_db"] > 12.0


def test_a_rise_on_noise_alone_is_NOT_zero_so_the_threshold_must_be_calibrated():
    """second_arrival always returns the maximum of its search window.  On noise that maximum is
    already ~10 dB above the trough, so 'there is a rise' is not a detection -- which is why
    rise_reference exists and why nothing here hard-codes a threshold."""
    fs = 48000.0
    rng = np.random.default_rng(3)
    n = int(2.0 * fs)
    x = 1e-4 * rng.standard_normal(n)
    ref = rise_reference(x, fs, np.linspace(0.2, 1.7, 300), frame_s=0.004)
    assert len(ref) >= 290          # a few anchors run the search window off the end
    assert 4.0 < np.median(ref) < 18.0
    assert np.percentile(ref, 99) < 25.0


def test_a_planted_arrival_stands_clear_of_that_noise_reference():
    fs = 48000.0
    rng = np.random.default_rng(4)
    n = int(0.4 * fs)
    x = _click(fs, n, 0.100, 1.0, 3000.0, 9000.0, 0.0006, rng)
    x += _click(fs, n, 0.100 + 0.0538, 0.15, 150.0, 800.0, 0.004, rng)
    x += 1e-4 * rng.standard_normal(n)
    o = crack_onset(x, fs, (0.060, 0.160), floor_s=(0.0, 0.050))
    planted = second_arrival(x, fs, o["t_s"], frame_s=0.004)["rise_db"]
    noise = rise_reference(1e-4 * rng.standard_normal(int(2.0 * fs)), fs,
                           np.linspace(0.2, 1.7, 300), frame_s=0.004)
    assert planted > np.percentile(noise, 99)


# ---------------------------------------------------------------- cross-mic bound


def test_a_bound_with_no_tolerance_is_refused():
    with pytest.raises(ValueError):
        interval_consistent({0: 0.0538, 1: 0.0538}, ESP_MICS, c=C)
    with pytest.raises(ValueError):
        interval_consistent({0: 0.0538, 1: 0.0538}, ESP_MICS, c=C, tol_s=1e-3, sigma_s=1e-4)


def test_identical_intervals_pass_every_pair():
    chk = interval_consistent({i: 0.0538 for i in range(6)}, ESP_MICS, c=C, tol_s=0.0)
    assert chk and chk["valid"] and chk["spread_s"] == 0.0
    assert isinstance(chk, IntervalCheck)


def test_a_difference_beyond_two_d_over_c_is_rejected():
    iv = {i: 0.0538 for i in range(6)}
    iv[4] = 0.0538 + 2.0 * 0.714 / C + 0.002        # rear board, 2 ms past its bound
    chk = interval_consistent(iv, ESP_MICS, c=C, tol_s=0.0)
    assert not chk and any("dt0-dt4" in r or "dt4" in r for r in chk["reasons"])


def test_a_per_channel_clock_offset_cancels_in_the_interval():
    """The point of the whole method on this hardware: the ESP ring's per-board offsets are tens
    of milliseconds and drift, and they subtract out of dt exactly."""
    crack = {0: 1.000, 1: 1.000, 2: 1.000, 3: 1.000, 4: 1.000, 5: 1.000}
    blast = {k: v + 0.0538 for k, v in crack.items()}
    off = {0: 0.0, 1: 0.0, 2: -0.0386, 3: -0.0386, 4: 0.0285, 5: 0.0285}   # measured 2026-09-05
    iv = {k: (blast[k] + off[k]) - (crack[k] + off[k]) for k in crack}
    chk = interval_consistent(iv, ESP_MICS, c=C, tol_s=0.0)
    assert chk["valid"] and chk["interval_s"] == pytest.approx(0.0538)


def test_sigma_gives_a_three_sigma_tolerance():
    iv = {0: 0.0538, 1: 0.0538 + 0.0009}            # 0.9 ms across a 38 mm pair (bound 0.22 ms)
    assert not interval_consistent(iv, ESP_MICS, c=C, tol_s=0.0)
    assert interval_consistent(iv, ESP_MICS, c=C, sigma_s=0.00025)      # 3*sqrt2*0.25 = 1.06 ms
    assert not interval_consistent(iv, ESP_MICS, c=C, sigma_s=0.00005)


def test_measured_2026_09_05_board_intervals():
    """Real board medians.  The surveyed string passes; the 21:03:47 event, where the detector
    disagreed by 24 ms across boards, is refused -- which is the gate earning its place."""
    surveyed = {0: 0.05230, 2: 0.05392, 4: 0.05345}          # right, left, rear (round 40.2615)
    assert interval_consistent(surveyed, ESP_MICS, c=C, sigma_s=0.00025)["valid"]
    broken = {0: 0.03216, 2: 0.05673, 4: 0.03928}            # 21:03:47.150
    chk = interval_consistent(broken, ESP_MICS, c=C, sigma_s=0.00025)
    assert not chk["valid"] and len(chk["reasons"]) >= 2
