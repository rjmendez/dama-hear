"""One-shot, RAM-only waveform correlation for privacy-safe TDoA evidence.

The input waveforms exist only inside ``TransientTdoaWindow``. ``analyze`` derives relative
arrival delays, runs the supplied VAD, and zeroes every owned sample buffer in a ``finally``
block. The returned record contains no waveform, spectrum, embedding, or speech content.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

import numpy as np

SCHEMA = "hear.tdoa.transient.v1"
MAX_WINDOW_S = 10.0
_FORBIDDEN_KEYS = (
    "audio", "sample", "waveform", "pcm", "spectrum", "spectrogram", "embedding",
    "voiceprint", "transcript", "words", "content",
)
_SAFE_KEYS = frozenset({"sample_rate_hz", "zero_audio_retained"})
_RECORD_KEYS = frozenset({
    "schema", "generated_at", "reference_node_id", "sample_rate_hz", "window_duration_s",
    "timing_resolution_s", "arrivals", "human_speech_detected", "vad_failed_closed",
    "zero_audio_retained", "position_enu_m",
})
_ARRIVAL_KEYS = frozenset({
    "node_id", "delta_s", "correlation_peak", "delay_sigma_s", "arrival_utc_s",
})
_POSITION_KEYS = frozenset({"east_m", "north_m", "up_m"})


class TransientTdoaError(ValueError):
    """The transient window cannot be processed without weakening its contract."""


@dataclass(frozen=True)
class DelayEstimate:
    node_id: str
    delta_s: float
    correlation_peak: float
    delay_sigma_s: float
    arrival_utc_s: Optional[float]

    def as_record(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "node_id": self.node_id,
            "delta_s": self.delta_s,
            "correlation_peak": self.correlation_peak,
            "delay_sigma_s": self.delay_sigma_s,
        }
        if self.arrival_utc_s is not None:
            record["arrival_utc_s"] = self.arrival_utc_s
        return record


def _peak_sigma(correlation: np.ndarray, peak_index: int, fs_hz: float) -> float:
    """Estimate peak width as Gaussian sigma, bounded by sample quantisation."""
    magnitude = np.abs(correlation)
    peak = float(magnitude[peak_index])
    quantisation_sigma = 1.0 / (math.sqrt(12.0) * fs_hz)
    if peak <= 0.0:
        return quantisation_sigma
    half = peak * 0.5
    left = peak_index
    right = peak_index
    while left > 0 and magnitude[left] >= half:
        left -= 1
    while right + 1 < len(magnitude) and magnitude[right] >= half:
        right += 1
    fwhm_s = max(1, right - left) / fs_hz
    return max(quantisation_sigma, fwhm_s / 2.354820045)


def gcc_phat(
    reference: np.ndarray,
    target: np.ndarray,
    fs_hz: float,
    max_delay_s: float,
) -> Tuple[float, float, float]:
    """Return target-minus-reference delay, normalized peak, and peak-width sigma."""
    ref = np.asarray(reference, dtype=np.float64)
    tgt = np.asarray(target, dtype=np.float64)
    if ref.ndim != 1 or tgt.ndim != 1 or len(ref) == 0 or len(tgt) == 0:
        raise TransientTdoaError("correlation inputs must be non-empty one-dimensional arrays")
    if not np.isfinite(fs_hz) or fs_hz <= 0.0:
        raise TransientTdoaError("fs_hz must be finite and positive")
    if not np.isfinite(max_delay_s) or max_delay_s <= 0.0:
        raise TransientTdoaError("max_delay_s must be finite and positive")
    if not np.all(np.isfinite(ref)) or not np.all(np.isfinite(tgt)):
        raise TransientTdoaError("correlation input contains non-finite samples")

    ref = ref - float(np.mean(ref))
    tgt = tgt - float(np.mean(tgt))
    ref_norm = float(np.linalg.norm(ref))
    tgt_norm = float(np.linalg.norm(tgt))
    if ref_norm <= 1e-12 or tgt_norm <= 1e-12:
        raise TransientTdoaError("correlation input has no measurable energy")

    n_fft = 1 << (len(ref) + len(tgt) - 2).bit_length()
    cross_spectrum = np.fft.rfft(tgt, n=n_fft) * np.conj(np.fft.rfft(ref, n=n_fft))
    magnitude = np.abs(cross_spectrum)
    cross_spectrum /= np.maximum(magnitude, np.finfo(np.float64).eps)
    correlation = np.fft.irfft(cross_spectrum, n=n_fft)

    max_lag = min(int(round(max_delay_s * fs_hz)), n_fft // 2)
    correlation = np.concatenate((correlation[-max_lag:], correlation[:max_lag + 1]))
    peak_index = int(np.argmax(np.abs(correlation)))
    lag_samples = float(peak_index - max_lag)

    if 0 < peak_index < len(correlation) - 1:
        y0, y1, y2 = np.abs(correlation[peak_index - 1:peak_index + 2])
        denominator = y0 - 2.0 * y1 + y2
        if abs(float(denominator)) > np.finfo(np.float64).eps:
            lag_samples += 0.5 * float(y0 - y2) / float(denominator)

    peak = float(np.abs(correlation[peak_index]))
    return lag_samples / fs_hz, min(1.0, peak), _peak_sigma(correlation, peak_index, fs_hz)


def assert_non_reconstructible(record: Mapping[str, Any]) -> None:
    """Refuse fields or values capable of carrying acoustic or speech content."""
    unknown = set(record) - _RECORD_KEYS
    if unknown:
        raise TransientTdoaError("record contains unknown fields: %s" % sorted(unknown))
    arrivals = record.get("arrivals")
    if arrivals is not None:
        if not isinstance(arrivals, (list, tuple)):
            raise TransientTdoaError("record.arrivals must be a sequence")
        for index, arrival in enumerate(arrivals):
            if not isinstance(arrival, Mapping):
                raise TransientTdoaError("record.arrivals[%d] must be an object" % index)
            extra = set(arrival) - _ARRIVAL_KEYS
            if extra:
                raise TransientTdoaError("record.arrivals[%d] contains unknown fields: %s"
                                         % (index, sorted(extra)))
    position = record.get("position_enu_m")
    if position is not None:
        if not isinstance(position, Mapping) or set(position) != _POSITION_KEYS:
            raise TransientTdoaError("record.position_enu_m has the wrong shape")

    def walk(value: Any, path: str) -> None:
        if isinstance(value, np.ndarray) or isinstance(value, (bytes, bytearray, memoryview)):
            raise TransientTdoaError("%s contains reconstructible data" % path)
        if isinstance(value, Mapping):
            for key, child in value.items():
                name = str(key).lower()
                if name not in _SAFE_KEYS and any(part in name for part in _FORBIDDEN_KEYS):
                    raise TransientTdoaError("%s.%s is forbidden" % (path, key))
                walk(child, "%s.%s" % (path, key))
        elif isinstance(value, (list, tuple)):
            if path != "record.arrivals":
                raise TransientTdoaError("%s contains an unapproved sequence" % path)
            for index, child in enumerate(value):
                walk(child, "%s[%d]" % (path, index))
        elif isinstance(value, float) and not math.isfinite(value):
            raise TransientTdoaError("%s is not finite" % path)

    walk(record, "record")


class TransientTdoaWindow:
    """Own a bounded copy of synchronized node waveforms and consume it exactly once."""

    def __init__(
        self,
        signals: Mapping[Any, np.ndarray],
        fs_hz: float,
        *,
        max_window_s: float = MAX_WINDOW_S,
    ) -> None:
        if len(signals) < 2:
            raise TransientTdoaError("at least two node waveforms are required")
        if not np.isfinite(fs_hz) or fs_hz <= 0.0:
            raise TransientTdoaError("fs_hz must be finite and positive")
        if not 0.0 < max_window_s <= MAX_WINDOW_S:
            raise TransientTdoaError("max_window_s must be in (0, %.1f]" % MAX_WINDOW_S)

        owned: Dict[str, np.ndarray] = {}
        lengths = set()
        try:
            for node_id, signal in signals.items():
                samples = np.array(signal, dtype=np.float32, copy=True)
                if samples.ndim != 1 or samples.size == 0:
                    raise TransientTdoaError(
                        "node %r waveform must be one-dimensional and non-empty" % (node_id,)
                    )
                if not np.all(np.isfinite(samples)):
                    raise TransientTdoaError(
                        "node %r waveform contains non-finite samples" % (node_id,)
                    )
                if samples.size / float(fs_hz) > max_window_s:
                    raise TransientTdoaError("node %r waveform exceeds %.1f second RAM window"
                                             % (node_id, max_window_s))
                key = str(node_id)
                if key in owned:
                    raise TransientTdoaError("node ids are not unique after string normalization")
                owned[key] = samples
                lengths.add(samples.size)
        except Exception:
            for samples in owned.values():
                samples.fill(0.0)
            raise
        if len(lengths) != 1:
            raise TransientTdoaError("all node waveforms must have the same sample count")

        self.fs_hz = float(fs_hz)
        self.duration_s = next(iter(lengths)) / self.fs_hz
        self._signals = owned
        self._consumed = False

    @property
    def erased(self) -> bool:
        return self._consumed and not self._signals

    def erase(self) -> None:
        for samples in self._signals.values():
            samples.fill(0.0)
        self._signals.clear()
        self._consumed = True

    def analyze(
        self,
        vad: Any,
        *,
        reference_node_id: Optional[Any] = None,
        reference_arrival_utc_s: Optional[float] = None,
        max_delay_s: float = 0.05,
        position_enu_m: Optional[Tuple[float, float, float]] = None,
    ) -> Dict[str, Any]:
        """Correlate, score speech, return derived evidence, then irreversibly clear buffers."""
        if self._consumed:
            raise TransientTdoaError("transient window has already been consumed")
        node_ids = sorted(self._signals)
        reference = str(reference_node_id) if reference_node_id is not None else node_ids[0]
        if reference not in self._signals:
            raise TransientTdoaError("reference node is not present in the transient window")
        if reference_arrival_utc_s is not None and not math.isfinite(reference_arrival_utc_s):
            raise TransientTdoaError("reference_arrival_utc_s must be finite")

        try:
            ref = self._signals[reference]
            estimates = []
            for node_id in node_ids:
                if node_id == reference:
                    delay_s = 0.0
                    peak = 1.0
                    sigma_s = 1.0 / (math.sqrt(12.0) * self.fs_hz)
                else:
                    delay_s, peak, sigma_s = gcc_phat(
                        ref, self._signals[node_id], self.fs_hz, max_delay_s
                    )
                arrival = (None if reference_arrival_utc_s is None
                           else float(reference_arrival_utc_s) + delay_s)
                estimates.append(DelayEstimate(node_id, delay_s, peak, sigma_s, arrival))

            speech_detected = False
            vad_failed_closed = False
            for samples in self._signals.values():
                vad_samples = np.array(samples, copy=True)
                try:
                    decision = vad.detect(vad_samples, int(round(self.fs_hz)))
                    speech_detected = speech_detected or bool(decision.speech)
                except Exception:
                    speech_detected = True
                    vad_failed_closed = True
                finally:
                    vad_samples.fill(0.0)

            record: Dict[str, Any] = {
                "schema": SCHEMA,
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                "reference_node_id": reference,
                "sample_rate_hz": self.fs_hz,
                "window_duration_s": self.duration_s,
                "timing_resolution_s": 1.0 / self.fs_hz,
                "arrivals": [estimate.as_record() for estimate in estimates],
                "human_speech_detected": speech_detected,
                "vad_failed_closed": vad_failed_closed,
                "zero_audio_retained": True,
            }
            if position_enu_m is not None:
                if len(position_enu_m) != 3 or not all(math.isfinite(float(v))
                                                       for v in position_enu_m):
                    raise TransientTdoaError("position_enu_m must contain three finite values")
                record["position_enu_m"] = {
                    "east_m": float(position_enu_m[0]),
                    "north_m": float(position_enu_m[1]),
                    "up_m": float(position_enu_m[2]),
                }
            assert_non_reconstructible(record)
            return record
        finally:
            self.erase()
