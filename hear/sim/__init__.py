"""Synthetic acoustic simulation helpers and validation metrics."""
from .association_stress import (AssociationStressTest, format_benchmark_summary, simulate_multi_source_burst)
from .forward_model import (ForwardModelResult, SimulationResult, SimNode, arrival_times, propagation_delays, simulate, simulate_forward_model, tdoa)
from .metrics import GateCompliance, TDoAEvaluationMetrics, TDoAReport, position_error, report
from .recorded_replay import RecordedReplayHarness, replay_impulse
from .monte_carlo import MonteCarloSweep
from .placement_optimizer import PlacementOptimizer

__all__ = [
    "AssociationStressTest",
    "simulate_multi_source_burst",
    "format_benchmark_summary",
    "ForwardModelResult",
    "SimulationResult",
    "SimNode",
    "arrival_times",
    "propagation_delays",
    "simulate",
    "simulate_forward_model",
    "tdoa",
    "GateCompliance",
    "TDoAEvaluationMetrics",
    "TDoAReport",
    "position_error",
    "report",
    "RecordedReplayHarness",
    "replay_impulse",
    "MonteCarloSweep",
    "PlacementOptimizer",
    "AcousticImpulseEvent", "CalibrationError", "CalibrationEstimate", "CalibrationRefusal", "LatencyCalibrator",
]

from .latency_calibrator import (AcousticImpulseEvent, CalibrationError, CalibrationEstimate, CalibrationRefusal, LatencyCalibrator)
