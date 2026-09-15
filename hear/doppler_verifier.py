"""Acoustic harmonic Doppler tracking and kinematic speed verification."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
from scipy.optimize import least_squares
from scipy.signal import find_peaks, stft

ReceiverId = Union[int, str]


@dataclass(frozen=True)
class HarmonicTrack:
    """One frequency ridge associated with one receiver and one source harmonic."""
    times_s: np.ndarray
    frequencies_hz: np.ndarray
    amplitudes: np.ndarray
    receiver_id: ReceiverId = 0
    harmonic: int = 1

    def __post_init__(self) -> None:
        t = np.asarray(self.times_s, dtype=float).ravel()
        f = np.asarray(self.frequencies_hz, dtype=float).ravel()
        a = np.asarray(self.amplitudes, dtype=float).ravel()
        if not (len(t) == len(f) == len(a)) or len(t) < 2:
            raise ValueError("track arrays must have equal length and at least two samples")
        if not (np.all(np.isfinite(t)) and np.all(np.isfinite(f)) and np.all(np.isfinite(a))):
            raise ValueError("track arrays must be finite")
        if np.any(np.diff(t) <= 0.0) or np.any(f <= 0.0) or np.any(a < 0.0):
            raise ValueError("times must increase, frequencies must be positive, amplitudes non-negative")
        object.__setattr__(self, "times_s", t)
        object.__setattr__(self, "frequencies_hz", f)
        object.__setattr__(self, "amplitudes", a)


@dataclass(frozen=True)
class DopplerFit:
    f0_hz: float
    velocity_mps: float
    t_cpa_s: float
    d_perp_m: float
    residual_rms_hz: float
    n_observations: int
    success: bool
    message: str = ""

    @property
    def speed_mps(self) -> float:
        return abs(self.velocity_mps)


@dataclass(frozen=True)
class VelocityVerification:
    doppler_velocity_mps: float
    tdoa_velocity_mps: float
    difference_mps: float
    tolerance_mps: float
    consistent: bool


class KinematicDopplerVerifier:
    """Extract harmonic ridges and fit a differential Doppler S-curve."""

    def __init__(self, sound_speed_mps: float = 343.0, n_fft: int = 2048,
                 hop_length: Optional[int] = None, peak_prominence: float = 0.0) -> None:
        if sound_speed_mps <= 0 or n_fft < 32 or n_fft & (n_fft - 1):
            raise ValueError("sound speed must be positive and n_fft must be a power of two >= 32")
        self.sound_speed_mps = float(sound_speed_mps)
        self.n_fft = int(n_fft)
        self.hop_length = int(hop_length or n_fft // 4)
        if self.hop_length <= 0 or self.hop_length > self.n_fft:
            raise ValueError("hop_length must be positive and no greater than n_fft")
        self.peak_prominence = float(peak_prominence)

    def extract_harmonic_tracks(self, audio: Sequence[float], sample_rate: float,
                                receiver_id: ReceiverId = 0, start_time_s: float = 0.0,
                                fmin_hz: float = 40.0, fmax_hz: Optional[float] = None,
                                max_tracks: int = 8, min_track_length: int = 3) -> List[HarmonicTrack]:
        """Track STFT peaks using phase-vocoder instantaneous-frequency estimates."""
        x = np.asarray(audio, dtype=float).ravel()
        if sample_rate <= 0 or len(x) < self.n_fft or fmin_hz < 0 or max_tracks < 1:
            raise ValueError("invalid audio, sample rate, frequency bounds, or max_tracks")
        if fmax_hz is None:
            fmax_hz = sample_rate / 2.0
        if fmax_hz <= fmin_hz:
            raise ValueError("fmax_hz must exceed fmin_hz")
        noverlap = self.n_fft - self.hop_length
        freqs, times, zxx = stft(x, fs=sample_rate, nperseg=self.n_fft,
                                 noverlap=noverlap, nfft=self.n_fft, boundary=None,
                                 padded=False)
        magnitude = np.abs(zxx)
        phase = np.angle(zxx)
        bin_hz = sample_rate / self.n_fft
        if len(times) < 2:
            return []
        inst = np.empty_like(magnitude)
        inst[:, 0] = freqs
        expected = 2.0 * np.pi * np.arange(len(freqs)) * self.hop_length / self.n_fft
        dphase = np.angle(np.exp(1j * (phase[:, 1:] - phase[:, :-1] - expected[:, None])))
        inst[:, 1:] = freqs[:, None] + dphase * sample_rate / (2.0 * np.pi * self.hop_length)
        lo = max(0, int(math.floor(fmin_hz / bin_hz)))
        hi = min(len(freqs), int(math.ceil(fmax_hz / bin_hz)) + 1)

        frames: List[List[Tuple[float, float]]] = []
        for frame in range(len(times)):
            mag = magnitude[lo:hi, frame]
            kwargs = {"prominence": self.peak_prominence} if self.peak_prominence > 0 else {}
            peaks, _ = find_peaks(mag, **kwargs)
            peaks = peaks[np.argsort(mag[peaks])[::-1]][:max_tracks * 2]
            frames.append([(float(inst[lo + p, frame]), float(mag[p])) for p in peaks
                           if fmin_hz <= inst[lo + p, frame] <= fmax_hz])

        active: List[dict] = []
        finished: List[dict] = []
        gate_hz = max(3.0 * bin_hz, 0.04 * (fmax_hz - fmin_hz))
        for frame, peaks in enumerate(frames):
            used = set()
            for track in active:
                if not peaks:
                    track["misses"] += 1
                    continue
                available = [j for j in range(len(peaks)) if j not in used]
                j = min(available, key=lambda k: abs(peaks[k][0] - track["freq"]), default=None)
                if j is not None and abs(peaks[j][0] - track["freq"]) <= gate_hz:
                    used.add(j)
                    track["times"].append(float(times[frame] + start_time_s))
                    track["freqs"].append(peaks[j][0])
                    track["amps"].append(peaks[j][1])
                    track["freq"] = peaks[j][0]
                    track["misses"] = 0
                else:
                    track["misses"] += 1
            for track in list(active):
                if track["misses"] > 1:
                    active.remove(track)
                    finished.append(track)
            for j, (freq, amp) in enumerate(peaks):
                if j not in used:
                    active.append({"times": [float(times[frame] + start_time_s)],
                                   "freqs": [freq], "amps": [amp], "freq": freq, "misses": 0})
        finished.extend(active)
        tracks = [HarmonicTrack(t["times"], t["freqs"], t["amps"], receiver_id)
                  for t in finished if len(t["times"]) >= min_track_length]
        tracks.sort(key=lambda t: float(np.average(t.amplitudes)), reverse=True)
        return tracks[:max_tracks]

    extract_tracks = extract_harmonic_tracks

    def doppler_frequency(self, times_s: Sequence[float], f0_hz: float, velocity_mps: float,
                          t_cpa_s: float, d_perp_m: float, receiver_x_m: float = 0.0) -> np.ndarray:
        """Evaluate the differential Doppler model used by :meth:`fit`."""
        t = np.asarray(times_s, dtype=float)
        if f0_hz <= 0 or d_perp_m <= 0 or abs(velocity_mps) >= self.sound_speed_mps:
            raise ValueError("invalid Doppler parameters")
        dx = velocity_mps * (t - t_cpa_s) - float(receiver_x_m)
        return f0_hz * (1.0 - (velocity_mps / self.sound_speed_mps) * dx /
                        np.sqrt(dx * dx + d_perp_m * d_perp_m))

    def fit(self, tracks: Iterable[HarmonicTrack], receiver_positions_m: Mapping[ReceiverId, float],
            initial: Optional[Tuple[float, float, float, float]] = None,
            loss: str = "soft_l1") -> DopplerFit:
        """Jointly fit ``(f0, velocity, t_cpa, d_perp)`` across synchronized receivers."""
        tracks = list(tracks)
        rows = [(tr, float(receiver_positions_m[tr.receiver_id])) for tr in tracks
                if tr.receiver_id in receiver_positions_m]
        if not rows or sum(len(t.times_s) for t, _ in rows) < 8:
            raise ValueError("at least eight observations with surveyed receiver positions are required")
        f0 = float(np.median(np.concatenate([t.frequencies_hz / max(1, t.harmonic) for t, _ in rows])))
        all_t = np.concatenate([t.times_s for t, _ in rows])
        if initial is None:
            initial = (f0, min(30.0, self.sound_speed_mps * 0.2), float(np.median(all_t)), 10.0)
        x0 = np.asarray(initial, dtype=float)
        if len(x0) != 4:
            raise ValueError("initial must be (f0_hz, velocity_mps, t_cpa_s, d_perp_m)")
        span = max(float(np.ptp(all_t)), 1.0)

        def residual(p: np.ndarray) -> np.ndarray:
            return np.concatenate([max(1, t.harmonic) * self.doppler_frequency(t.times_s, p[0], p[1], p[2], p[3], rx) -
                                    t.frequencies_hz for t, rx in rows])

        result = least_squares(residual, x0,
                               bounds=([1e-6, -0.99 * self.sound_speed_mps, all_t.min() - 10 * span, 1e-6],
                                       [np.inf, 0.99 * self.sound_speed_mps, all_t.max() + 10 * span, np.inf]),
                               loss=loss)
        rms = float(np.sqrt(np.mean(result.fun ** 2)))
        return DopplerFit(float(result.x[0]), float(result.x[1]), float(result.x[2]),
                          float(result.x[3]), rms, len(result.fun), bool(result.success), result.message)

    def cross_verify_velocity(self, fit: DopplerFit, tdoa_velocities_mps: Sequence[float],
                              tolerance_mps: float = 5.0) -> VelocityVerification:
        """Compare Doppler speed with TDoA arrival-time velocity estimates."""
        values = np.asarray(tdoa_velocities_mps, dtype=float).ravel()
        if len(values) == 0 or not np.all(np.isfinite(values)) or tolerance_mps < 0:
            raise ValueError("tdoa velocities must be a non-empty finite sequence")
        tdoa = float(np.mean(np.abs(values)))
        doppler = float(abs(fit.velocity_mps))
        difference = abs(doppler - tdoa)
        return VelocityVerification(doppler, tdoa, difference, float(tolerance_mps),
                                    difference <= tolerance_mps)

    verify_velocity = cross_verify_velocity


__all__ = ["DopplerFit", "HarmonicTrack", "KinematicDopplerVerifier", "VelocityVerification"]
