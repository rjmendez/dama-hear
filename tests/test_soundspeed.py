#!/usr/bin/env python3
"""Tests for hear.solve.soundspeed."""
import math

import numpy as np
import pytest

from hear.solve.soundspeed import (
    Budget, SoundSpeedUnrecoverable, absolute_budget, apparent_speed, determines_speed,
    fractional_speed_error, recover_from_baseline, recover_from_delays, sound_speed,
    speed_from_baseline, speed_upper_bound, temperature_c, temperature_sigma_c,
)

# The three surveyed phone positions of 2026-09-05, ENU metres about the map origin.
FANCY = np.array([5.14398715, -19.75622501, 243.68])     # 'South triangle'
FIN = np.array([13.74922948, -1.93390216, 240.76])       # 'triangle north'
MYA = np.array([13.88748858, -14.87031993, 242.15])      # 'target'
PHONES = np.array([FANCY, FIN, MYA])


# ── the relation and its inverse ────────────────────────────────────────────────────────────────

def test_sound_speed_round_trips_through_temperature():
    for t in (-10.0, 0.0, 20.0, 23.0, 40.0):
        assert temperature_c(sound_speed(t)) == pytest.approx(t, abs=1e-9)


def test_23_c_is_the_number_the_session_assumed():
    assert sound_speed(23.0) == pytest.approx(345.238, abs=5e-4)


def test_one_degree_is_0_176_percent_of_c():
    assert fractional_speed_error(1.0) == pytest.approx(0.00176, rel=0.02)


def test_temperature_sigma_is_speed_sigma_over_0_606():
    assert temperature_sigma_c(345.0, 0.606) == pytest.approx(1.0)


# ── the baseline inversion ──────────────────────────────────────────────────────────────────────

def test_speed_from_baseline_inverts_a_synthetic_delay():
    c = sound_speed(17.0)
    d = 10.132
    assert speed_from_baseline(d / c, d) == pytest.approx(c, rel=1e-12)


def test_speed_from_baseline_refuses_a_zero_delay():
    with pytest.raises(ValueError):
        speed_from_baseline(0.0, 10.0)


def test_a_phone_at_the_muzzle_buys_about_two_degrees():
    """The measurement that WAS available and was not taken: sync sigma 110 us on 10.13 m."""
    v = recover_from_baseline(10.132 / 345.238, 10.132, 110e-6)
    assert v.valid
    assert v.temp_c == pytest.approx(23.0, abs=0.05)
    assert 1.5 < v.temp_sigma_c < 3.0


def test_the_widest_phone_pair_would_have_been_better_still():
    near = recover_from_baseline(10.132 / 345.238, 10.132, 110e-6)
    far = recover_from_baseline(20.005 / 345.238, 20.005, 110e-6)
    assert far.temp_sigma_c < near.temp_sigma_c


def test_baseline_error_propagates_alongside_timing_error():
    tight = recover_from_baseline(10.132 / 345.238, 10.132, 110e-6, sigma_d_m=0.0)
    loose = recover_from_baseline(10.132 / 345.238, 10.132, 110e-6, sigma_d_m=0.5)
    assert loose.temp_sigma_c > tight.temp_sigma_c


def test_a_refused_verdict_will_not_hand_over_a_temperature():
    v = recover_from_baseline(0.0, 10.0, 1e-4)
    assert not v.valid
    with pytest.raises(SoundSpeedUnrecoverable):
        v.require_temperature()


# ── the one-sided bound ─────────────────────────────────────────────────────────────────────────

def test_speed_upper_bound_is_the_plane_wave_bound_read_the_other_way():
    assert speed_upper_bound(0.02742, 10.132) == pytest.approx(369.5, rel=1e-3)


def test_the_2026_09_05_phone_bound_excludes_only_absurd_temperatures():
    """Measured: the best gated phone pair gives c <= 369.5 m/s, i.e. T <= 63 C."""
    assert temperature_c(speed_upper_bound(0.02742, 10.132)) == pytest.approx(63.0, abs=0.5)


def test_a_zero_delay_bounds_nothing():
    assert speed_upper_bound(0.0, 10.0) == math.inf


# ── apparent speed is an upper bound, never an estimate ─────────────────────────────────────────

def test_apparent_speed_recovers_c_for_a_wave_in_the_receiver_plane():
    c = 345.238
    P = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 12.0, 0.0]])
    u = np.array([0.6, 0.8, 0.0])                     # horizontal, in the receivers' plane
    t = -(P @ u) / c
    assert apparent_speed(P, t) == pytest.approx(c, rel=1e-9)


def test_apparent_speed_is_inflated_by_elevation_and_never_deflated():
    c = 345.238
    P = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [0.0, 12.0, 0.0]])
    for elev in (5.0, 15.0, 30.0):
        r = math.radians(elev)
        u = np.array([0.6 * math.cos(r), 0.8 * math.cos(r), math.sin(r)])
        t = -(P @ u) / c
        assert apparent_speed(P, t) == pytest.approx(c / math.cos(r), rel=1e-9)
        assert apparent_speed(P, t) >= c


def test_apparent_speed_needs_three_receivers():
    with pytest.raises(ValueError):
        apparent_speed(np.array([[0.0, 0.0], [1.0, 0.0]]), [0.0, 1.0])


# ── the counting argument ───────────────────────────────────────────────────────────────────────

def test_three_ground_receivers_and_an_unknown_source_cannot_determine_c():
    d = determines_speed(3, n_events=1, dim=2)
    assert not d.determined and d.deficit == 1


def test_more_firing_points_do_not_rescue_three_receivers():
    """Each new source brings its own two unknowns; the deficit stays at one for ever."""
    for n in (1, 2, 5, 50):
        assert determines_speed(3, n_events=n, dim=2).deficit == 1


def test_one_surveyed_source_closes_it():
    assert determines_speed(3, n_events=1, dim=2, n_known_sources=1).determined


def test_a_fourth_receiver_closes_it_too():
    assert determines_speed(4, n_events=1, dim=2).determined


def test_uncalibrated_per_device_mic_latency_reopens_it():
    """Phone clips ship `mic_bias_ns=0  UNCALIBRATED`; two unknown offsets undo the fourth node."""
    assert determines_speed(4, n_events=1, dim=2).determined
    assert not determines_speed(4, n_events=1, dim=2, n_unknown_clock_offsets=3).determined


def test_determinacy_rejects_impossible_inputs():
    with pytest.raises(ValueError):
        determines_speed(1)
    with pytest.raises(ValueError):
        determines_speed(3, n_events=1, n_known_sources=2)


# ── the top-level call refuses the 2026-09-05 configuration ─────────────────────────────────────

def test_recover_from_delays_refuses_three_phones_with_an_unknown_source():
    c = 345.238
    S = np.array([8.16, -14.97, 242.5])
    d = np.linalg.norm(PHONES - S, axis=1)
    taus = {(0, 1): (d[1] - d[0]) / c, (0, 2): (d[2] - d[0]) / c, (1, 2): (d[2] - d[1]) / c}
    v = recover_from_delays(taus, PHONES)
    assert not v.valid
    assert v.temp_c is None
    assert v.c_upper_mps is not None and v.c_upper_mps > c


def test_the_same_delays_invert_cleanly_once_the_source_is_surveyed():
    c = 345.238
    S = np.array([8.16, -14.97, 242.5])
    d = np.linalg.norm(PHONES - S, axis=1)
    taus = {(0, 1): (d[1] - d[0]) / c, (0, 2): (d[2] - d[0]) / c, (1, 2): (d[2] - d[1]) / c}
    v = recover_from_delays(taus, PHONES, source_position=S)
    assert v.valid
    assert v.c_mps == pytest.approx(c, rel=1e-9)
    assert v.temp_c == pytest.approx(23.0, abs=1e-3)


def test_a_wrong_assumed_source_is_caught_by_the_pairs_disagreeing():
    c = 345.238
    S = np.array([8.16, -14.97, 242.5])
    d = np.linalg.norm(PHONES - S, axis=1)
    taus = {(0, 1): (d[1] - d[0]) / c, (0, 2): (d[2] - d[0]) / c, (1, 2): (d[2] - d[1]) / c}
    v = recover_from_delays(taus, PHONES, source_position=np.array([13.887, -14.870, 242.15]))
    assert not v.valid
    assert any('disagree' in r or 'not a speed of sound' in r for r in v.reasons)


def test_the_degeneracy_is_real_not_a_conditioning_problem():
    """A source refitted at 320 and at 360 m/s reproduces the SAME delays exactly.

    This is the measured 2026-09-05 result in synthetic form: the fitted source moves less
    than a metre across a 12.5% span in c, so nothing in the delays distinguishes them.
    """
    from scipy.optimize import least_squares
    c_true, z = 345.238, 242.5
    S = np.array([8.16, -14.97, z])
    d = np.linalg.norm(PHONES - S, axis=1)
    obs = np.array([(d[1] - d[0]) / c_true, (d[2] - d[0]) / c_true])

    def refit(c):
        def res(p):
            dd = np.linalg.norm(PHONES - np.array([p[0], p[1], z]), axis=1)
            return [(dd[1] - dd[0]) / c - obs[0], (dd[2] - dd[0]) / c - obs[1]]
        r = least_squares(res, [0.0, 0.0], xtol=1e-14, ftol=1e-14)
        return r.x, float(np.max(np.abs(r.fun)))

    cold, r_cold = refit(320.0)
    hot, r_hot = refit(360.0)
    assert r_cold < 1e-6 and r_hot < 1e-6            # both fit the delays perfectly
    assert np.linalg.norm(hot - cold) < 2.0          # and the source barely moves


# ── budgets ─────────────────────────────────────────────────────────────────────────────────────

def test_the_surveyed_string_needs_185_us_for_one_degree():
    b = absolute_budget(36.26, 1.0, c=345.238)
    assert b.path_time_s == pytest.approx(0.10503, rel=1e-3)
    assert b.required_sigma_s == pytest.approx(184.7e-6, rel=0.01)


def test_the_ring_anchor_is_two_orders_too_coarse_for_that_path():
    """Ring timebase sigma measured 41.6 ms on 2026-09-04 against a 105 ms path."""
    b = absolute_budget(36.26, 1.0, c=345.238, available_sigma_s=0.0416)
    assert b.feasible is False
    assert b.shortfall > 100
    assert b.achievable_dT_c() > 100


def test_a_budget_without_a_clock_states_the_requirement_and_nothing_else():
    b = absolute_budget(36.26, 1.0, c=345.238)
    assert b.feasible is None and b.shortfall is None and b.achievable_dT_c() is None
    assert 'needs the path timed to' in b.summary()


def test_budget_rejects_impossible_inputs():
    with pytest.raises(ValueError):
        absolute_budget(0.0, 1.0)
    with pytest.raises(ValueError):
        absolute_budget(36.0, 0.0)


# ── sizing the known-source (phone chirp) geometry ──────────────────────────────────────────────

def test_the_required_separation_scales_the_way_the_algebra_says():
    from hear.solve.soundspeed import separation_for_temperature, temperature_from_separation
    r = separation_for_temperature(1.0, 0.028, 0.0, c=345.238)
    assert r["required_separation_m"] == pytest.approx(345.238 * 0.028 / 0.606, rel=1e-12)
    assert separation_for_temperature(0.5, 0.028, 0.0)["required_separation_m"] == \
        pytest.approx(2 * separation_for_temperature(1.0, 0.028, 0.0)["required_separation_m"])
    back = temperature_from_separation(r["required_separation_m"], 0.028, 0.0, c=345.238)
    assert back["dT_c"] == pytest.approx(1.0, rel=1e-9)


def test_the_existing_pair_cannot_measure_air_temperature_at_all():
    """MEASURED from survey.json: sigma_m 0.717 and 0.521 m differenced is 0.89 m against a
    maximum range difference of 16.873 m. The clock is irrelevant here -- the timing term is
    0.3 degC and the survey term is 30."""
    from hear.solve.soundspeed import temperature_from_separation
    r = temperature_from_separation(16.873, 0.890, math.sqrt(2) * 19.3e-6, c=345.238)
    assert r["dT_c"] > 25.0
    assert r["dT_c_survey_term"] / r["dT_c_timing_term"] > 50.0


def test_rtk_nodes_reach_one_degree_on_the_existing_baseline_and_need_more_for_half():
    from hear.solve.soundspeed import separation_for_temperature, temperature_from_separation
    rtk = temperature_from_separation(16.873, 0.028, math.sqrt(2) * 19.3e-6, c=345.238)
    assert 0.8 < rtk["dT_c"] < 1.2
    need = separation_for_temperature(0.5, 0.028, math.sqrt(2) * 19.3e-6, c=345.238)
    assert 30.0 < need["required_separation_m"] < 40.0


def test_it_refuses_impossible_targets():
    from hear.solve.soundspeed import separation_for_temperature, temperature_from_separation
    with pytest.raises(ValueError):
        separation_for_temperature(0.0, 0.02)
    with pytest.raises(ValueError):
        separation_for_temperature(1.0, -0.02)
    with pytest.raises(ValueError):
        temperature_from_separation(0.0, 0.02)
