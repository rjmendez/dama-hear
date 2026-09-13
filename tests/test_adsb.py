#!/usr/bin/env python3
"""Tests for hear.adsb: ingestion, geometry, CPA, Doppler and acoustic-flightpath correlation."""
import json
import math

import pytest

from hear import adsb as ADSB
from hear.backend import survey as SV

# A tiny two-node survey with a REAL (non-fictional) origin, so require_real_origin=False loads
# it plainly and the geometry helpers have somewhere to convert lat/lon against.
ORIGIN = {"lat_deg": 40.0, "lon_deg": -79.0, "h_ell_m": 0.0}
SURVEY_DOC = {
    "frame": "enu_local", "units": "m", "origin": ORIGIN,
    "nodes": [{"node_id": 1, "name": "nyquist", "e_m": 0.0, "n_m": 0.0, "u_m": 0.0,
              "sigma_m": 0.5}],
}


@pytest.fixture
def sy():
    return SV.from_dict(SURVEY_DOC, min_nodes=1)


def _aircraft_json(now, aircraft):
    return {"now": now, "aircraft": aircraft}


# ── ingestion ────────────────────────────────────────────────────────────────────────────────

class TestParseAircraftJson:

    def test_a_positioned_aircraft_is_parsed_with_seen_subtracted_from_now(self):
        doc = _aircraft_json(1000.0, [{"hex": "A1B2C3", "flight": "UAL123 ", "lat": 40.01,
                                       "lon": -79.0, "alt_baro": 5000, "gs": 250, "track": 90,
                                       "baro_rate": -500, "seen": 2.5}])
        svs = ADSB.parse_aircraft_json(doc)
        assert len(svs) == 1
        sv = svs[0]
        assert sv.icao == "a1b2c3"                 # lower-cased
        assert sv.callsign == "UAL123"              # stripped
        assert sv.lat_deg == 40.01 and sv.alt_ft == 5000
        assert sv.ground_speed_kts == 250 and sv.track_deg == 90
        assert sv.vertical_rate_fpm == -500
        assert sv.ts_s == pytest.approx(997.5)

    def test_an_aircraft_with_no_position_yet_is_skipped_not_zeroed(self):
        doc = _aircraft_json(1000.0, [{"hex": "DEAD01", "alt_baro": 3000}])
        assert ADSB.parse_aircraft_json(doc) == []

    def test_alternate_dialect_field_names_are_accepted(self):
        doc = _aircraft_json(500.0, [{"icao24": "beef01", "callsign": "N1", "lat": 1.0,
                                      "lon": 2.0, "altitude": 1200, "speed": 90,
                                      "true_heading": 10, "vert_rate": 0, "seen": 0}])
        sv = ADSB.parse_aircraft_json(doc)[0]
        assert sv.icao == "beef01" and sv.alt_ft == 1200 and sv.ground_speed_kts == 90

    def test_missing_altitude_is_skipped(self):
        doc = _aircraft_json(1.0, [{"hex": "ABCDEF", "lat": 1.0, "lon": 2.0}])
        assert ADSB.parse_aircraft_json(doc) == []

    def test_missing_now_falls_back_to_wall_clock_without_raising(self):
        doc = {"aircraft": [{"hex": "ABCDEF", "lat": 1.0, "lon": 2.0, "alt_baro": 100}]}
        svs = ADSB.parse_aircraft_json(doc)
        assert len(svs) == 1 and svs[0].ts_s > 0

    def test_non_dict_aircraft_entries_are_ignored(self):
        doc = _aircraft_json(1.0, ["not-a-dict", None, 42])
        assert ADSB.parse_aircraft_json(doc) == []

    def test_an_entry_with_no_icao_is_skipped(self):
        doc = _aircraft_json(1.0, [{"lat": 1.0, "lon": 2.0, "alt_baro": 100}])
        assert ADSB.parse_aircraft_json(doc) == []


class TestFileAndReplayIngestion:

    def test_load_aircraft_json_file_round_trips(self, tmp_path):
        p = tmp_path / "aircraft.json"
        p.write_text(json.dumps(_aircraft_json(10.0, [{"hex": "aa0001", "lat": 1.0, "lon": 1.0,
                                                        "alt_baro": 1000, "seen": 0}])))
        svs = ADSB.load_aircraft_json_file(str(p))
        assert len(svs) == 1 and svs[0].icao == "aa0001"

    def test_replay_ndjson_yields_one_snapshot_per_line_in_order(self, tmp_path):
        p = tmp_path / "capture.ndjson"
        lines = [json.dumps(_aircraft_json(t, [{"hex": "aa0001", "lat": 1.0, "lon": 1.0,
                                                "alt_baro": 1000, "seen": 0}]))
                for t in (1.0, 2.0, 3.0)]
        p.write_text("\n".join(lines) + "\n")
        snapshots = list(ADSB.replay_ndjson(str(p)))
        assert [s[0].ts_s for s in snapshots] == [1.0, 2.0, 3.0]

    def test_replay_ndjson_skips_a_malformed_line_rather_than_failing(self, tmp_path):
        p = tmp_path / "capture.ndjson"
        good = json.dumps(_aircraft_json(1.0, [{"hex": "aa0001", "lat": 1.0, "lon": 1.0,
                                                "alt_baro": 1000, "seen": 0}]))
        p.write_text(good + "\n" + "{not json" + "\n" + "\n")
        snapshots = list(ADSB.replay_ndjson(str(p)))
        assert len(snapshots) == 1


class TestDiscoverLocalSource:

    def test_no_source_answering_returns_none_without_raising(self):
        # port 1 is a reserved/unlikely-bound port; nothing there should ever answer this probe.
        assert ADSB.discover_local_source(hosts=["127.0.0.1"], ports=[1], timeout=0.2) is None


# ── geometry ─────────────────────────────────────────────────────────────────────────────────

class TestGeometry:

    def test_directly_overhead_is_90_degrees_elevation_any_azimuth(self, sy):
        sv = ADSB.StateVector(icao="a", callsign=None, lat_deg=40.0, lon_deg=-79.0,
                              alt_ft=1000.0 / ADSB.FT_TO_M, ground_speed_kts=0, track_deg=0,
                              vertical_rate_fpm=0, ts_s=0.0)
        geo = ADSB.compute_geometry(sv, sy)
        assert geo.elevation_deg == pytest.approx(90.0, abs=1e-6)
        assert geo.slant_range_m == pytest.approx(1000.0, rel=1e-6)

    def test_due_east_on_the_horizon_is_azimuth_90_elevation_0(self, sy):
        # 1 degree of longitude at the equator-ish latitude is used loosely here; the exact
        # metres do not matter, only that displacement is purely eastward at the origin's height.
        from hear import geodesy as GEO
        lat, lon, h = GEO.enu_to_geodetic(2000.0, 0.0, 0.0, *sy.origin_geodetic())
        sv = ADSB.StateVector(icao="a", callsign=None, lat_deg=lat, lon_deg=lon,
                              alt_ft=h / ADSB.FT_TO_M, ground_speed_kts=0, track_deg=0,
                              vertical_rate_fpm=0, ts_s=0.0)
        geo = ADSB.compute_geometry(sv, sy)
        assert geo.azimuth_deg == pytest.approx(90.0, abs=1e-3)
        assert geo.elevation_deg == pytest.approx(0.0, abs=1e-3)
        assert geo.slant_range_m == pytest.approx(2000.0, rel=1e-6)

    def test_acoustic_delay_is_range_over_sound_speed(self, sy):
        from hear.solve.shockwave import sound_speed
        sv = ADSB.StateVector(icao="a", callsign=None, lat_deg=40.0, lon_deg=-79.0,
                              alt_ft=343.0 / ADSB.FT_TO_M, ground_speed_kts=0, track_deg=0,
                              vertical_rate_fpm=0, ts_s=0.0)
        geo = ADSB.compute_geometry(sv, sy, temp_c=15.0)
        assert geo.acoustic_delay_s == pytest.approx(343.0 / sound_speed(15.0), rel=1e-9)


class TestClosestPointOfApproach:

    def test_a_level_flyover_directly_over_the_origin_has_zero_cpa_range(self, sy):
        # Aircraft 10 km west of the origin, flying due east at 100 m/s, 1000 m up: it passes
        # directly over the origin, so CPA range should be its altitude and t* should be positive
        # (still ahead of the report) since it starts west of the array.
        from hear import geodesy as GEO
        lat, lon, h = GEO.enu_to_geodetic(-10000.0, 0.0, 1000.0, *sy.origin_geodetic())
        sv = ADSB.StateVector(icao="a", callsign=None, lat_deg=lat, lon_deg=lon,
                              alt_ft=h / ADSB.FT_TO_M, ground_speed_kts=100 / ADSB.KT_TO_MPS,
                              track_deg=90, vertical_rate_fpm=0, ts_s=0.0)
        cpa = ADSB.closest_point_of_approach(sv, sy)
        assert cpa.range_m == pytest.approx(1000.0, abs=1e-2)
        assert cpa.t_s == pytest.approx(100.0, rel=1e-3)   # 10000 m / 100 m/s

    def test_a_receding_aircraft_has_a_cpa_in_the_past(self, sy):
        from hear import geodesy as GEO
        lat, lon, h = GEO.enu_to_geodetic(10000.0, 0.0, 1000.0, *sy.origin_geodetic())
        sv = ADSB.StateVector(icao="a", callsign=None, lat_deg=lat, lon_deg=lon,
                              alt_ft=h / ADSB.FT_TO_M, ground_speed_kts=100 / ADSB.KT_TO_MPS,
                              track_deg=90, vertical_rate_fpm=0, ts_s=0.0)
        cpa = ADSB.closest_point_of_approach(sv, sy)
        assert cpa.t_s < 0.0

    def test_a_stationary_report_returns_the_reported_fix_itself(self, sy):
        sv = ADSB.StateVector(icao="a", callsign=None, lat_deg=40.0, lon_deg=-79.0,
                              alt_ft=1000.0 / ADSB.FT_TO_M, ground_speed_kts=0, track_deg=0,
                              vertical_rate_fpm=0, ts_s=42.0)
        cpa = ADSB.closest_point_of_approach(sv, sy)
        assert cpa.t_s == 0.0
        assert cpa.ts_unix_s == 42.0


class TestDopplerCurve:

    def test_ratio_is_above_one_while_approaching_and_below_while_receding(self, sy):
        from hear import geodesy as GEO
        # Directly overhead AT t=0 (CPA is at t*=0 for a purely-eastbound track with e0=0), so
        # every negative sample time is unambiguously before CPA (approaching) and every positive
        # sample time is unambiguously after (receding).
        lat, lon, h = GEO.enu_to_geodetic(0.0, 0.0, 500.0, *sy.origin_geodetic())
        sv = ADSB.StateVector(icao="a", callsign=None, lat_deg=lat, lon_deg=lon,
                              alt_ft=h / ADSB.FT_TO_M, ground_speed_kts=150 / ADSB.KT_TO_MPS,
                              track_deg=90, vertical_rate_fpm=0, ts_s=0.0)
        curve = ADSB.doppler_curve(sv, sy, t_start_s=-30.0, t_end_s=30.0, dt_s=5.0)
        before = [r for t, _, r in curve if t < -5.0]
        after = [r for t, _, r in curve if t > 5.0]
        assert all(r > 1.0 for r in before)
        assert all(r < 1.0 for r in after)

    def test_rejects_a_backwards_time_range(self, sy):
        sv = ADSB.StateVector(icao="a", callsign=None, lat_deg=40.0, lon_deg=-79.0,
                              alt_ft=1000.0, ground_speed_kts=100, track_deg=0,
                              vertical_rate_fpm=0, ts_s=0.0)
        with pytest.raises(ValueError):
            ADSB.doppler_curve(sv, sy, t_start_s=10.0, t_end_s=-10.0)

    def test_rejects_a_non_positive_step(self, sy):
        sv = ADSB.StateVector(icao="a", callsign=None, lat_deg=40.0, lon_deg=-79.0,
                              alt_ft=1000.0, ground_speed_kts=100, track_deg=0,
                              vertical_rate_fpm=0, ts_s=0.0)
        with pytest.raises(ValueError):
            ADSB.doppler_curve(sv, sy, dt_s=0.0)


# ── correlation ──────────────────────────────────────────────────────────────────────────────

class TestCorrelate:

    def _overhead_flyover(self, sy, ts_s=0.0):
        """An aircraft 5 km west, 500 m up, flying due east at 120 m/s -- passes over the array a
        little over 41 s after `ts_s`."""
        from hear import geodesy as GEO
        lat, lon, h = GEO.enu_to_geodetic(-5000.0, 0.0, 500.0, *sy.origin_geodetic())
        return ADSB.StateVector(icao="flyby", callsign="TEST1", lat_deg=lat, lon_deg=lon,
                                alt_ft=h / ADSB.FT_TO_M,
                                ground_speed_kts=120 / ADSB.KT_TO_MPS, track_deg=90,
                                vertical_rate_fpm=0, ts_s=ts_s)

    def test_an_observation_near_the_predicted_arrival_time_and_bearing_matches(self, sy):
        sv = self._overhead_flyover(sy)
        cpa = ADSB.closest_point_of_approach(sv, sy)
        geo_at_cpa = ADSB.compute_geometry(
            ADSB.StateVector(icao=sv.icao, callsign=sv.callsign, lat_deg=sv.lat_deg,
                             lon_deg=sv.lon_deg, alt_ft=sv.alt_ft,
                             ground_speed_kts=sv.ground_speed_kts, track_deg=sv.track_deg,
                             vertical_rate_fpm=sv.vertical_rate_fpm, ts_s=sv.ts_s + cpa.t_s), sy)
        obs = ADSB.AcousticObservation(ts_s=cpa.ts_unix_s + cpa.range_m / 343.0,
                                       azimuth_deg=geo_at_cpa.azimuth_deg)
        matches = ADSB.correlate([obs], [sv], sy, time_window_s=5.0, bearing_window_deg=10.0)
        assert len(matches) == 1
        assert matches[0].state_vector.icao == "flyby"
        assert abs(matches[0].dt_s) < 5.0

    def test_an_observation_far_outside_the_time_window_does_not_match(self, sy):
        sv = self._overhead_flyover(sy)
        obs = ADSB.AcousticObservation(ts_s=sv.ts_s + 10000.0)
        matches = ADSB.correlate([obs], [sv], sy, time_window_s=30.0)
        assert matches == []

    def test_a_bearing_outside_the_window_is_refused_even_at_the_right_time(self, sy):
        sv = self._overhead_flyover(sy)
        cpa = ADSB.closest_point_of_approach(sv, sy)
        obs = ADSB.AcousticObservation(ts_s=cpa.ts_unix_s, azimuth_deg=(180.0))
        matches = ADSB.correlate([obs], [sv], sy, time_window_s=30.0, bearing_window_deg=5.0)
        assert matches == []

    def test_no_bearing_gate_correlates_on_time_alone(self, sy):
        sv = self._overhead_flyover(sy)
        cpa = ADSB.closest_point_of_approach(sv, sy)
        obs = ADSB.AcousticObservation(ts_s=cpa.ts_unix_s, azimuth_deg=180.0, energy=0.9)
        matches = ADSB.correlate([obs], [sv], sy, time_window_s=30.0, bearing_window_deg=None)
        assert len(matches) == 1
        assert matches[0].bearing_error_deg is not None    # still computed, just not gated

    def test_an_observation_with_no_bearing_at_all_still_matches_on_time(self, sy):
        sv = self._overhead_flyover(sy)
        cpa = ADSB.closest_point_of_approach(sv, sy)
        obs = ADSB.AcousticObservation(ts_s=cpa.ts_unix_s, energy=1.4)
        matches = ADSB.correlate([obs], [sv], sy, time_window_s=30.0, bearing_window_deg=10.0)
        assert len(matches) == 1
        assert matches[0].bearing_error_deg is None

    def test_results_are_sorted_best_score_first(self, sy):
        sv = self._overhead_flyover(sy)
        cpa = ADSB.closest_point_of_approach(sv, sy)
        near = ADSB.AcousticObservation(ts_s=cpa.ts_unix_s + 1.0)
        far = ADSB.AcousticObservation(ts_s=cpa.ts_unix_s + 25.0)
        matches = ADSB.correlate([far, near], [sv], sy, time_window_s=30.0,
                                 bearing_window_deg=None)
        assert len(matches) == 2
        assert matches[0].observation is near

    def test_rejects_a_non_positive_time_window(self, sy):
        sv = self._overhead_flyover(sy)
        with pytest.raises(ValueError):
            ADSB.correlate([ADSB.AcousticObservation(ts_s=0.0)], [sv], sy, time_window_s=0.0)

    def test_rejects_a_non_positive_bearing_window(self, sy):
        sv = self._overhead_flyover(sy)
        with pytest.raises(ValueError):
            ADSB.correlate([ADSB.AcousticObservation(ts_s=0.0)], [sv], sy,
                          bearing_window_deg=0.0)
