#!/usr/bin/env python3
"""Biomechanical IMU and acoustic gait classifier for the DAMA acoustic/seismic fleet.

Extracts multi-modal biomechanical features from phone and node telemetry:
  - Acoustic and seismic Crest Factor: max(|x|) / RMS(x)
  - Spectral Centroid & Spectral Flatness across seismic (0.5-50 Hz) and acoustic bands
  - Z-axis seismic kurtosis & vertical Ground Reaction Force (GRF) proxy
  - 20%-to-80% impact rise time (stomp < 5 ms vs normal step 10-30 ms)
  - Cadence estimator (in Hz and Steps Per Minute / SPM) with inter-impact interval CV
  - Dual-peak multiplicity fraction (walking heel-strike/toe-off vs single-peak running)

Classifies strides and streaming activity into:
  - gait.stomp: isolated, high GRF (>3x), <5ms rise time, high Z-kurtosis, reverberant tail
  - gait.walk: cadence 1.6-2.2 Hz / 95-135 SPM, dual-peak heel-toe, moderate GRF (1.1-1.9x BW)
  - gait.run: cadence 2.6-3.8 Hz / 155-230 SPM, single dominant peak, flight phase, periodic
  - gait.stationary: static baseline, low variance (<0.15 BW), no impact peaks
  - gait.ambient_noise: uncorrelated vibration/acoustic noise, high flatness, no rhythmic cadence
"""
from __future__ import annotations

import collections
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

# Canonical Gait Classification Labels
GAIT_STOMP = "gait.stomp"
GAIT_WALK = "gait.walk"
GAIT_RUN = "gait.run"
GAIT_STATIONARY = "gait.stationary"
GAIT_AMBIENT_NOISE = "gait.ambient_noise"
ALL_GAIT_CLASSES = (GAIT_STOMP, GAIT_WALK, GAIT_RUN, GAIT_STATIONARY, GAIT_AMBIENT_NOISE)

# Biomechanical constants
GRAVITY_MPS2 = 9.80665
DEFAULT_IMU_FS_HZ = 100.0
DEFAULT_AUDIO_FS_HZ = 16000.0


@dataclass
class BiomechanicalFeatures:
    """Biomechanical feature set extracted from an IMU/acoustic window."""

    acoustic_crest_factor: float = 0.0
    seismic_crest_factor: float = 0.0
    spectral_centroid_seismic_hz: float = 0.0
    spectral_centroid_acoustic_hz: float = 0.0
    spectral_flatness_seismic: float = 0.0
    spectral_flatness_acoustic: float = 0.0
    z_kurtosis: float = 0.0
    z_excess_kurtosis: float = 0.0
    grf_peak_bw: float = 1.0
    grf_mean_bw: float = 1.0
    impact_rise_time_ms: float = 0.0
    cadence_hz: float = 0.0
    cadence_spm: float = 0.0
    interval_cv: float = 0.0
    dual_peak_fraction: float = 0.0
    flight_phase_fraction: float = 0.0
    stride_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "acoustic_crest_factor": round(float(self.acoustic_crest_factor), 4),
            "seismic_crest_factor": round(float(self.seismic_crest_factor), 4),
            "spectral_centroid_seismic_hz": round(float(self.spectral_centroid_seismic_hz), 2),
            "spectral_centroid_acoustic_hz": round(float(self.spectral_centroid_acoustic_hz), 2),
            "spectral_flatness_seismic": round(float(self.spectral_flatness_seismic), 4),
            "spectral_flatness_acoustic": round(float(self.spectral_flatness_acoustic), 4),
            "z_kurtosis": round(float(self.z_kurtosis), 4),
            "z_excess_kurtosis": round(float(self.z_excess_kurtosis), 4),
            "grf_peak_bw": round(float(self.grf_peak_bw), 4),
            "grf_mean_bw": round(float(self.grf_mean_bw), 4),
            "impact_rise_time_ms": round(float(self.impact_rise_time_ms), 2),
            "cadence_hz": round(float(self.cadence_hz), 3),
            "cadence_spm": round(float(self.cadence_spm), 1),
            "interval_cv": round(float(self.interval_cv), 4),
            "dual_peak_fraction": round(float(self.dual_peak_fraction), 4),
            "flight_phase_fraction": round(float(self.flight_phase_fraction), 4),
            "stride_count": int(self.stride_count),
        }


@dataclass
class GaitClassificationResult:
    """Output verdict from GaitBiomechanicalClassifier."""

    label: str
    confidence: float
    scores: Dict[str, float]
    features: BiomechanicalFeatures
    stride_times_s: List[float] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "confidence": round(float(self.confidence), 4),
            "scores": {k: round(float(v), 4) for k, v in self.scores.items()},
            "features": self.features.to_dict(),
            "stride_times_s": [round(float(t), 4) for t in self.stride_times_s],
            "details": self.details,
        }

    def to_tag_dict(
        self,
        node: Optional[str] = None,
        clip_key: Optional[str] = None,
        ts_utc_s: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Convert classification result into a schema v2 tag dictionary."""
        sorted_preds = sorted(
            [{"label": k, "score": round(float(v), 4)} for k, v in self.scores.items()],
            key=lambda p: p["score"],
            reverse=True,
        )
        for rank, p in enumerate(sorted_preds):
            p["rank"] = rank

        ckey = clip_key or f"gait-{node or 'phone'}-{int((ts_utc_s or 0.0) * 1000)}"
        return {
            "schema_version": 2,
            "provenance": "model",
            "clip_key": ckey,
            "node": node,
            "ts_utc_s": ts_utc_s,
            "model": {
                "name": "gait_biomechanical_classifier",
                "version": "v1.0.0",
                "runtime": "python_numpy",
            },
            "predictions": sorted_preds,
            "claim": {
                "is_species_id": False,
                "human_verified": False,
                "calibrated_to_this_site": True,
                "trained_on_this_corpus": True,
                "usable_as_training_label": self.confidence >= 0.85,
            },
            "extra": {
                "features": self.features.to_dict(),
                "details": self.details,
            },
        }

    def to_tag_record(
        self,
        clip_key: str,
        node: Optional[str] = None,
        ts_utc_s: Optional[float] = None,
    ) -> Any:
        """Construct a TagRecord object if hear_tagging is available."""
        try:
            from hear_tagging.schema import ModelProvenance, Prediction, TagRecord

            model = ModelProvenance(
                name="gait_biomechanical_classifier",
                version="v1.0.0",
                runtime="python_numpy",
            )
            sorted_preds = sorted(
                [Prediction(label=k, score=float(v)) for k, v in self.scores.items()],
                key=lambda p: p.score,
                reverse=True,
            )
            ranked_preds = [
                Prediction(label=p.label, score=p.score, rank=rank)
                for rank, p in enumerate(sorted_preds)
            ]
            return TagRecord.build(
                clip_key=clip_key,
                model=model,
                predictions=ranked_preds,
                node=node,
                ts_utc_s=ts_utc_s,
                claim={
                    "is_species_id": False,
                    "human_verified": False,
                    "calibrated_to_this_site": True,
                    "trained_on_this_corpus": True,
                    "usable_as_training_label": self.confidence >= 0.85,
                },
            )
        except ImportError:
            return self.to_tag_dict(node=node, clip_key=clip_key, ts_utc_s=ts_utc_s)


def compute_crest_factor(x: np.ndarray) -> float:
    """Compute Crest Factor: max(abs(x)) / RMS(x).

    Returns 0.0 for near-zero signals.
    """
    arr = np.asarray(x, dtype=float)
    if arr.size == 0:
        return 0.0
    peak = float(np.max(np.abs(arr)))
    rms = float(np.sqrt(np.mean(arr**2)))
    if rms < 1e-12:
        return 0.0
    return float(peak / rms)


def compute_spectral_centroid(
    x: np.ndarray,
    fs: float,
    f_min: float = 0.0,
    f_max: Optional[float] = None,
) -> float:
    """Compute Spectral Centroid: sum(f * |X(f)|) / sum(|X(f)|) in Hz."""
    arr = np.asarray(x, dtype=float)
    if arr.size < 4 or fs <= 0.0:
        return 0.0
    arr_centered = arr - np.mean(arr)
    n = arr_centered.size
    fft_vals = np.fft.rfft(arr_centered)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    mag = np.abs(fft_vals)

    if f_max is None:
        f_max = freqs[-1]
    mask = (freqs >= f_min) & (freqs <= f_max)
    if not np.any(mask):
        return 0.0
    sub_freqs = freqs[mask]
    sub_mag = mag[mask]
    total_mag = float(np.sum(sub_mag))
    if total_mag < 1e-12:
        return 0.0
    return float(np.sum(sub_freqs * sub_mag) / total_mag)


def compute_spectral_flatness(
    x: np.ndarray,
    fs: float,
    f_min: float = 0.0,
    f_max: Optional[float] = None,
) -> float:
    """Compute Spectral Flatness (Wiener entropy): geometric_mean(P) / arithmetic_mean(P).

    Returns a value in [0, 1]. Near 1.0 indicates flat white noise; near 0.0 indicates tonal/peaked.
    """
    arr = np.asarray(x, dtype=float)
    if arr.size < 4 or fs <= 0.0:
        return 0.0
    arr_centered = arr - np.mean(arr)
    n = arr_centered.size
    fft_vals = np.fft.rfft(arr_centered)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    power = np.abs(fft_vals) ** 2

    if f_max is None:
        f_max = freqs[-1]
    mask = (freqs >= f_min) & (freqs <= f_max)
    if not np.any(mask):
        return 0.0
    sub_power = power[mask]
    if sub_power.size == 0:
        return 0.0
    eps = 1e-12
    sub_power_clamped = np.maximum(sub_power, eps)
    geom_mean = float(np.exp(np.mean(np.log(sub_power_clamped))))
    arith_mean = float(np.mean(sub_power_clamped))
    if arith_mean < eps:
        return 0.0
    flatness = float(geom_mean / arith_mean)
    return float(np.clip(flatness, 0.0, 1.0))


def compute_z_kurtosis(z: np.ndarray) -> Tuple[float, float]:
    """Compute vertical axis Z kurtosis and excess kurtosis.

    Returns (kurtosis, excess_kurtosis). For Gaussian noise, kurtosis ~ 3.0, excess ~ 0.0.
    """
    arr = np.asarray(z, dtype=float)
    if arr.size < 4:
        return (0.0, 0.0)
    mean_val = float(np.mean(arr))
    var_val = float(np.var(arr))
    if var_val < 1e-12:
        return (0.0, 0.0)
    m4 = float(np.mean((arr - mean_val) ** 4))
    kurt = float(m4 / (var_val**2))
    excess_kurt = float(kurt - 3.0)
    return (kurt, excess_kurt)


def compute_grf_proxy(z: np.ndarray) -> Tuple[float, float, float]:
    """Compute vertical Ground Reaction Force (GRF) proxy in multiples of Body Weight (BW / g).

    Returns (grf_peak_bw, grf_mean_bw, g_scale).
    Auto-detects whether z is in m/s^2 (median/mean/max in m/s^2 range) or g (in g range).
    """
    arr = np.asarray(z, dtype=float)
    if arr.size == 0:
        return (1.0, 1.0, 1.0)
    abs_z = np.abs(arr)
    med_val = float(np.median(abs_z))
    mean_val = float(np.mean(abs_z))
    max_val = float(np.max(abs_z))

    # If median/mean > 4.5 m/s^2 or peak > 18.0 m/s^2, scale by 9.80665 m/s^2
    if med_val > 4.5 or mean_val > 4.5 or max_val > 18.0:
        g_scale = GRAVITY_MPS2
    else:
        g_scale = 1.0

    grf_peak = float(max_val / g_scale)
    grf_mean = float(mean_val / g_scale)
    return (grf_peak, grf_mean, g_scale)


def compute_impact_rise_time_ms(
    z: np.ndarray,
    fs: float,
    peak_indices: Optional[Sequence[int]] = None,
    g_scale: float = 1.0,
) -> float:
    """Compute 20%-to-80% impact rise time in milliseconds.

    For each detected impact peak, measures time delta from 20% to 80% of peak above local floor.
    Stomp impact: < 5 ms.
    Normal step impact: 10 - 30 ms.
    """
    arr = np.asarray(z, dtype=float)
    if arr.size < 4 or fs <= 0.0:
        return 0.0

    # Moving envelope (~2 ms window)
    window_pts = max(1, int(0.002 * fs))
    env = np.convolve(np.abs(arr), np.ones(window_pts) / window_pts, mode="same")

    if peak_indices is None or len(peak_indices) == 0:
        # Fallback to absolute maximum in window
        peak_idx = int(np.argmax(env))
        peak_indices = [peak_idx]

    rise_times_ms: List[float] = []
    # Search window back before peak: up to 80 ms or 40 samples
    back_pts = max(3, min(int(0.080 * fs), len(env)))

    for p_idx in peak_indices:
        if p_idx <= 0 or p_idx >= len(env):
            continue
        lo = max(0, p_idx - back_pts)
        if lo >= p_idx:
            continue
        seg = env[lo:p_idx + 1]
        floor_val = float(np.min(seg))
        peak_val = float(env[p_idx])
        height = peak_val - floor_val
        if height < 1e-6:
            continue

        target_20 = floor_val + 0.20 * height
        target_80 = floor_val + 0.80 * height

        # Find 20% crossing
        below_20 = np.nonzero(seg < target_20)[0]
        if below_20.size > 0:
            j20 = lo + int(below_20[-1])
            if j20 + 1 < len(env) and env[j20 + 1] > env[j20]:
                idx_20 = j20 + (target_20 - env[j20]) / (env[j20 + 1] - env[j20])
            else:
                idx_20 = float(j20)
        else:
            idx_20 = float(lo)

        # Find 80% crossing
        above_80 = np.nonzero(seg >= target_80)[0]
        if above_80.size > 0:
            j80 = lo + int(above_80[0])
            if j80 > 0 and env[j80] > env[j80 - 1]:
                idx_80 = (j80 - 1) + (target_80 - env[j80 - 1]) / (env[j80] - env[j80 - 1])
            else:
                idx_80 = float(j80)
        else:
            idx_80 = float(p_idx)

        dt_samples = max(0.0, idx_80 - idx_20)
        dt_ms = (dt_samples / fs) * 1000.0
        rise_times_ms.append(dt_ms)

    if not rise_times_ms:
        return 0.0
    return float(np.median(rise_times_ms))


def detect_footstep_impacts(
    z: np.ndarray,
    fs: float,
    timestamps: Optional[Sequence[float]] = None,
    g_scale: float = 1.0,
) -> Tuple[List[int], List[float]]:
    """Detect prominent impact peak sample indices and timestamps."""
    arr = np.asarray(z, dtype=float)
    if arr.size < 4 or fs <= 0.0:
        return ([], [])

    abs_z = np.abs(arr)
    # 5 ms envelope for peak picking
    n_env = max(1, int(0.005 * fs))
    env = np.convolve(abs_z, np.ones(n_env) / n_env, mode="same")

    mean_env = float(np.mean(env))
    std_env = float(np.std(env))
    # Threshold for contact impact: at least 1.15 BW or mean + 1.2 * std
    thresh = max(1.15 * g_scale, mean_env + 1.2 * std_env)

    # Minimum refractory distance between strides: 200 ms (max 5 Hz / 300 SPM)
    min_dist = max(2, int(0.200 * fs))

    peak_indices: List[int] = []
    i = 1
    while i < len(env) - 1:
        if env[i] > thresh and env[i] >= env[i - 1] and env[i] >= env[i + 1]:
            # Look ahead within min_dist to find local maximum
            j_end = min(len(env), i + min_dist)
            local_max_idx = i + int(np.argmax(env[i:j_end]))
            peak_indices.append(local_max_idx)
            i = local_max_idx + min_dist
        else:
            i += 1

    if timestamps is not None and len(timestamps) == len(arr):
        ts_arr = np.asarray(timestamps, dtype=float)
        impact_times = [float(ts_arr[idx]) for idx in peak_indices]
    else:
        impact_times = [float(idx / fs) for idx in peak_indices]

    return (peak_indices, impact_times)


def estimate_cadence_and_cv(impact_times_s: Sequence[float]) -> Tuple[float, float, float]:
    """Estimate cadence in Hz and SPM, plus inter-impact interval Coefficient of Variation (CV).

    Returns (cadence_hz, cadence_spm, interval_cv).
    """
    times = np.asarray(impact_times_s, dtype=float)
    if times.size < 2:
        return (0.0, 0.0, 1.0 if times.size == 1 else 0.0)

    intervals = np.diff(times)
    valid = intervals[intervals > 0.10]  # Stride intervals > 100 ms
    if valid.size == 0:
        return (0.0, 0.0, 1.0)

    med_dt = float(np.median(valid))
    mean_dt = float(np.mean(valid))
    std_dt = float(np.std(valid)) if valid.size >= 2 else 0.0

    cadence_hz = float(1.0 / med_dt) if med_dt > 0.0 else 0.0
    cadence_spm = float(cadence_hz * 60.0)
    interval_cv = float(std_dt / mean_dt) if mean_dt > 1e-6 else 0.0

    return (cadence_hz, cadence_spm, interval_cv)


def compute_dual_peak_and_flight_fractions(
    z: np.ndarray,
    fs: float,
    peak_indices: Sequence[int],
    g_scale: float = 1.0,
) -> Tuple[float, float]:
    """Compute dual-peak multiplicity fraction and flight phase fraction.

    In walking, foot contact displays dual peaks (heel strike and toe-off).
    In running, contact is a single impact spike followed by an airborne flight phase.

    Returns (dual_peak_fraction, flight_phase_fraction).
    """
    arr = np.asarray(z, dtype=float)
    if arr.size < 4 or fs <= 0.0:
        return (0.0, 0.0)

    abs_z = np.abs(arr)
    stride_count = len(peak_indices)
    if stride_count >= 2:
        diff_pts = np.diff(np.asarray(peak_indices, dtype=float))
        valid_diff = diff_pts[diff_pts > 0]
        med_stride_pts = float(np.median(valid_diff)) if valid_diff.size > 0 else (0.500 * fs)
        # Stance contact window is bounded by half the stride cycle or 240 ms max
        contact_win_pts = max(int(0.100 * fs), min(int(0.240 * fs), int(0.55 * med_stride_pts)))
    else:
        contact_win_pts = int(0.240 * fs)

    min_peak_spacing = max(2, int(0.045 * fs))

    dual_peak_count = 0

    for p_idx in peak_indices:
        win_start = max(0, p_idx - int(0.040 * fs))
        win_end = min(len(abs_z), p_idx + contact_win_pts)
        if win_end - win_start < min_peak_spacing * 2:
            continue
        stride_seg = abs_z[win_start:win_end]
        seg_floor = float(np.min(stride_seg))
        seg_max = float(np.max(stride_seg))
        dyn_range = seg_max - seg_floor
        if dyn_range < 0.15 * g_scale:
            continue

        # Dynamic profile relative to floor
        dyn_seg = stride_seg - seg_floor
        dyn_thresh = 0.35 * dyn_range

        # Find distinct local peaks that rise above dynamic threshold
        peak_candidates: List[int] = []
        for k in range(1, len(stride_seg) - 1):
            if dyn_seg[k] >= dyn_thresh:
                # Must be a true peak: greater than at least one neighbor and >= both
                if (stride_seg[k] > stride_seg[k - 1] and stride_seg[k] >= stride_seg[k + 1]) or (
                    stride_seg[k] >= stride_seg[k - 1] and stride_seg[k] > stride_seg[k + 1]
                ):
                    if not peak_candidates or (k - peak_candidates[-1]) >= min_peak_spacing:
                        peak_candidates.append(k)
                    elif stride_seg[k] > stride_seg[peak_candidates[-1]]:
                        peak_candidates[-1] = k

        # Check if there are at least 2 prominent peaks separated by a trough
        if len(peak_candidates) >= 2:
            p1 = peak_candidates[0]
            p2 = peak_candidates[1]
            trough_val = float(np.min(stride_seg[p1:p2 + 1]))
            lower_peak = min(stride_seg[p1], stride_seg[p2])
            # Trough must dip below lower peak by at least 15% of dynamic range or 10% of lower peak
            if (lower_peak - trough_val) >= max(0.10 * dyn_range, 0.05 * lower_peak):
                dual_peak_count += 1

    dual_peak_fraction = float(dual_peak_count / stride_count) if stride_count > 0 else 0.0

    # Flight phase fraction: fraction of signal where vertical force drops below 0.35 BW
    flight_samples = np.sum(abs_z < (0.35 * g_scale))
    flight_phase_fraction = float(flight_samples / len(abs_z))

    return (dual_peak_fraction, flight_phase_fraction)


class GaitBiomechanicalClassifier:
    """Multi-modal biomechanical classifier for phone IMU and acoustic streams."""

    def __init__(
        self,
        imu_fs: float = DEFAULT_IMU_FS_HZ,
        audio_fs: float = DEFAULT_AUDIO_FS_HZ,
        window_s: float = 2.5,
        hop_s: float = 0.5,
    ) -> None:
        self.imu_fs = float(imu_fs)
        self.audio_fs = float(audio_fs)
        self.window_s = float(window_s)
        self.hop_s = float(hop_s)

        # Streaming buffer state
        self._imu_t_buf: List[float] = []
        self._imu_z_buf: List[float] = []
        self._audio_t_buf: List[float] = []
        self._audio_buf: List[float] = []
        self._last_processed_t: float = 0.0

    def extract_features(
        self,
        imu_z: Sequence[float],
        audio: Optional[Sequence[float]] = None,
        timestamps: Optional[Sequence[float]] = None,
        imu_fs: Optional[float] = None,
        audio_fs: Optional[float] = None,
    ) -> BiomechanicalFeatures:
        """Extract comprehensive biomechanical features from IMU vertical acceleration and audio."""
        z_arr = np.asarray(imu_z, dtype=float)
        aud_arr = np.asarray(audio, dtype=float) if audio is not None else np.empty(0, dtype=float)
        fs_imu = float(imu_fs or self.imu_fs)
        fs_aud = float(audio_fs or self.audio_fs)

        # Adapt sample rate from timestamps if provided
        if timestamps is not None and len(timestamps) > 3:
            dt = np.diff(np.asarray(timestamps, dtype=float))
            valid_dt = dt[dt > 1e-5]
            if valid_dt.size > 0:
                fs_imu = float(1.0 / np.median(valid_dt))

        if z_arr.size == 0:
            return BiomechanicalFeatures()

        # 1. Crest factors
        seismic_cf = compute_crest_factor(z_arr)
        acoustic_cf = compute_crest_factor(aud_arr) if aud_arr.size > 0 else 0.0

        # 2. Spectral centroid & flatness
        seis_centroid = compute_spectral_centroid(z_arr, fs_imu, f_min=0.5, f_max=min(50.0, fs_imu / 2.0))
        seis_flatness = compute_spectral_flatness(z_arr, fs_imu, f_min=0.5, f_max=min(50.0, fs_imu / 2.0))

        if aud_arr.size > 16:
            aud_centroid = compute_spectral_centroid(aud_arr, fs_aud, f_min=20.0, f_max=min(4000.0, fs_aud / 2.0))
            aud_flatness = compute_spectral_flatness(aud_arr, fs_aud, f_min=20.0, f_max=min(4000.0, fs_aud / 2.0))
        else:
            aud_centroid = 0.0
            aud_flatness = 0.0

        # 3. Z-axis kurtosis & GRF
        z_kurt, z_excess = compute_z_kurtosis(z_arr)
        grf_peak, grf_mean, g_scale = compute_grf_proxy(z_arr)

        # 4. Footstep impacts & cadence
        peak_indices, impact_times = detect_footstep_impacts(z_arr, fs_imu, timestamps, g_scale)
        cadence_hz, cadence_spm, interval_cv = estimate_cadence_and_cv(impact_times)

        # 5. Rise time
        rise_time_ms = compute_impact_rise_time_ms(z_arr, fs_imu, peak_indices, g_scale)

        # 6. Dual-peak multiplicity & flight phase fractions
        dual_frac, flight_frac = compute_dual_peak_and_flight_fractions(z_arr, fs_imu, peak_indices, g_scale)

        return BiomechanicalFeatures(
            acoustic_crest_factor=acoustic_cf,
            seismic_crest_factor=seismic_cf,
            spectral_centroid_seismic_hz=seis_centroid,
            spectral_centroid_acoustic_hz=aud_centroid,
            spectral_flatness_seismic=seis_flatness,
            spectral_flatness_acoustic=aud_flatness,
            z_kurtosis=z_kurt,
            z_excess_kurtosis=z_excess,
            grf_peak_bw=grf_peak,
            grf_mean_bw=grf_mean,
            impact_rise_time_ms=rise_time_ms,
            cadence_hz=cadence_hz,
            cadence_spm=cadence_spm,
            interval_cv=interval_cv,
            dual_peak_fraction=dual_frac,
            flight_phase_fraction=flight_frac,
            stride_count=len(peak_indices),
        )

    def classify(
        self,
        imu_z: Sequence[float],
        audio: Optional[Sequence[float]] = None,
        timestamps: Optional[Sequence[float]] = None,
        imu_fs: Optional[float] = None,
        audio_fs: Optional[float] = None,
    ) -> GaitClassificationResult:
        """Classify biomechanical activity over an IMU/audio window."""
        z_arr = np.asarray(imu_z, dtype=float)
        fs_imu = float(imu_fs or self.imu_fs)

        if z_arr.size < 4:
            feat = BiomechanicalFeatures()
            scores = {c: (1.0 if c == GAIT_STATIONARY else 0.0) for c in ALL_GAIT_CLASSES}
            return GaitClassificationResult(
                label=GAIT_STATIONARY,
                confidence=0.5,
                scores=scores,
                features=feat,
                stride_times_s=[],
                details={"reason": "empty or insufficient samples"},
            )

        features = self.extract_features(
            imu_z=z_arr,
            audio=audio,
            timestamps=timestamps,
            imu_fs=fs_imu,
            audio_fs=audio_fs,
        )

        _, _, g_scale = compute_grf_proxy(z_arr)
        _, impact_times = detect_footstep_impacts(
            z_arr, fs_imu, timestamps, g_scale=g_scale
        )

        scores = self._score_classes(features, z_arr, g_scale=g_scale)
        best_label = max(scores, key=lambda k: scores[k])
        confidence = float(scores[best_label])

        return GaitClassificationResult(
            label=best_label,
            confidence=confidence,
            scores=scores,
            features=features,
            stride_times_s=impact_times,
            details={
                "cadence_hz": round(features.cadence_hz, 3),
                "cadence_spm": round(features.cadence_spm, 1),
                "grf_peak_bw": round(features.grf_peak_bw, 3),
                "rise_time_ms": round(features.impact_rise_time_ms, 2),
                "dual_peak_fraction": round(features.dual_peak_fraction, 3),
            },
        )

    def _score_classes(
        self,
        f: BiomechanicalFeatures,
        z_raw: np.ndarray,
        g_scale: float = 1.0,
    ) -> Dict[str, float]:
        """Compute normalized class likelihoods from biomechanical features."""
        # Baseline raw signals
        z_std = float(np.std(z_raw))
        z_std_bw = z_std / g_scale

        # 1. Stationary Score
        if z_std_bw < 0.15 and f.grf_peak_bw < 1.15 and f.stride_count == 0:
            s_stat = 6.0 * (1.0 - z_std_bw / 0.15)
        else:
            s_stat = 0.001

        # 2. Stomp Score (isolated, GRF > 3x, rise time < 5ms, high kurtosis, high crest factor)
        s_stomp = 0.0
        # If periodic steps (stride_count >= 3 and low CV), it is running or walking, not stomp
        is_periodic_gait = f.stride_count >= 3 and f.interval_cv < 0.25
        if f.grf_peak_bw >= 2.6 and not is_periodic_gait:
            grf_boost = min(4.5, (f.grf_peak_bw - 2.5) * 2.0)
            rise_boost = 3.5 if (0.0 < f.impact_rise_time_ms < 5.5) else max(0.0, 2.5 - f.impact_rise_time_ms * 0.2)
            kurt_boost = min(3.0, max(0.0, (f.z_kurtosis - 4.0) * 0.5))
            crest_boost = min(2.5, max(0.0, (f.seismic_crest_factor - 3.5) * 0.5))
            isolated_boost = 3.0 if (f.stride_count <= 2 or f.interval_cv > 0.30) else 0.5
            s_stomp = grf_boost + rise_boost + kurt_boost + crest_boost + isolated_boost

        # 3. Walk Score (Cadence 1.6-2.2 Hz / 95-135 SPM, dual-peak heel-toe, moderate GRF 1.1-1.9x BW)
        s_walk = 0.0
        if 0.9 <= f.cadence_hz <= 2.6 and f.stride_count >= 2:
            cad_dist = abs(f.cadence_hz - 1.9)
            cad_score = max(0.0, 3.5 - cad_dist * 4.0)
            dual_score = 3.5 * f.dual_peak_fraction
            grf_score = 2.5 if (1.1 <= f.grf_peak_bw <= 2.0) else max(0.0, 2.0 - abs(f.grf_peak_bw - 1.5))
            rise_score = 2.0 if (8.0 <= f.impact_rise_time_ms <= 35.0) else 0.5
            cv_score = max(0.0, 2.0 - f.interval_cv * 6.0)
            flight_pen = -3.0 * f.flight_phase_fraction
            s_walk = max(0.0, cad_score + dual_score + grf_score + rise_score + cv_score + flight_pen)

        # 4. Run Score (Cadence 2.6-3.8 Hz / 155-230 SPM, single dominant peak, flight phase, GRF 2.0-3.5x BW)
        s_run = 0.0
        if f.cadence_hz >= 2.2 and f.stride_count >= 2:
            cad_dist = abs(f.cadence_hz - 3.1)
            cad_score = max(0.0, 4.5 - cad_dist * 2.5)
            flight_score = min(3.5, f.flight_phase_fraction * 12.0)
            single_peak_score = 3.0 * (1.0 - f.dual_peak_fraction)
            grf_score = 3.0 if (1.8 <= f.grf_peak_bw <= 3.8) else 1.0
            cv_score = max(0.0, 2.5 - f.interval_cv * 6.0)
            s_run = max(0.0, cad_score + flight_score + single_peak_score + grf_score + cv_score)

        # 5. Ambient Noise Score (high flatness, low/moderate GRF, irregular cadence, non-stationary)
        s_amb = 0.0
        if (f.spectral_flatness_seismic > 0.35 or f.spectral_flatness_acoustic > 0.40) and z_std_bw >= 0.12:
            flat_score = 2.5 * (f.spectral_flatness_seismic + f.spectral_flatness_acoustic)
            no_cad_score = 2.0 if (f.stride_count == 0 or f.interval_cv > 0.35) else 0.0
            low_grf_score = 2.0 if (f.grf_peak_bw < 2.2) else 0.0
            s_amb = flat_score + no_cad_score + low_grf_score

        raw_scores = {
            GAIT_STOMP: max(0.001, s_stomp),
            GAIT_WALK: max(0.001, s_walk),
            GAIT_RUN: max(0.001, s_run),
            GAIT_STATIONARY: max(0.001, s_stat),
            GAIT_AMBIENT_NOISE: max(0.001, s_amb),
        }

        # Softmax normalization with temperature
        keys = list(raw_scores.keys())
        vals = np.array([raw_scores[k] for k in keys], dtype=float)
        exp_vals = np.exp(vals - np.max(vals))
        probs = exp_vals / np.sum(exp_vals)

        return {k: float(p) for k, p in zip(keys, probs)}

    # Streaming API
    def reset_streaming(self) -> None:
        """Reset internal streaming buffers."""
        self._imu_t_buf.clear()
        self._imu_z_buf.clear()
        self._audio_t_buf.clear()
        self._audio_buf.clear()
        self._last_processed_t = 0.0

    def ingest_imu_frame(
        self,
        t_s: float,
        z: float,
        x: float = 0.0,
        y: float = 0.0,
    ) -> Optional[GaitClassificationResult]:
        """Ingest a single IMU frame and evaluate when window is ready."""
        self._imu_t_buf.append(float(t_s))
        self._imu_z_buf.append(float(z))
        return self._maybe_evaluate_stream()

    def ingest_imu_block(
        self,
        timestamps: Sequence[float],
        z_samples: Sequence[float],
    ) -> Optional[GaitClassificationResult]:
        """Ingest a block of IMU samples."""
        self._imu_t_buf.extend(float(t) for t in timestamps)
        self._imu_z_buf.extend(float(z) for z in z_samples)
        return self._maybe_evaluate_stream()

    def ingest_audio_block(
        self,
        timestamps: Sequence[float],
        audio_samples: Sequence[float],
    ) -> None:
        """Ingest a block of acoustic samples."""
        self._audio_t_buf.extend(float(t) for t in timestamps)
        self._audio_buf.extend(float(a) for a in audio_samples)

    def _maybe_evaluate_stream(self) -> Optional[GaitClassificationResult]:
        if not self._imu_t_buf:
            return None
        t_now = self._imu_t_buf[-1]
        t_start = self._imu_t_buf[0]
        duration = t_now - t_start

        if duration >= self.window_s and (t_now - self._last_processed_t) >= self.hop_s:
            # Extract window
            win_mask = [t >= (t_now - self.window_s) for t in self._imu_t_buf]
            win_z = [z for z, m in zip(self._imu_z_buf, win_mask) if m]
            win_t = [t for t, m in zip(self._imu_t_buf, win_mask) if m]

            # Matching audio
            win_aud: Optional[List[float]] = None
            if self._audio_t_buf:
                aud_mask = [t >= (t_now - self.window_s) for t in self._audio_t_buf]
                win_aud = [a for a, m in zip(self._audio_buf, aud_mask) if m]

            # Trim stale buffer before window start
            trim_t = t_now - self.window_s * 2.0
            trim_idx = next((i for i, t in enumerate(self._imu_t_buf) if t >= trim_t), 0)
            if trim_idx > 0:
                del self._imu_t_buf[:trim_idx]
                del self._imu_z_buf[:trim_idx]
            if self._audio_t_buf:
                trim_aud_idx = next((i for i, t in enumerate(self._audio_t_buf) if t >= trim_t), 0)
                if trim_aud_idx > 0:
                    del self._audio_t_buf[:trim_aud_idx]
                    del self._audio_buf[:trim_aud_idx]

            self._last_processed_t = t_now
            return self.classify(win_z, audio=win_aud, timestamps=win_t)

        return None

    def classify_phone_telemetry(
        self,
        phone: Any,
        window_s: Optional[float] = None,
    ) -> GaitClassificationResult:
        """Evaluate gait classification directly on a PhoneTelemetry dataclass instance."""
        imu_z = getattr(phone, "imu_z", [])
        audio = getattr(phone, "audio", [])
        n_imu = len(imu_z)
        if n_imu == 0:
            return self.classify([])

        win_pts = int((window_s or self.window_s) * self.imu_fs)
        sub_z = imu_z[-win_pts:] if win_pts < n_imu else imu_z
        aud_pts = int((window_s or self.window_s) * self.audio_fs)
        sub_aud = audio[-aud_pts:] if (audio and aud_pts < len(audio)) else audio

        return self.classify(sub_z, audio=sub_aud)

    def ingest_mqtt_payload(self, topic: str, payload: Any) -> Optional[GaitClassificationResult]:
        """Ingest an MQTT message on dama/+/imu_stream or dama/+/audio_pull and process."""
        if not isinstance(topic, str):
            return None
        match = re.match(r"^dama/([^/]+)/(.+)$", topic)
        if not match:
            return None
        channel = match.group(2)
        samples = self._coerce_samples(payload)
        if not samples:
            return None

        import time
        now = time.time()

        if channel.endswith("imu_stream"):
            dt = 1.0 / self.imu_fs
            ts = [now - (len(samples) - 1 - i) * dt for i in range(len(samples))]
            return self.ingest_imu_block(ts, samples)
        elif channel.endswith("audio_pull") or channel.endswith("audio_trigger"):
            dt = 1.0 / self.audio_fs
            ts = [now - (len(samples) - 1 - i) * dt for i in range(len(samples))]
            self.ingest_audio_block(ts, samples)
        return None

    @staticmethod
    def _coerce_samples(payload: Any) -> List[float]:
        if isinstance(payload, (int, float)):
            return [float(payload)]
        if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
            out: List[float] = []
            for item in payload:
                try:
                    out.append(float(item))
                except (TypeError, ValueError):
                    continue
            return out
        if isinstance(payload, Mapping):
            for key in ("samples", "values", "data", "z", "imu_z"):
                if key in payload:
                    return GaitBiomechanicalClassifier._coerce_samples(payload[key])
            for key in ("value", "amplitude", "sample"):
                if key in payload:
                    try:
                        return [float(payload[key])]
                    except (TypeError, ValueError):
                        pass
        return []
