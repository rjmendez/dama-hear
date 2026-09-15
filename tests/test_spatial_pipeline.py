#!/usr/bin/env python3
"""Unit tests for the integrated spatial acoustic localization pipeline.

Verifies multi-node coincidence solving, GeoJSON feature generation, GDOP error bounds
and confidence ellipses, sliding-window cross-correlation trajectory reconstruction,
and file output / publishing integrations.
"""
import json
import math
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hear import geodesy as GEO
from hear.backend import survey as SV
from hear.solve import point as PT
from hear.solve import shockwave as SW
from hear.spatial import (
    SpatialEvent,
    SpatialEventPipeline,
    calculate_gdop_bounds,
    calculate_sound_level_db,
    cross_correlate_signals,
    estimate_trajectory_from_clips,
    format_iso_timestamp,
    to_geojson_feature,
    to_geojson_feature_collection,
)
from tools import hear_spatial as HS

# Test Survey Setup
ORIGIN_LAT, ORIGIN_LON, ORIGIN_H = 40.0, -77.0, 100.0
TEMP_C = 20.0
SOUND_SPEED = SW.sound_speed(TEMP_C)  # ~343.21 m/s

# 4-node square array 200m x 200m
NODE_POSITIONS = {
    101: [0.0, 0.0, 0.0],
    102: [200.0, 0.0, 0.0],
    103: [200.0, 200.0, 0.0],
    104: [0.0, 200.0, 0.0],
}


@pytest.fixture
def sample_survey():
    nodes = [
        {"node_id": 101, "name": "node101", "e_m": 0.0, "n_m": 0.0, "u_m": 0.0, "class": "xiao-s3-pps"},
        {"node_id": 102, "name": "node102", "e_m": 200.0, "n_m": 0.0, "u_m": 0.0, "class": "xiao-s3-pps"},
        {"node_id": 103, "name": "node103", "e_m": 200.0, "n_m": 200.0, "u_m": 0.0, "class": "xiao-s3-pps"},
        {"node_id": 104, "name": "node104", "e_m": 0.0, "n_m": 200.0, "u_m": 0.0, "class": "xiao-s3-pps"},
    ]
    data = {
        "frame": "enu_local",
        "units": "m",
        "nodes": nodes,
        "origin": {"lat_deg": ORIGIN_LAT, "lon_deg": ORIGIN_LON, "h_ell_m": ORIGIN_H},
    }
    return SV.from_dict(data)


class TestTimestampFormatting:
    def test_iso_timestamp(self):
        ts = 1757772000.0  # 2025-09-13T14:00:00Z epoch (or similar)
        iso = format_iso_timestamp(ts)
        assert iso.endswith("Z")
        assert "T" in iso
        assert "2025" in iso or "2026" in iso


class TestGdopAndErrorBounds:
    def test_calculate_gdop_bounds_center_source(self):
        # Source at center of array (100, 100, 0)
        source_pos = [100.0, 100.0, 0.0]
        positions = np.array(list(NODE_POSITIONS.values()))

        bounds = calculate_gdop_bounds(positions, source_pos, sigma_t_s=0.001, temp_c=TEMP_C)

        assert bounds["hdop"] is not None
        assert bounds["hdop"] > 0.0
        assert bounds["hdop"] < 5.0  # Good geometry at center
        assert bounds["error_radius_m"] > 0.0
        assert "confidence_ellipse" in bounds

        ellipse = bounds["confidence_ellipse"]
        assert ellipse["semi_major_m"] > 0.0
        assert ellipse["semi_minor_m"] > 0.0
        assert ellipse["confidence"] == 0.95

    def test_calculate_gdop_bounds_far_source(self):
        # Source far outside array (1000, 1000, 0) -> HDOP should be worse
        source_center = [100.0, 100.0, 0.0]
        source_far = [1000.0, 1000.0, 0.0]
        positions = np.array(list(NODE_POSITIONS.values()))

        b_center = calculate_gdop_bounds(positions, source_center, sigma_t_s=0.001, temp_c=TEMP_C)
        b_far = calculate_gdop_bounds(positions, source_far, sigma_t_s=0.001, temp_c=TEMP_C)

        assert b_far["hdop"] > b_center["hdop"]
        assert b_far["error_radius_m"] > b_center["error_radius_m"]

    def test_gps_position_sigma_widens_the_bound(self):
        source_pos = [100.0, 100.0, 0.0]
        positions = np.array(list(NODE_POSITIONS.values()))
        timing_only = calculate_gdop_bounds(positions, source_pos, sigma_t_s=0.001, temp_c=TEMP_C)
        gps_positioned = calculate_gdop_bounds(
            positions, source_pos, sigma_t_s=0.001, temp_c=TEMP_C,
            position_sigma_m=[0.0, 0.0, 0.0, 2.1],
        )
        assert gps_positioned["error_radius_m"] > timing_only["error_radius_m"]


class TestMultiNodeCoincidenceSolving:
    def test_stationary_bioacoustic_event_solving(self, sample_survey):
        # Ground truth bioacoustic source (e.g. dog bark) at (150.0, 80.0, 0.0)
        gt_source = np.array([150.0, 80.0, 0.0])
        t0_utc = 1757772000.0

        # Calculate exact arrival times for all 4 nodes
        arrivals = []
        node_ids = sorted(NODE_POSITIONS.keys())
        for nid in node_ids:
            p = np.array(NODE_POSITIONS[nid])
            dist = np.linalg.norm(gt_source - p)
            arrivals.append(t0_utc + dist / SOUND_SPEED)

        # Solve via point solver
        positions = sample_survey.positions(node_ids)
        sol = PT.solve(positions, arrivals, source_class="point", temp_c=TEMP_C, fixed_up_m=0.0)

        assert sol["position_observable"] is True
        assert sol["east_m"] == pytest.approx(gt_source[0], abs=0.1)
        assert sol["north_m"] == pytest.approx(gt_source[1], abs=0.1)
        assert sol["t0_utc_s"] == pytest.approx(t0_utc, abs=0.001)

        # Convert to WGS84
        lat, lon, h = GEO.enu_to_geodetic(sol["east_m"], sol["north_m"], sol["up_m"], ORIGIN_LAT, ORIGIN_LON, ORIGIN_H)
        assert 39.9 < lat < 40.1
        assert -77.1 < lon < -76.9


class TestGeoJsonFormatting:
    def test_point_feature_generation(self):
        ev = SpatialEvent(
            event_id="bark_001",
            event_type="dog_bark",
            geometry_type="Point",
            timestamp_utc_s=1757772000.0,
            confidence=0.92,
            sound_level_db=65.0,
            error_radius_m=3.5,
            hdop=1.2,
            rms_residual_ms=0.25,
            enu_coords=(150.0, 80.0, 0.0),
            wgs84_coords=(40.00072, -76.99824, 100.0),
            confidence_ellipse={"semi_major_m": 3.5, "semi_minor_m": 2.1, "orientation_deg": 45.0, "confidence": 0.95},
            metadata={"contributing_node_ids": [101, 102, 103, 104]},
        )

        feat = to_geojson_feature(ev)

        assert feat["type"] == "Feature"
        assert feat["geometry"]["type"] == "Point"
        # GeoJSON is [lon, lat, elev]
        assert feat["geometry"]["coordinates"][0] == pytest.approx(-76.99824)
        assert feat["geometry"]["coordinates"][1] == pytest.approx(40.00072)

        props = feat["properties"]
        assert props["event_id"] == "bark_001"
        assert props["event_type"] == "dog_bark"
        assert props["confidence"] == 0.92
        assert props["sound_level_db"] == 65.0
        assert props["error_radius_m"] == 3.5
        assert props["hdop"] == 1.2
        assert props["east_m"] == 150.0
        assert props["north_m"] == 80.0

    def test_linestring_feature_generation(self):
        ev = SpatialEvent(
            event_id="aircraft_001",
            event_type="aircraft",
            geometry_type="LineString",
            timestamp_utc_s=1757772000.0,
            confidence=0.88,
            sound_level_db=72.5,
            error_radius_m=5.0,
            hdop=1.4,
            enu_trajectory=[(0.0, 0.0, 50.0), (50.0, 50.0, 50.0), (100.0, 100.0, 50.0)],
            wgs84_trajectory=[(40.0, -77.0, 150.0), (40.00045, -76.99941, 150.0), (40.00090, -76.99882, 150.0)],
            timestamps_utc_s=[1757772000.0, 1757772001.0, 1757772002.0],
            trajectory_length_m=141.42,
            metadata={"num_points": 3},
        )

        feat = to_geojson_feature(ev)

        assert feat["type"] == "Feature"
        assert feat["geometry"]["type"] == "LineString"
        coords = feat["geometry"]["coordinates"]
        assert len(coords) == 3
        # First point [lon, lat, elev]
        assert coords[0][0] == pytest.approx(-77.0)
        assert coords[0][1] == pytest.approx(40.0)

        props = feat["properties"]
        assert props["event_type"] == "aircraft"
        assert props["num_points"] == 3
        assert props["trajectory_length_m"] == pytest.approx(141.42, abs=0.1)

    def test_feature_collection(self):
        ev = SpatialEvent(
            event_id=1,
            event_type="owl_hoot",
            geometry_type="Point",
            timestamp_utc_s=1757772000.0,
            enu_coords=(10.0, 20.0, 0.0),
            wgs84_coords=(40.0, -77.0, 100.0),
        )
        feat = to_geojson_feature(ev)
        fc = to_geojson_feature_collection([feat])

        assert fc["type"] == "FeatureCollection"
        assert len(fc["features"]) == 1
        assert fc["features"][0]["properties"]["event_type"] == "owl_hoot"


class TestCrossCorrelationAndTrajectory:
    def test_cross_correlate_signals_exact_delay(self):
        fs = 48000.0
        t = np.linspace(0, 0.1, int(fs * 0.1), endpoint=False)
        # 1 kHz tone pulse
        signal_ref = np.sin(2 * np.pi * 1000.0 * t) * np.exp(-t * 30.0)

        # Target signal delayed by exactly 10 samples (10 / 48000 s = 0.00020833 s)
        delay_samples = 10
        signal_target = np.pad(signal_ref, (delay_samples, 0))[: len(signal_ref)]

        delay_s, peak_val = cross_correlate_signals(signal_ref, signal_target, fs, max_delay_s=0.01)

        expected_delay_s = delay_samples / fs
        assert delay_s == pytest.approx(expected_delay_s, abs=1.0 / fs)
        assert peak_val > 0.95

    def test_sound_level_calculation(self):
        fs = 48000.0
        t = np.linspace(0, 0.1, int(fs * 0.1), endpoint=False)
        sig = 0.5 * np.sin(2 * np.pi * 1000.0 * t)
        db = calculate_sound_level_db(sig)
        assert db > 80.0

    def test_trajectory_reconstruction(self, sample_survey):
        fs = 16000.0
        duration_s = 2.0
        num_samples = int(fs * duration_s)

        # Ground truth trajectory: source moving from (50, 50, 10) to (150, 150, 10)
        # over 2 seconds -> velocity = (50 m/s, 50 m/s, 0)
        node_ids = sorted(NODE_POSITIONS.keys())

        # Synthesize multi-channel audio for moving source emitting a 500Hz continuous tone
        clips = {}
        window_s = 0.4
        hop_s = 0.2
        start_utc_s = 1757772000.0

        time_axis = np.linspace(0, duration_s, num_samples, endpoint=False)
        source_audio_base = np.sin(2 * np.pi * 500.0 * time_axis)

        for nid in node_ids:
            p_node = np.array(NODE_POSITIONS[nid])
            # Synthesize received signal with time-varying delay
            node_samples = np.zeros(num_samples)
            for idx, t_cur in enumerate(time_axis):
                # Interpolate source position at time t_cur
                pos_src = np.array([50.0 + 50.0 * t_cur, 50.0 + 50.0 * t_cur, 10.0])
                dist = np.linalg.norm(pos_src - p_node)
                prop_delay = dist / SOUND_SPEED
                # Source emission time
                t_emit = t_cur - prop_delay
                if t_emit >= 0:
                    node_samples[idx] = np.sin(2 * np.pi * 500.0 * t_emit)
            clips[nid] = (node_samples, start_utc_s)

        traj = estimate_trajectory_from_clips(
            clips,
            sample_survey,
            fs=fs,
            window_s=window_s,
            hop_s=hop_s,
            fixed_up_m=10.0,
            temp_c=TEMP_C,
        )

        assert traj["num_points"] >= 3
        assert len(traj["enu_points"]) == traj["num_points"]
        assert len(traj["wgs84_points"]) == traj["num_points"]
        assert traj["trajectory_length_m"] > 50.0

        # Verify start and end points of trajectory move from ~50 to ~150
        first_pt = traj["enu_points"][0]
        last_pt = traj["enu_points"][-1]

        assert first_pt[0] < last_pt[0]
        assert first_pt[1] < last_pt[1]


class TestSpatialPipelineIntegration:
    def test_gps_fallback_is_loaded_and_its_source_is_emitted(self, sample_survey, tmp_path):
        east_m, north_m, up_m = (300.0, 0.0, 0.0)
        lat, lon, h = GEO.enu_to_geodetic(east_m, north_m, up_m, ORIGIN_LAT, ORIGIN_LON, ORIGIN_H)
        positions_path = tmp_path / "node_positions.json"
        positions_path.write_text(json.dumps({"nodes": {
            "gps-node": {"lat_deg": lat, "lon_deg": lon, "h_ell_m": h, "hacc_m": 2.1,
                         "fixes": 60, "at": 1000.0, "class": "xiao-s3-pps"},
        }}))
        survey, report = HS.augment_survey_from_gps(sample_survey, str(positions_path), now=1001.0)
        gps_id = SV.gps_node_id("gps-node")
        assert report[0]["used"] is True
        assert survey.position_sources[gps_id] == SV.POSITION_SOURCE_GPS

        positions = {node_id: sample_survey.position(node_id) for node_id in sample_survey.ids}
        positions[gps_id] = survey.position(gps_id)
        pipeline_survey = SV.Survey(
            positions, names={**sample_survey.names, gps_id: "gps-node"},
            sigma_m={**sample_survey.sigma_m, gps_id: survey.sigma_m[gps_id]},
            classes={**sample_survey.classes, gps_id: "xiao-s3-pps"},
            position_sources={**sample_survey.position_sources, gps_id: SV.POSITION_SOURCE_GPS},
            origin=sample_survey.origin,
        )
        pipeline = SpatialEventPipeline(pipeline_survey, temp_c=TEMP_C, fixed_up_m=0.0,
                                        geojsonl_path=None)
        source = np.array([120.0, 60.0, 0.0])
        detections = [
            {"node_id": node_id, "seq": 1,
             "t_utc_s": 1757772000.0 + np.linalg.norm(source - pipeline_survey.position(node_id))
             / SOUND_SPEED, "onset_found": True, "utc_trusted": True, "stamp_admissible": True}
            for node_id in pipeline_survey.ids if node_id != 104
        ]
        features = pipeline.process_coincidences(detections)
        assert features[0]["properties"]["position_sources"]["gps-node"] == SV.POSITION_SOURCE_GPS

    def test_gps_fallback_sigma_is_folded_into_solver_weights(self, sample_survey):
        gps_id = SV.gps_node_id("gps-node")
        pipeline_survey = SV.Survey(
            {**{node_id: sample_survey.position(node_id) for node_id in sample_survey.ids},
             gps_id: np.array([300.0, 0.0, 0.0])},
            names={**sample_survey.names, gps_id: "gps-node"},
            sigma_m={**sample_survey.sigma_m, gps_id: 3.0},
            classes={**sample_survey.classes, gps_id: "xiao-s3-pps"},
            position_sources={**sample_survey.position_sources, gps_id: SV.POSITION_SOURCE_GPS},
            origin=sample_survey.origin,
        )
        pipeline = SpatialEventPipeline(pipeline_survey, temp_c=TEMP_C, fixed_up_m=0.0,
                                        geojsonl_path=None)
        source = np.array([120.0, 60.0, 0.0])
        detections = [
            {"node_id": node_id, "seq": 1,
             "t_utc_s": 1757772000.0 + np.linalg.norm(source - pipeline_survey.position(node_id))
             / SOUND_SPEED, "onset_found": True, "utc_trusted": True, "stamp_admissible": True,
             "sync_sigma_ns": 50.0}
            for node_id in pipeline_survey.ids if node_id != 104
        ]

        with patch("hear.spatial.PT.solve", wraps=PT.solve) as solve_spy:
            features = pipeline.process_coincidences(detections)

        assert features
        sigmas = solve_spy.call_args.kwargs["sigmas"]
        k = len(sigmas) - 1
        other = sigmas[0]
        assert math.isclose(sigmas[k], math.hypot(other, 3.0 / SOUND_SPEED), rel_tol=1e-9)
        assert sigmas[:k] == pytest.approx([other] * k)

    def test_pipeline_stationary_coincidence_processing(self, sample_survey, tmp_path):
        geojsonl_file = tmp_path / "events.geojsonl"

        pipeline = SpatialEventPipeline(
            survey=sample_survey,
            temp_c=TEMP_C,
            fixed_up_m=0.0,
            geojsonl_path=str(geojsonl_file),
            publish_mqtt=False,
        )

        # Ground truth source at (120, 60, 0)
        gt_source = np.array([120.0, 60.0, 0.0])
        t0_utc = 1757772000.0

        detections = []
        for nid in sorted(NODE_POSITIONS.keys()):
            p = np.array(NODE_POSITIONS[nid])
            dist = np.linalg.norm(gt_source - p)
            arr = t0_utc + dist / SOUND_SPEED
            detections.append(
                {
                    "node_id": nid,
                    "seq": 1,
                    "t_utc_s": arr,
                    "onset_found": True,
                    "utc_trusted": True,
                    "stamp_admissible": True,
                }
            )

        features = pipeline.process_coincidences(detections, event_type="dog_bark")

        assert len(features) == 1
        feat = features[0]

        assert feat["geometry"]["type"] == "Point"
        assert feat["properties"]["event_type"] == "dog_bark"
        assert feat["properties"]["east_m"] == pytest.approx(120.0, abs=0.5)
        assert feat["properties"]["north_m"] == pytest.approx(60.0, abs=0.5)

        # Check GeoJSONL output file was written
        assert os.path.exists(geojsonl_file)
        with open(geojsonl_file, "r") as fh:
            lines = fh.readlines()
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["properties"]["event_type"] == "dog_bark"

    def test_pipeline_trajectory_processing(self, sample_survey, tmp_path):
        geojsonl_file = tmp_path / "events.geojsonl"

        pipeline = SpatialEventPipeline(
            survey=sample_survey,
            temp_c=TEMP_C,
            fixed_up_m=0.0,
            geojsonl_path=str(geojsonl_file),
            publish_mqtt=False,
        )

        fs = 16000.0
        duration_s = 1.0
        num_samples = int(fs * duration_s)

        clips = {}
        for nid in sample_survey.ids:
            # 1kHz tone with slight offset per node
            t = np.linspace(0, duration_s, num_samples, endpoint=False)
            clips[nid] = (np.sin(2 * np.pi * 1000.0 * t), 1757772000.0)

        feature = pipeline.process_continuous_clips(
            clips,
            event_type="aircraft",
            fs=fs,
            window_s=0.3,
            hop_s=0.1,
            event_id="flight_42",
        )

        assert feature is not None
        assert feature["geometry"]["type"] == "LineString"
        assert feature["properties"]["event_id"] == "flight_42"
        assert feature["properties"]["event_type"] == "aircraft"

        with open(geojsonl_file, "r") as fh:
            lines = fh.readlines()
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["properties"]["event_id"] == "flight_42"
