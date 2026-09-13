#!/usr/bin/env python3
"""CLI tool for integrated spatial acoustic localization and GeoJSON event generation.

Examples:
    # Process stationary bioacoustic events from dets.csv and append to GeoJSONL:
    python3 tools/hear_spatial.py --dets testdata/dets.csv --survey survey.json --event-type dog_bark

    # Process moving source / aircraft trajectory from audio clips:
    python3 tools/hear_spatial.py --clips-dir /clips --survey survey.json --mode trajectory --event-type aircraft

    # Publish to MQTT topic dama/hear/spatial_events:
    python3 tools/hear_spatial.py --dets testdata/dets.csv --survey survey.json --publish-mqtt
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, Tuple

import numpy as np
from scipy.io import wavfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hear.backend import survey as SV
from hear.detsfile import read_file
from hear.spatial import (
    DEFAULT_GEOJSONL_PATH,
    DEFAULT_MQTT_HOST,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TOPIC,
    SpatialEventPipeline,
    to_geojson_feature_collection,
)


def load_survey_from_file_or_origin(survey_path: str, site_origin_str: str | None = None) -> SV.Survey:
    """Load survey from JSON file, with optional site origin override."""
    if not os.path.exists(survey_path):
        raise FileNotFoundError("Survey file not found: %s" % survey_path)

    with open(survey_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    if site_origin_str:
        parts = [float(p.strip()) for p in site_origin_str.split(",")]
        if len(parts) == 3:
            data["origin"] = {"lat_deg": parts[0], "lon_deg": parts[1], "h_ell_m": parts[2]}

    return SV.from_dict(data)


def load_audio_clips_dir(clips_dir: str) -> Tuple[Dict[int, Tuple[np.ndarray, float]], float]:
    """Load WAV audio clips from directory into dict of node_id -> (audio_array, start_time_utc_s)."""
    clips = {}
    fs = 48000.0

    if not os.path.exists(clips_dir):
        return clips, fs

    for fname in sorted(os.listdir(clips_dir)):
        if not fname.endswith(".wav"):
            continue
        path = os.path.join(clips_dir, fname)
        try:
            sample_rate, data = wavfile.read(path)
            fs = float(sample_rate)

            # Convert to float array [-1.0, +1.0] if integer PCM
            if data.dtype == np.int16:
                samples = data.astype(np.float64) / 32768.0
            elif data.dtype == np.int32:
                samples = data.astype(np.float64) / 2147483648.0
            else:
                samples = data.astype(np.float64)

            # If stereo/multi-channel, take channel 0
            if samples.ndim > 1:
                samples = samples[:, 0]

            # Parse node_id and timestamp from filename if available (e.g. node101-1757772000.wav)
            parts = fname.replace(".wav", "").split("-")
            node_id = 1
            start_ts = 0.0

            for p in parts:
                if p.isdigit():
                    if len(p) >= 10:
                        start_ts = float(p)
                    elif int(p) < 65535:
                        node_id = int(p)

            clips[node_id] = (samples, start_ts)
        except Exception as err:
            sys.stderr.write("Warning: failed to load clip %s: %s\n" % (path, err))

    return clips, fs


def main() -> int:
    parser = argparse.ArgumentParser(description="Spatial acoustic localization service CLI.")
    parser.add_argument("--dets", help="Path to dets.csv detection file")
    parser.add_argument("--survey", required=True, help="Path to survey.json file")
    parser.add_argument("--site-origin", help="Site origin as 'lat,lon,h_ell_m'")
    parser.add_argument("--clips-dir", help="Directory containing node audio WAV clips")
    parser.add_argument(
        "--mode",
        choices=["stationary", "trajectory"],
        default="stationary",
        help="Localization mode: stationary (Point) or trajectory (LineString)",
    )
    parser.add_argument(
        "--event-type",
        default="bioacoustic",
        help="Event type label (e.g., dog_bark, owl_hoot, aircraft)",
    )
    parser.add_argument("--temp-c", type=float, default=20.0, help="Air temperature in Celsius")
    parser.add_argument(
        "--fixed-up-m",
        type=float,
        default=0.0,
        help="Fixed height in meters (or set -999 for full 3D solve)",
    )
    parser.add_argument(
        "--geojsonl",
        default=DEFAULT_GEOJSONL_PATH,
        help="Path to output GeoJSONL file",
    )
    parser.add_argument("--publish-mqtt", action="store_true", help="Publish GeoJSON features to MQTT")
    parser.add_argument("--mqtt-host", default=DEFAULT_MQTT_HOST, help="MQTT host")
    parser.add_argument("--mqtt-port", type=int, default=DEFAULT_MQTT_PORT, help="MQTT port")
    parser.add_argument("--mqtt-topic", default=DEFAULT_MQTT_TOPIC, help="MQTT topic")
    parser.add_argument("--window-s", type=float, default=0.5, help="Sliding window size in seconds")
    parser.add_argument("--hop-s", type=float, default=0.1, help="Sliding window hop size in seconds")
    parser.add_argument("--pretty", action="store_true", help="Print pretty JSON to stdout")

    args = parser.parse_args()

    fixed_up = None if args.fixed_up_m == -999 else args.fixed_up_m
    survey = load_survey_from_file_or_origin(args.survey, args.site_origin)

    pipeline = SpatialEventPipeline(
        survey=survey,
        temp_c=args.temp_c,
        fixed_up_m=fixed_up,
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        mqtt_topic=args.mqtt_topic,
        geojsonl_path=args.geojsonl,
        publish_mqtt=args.publish_mqtt,
    )

    features = []

    if args.mode == "stationary" and args.dets:
        dets_read = read_file(args.dets)
        features = pipeline.process_coincidences(dets_read.rows, event_type=args.event_type)

    elif args.mode == "trajectory" and args.clips_dir:
        clips, fs = load_audio_clips_dir(args.clips_dir)
        feature = pipeline.process_continuous_clips(
            clips,
            event_type=args.event_type,
            fs=fs,
            window_s=args.window_s,
            hop_s=args.hop_s,
        )
        if feature:
            features = [feature]

    elif args.dets:
        dets_read = read_file(args.dets)
        features = pipeline.process_coincidences(dets_read.rows, event_type=args.event_type)

    fc = to_geojson_feature_collection(features)

    if args.pretty:
        print(json.dumps(fc, indent=2))
    else:
        print(json.dumps(fc))

    sys.stderr.write("Processed %d spatial event features.\n" % len(features))
    return 0


if __name__ == "__main__":
    sys.exit(main())
