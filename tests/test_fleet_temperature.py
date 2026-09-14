#!/usr/bin/env python3
"""Unit tests for fleet temperature averaging and speed-of-sound TDoA variance propagation."""

import math
import time
import pytest

from hear.solve import (
    EffectiveSoundSpeed,
    FleetTemperatureEstimate,
    FleetTemperatureProvider,
    physically_possible,
    propagate_pairwise_tdoa_uncertainty,
    propagate_tdoa_variance,
    sound_speed,
    temperature_c,
    temperature_sigma_c,
)


def test_sound_speed_acoustic_formula():
    """Verify acoustic formula c = 331.3 + 0.606 * T."""
    assert sound_speed(0.0) == pytest.approx(331.3)
    assert sound_speed(20.0) == pytest.approx(343.42)
    assert sound_speed(25.0) == pytest.approx(346.45)
    assert sound_speed(-10.0) == pytest.approx(325.24)

    # Round trip temperature check
    for t in (-20.0, -10.0, 0.0, 15.0, 20.0, 30.0, 45.0):
        c = sound_speed(t)
        assert temperature_c(c) == pytest.approx(t, abs=1e-6)


def test_temperature_sigma_conversion():
    """Verify linear derivative dc/dT = 0.606 m/s per degC."""
    # 1 degC uncertainty -> 0.606 m/s uncertainty
    assert temperature_sigma_c(343.42, 0.606) == pytest.approx(1.0)
    # 2.0 m/s uncertainty -> ~3.3003 degC uncertainty
    assert temperature_sigma_c(343.42, 2.0) == pytest.approx(2.0 / 0.606)


def test_fleet_temperature_provider_fallback():
    """When no nodes report temperature, fallback default is used."""
    provider = FleetTemperatureProvider(fallback_temp_c=20.0, fallback_sigma_temp_c=4.0)
    est = provider.get_fleet_temperature()

    assert est.node_count == 0
    assert est.mean_temp_c == pytest.approx(20.0)
    assert est.sigma_temp_c == pytest.approx(4.0)
    assert est.source == "fallback_default"

    # Effective sound speed for bare mic node
    eff = provider.get_effective_sound_speed("bare_mic_1")
    assert not eff.is_local
    assert eff.temp_c == pytest.approx(20.0)
    assert eff.c_mps == pytest.approx(331.3 + 0.606 * 20.0)
    assert eff.sigma_c_mps == pytest.approx(0.606 * 4.0)
    assert eff.variance_c == pytest.approx((0.606 * 4.0) ** 2)
    assert eff.source == "fallback_default"


def test_fleet_temperature_provider_single_node():
    """Single node reporting temperature."""
    provider = FleetTemperatureProvider(default_sensor_sigma_c=0.5)
    now = time.time()
    assert provider.update_node_temperature("node_a", 25.0, timestamp_s=now)

    est = provider.get_fleet_temperature(now_s=now)
    assert est.node_count == 1
    assert est.mean_temp_c == pytest.approx(25.0)
    assert est.source == "measured_n1"

    # Node A gets its local temperature
    eff_a = provider.get_effective_sound_speed("node_a", now_s=now)
    assert eff_a.is_local
    assert eff_a.temp_c == pytest.approx(25.0)
    assert eff_a.c_mps == pytest.approx(331.3 + 0.606 * 25.0)  # 346.45 m/s
    assert eff_a.source == "local_node_a"

    # Bare mic node gets fleet average (25.0 °C from node_a)
    eff_bare = provider.get_effective_sound_speed("bare_mic_1", now_s=now)
    assert not eff_bare.is_local
    assert eff_bare.temp_c == pytest.approx(25.0)
    assert eff_bare.c_mps == pytest.approx(346.45)
    assert eff_bare.source == "measured_n1"


def test_fleet_temperature_provider_multiple_nodes_averaging():
    """Multiple network nodes reporting temperatures are aggregated."""
    provider = FleetTemperatureProvider(default_sensor_sigma_c=0.5)
    now = time.time()

    provider.update_node_temperature("node_1", 18.0, timestamp_s=now)
    provider.update_node_temperature("node_2", 20.0, timestamp_s=now)
    provider.update_node_temperature("node_3", 22.0, timestamp_s=now)

    est = provider.get_fleet_temperature(now_s=now)
    assert est.node_count == 3
    assert est.mean_temp_c == pytest.approx(20.0)
    # Sample variance of [18, 20, 22] is 4.0 (degC^2), sigma = 2.0 °C
    assert est.variance_temp_c2 == pytest.approx(4.0, rel=0.05)
    assert est.sigma_temp_c == pytest.approx(2.0, rel=0.05)
    assert est.source == "measured_n3"

    # Bare mic node uses fleet average (20.0 °C)
    eff_bare = provider.get_effective_sound_speed("bare_mic_node", now_s=now)
    assert not eff_bare.is_local
    assert eff_bare.temp_c == pytest.approx(20.0)
    assert eff_bare.c_mps == pytest.approx(343.42)
    assert eff_bare.sigma_c_mps == pytest.approx(0.606 * est.sigma_temp_c)


def test_fleet_temperature_provider_staleness():
    """Stale temperature readings (> max_age_s) are excluded."""
    provider = FleetTemperatureProvider(max_age_s=300.0, fallback_temp_c=15.0)
    now = time.time()

    # Node 1 reported 400s ago (stale)
    provider.update_node_temperature("node_stale", 30.0, timestamp_s=now - 400.0)
    # Node 2 reported 50s ago (fresh)
    provider.update_node_temperature("node_fresh", 22.0, timestamp_s=now - 50.0)

    est = provider.get_fleet_temperature(now_s=now)
    assert est.node_count == 1
    assert est.mean_temp_c == pytest.approx(22.0)

    # Stale node's local query falls back to fresh fleet average
    eff_stale = provider.get_effective_sound_speed("node_stale", now_s=now)
    assert not eff_stale.is_local
    assert eff_stale.temp_c == pytest.approx(22.0)


def test_unphysical_temperature_rejection():
    """Temperatures outside physical bounds (-50 to +60 C) are rejected."""
    provider = FleetTemperatureProvider()
    assert not provider.update_node_temperature("bad_1", -100.0)
    assert not provider.update_node_temperature("bad_2", 150.0)
    assert provider.get_fleet_temperature().node_count == 0


def test_telemetry_dict_parsing():
    """Provider parses temperature from various telemetry structures."""
    provider = FleetTemperatureProvider()
    now = time.time()

    # Direct temp_c
    assert provider.update_node_telemetry("node_a", {"temp_c": 21.0, "ts_utc_ms": int(now * 1000)})
    # Nested env
    assert provider.update_node_telemetry("node_b", {"env": {"temp_c": 23.0}, "ts_utc_ms": int(now * 1000)})
    # From sound_speed_mps
    assert provider.update_node_telemetry("node_c", {"sound_speed_mps": 346.45, "ts_utc_ms": int(now * 1000)})

    est = provider.get_fleet_temperature(now_s=now)
    assert est.node_count == 3
    assert est.mean_temp_c == pytest.approx((21.0 + 23.0 + 25.0) / 3.0, abs=0.1)


def test_propagate_tdoa_variance():
    """Test TDoA range variance propagation: sigma_d^2 = tau^2 * sigma_c^2 + c^2 * sigma_tau^2."""
    tau_s = 0.100       # 100 ms TDoA delay
    sigma_tau_s = 1e-4  # 100 us timing uncertainty
    c_mps = 340.0
    sigma_c_mps = 1.0   # 1 m/s sound speed uncertainty (~1.65 °C)

    res = propagate_tdoa_variance(tau_s, sigma_tau_s, c_mps, sigma_c_mps)

    expected_range_m = 340.0 * 0.100  # 34.0 m
    # var_d = (0.100 * 1.0)^2 + (340.0 * 1e-4)^2 = 0.01 + 0.001156 = 0.011156 m^2
    expected_var_d = (0.100 * 1.0) ** 2 + (340.0 * 1e-4) ** 2
    expected_sigma_d = math.sqrt(expected_var_d)

    assert res["range_diff_m"] == pytest.approx(expected_range_m)
    assert res["range_diff_var_m2"] == pytest.approx(expected_var_d)
    assert res["range_diff_sigma_m"] == pytest.approx(expected_sigma_d)


def test_propagate_pairwise_tdoa_uncertainty():
    """Pairwise baseline propagation with Node A (local sensor) and Node B (bare mic node)."""
    provider = FleetTemperatureProvider(default_sensor_sigma_c=0.5)
    now = time.time()

    # Node A local sensor = 25 °C (c = 346.45 m/s)
    provider.update_node_temperature("node_a", 25.0, timestamp_s=now)
    # Node C sensor = 15 °C (c = 340.39 m/s) -> Fleet mean = 20 °C (c = 343.42 m/s)
    provider.update_node_temperature("node_c", 15.0, timestamp_s=now)

    # Node B is a bare mic node -> gets fleet average 20 °C
    tau_s = 0.050      # 50 ms delay
    sigma_tau_s = 5e-5 # 50 us
    d_baseline = 20.0  # 20 m baseline

    res = propagate_pairwise_tdoa_uncertainty(
        "node_a", "bare_mic_b", tau_s, sigma_tau_s,
        baseline_distance_m=d_baseline, provider=provider, now_s=now
    )

    assert res["node_a_id"] == "node_a"
    assert res["node_b_id"] == "bare_mic_b"
    assert res["c_node_a_mps"] == pytest.approx(346.45)
    assert res["c_node_b_mps"] == pytest.approx(343.42)
    # Path average c = (346.45 + 343.42) / 2 = 344.935 m/s
    assert res["c_mps"] == pytest.approx(344.935)
    assert res["range_diff_m"] == pytest.approx(344.935 * 0.050)
    assert "max_tdoa_bound_s" in res
    assert res["max_tdoa_bound_s"] == pytest.approx(20.0 / 344.935)


def test_physically_possible_with_sound_speed_sigma():
    """physically_possible bound expands when sound speed uncertainty sigma_c > 0."""
    d_m = 34.3            # 34.3 m baseline
    c_mps = 343.0         # nominal c -> max delay d/c = 0.100 s (100 ms)
    tau_exact_endfire = 0.1001  # 100.1 ms (slightly past nominal bound)

    # Without sigma_c, 100.1 ms is rejected for 34.3 m / 343 m/s
    assert not physically_possible(tau_exact_endfire, d_m, c_mps, tol_s=0.0)

    # With sigma_c = 1.0 m/s (~1.65 °C temp uncertainty),
    # bound_s expansion = (34.3 / 343^2) * 1.0 = 0.0002917 s (~0.29 ms)
    # New bound = 0.1002917 s > 0.1001 s -> accepted!
    assert physically_possible(tau_exact_endfire, d_m, c_mps, tol_s=0.0, sigma_c=1.0)
