"""Multi-microphone orthopteran-silencing perimeter tripwire."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from modules.bioacoustic.detect import CICADA_BAND_HZ, band_limit


@dataclass(frozen=True)
class SeismicImpact:
    t_s: float
    station: Optional[str] = None
    amplitude: Optional[float] = None


@dataclass
class SilencingEvent:
    onset_time_s: float
    recovery_time_s: Optional[float]
    tau_rec_s: Optional[float]
    drop_db: float
    microphone_onsets_s: Dict[str, float]
    spatial_silencing_perimeter_m: float
    propagation_velocity_mps: Optional[float]
    seismic_correlated: bool
    seismic_impact_times_s: Tuple[float, ...] = ()
    confidence: float = 0.0

    @property
    def onset_s(self) -> float:
        return self.onset_time_s

    def to_dict(self) -> Dict[str, Any]:
        return {
            "onset_time_s": self.onset_time_s,
            "recovery_time_s": self.recovery_time_s,
            "tau_rec_s": self.tau_rec_s,
            "drop_db": self.drop_db,
            "microphone_onsets_s": dict(self.microphone_onsets_s),
            "spatial_silencing_perimeter_m": self.spatial_silencing_perimeter_m,
            "propagation_velocity_mps": self.propagation_velocity_mps,
            "seismic_correlated": self.seismic_correlated,
            "seismic_impact_times_s": list(self.seismic_impact_times_s),
            "confidence": self.confidence,
        }


@dataclass
class _MicState:
    baseline_db: Optional[float] = None
    last_db: Optional[float] = None
    last_t_s: Optional[float] = None
    onset_t_s: Optional[float] = None
    trough_db: Optional[float] = None
    recovery_start_t_s: Optional[float] = None
    recovered: bool = False


def _db_from_snapshot(snapshot: Any, freqs_hz: Optional[Sequence[float]], band: Tuple[float, float]) -> float:
    """Reduce a PSD snapshot to dB, accepting scalars and common PSD mappings."""
    if np.isscalar(snapshot):
        value = float(snapshot)
        if not np.isfinite(value):
            raise ValueError("PSD value must be finite")
        return value
    if isinstance(snapshot, Mapping):
        if "psd_db" in snapshot:
            return _db_from_snapshot(snapshot["psd_db"], None, band)
        freqs_hz = snapshot.get("frequencies_hz", snapshot.get("freqs_hz", freqs_hz))
        snapshot = snapshot.get("power", snapshot.get("psd", snapshot.get("values")))
    if isinstance(snapshot, (tuple, list)) and len(snapshot) == 2 and freqs_hz is None:
        freqs_hz, snapshot = snapshot
    power = np.asarray(snapshot, dtype=float).reshape(-1)
    if freqs_hz is None:
        raise ValueError("frequency bins are required with a PSD array")
    freqs = np.asarray(freqs_hz, dtype=float).reshape(-1)
    if freqs.size != power.size or not power.size:
        raise ValueError("frequency and PSD arrays must have the same non-zero length")
    mask = (freqs >= band[0]) & (freqs <= band[1])
    if not np.any(mask):
        raise ValueError("PSD has no bins in the configured insect band")
    return float(10.0 * np.log10(np.sum(np.maximum(power[mask], 0.0)) + 1e-20))


class BioacousticPerimeterTripwire:
    """Detect rapid orthopteran chorus silencing across a microphone perimeter."""

    def __init__(self, fs_hz: float = 16000.0, *, band_hz: Tuple[float, float] = CICADA_BAND_HZ,
                 positions_m: Optional[Mapping[str, Sequence[float]]] = None,
                 drop_threshold_db: float = 9.0, onset_window_s: float = 0.1,
                 tracker_tau_s: float = 30.0, recovery_fraction: float = 1.0 - math.exp(-1.0),
                 seismic_window_s: float = 0.15, min_silenced_mics: int = 2) -> None:
        if drop_threshold_db < 6.0 or drop_threshold_db > 12.0:
            raise ValueError("drop_threshold_db must be between 6 and 12 dB")
        if onset_window_s <= 0 or tracker_tau_s <= 0 or seismic_window_s < 0:
            raise ValueError("time constants and windows must be positive")
        if not 0.0 < recovery_fraction < 1.0:
            raise ValueError("recovery_fraction must be between 0 and 1")
        self.fs_hz = float(fs_hz)
        self.limit = band_limit(band_hz[0], band_hz[1], self.fs_hz)
        if not self.limit["reachable"]:
            raise ValueError("band unreachable: " + self.limit["note"])
        self.band_hz = (self.limit["f_lo"], self.limit["f_hi_eff"])
        self.drop_threshold_db = float(drop_threshold_db)
        self.onset_window_s = float(onset_window_s)
        self.tracker_tau_s = float(tracker_tau_s)
        self.recovery_fraction = float(recovery_fraction)
        self.seismic_window_s = float(seismic_window_s)
        self.min_silenced_mics = max(1, int(min_silenced_mics))
        self.positions_m = {k: tuple(float(v) for v in p) for k, p in (positions_m or {}).items()}
        self._states: Dict[str, _MicState] = {}
        self._active: Dict[str, float] = {}
        self._seismic: List[SeismicImpact] = []
        self._last_t_s: Optional[float] = None

    @property
    def baselines_db(self) -> Dict[str, Optional[float]]:
        return {mic: state.baseline_db for mic, state in self._states.items()}

    def _update_state(self, mic: str, t_s: float, value_db: float) -> None:
        state = self._states.setdefault(mic, _MicState())
        if state.baseline_db is None:
            state.baseline_db = value_db
        drop = state.baseline_db - value_db
        dt = None if state.last_t_s is None else max(0.0, t_s - state.last_t_s)
        sudden = dt is not None and dt <= self.onset_window_s
        if mic not in self._active and sudden and drop >= self.drop_threshold_db:
            state.onset_t_s = t_s
            state.trough_db = value_db
            state.recovery_start_t_s = None
            state.recovered = False
            self._active[mic] = t_s
        elif mic in self._active:
            state.trough_db = min(state.trough_db if state.trough_db is not None else value_db, value_db)
            if drop <= self.drop_threshold_db * 0.5:
                if state.recovery_start_t_s is None:
                    state.recovery_start_t_s = t_s
                if state.trough_db is not None and state.baseline_db is not None:
                    recovered_fraction = ((value_db - state.trough_db) /
                                          max(1e-9, state.baseline_db - state.trough_db))
                    state.recovered = recovered_fraction >= self.recovery_fraction
        if mic not in self._active:
            dt = 0.0 if state.last_t_s is None else max(0.0, t_s - state.last_t_s)
            alpha = 1.0 - math.exp(-dt / self.tracker_tau_s) if dt else 1.0
            state.baseline_db += alpha * (value_db - state.baseline_db)
        state.last_db, state.last_t_s = value_db, t_s

    def process_frame(self, timestamp_s: float, spectra: Mapping[str, Any], *,
                      freqs_hz: Optional[Sequence[float]] = None,
                      seismic_impacts: Iterable[Any] = ()) -> List[SilencingEvent]:
        """Consume one synchronous perimeter frame and return newly completed events."""
        t_s = float(timestamp_s)
        if self._last_t_s is not None and t_s < self._last_t_s:
            raise ValueError("timestamps must be monotonic")
        for impact in seismic_impacts:
            self.add_seismic_impact(impact)
        for mic, snapshot in spectra.items():
            self._update_state(str(mic), t_s, _db_from_snapshot(snapshot, freqs_hz, self.band_hz))
        self._last_t_s = t_s
        return self._complete_ready(t_s)

    process = process_frame

    def add_seismic_impact(self, impact: Any) -> None:
        if isinstance(impact, SeismicImpact):
            item = impact
        elif isinstance(impact, Mapping):
            item = SeismicImpact(float(impact["t_s"] if "t_s" in impact else impact["timestamp_s"]),
                                 impact.get("station"), impact.get("amplitude"))
        else:
            item = SeismicImpact(float(impact))
        self._seismic.append(item)
        self._seismic.sort(key=lambda x: x.t_s)

    def _complete_ready(self, t_s: float) -> List[SilencingEvent]:
        if not self._active:
            return []
        first = min(self._active.values())
        group = {mic: onset for mic, onset in self._active.items()
                 if onset - first <= self.onset_window_s}
        if len(group) < self.min_silenced_mics or not all(self._states[mic].recovered for mic in group):
            return []
        return [self._finish(first, t_s, group)]

    def _finish(self, onset_s: float, recovery_s: float,
                active: Optional[Mapping[str, float]] = None) -> SilencingEvent:
        onsets = dict(self._active if active is None else active)
        states = [self._states[mic] for mic in onsets]
        drops = [float((s.baseline_db or 0.0) - (s.trough_db or 0.0)) for s in states]
        points = [self.positions_m[mic] for mic in onsets if mic in self.positions_m]
        perimeter = 0.0
        for i, left in enumerate(points):
            for right in points[i + 1:]:
                perimeter = max(perimeter, float(np.linalg.norm(np.asarray(left) - right)))
        ordered = sorted(onsets.values())
        velocity = perimeter / (ordered[-1] - ordered[0]) if perimeter and ordered[-1] > ordered[0] else None
        impacts = tuple(i.t_s for i in self._seismic
                        if abs(i.t_s - onset_s) <= self.seismic_window_s)
        tau_values = []
        for state in states:
            if state.recovery_start_t_s is not None:
                tau_values.append((state.recovery_start_t_s - (state.onset_t_s or onset_s)) /
                                  max(1e-9, -math.log(max(1e-9, 1.0 - self.recovery_fraction))))
        event = SilencingEvent(
            onset_s, recovery_s, float(np.median(tau_values)) if tau_values else None,
            float(np.median(drops)), onsets, perimeter, velocity, bool(impacts), impacts,
            confidence=min(1.0, len(onsets) / max(1, self.min_silenced_mics)) *
            (1.0 if impacts else 0.75))
        self._active.clear()
        return event

    def flush(self, timestamp_s: Optional[float] = None) -> List[SilencingEvent]:
        """Close a recovered event at end-of-stream; unrecovered silence is not claimed."""
        if not self._active or len(self._active) < self.min_silenced_mics:
            return []
        first = min(self._active.values())
        group = {mic: onset for mic, onset in self._active.items()
                 if onset - first <= self.onset_window_s}
        if len(group) < self.min_silenced_mics or not all(self._states[mic].recovered for mic in group):
            return []
        t_s = self._last_t_s if timestamp_s is None else float(timestamp_s)
        if t_s is None:
            return []
        return [self._finish(first, t_s, group)]


__all__ = ["BioacousticPerimeterTripwire", "SeismicImpact", "SilencingEvent"]
