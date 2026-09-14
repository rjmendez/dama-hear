"""Acoustic capture-path latency calibration for synthetic TDoA runs."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence
import numpy as np

class CalibrationError(ValueError):
    """Calibration input is malformed or cannot produce an estimate."""
class CalibrationRefusal(CalibrationError):
    """A node has not met the calibration confidence gate."""
@dataclass(frozen=True)
class AcousticImpulseEvent:
    event_id: str
    node_id: str
    timestamp_s: float
    calibration_position: tuple[float, ...]
    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AcousticImpulseEvent":
        try:
            return cls(str(value["event_id"]), str(value["node_id"]), float(value["timestamp_s"]), tuple(float(v) for v in value["calibration_position"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise CalibrationError("calibration event is malformed") from exc
@dataclass(frozen=True)
class CalibrationEstimate:
    node_id: str
    bias_s: float
    sigma_b_s: float
    residual_rms_s: float
    n_events: int
    @property
    def bias_us(self) -> float: return self.bias_s * 1e6
    @property
    def sigma_b_us(self) -> float: return self.sigma_b_s * 1e6
class LatencyCalibrator:
    """Estimate fixed capture delay from known-position impulses and PPS references."""
    def __init__(self, nodes: Mapping[str, Sequence[float]], reference_nodes: Iterable[str] = ("xiao-s3-pps",), *, reference_bias_s: Optional[Mapping[str, float]] = None, sound_speed_mps: float = 343.0, admissibility_tolerance_s: float = 30e-6) -> None:
        if not nodes: raise CalibrationError("nodes must be non-empty")
        self.nodes = {str(k): np.asarray(v, dtype=float) for k, v in nodes.items()}
        shapes = {v.shape for v in self.nodes.values()}
        if len(shapes) != 1 or next(iter(shapes)) not in ((2,), (3,)): raise CalibrationError("node positions must all be finite 2D or 3D coordinates")
        if any(not np.isfinite(v).all() for v in self.nodes.values()): raise CalibrationError("node positions must be finite")
        self.reference_nodes = tuple(str(v) for v in reference_nodes)
        if not self.reference_nodes or any(v not in self.nodes for v in self.reference_nodes): raise CalibrationError("reference_nodes must identify nodes in nodes")
        self.reference_bias_s = {v: 0.0 for v in self.reference_nodes}
        if reference_bias_s is not None: self.reference_bias_s.update({str(k): float(v) for k, v in reference_bias_s.items()})
        self.sound_speed_mps = float(sound_speed_mps); self.admissibility_tolerance_s = float(admissibility_tolerance_s)
        if not np.isfinite(self.sound_speed_mps) or self.sound_speed_mps <= 0: raise CalibrationError("sound speed must be finite and positive")
        if not np.isfinite(self.admissibility_tolerance_s) or self.admissibility_tolerance_s <= 0: raise CalibrationError("admissibility tolerance must be finite and positive")
        self.events: list[AcousticImpulseEvent] = []; self.estimates: Dict[str, CalibrationEstimate] = {}
    def ingest(self, events: Iterable[AcousticImpulseEvent | Mapping[str, Any]]) -> None:
        dimension = next(iter(self.nodes.values())).size
        for event in events:
            event = event if isinstance(event, AcousticImpulseEvent) else AcousticImpulseEvent.from_mapping(event)
            if event.node_id not in self.nodes: raise CalibrationError("event names unknown node %r" % event.node_id)
            if len(event.calibration_position) != dimension or not np.isfinite(event.calibration_position).all(): raise CalibrationError("calibration position is invalid")
            if not np.isfinite(event.timestamp_s): raise CalibrationError("event timestamp must be finite")
            self.events.append(event)
    ingest_reference_events = ingest
    def simulate_impulses(self, calibration_positions: Sequence[Sequence[float]], *, emission_times_s: Optional[Sequence[float]] = None, capture_biases_s: Optional[Mapping[str, float]] = None, timing_noise_s: float = 0.0, seed: int = 0, ingest: bool = True) -> list[AcousticImpulseEvent]:
        dimensions = next(iter(self.nodes.values())).shape; positions = [np.asarray(v, dtype=float) for v in calibration_positions]
        if not positions or any(v.shape != dimensions or not np.isfinite(v).all() for v in positions): raise CalibrationError("calibration positions must match node dimensions and be finite")
        if emission_times_s is None: emission_times_s = [0.0] * len(positions)
        if len(emission_times_s) != len(positions): raise CalibrationError("emission_times_s must match calibration_positions")
        noise = float(timing_noise_s)
        if not np.isfinite(noise) or noise < 0: raise CalibrationError("timing noise must be finite and non-negative")
        biases = {k: 0.0 for k in self.nodes}
        if capture_biases_s is not None: biases.update({str(k): float(v) for k, v in capture_biases_s.items()})
        if any(not np.isfinite(v) for v in biases.values()): raise CalibrationError("capture biases must be finite")
        rng = np.random.default_rng(seed); rendered = []
        for index, (position, emission_time) in enumerate(zip(positions, emission_times_s)):
            emission_time = float(emission_time)
            if not np.isfinite(emission_time): raise CalibrationError("emission times must be finite")
            for node_id, node_position in self.nodes.items():
                timestamp = emission_time + np.linalg.norm(node_position - position) / self.sound_speed_mps + biases[node_id] + (float(rng.normal(0.0, noise)) if noise else 0.0)
                rendered.append(AcousticImpulseEvent("cal-%d" % index, node_id, timestamp, tuple(position)))
        if ingest: self.ingest(rendered)
        return rendered
    simulate = simulate_impulses
    def _observations(self, node_id: str) -> np.ndarray:
        grouped: Dict[str, list[AcousticImpulseEvent]] = {}
        for event in self.events: grouped.setdefault(event.event_id, []).append(event)
        observations = []
        for members in grouped.values():
            candidates = [e for e in members if e.node_id == node_id]; references = [e for e in members if e.node_id in self.reference_nodes]
            for candidate in candidates:
                position = np.asarray(candidate.calibration_position)
                for reference in references:
                    if not np.allclose(position, reference.calibration_position): raise CalibrationError("nodes in one calibration event disagree on position")
                    d_i = np.linalg.norm(self.nodes[node_id] - position); d_ref = np.linalg.norm(self.nodes[reference.node_id] - position)
                    observations.append(candidate.timestamp_s - (reference.timestamp_s - self.reference_bias_s.get(reference.node_id, 0.0)) - (d_i - d_ref) / self.sound_speed_mps)
        return np.asarray(observations, dtype=float)
    def calibrate(self, node_id: str) -> CalibrationEstimate:
        node_id = str(node_id)
        if node_id not in self.nodes: raise CalibrationError("unknown node %r" % node_id)
        if node_id in self.reference_nodes:
            estimate = CalibrationEstimate(node_id, self.reference_bias_s[node_id], 0.0, 0.0, 0); self.estimates[node_id] = estimate; return estimate
        observations = self._observations(node_id)
        if observations.size == 0: raise CalibrationError("no reference impulse pairs for node %r" % node_id)
        bias = float(observations.mean()); residuals = observations - bias
        sigma = float(np.std(observations, ddof=1) / np.sqrt(observations.size)) if observations.size > 1 else 0.0
        estimate = CalibrationEstimate(node_id, bias, sigma, float(np.sqrt(np.mean(residuals * residuals))), len(observations)); self.estimates[node_id] = estimate; return estimate
    def calibrate_all(self) -> Dict[str, CalibrationEstimate]:
        for node_id in self.nodes:
            if node_id not in self.reference_nodes: self.calibrate(node_id)
        return dict(self.estimates)
    def is_admissible(self, node_id: str) -> bool:
        estimate = self.estimates.get(str(node_id)); return estimate is not None and estimate.sigma_b_s <= self.admissibility_tolerance_s
    def status(self, node_id: str) -> str: return "admissible" if self.is_admissible(node_id) else "refused"
    def require_arrival(self, node_id: str) -> CalibrationEstimate:
        node_id = str(node_id)
        if not self.is_admissible(node_id):
            estimate = self.estimates.get(node_id); sigma_us = estimate.sigma_b_us if estimate is not None else float("inf")
            raise CalibrationRefusal("node %r capture path is uncalibrated or confidence %.1f us exceeds %.1f us" % (node_id, sigma_us, self.admissibility_tolerance_s * 1e6))
        return self.estimates[node_id]
