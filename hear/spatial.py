#!/usr/bin/env python3
"""Integrated spatial acoustic localization service and GeoJSON pipeline.

Provides 2D/3D ENU and WGS84 spatial localization for stationary bioacoustic events
(e.g., dog barks, owl hoots) with GDOP error bounds / confidence ellipses, as well as
sliding-window cross-correlation and trajectory estimation for continuous/moving sources
(e.g., aircraft, vehicles, drones).

Emits standard GeoJSON features (Point for stationary, LineString for trajectories) and
supports publishing to MQTT topic `dama/hear/spatial_events` and appending to
`/pool/spatial/events.geojsonl`.
"""
from __future__ import annotations

import datetime
import json
import math
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
from scipy.signal import correlate

from . import geodesy as GEO
from . import nodeclass as NC
from .backend import associate as AS
from .backend import survey as SV
from .detsfile import read_file, read_text
from .loci_validation import LociSpatialMemory
from .solve import placement as PL
from .solve import point as PT
from .solve import shockwave as SW

DEFAULT_MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
DEFAULT_MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
DEFAULT_MQTT_TOPIC = os.environ.get("MQTT_SPATIAL_TOPIC", "dama/hear/spatial_events")
DEFAULT_GEOJSONL_PATH = os.environ.get("SPATIAL_GEOJSONL_PATH", "/pool/spatial/events.geojsonl")


def format_iso_timestamp(ts_utc_s: float) -> str:
    """Format UTC epoch seconds as ISO 8601 string (YYYY-MM-DDTHH:MM:SS.mmmZ)."""
    dt = datetime.datetime.fromtimestamp(ts_utc_s, datetime.timezone.utc)
    ms = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms:03d}Z"


def calculate_gdop_bounds(
    positions: Sequence,
    source_pos: Sequence,
    sigma_t_s: float = 0.001,
    temp_c: float = 20.0,
    confidence_level: float = 0.95,
    position_sigma_m: Optional[Sequence[float]] = None,
) -> Dict[str, Any]:
    """Calculate GDOP (HDOP, VDOP, PDOP) and confidence ellipse / error radius for a 2D/3D solve.

    Args:
        positions: List of node ENU positions (N x 3 or N x 2).
        source_pos: Solved source ENU position (3, or 2).
        sigma_t_s: Arrival time uncertainty in seconds (1-sigma).
        temp_c: Air temperature in Celsius for sound speed.
        confidence_level: Confidence level for error ellipse (default 0.95 -> scale ~ 2.447).
        position_sigma_m: Per-node horizontal position uncertainty (1-sigma), folded into
            the range uncertainty. Omit for the legacy timing-only bound.

    Returns:
        Dict with hdop, vdop, pdop, error_radius_m, and confidence_ellipse details.
    """
    P = PL._as3(positions)
    s_arr = np.asarray(source_pos, float).ravel()
    s = np.array([s_arr[0], s_arr[1], s_arr[2] if len(s_arr) > 2 else 0.0])
    c = SW.sound_speed(temp_c)
    sigma_r = c * sigma_t_s
    if position_sigma_m is None:
        range_sigma_m = None
    else:
        position_sigma_m = np.asarray(position_sigma_m, float).ravel()
        if len(position_sigma_m) != len(P):
            raise ValueError("position_sigma_m must contain one value per node")
        if not np.all(np.isfinite(position_sigma_m)) or np.any(position_sigma_m < 0.0):
            raise ValueError("position_sigma_m values must be finite and non-negative")
        range_sigma_m = np.hypot(sigma_r, position_sigma_m)

    d3 = PL.dop3(P, s) if len(P) >= 4 else {}
    hdop = d3.get("hdop", float("inf"))
    vdop = d3.get("vdop", float("inf"))
    pdop = d3.get("pdop", float("inf"))

    # If 3D hdop is singular due to coplanar ground array, fall back to 2D dop
    if not math.isfinite(hdop):
        d2 = PL.dop(P[:, :2], s[:2])
        if math.isfinite(d2.get("dop", float("inf"))):
            hdop = d2["dop"]

    # Error radius (1-sigma horizontal position error bound)
    bound_sigma_m = sigma_r if range_sigma_m is None else float(np.max(range_sigma_m))
    error_radius_m = hdop * bound_sigma_m if math.isfinite(hdop) else 0.0

    # Calculate 2D covariance matrix in ENU for confidence ellipse
    n = len(P)
    G = PL._unit_rows(P[:, :2], s[:2])
    ellipse_info = {
        "semi_major_m": error_radius_m,
        "semi_minor_m": error_radius_m,
        "orientation_deg": 0.0,
        "confidence": confidence_level,
    }

    if G is not None and n >= 3:
        if range_sigma_m is None:
            M = np.eye(n) - np.ones((n, n)) / float(n)
            F = G.T @ M @ G
            covariance_scale = sigma_r ** 2
        else:
            inv_r = np.diag(1.0 / (range_sigma_m ** 2))
            one = np.ones((n, 1))
            M = inv_r - (inv_r @ one @ one.T @ inv_r) / (one.T @ inv_r @ one).item()
            F = G.T @ M @ G
            covariance_scale = 1.0
        try:
            if abs(float(np.linalg.det(F))) > 1e-12:
                Q = np.linalg.inv(F)
                C = Q * covariance_scale
                evals, evecs = np.linalg.eigh(C)
                idx = np.argsort(evals)[::-1]
                evals = np.maximum(evals[idx], 1e-12)
                evecs = evecs[:, idx]

                k_scale = math.sqrt(-2.0 * math.log(1.0 - confidence_level))
                a = math.sqrt(evals[0]) * k_scale
                b = math.sqrt(evals[1]) * k_scale
                angle_rad = math.atan2(evecs[1, 0], evecs[0, 0])
                angle_deg = math.degrees(angle_rad) % 360.0

                ellipse_info = {
                    "semi_major_m": float(a),
                    "semi_minor_m": float(b),
                    "orientation_deg": float(angle_deg),
                    "confidence": confidence_level,
                }
                error_radius_m = float(a)
        except np.linalg.LinAlgError:
            pass

    return {
        "hdop": float(hdop) if math.isfinite(hdop) else None,
        "vdop": float(vdop) if math.isfinite(vdop) else None,
        "pdop": float(pdop) if math.isfinite(pdop) else None,
        "error_radius_m": float(error_radius_m),
        "confidence_ellipse": ellipse_info,
    }


def cross_correlate_signals(
    signal_ref: np.ndarray,
    signal_target: np.ndarray,
    fs: float,
    max_delay_s: float = 0.5,
) -> Tuple[float, float]:
    """Sliding-window cross-correlation between reference and target audio signals.

    Args:
        signal_ref: 1D numpy array of reference channel audio.
        signal_target: 1D numpy array of target channel audio.
        fs: Sampling frequency in Hz.
        max_delay_s: Maximum expected delay in seconds.

    Returns:
        (delay_s, peak_correlation): Estimated time delay in seconds (target relative to ref)
        and normalized peak correlation value (0.0 to 1.0).
    """
    max_lag = int(round(max_delay_s * fs))
    if len(signal_ref) == 0 or len(signal_target) == 0:
        return 0.0, 0.0

    # Zero-mean
    r = signal_ref - np.mean(signal_ref)
    t = signal_target - np.mean(signal_target)

    norm_r = np.linalg.norm(r)
    norm_t = np.linalg.norm(t)
    if norm_r < 1e-9 or norm_t < 1e-9:
        return 0.0, 0.0

    corr = correlate(t, r, mode="full")
    mid = len(r) - 1  # zero lag index
    start_idx = max(0, mid - max_lag)
    end_idx = min(len(corr), mid + max_lag + 1)

    sub_corr = corr[start_idx:end_idx]
    peak_sub_idx = np.argmax(sub_corr)
    peak_idx = start_idx + peak_sub_idx
    lag_samples = peak_idx - mid

    delay_s = lag_samples / float(fs)
    peak_val = float(corr[peak_idx]) / (norm_r * norm_t)

    return float(delay_s), float(peak_val)


def calculate_sound_level_db(signal: np.ndarray, ref_dbfs: float = 94.0) -> float:
    """Calculate RMS sound level in relative dB from audio samples."""
    if len(signal) == 0:
        return 0.0
    rms = np.sqrt(np.mean(np.square(signal, dtype=np.float64)))
    if rms < 1e-12:
        return 0.0
    return float(20.0 * math.log10(rms) + ref_dbfs)


def estimate_trajectory_from_clips(
    clips: Dict[int, Tuple[np.ndarray, float]],
    survey: SV.Survey,
    fs: float = 48000.0,
    window_s: float = 0.5,
    hop_s: float = 0.1,
    max_delay_s: float = 0.5,
    temp_c: float = 20.0,
    fixed_up_m: Optional[float] = 0.0,
    min_nodes: int = 3,
    min_correlation: float = 0.001,
) -> Dict[str, Any]:
    """Estimate a continuous moving source trajectory using sliding-window cross-correlation.

    Args:
        clips: Dict mapping node_id -> (audio_samples_array, start_time_utc_s).
        survey: Node survey containing ENU positions.
        fs: Audio sample rate in Hz.
        window_s: Sliding window duration in seconds.
        hop_s: Window step size in seconds.
        max_delay_s: Max expected TDoA delay in seconds.
        temp_c: Temperature in Celsius.
        fixed_up_m: Optional declared height in meters for 2D solve.
        min_nodes: Minimum required nodes per window.
        min_correlation: Minimum cross-correlation peak threshold.

    Returns:
        Dict with trajectory points (ENU and WGS84), timestamps, HDOPs, and length.
    """
    if len(clips) < min_nodes:
        raise ValueError("Need clips from at least %d nodes to solve trajectory" % min_nodes)

    node_ids = sorted([nid for nid in clips if nid in survey])
    if len(node_ids) < min_nodes:
        raise ValueError("Need at least %d surveyed nodes with clips" % min_nodes)

    ref_id = node_ids[0]
    ref_samples, ref_start_s = clips[ref_id]

    window_len = int(round(window_s * fs))
    hop_len = int(round(hop_s * fs))

    total_samples = len(ref_samples)
    num_windows = (total_samples - window_len) // hop_len + 1

    enu_points: List[Tuple[float, float, float]] = []
    wgs84_points: List[Tuple[float, float, float]] = []
    timestamps_s: List[float] = []
    hdops: List[float] = []
    sound_levels: List[float] = []

    # Get site origin for ENU -> WGS84 conversion
    try:
        origin = survey.origin_geodetic()
    except Exception:
        # Fallback to centroid of surveyed nodes if no origin set
        pts_enu = survey.positions(node_ids)
        origin = (0.0, 0.0, 0.0)

    for w_idx in range(num_windows):
        start_samp = w_idx * hop_len
        end_samp = start_samp + window_len
        w_time_s = ref_start_s + (start_samp + window_len / 2.0) / fs

        ref_win = ref_samples[start_samp:end_samp]

        valid_nodes = [ref_id]
        arrivals = [w_time_s]
        win_levels = [calculate_sound_level_db(ref_win)]

        for nid in node_ids[1:]:
            tgt_samples, tgt_start_s = clips[nid]
            # Offset between target start time and reference start time
            time_offset_s = tgt_start_s - ref_start_s
            tgt_start_samp = start_samp - int(round(time_offset_s * fs))
            tgt_end_samp = tgt_start_samp + window_len

            if tgt_start_samp < 0 or tgt_end_samp > len(tgt_samples):
                continue

            tgt_win = tgt_samples[tgt_start_samp:tgt_end_samp]
            delay_s, peak_corr = cross_correlate_signals(ref_win, tgt_win, fs, max_delay_s=max_delay_s)

            if abs(peak_corr) >= min_correlation:
                valid_nodes.append(nid)
                arrivals.append(w_time_s + delay_s)
                win_levels.append(calculate_sound_level_db(tgt_win))

        if len(valid_nodes) >= min_nodes:
            positions = survey.positions(valid_nodes)
            try:
                sol = PT.solve(
                    positions,
                    arrivals,
                    source_class="point",
                    temp_c=temp_c,
                    fixed_up_m=fixed_up_m,
                )
                if sol.get("position_observable") and sol.get("east_m") is not None:
                    e, n, u = sol["east_m"], sol["north_m"], sol["up_m"]
                    enu_points.append((e, n, u))
                    timestamps_s.append(w_time_s)
                    hdops.append(sol.get("hdop", 1.0))
                    sound_levels.append(float(np.mean(win_levels)))

                    if origin != (0.0, 0.0, 0.0):
                        lat, lon, h = GEO.enu_to_geodetic(e, n, u, *origin)
                    else:
                        lat, lon, h = 0.0, 0.0, u
                    wgs84_points.append((lat, lon, h))
            except Exception:
                continue

    # Calculate total length
    traj_len_m = 0.0
    for i in range(1, len(enu_points)):
        p1 = np.array(enu_points[i - 1])
        p2 = np.array(enu_points[i])
        traj_len_m += float(np.linalg.norm(p2 - p1))

    return {
        "num_points": len(enu_points),
        "enu_points": enu_points,
        "wgs84_points": wgs84_points,
        "timestamps_s": timestamps_s,
        "hdops": hdops,
        "hdop_mean": float(np.mean(hdops)) if hdops else None,
        "sound_level_db": float(np.mean(sound_levels)) if sound_levels else None,
        "trajectory_length_m": traj_len_m,
    }


def _event_arrival_sigmas(
    event: Dict[str, Any],
    survey: SV.Survey,
    temp_c: float,
) -> Optional[List[float]]:
    """Per-receiver sigma vector for one solve, or None when not every receiver states one.

    `associate()` carries `t_sigma_s` through when the caller already resolved it, but
    tools/hear_spatial.py reads raw dets.csv rows that often only carry `sync_sigma_ns`. When
    every receiver in the event states that clock sigma, resolve it here through nodeclass and
    fold any GPS-positioned receiver's own position sigma into the same range budget
    hear-tdoa uses. A partial vector stays unweighted: the solvers refuse mixed stated/unstated
    sigmas rather than inventing the missing ones.
    """
    node_ids = event["node_ids"]
    detections = event.get("detections", [])
    sigmas = list(event.get("arrival_sigma_s") or [None] * len(node_ids))
    if len(sigmas) != len(node_ids):
        raise ValueError("arrival_sigma_s must align with node_ids")
    if len(detections) != len(node_ids):
        raise ValueError("detections must align with node_ids")
    if any(s is None for s in sigmas):
        for i, (node_id, det) in enumerate(zip(node_ids, detections)):
            if sigmas[i] is None and det.get("sync_sigma_ns") is not None:
                sigmas[i] = NC.stamp_t_sigma_s(
                    det["sync_sigma_ns"], survey.classes.get(int(node_id))
                )
    if any(s is None for s in sigmas):
        return None
    c_mps = SW.sound_speed(temp_c)
    return [
        math.hypot(float(sigmas[i]), float(survey.sigma_m[int(node_id)]) / c_mps)
        if survey.position_sources.get(int(node_id), "survey") == SV.POSITION_SOURCE_GPS
        else float(sigmas[i])
        for i, node_id in enumerate(node_ids)
    ]


@dataclass
class SpatialEvent:
    """Dataclass representing a localized spatial event."""

    event_id: Union[str, int]
    event_type: str  # e.g., "dog_bark", "owl_hoot", "aircraft", "bioacoustic", "continuous"
    geometry_type: str  # "Point" or "LineString"
    timestamp_utc_s: float
    confidence: float = 0.90
    sound_level_db: Optional[float] = None
    error_radius_m: float = 5.0
    hdop: Optional[float] = None
    rms_residual_ms: Optional[float] = None
    # For Point
    enu_coords: Optional[Tuple[float, float, float]] = None
    wgs84_coords: Optional[Tuple[float, float, float]] = None
    confidence_ellipse: Optional[Dict[str, Any]] = None
    # For LineString
    enu_trajectory: Optional[List[Tuple[float, float, float]]] = None
    wgs84_trajectory: Optional[List[Tuple[float, float, float]]] = None
    timestamps_utc_s: Optional[List[float]] = None
    trajectory_length_m: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def to_geojson_feature(event: SpatialEvent) -> Dict[str, Any]:
    """Convert a SpatialEvent into a standard GeoJSON Feature dict (RFC 7946)."""
    iso_ts = format_iso_timestamp(event.timestamp_utc_s)

    if event.geometry_type == "Point":
        if event.wgs84_coords is not None:
            lat, lon, h = event.wgs84_coords
            coords = [float(lon), float(lat), float(h)]
        elif event.enu_coords is not None:
            e, n, u = event.enu_coords
            coords = [float(e), float(n), float(u)]
        else:
            coords = [0.0, 0.0, 0.0]

        properties = {
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "confidence": float(event.confidence),
            "sound_level_db": event.sound_level_db,
            "timestamp": iso_ts,
            "timestamp_utc_s": float(event.timestamp_utc_s),
            "error_radius_m": float(event.error_radius_m),
            "hdop": event.hdop,
            "rms_residual_ms": event.rms_residual_ms,
        }
        if event.enu_coords is not None:
            properties["east_m"] = float(event.enu_coords[0])
            properties["north_m"] = float(event.enu_coords[1])
            properties["up_m"] = float(event.enu_coords[2])
        if event.confidence_ellipse is not None:
            properties["confidence_ellipse"] = event.confidence_ellipse
        properties.update(event.metadata)

        return {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": coords,
            },
            "properties": properties,
        }

    elif event.geometry_type == "LineString":
        if event.wgs84_trajectory is not None:
            coords = [[float(lon), float(lat), float(h)] for lat, lon, h in event.wgs84_trajectory]
        elif event.enu_trajectory is not None:
            coords = [[float(e), float(n), float(u)] for e, n, u in event.enu_trajectory]
        else:
            coords = []

        iso_timestamps = (
            [format_iso_timestamp(t) for t in event.timestamps_utc_s]
            if event.timestamps_utc_s
            else [iso_ts]
        )

        properties = {
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "confidence": float(event.confidence),
            "sound_level_db": event.sound_level_db,
            "timestamp": iso_ts,
            "timestamps": iso_timestamps,
            "error_radius_m": float(event.error_radius_m),
            "hdop_mean": event.hdop,
            "num_points": len(coords),
            "trajectory_length_m": event.trajectory_length_m,
        }
        properties.update(event.metadata)

        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coords,
            },
            "properties": properties,
        }

    else:
        raise ValueError("Unsupported geometry_type %r" % (event.geometry_type,))


def to_geojson_feature_collection(features: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Wrap a list of GeoJSON features into a FeatureCollection."""
    return {
        "type": "FeatureCollection",
        "features": features,
    }


def publish_mqtt_event(
    feature: Dict[str, Any],
    host: str = DEFAULT_MQTT_HOST,
    port: int = DEFAULT_MQTT_PORT,
    topic: str = DEFAULT_MQTT_TOPIC,
    client_id: str = "dama-hear-spatial",
) -> bool:
    """Publish a GeoJSON feature payload to MQTT broker. Fail-open if unreachable."""
    try:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2 if hasattr(mqtt, "CallbackAPIVersion") else client_id)
        client.connect(host, port, keepalive=10)
        payload = json.dumps(feature)
        info = client.publish(topic, payload, qos=1)
        info.wait_for_publish(timeout=2.0)
        client.disconnect()
        return True
    except Exception:
        return False


def append_geojsonl(
    feature: Dict[str, Any],
    filepath: str = DEFAULT_GEOJSONL_PATH,
) -> None:
    """Append a single GeoJSON feature line to a .geojsonl file."""
    dirpath = os.path.dirname(filepath)
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)
    with open(filepath, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(feature) + "\n")


class SpatialEventPipeline:
    """Integrated Spatial Acoustic Localization Pipeline service."""

    def __init__(
        self,
        survey: SV.Survey,
        temp_c: float = 20.0,
        fixed_up_m: Optional[float] = 0.0,
        mqtt_host: str = DEFAULT_MQTT_HOST,
        mqtt_port: int = DEFAULT_MQTT_PORT,
        mqtt_topic: str = DEFAULT_MQTT_TOPIC,
        geojsonl_path: str = DEFAULT_GEOJSONL_PATH,
        publish_mqtt: bool = False,
        loci_memory: Optional[LociSpatialMemory] = None,
    ) -> None:
        self.survey = survey
        self.temp_c = temp_c
        self.fixed_up_m = fixed_up_m
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_topic = mqtt_topic
        self.geojsonl_path = geojsonl_path
        self.publish_mqtt_enabled = publish_mqtt
        self.loci_memory = loci_memory
        self.loci_rogue_nodes = (
            loci_memory.rogue_nodes({node_id: survey.position(node_id) for node_id in survey.ids})
            if loci_memory is not None else {})

        try:
            self.origin = survey.origin_geodetic()
        except Exception:
            self.origin = (0.0, 0.0, 0.0)

    def process_coincidences(
        self,
        detections: Sequence[Dict[str, Any]],
        event_type: str = "bioacoustic",
        sound_level_db: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Group detection rows, solve stationary 2D/3D points, and generate GeoJSON features."""
        norm_dets = []
        for d in detections:
            d_copy = dict(d)
            if "t_utc_s" not in d_copy and "utc_us" in d_copy:
                try:
                    d_copy["t_utc_s"] = float(d_copy["utc_us"]) / 1e6
                except (ValueError, TypeError):
                    pass
            if "seq" not in d_copy:
                try:
                    d_copy["seq"] = int(d_copy.get("sample", 1))
                except (ValueError, TypeError):
                    d_copy["seq"] = 1
            norm_dets.append(d_copy)

        assoc_res = AS.associate(norm_dets, self.survey, temp_c=self.temp_c)
        events = assoc_res.get("events", [])

        features = []
        for ev in events:
            node_ids = ev["node_ids"]
            arrivals = ev["arrivals"]
            loci_rejections = []
            eligible = list(range(len(node_ids)))
            if self.loci_memory is not None:
                loci_rejections.extend(
                    {"node_id": node_ids[i], "reason": "loci_anchor_drift", **self.loci_rogue_nodes[node_ids[i]]}
                    for i in eligible if node_ids[i] in self.loci_rogue_nodes)
                eligible = [i for i in eligible if node_ids[i] not in self.loci_rogue_nodes]
                arrival_check = self.loci_memory.filter_arrivals(
                    [node_ids[i] for i in eligible], [arrivals[i] for i in eligible],
                    self.survey.positions([node_ids[i] for i in eligible]), self.temp_c)
                eligible = [eligible[i] for i in arrival_check["indices"]]
                loci_rejections.extend(arrival_check["rejected"])
                if len(eligible) < 3:
                    continue
                node_ids = [node_ids[i] for i in eligible]
                arrivals = [arrivals[i] for i in eligible]
            sigmas = _event_arrival_sigmas(ev, self.survey, self.temp_c)
            if self.loci_memory is not None and sigmas is not None:
                sigmas = [sigmas[i] for i in eligible]

            positions = self.survey.positions(node_ids)

            try:
                sol = PT.solve(
                    positions,
                    arrivals,
                    source_class="point",
                    temp_c=self.temp_c,
                    fixed_up_m=self.fixed_up_m,
                    sigmas=sigmas,
                )
            except Exception:
                continue

            if not sol.get("position_observable") or sol.get("east_m") is None:
                continue

            east_m, north_m, up_m = sol["east_m"], sol["north_m"], sol["up_m"]
            enu = (east_m, north_m, up_m)
            if self.loci_memory is not None and not self.loci_memory.accepts_position(enu):
                continue

            if self.origin != (0.0, 0.0, 0.0):
                lat, lon, h = GEO.enu_to_geodetic(east_m, north_m, up_m, *self.origin)
            else:
                lat, lon, h = 0.0, 0.0, up_m
            wgs84 = (lat, lon, h)

            rms_ms = sol.get("rms_residual_ms", 0.5)
            sigma_t_s = max(rms_ms / 1000.0 if rms_ms is not None else 0.001, 0.0001)

            gdop_bounds = calculate_gdop_bounds(
                positions, enu, sigma_t_s=sigma_t_s, temp_c=self.temp_c,
                position_sigma_m=[
                    self.survey.sigma_m[node_id]
                    if self.survey.position_sources.get(node_id, "survey") == SV.POSITION_SOURCE_GPS
                    else 0.0
                    for node_id in node_ids
                ],
            )

            # Confidence score heuristic
            chi2_red = sol.get("chi2_reduced")
            if chi2_red is not None and chi2_red > 0:
                conf = max(0.1, min(1.0, 1.0 / (1.0 + 0.1 * chi2_red)))
            else:
                conf = 0.95

            spatial_ev = SpatialEvent(
                event_id=ev["event_id"],
                event_type=event_type,
                geometry_type="Point",
                timestamp_utc_s=ev["t0_utc_s"],
                confidence=conf,
                sound_level_db=sound_level_db,
                error_radius_m=gdop_bounds["error_radius_m"],
                hdop=gdop_bounds.get("hdop"),
                rms_residual_ms=rms_ms,
                enu_coords=enu,
                wgs84_coords=wgs84,
                confidence_ellipse=gdop_bounds.get("confidence_ellipse"),
                metadata={
                    "contributing_node_ids": node_ids,
                    "position_sources": {
                        self.survey.names[node_id] or str(node_id):
                        self.survey.position_sources.get(node_id, "survey")
                        for node_id in node_ids
                    },
                    "n_nodes": len(node_ids),
                    "point_source_possible": ev.get("point_source_possible", True),
                    "loci_memory_rejections": loci_rejections,
                },
            )

            feature = to_geojson_feature(spatial_ev)
            features.append(feature)

            if self.geojsonl_path:
                append_geojsonl(feature, self.geojsonl_path)

            if self.publish_mqtt_enabled:
                publish_mqtt_event(
                    feature,
                    host=self.mqtt_host,
                    port=self.mqtt_port,
                    topic=self.mqtt_topic,
                )

        return features

    def process_continuous_clips(
        self,
        clips: Dict[int, Tuple[np.ndarray, float]],
        event_type: str = "aircraft",
        fs: float = 48000.0,
        window_s: float = 0.5,
        hop_s: float = 0.1,
        event_id: Union[str, int] = "traj_001",
    ) -> Optional[Dict[str, Any]]:
        """Process multi-node audio clips for a moving/continuous source and produce a LineString GeoJSON."""
        traj = estimate_trajectory_from_clips(
            clips,
            self.survey,
            fs=fs,
            window_s=window_s,
            hop_s=hop_s,
            temp_c=self.temp_c,
            fixed_up_m=self.fixed_up_m,
        )

        if traj["num_points"] < 2:
            return None

        first_ts = traj["timestamps_s"][0]
        hdop_avg = traj["hdop_mean"] or 1.5
        err_radius = float(hdop_avg * SW.sound_speed(self.temp_c) * 0.001)

        spatial_ev = SpatialEvent(
            event_id=event_id,
            event_type=event_type,
            geometry_type="LineString",
            timestamp_utc_s=first_ts,
            confidence=0.88,
            sound_level_db=traj["sound_level_db"],
            error_radius_m=err_radius,
            hdop=traj["hdop_mean"],
            enu_trajectory=traj["enu_points"],
            wgs84_trajectory=traj["wgs84_points"],
            timestamps_utc_s=traj["timestamps_s"],
            trajectory_length_m=traj["trajectory_length_m"],
            metadata={
                "num_points": traj["num_points"],
            },
        )

        feature = to_geojson_feature(spatial_ev)

        if self.geojsonl_path:
            append_geojsonl(feature, self.geojsonl_path)

        if self.publish_mqtt_enabled:
            publish_mqtt_event(
                feature,
                host=self.mqtt_host,
                port=self.mqtt_port,
                topic=self.mqtt_topic,
            )

        return feature
