"""Forward model for point-source TDoA simulations."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence
import numpy as np
from scipy.signal import fftconvolve, resample_poly

@dataclass(frozen=True, init=False)
class SimNode:
    position: Sequence[float]
    node_class: object
    node_id: str
    def __init__(self, *args, node_class="xiao-s3-pps", node_id=""):
        if len(args) in (2, 3) and not np.isscalar(args[0]):
            position, cls = args[:2]
            object.__setattr__(self, "position", position); object.__setattr__(self, "node_class", cls); object.__setattr__(self, "node_id", node_id if len(args) == 2 else str(args[2]))
        elif len(args) >= 2 and np.isscalar(args[0]):
            object.__setattr__(self, "node_id", str(args[0])); object.__setattr__(self, "position", args[1]); object.__setattr__(self, "node_class", node_class if len(args) < 3 else args[2])
        elif len(args) == 1:
            object.__setattr__(self, "position", args[0]); object.__setattr__(self, "node_class", node_class); object.__setattr__(self, "node_id", node_id)
        else:
            raise TypeError("SimNode requires position and node class")

@dataclass(frozen=True)
class ForwardModelResult:
    distances_m: np.ndarray
    propagation_delays_s: np.ndarray
    arrival_times_s: np.ndarray
    audio: tuple[np.ndarray, ...]
    sample_rates_hz: tuple[float, ...]
SimulationResult = ForwardModelResult

def _positions(nodes):
    values = [n.position if isinstance(n, SimNode) else n for n in nodes]
    result = np.asarray(values, dtype=float)
    if result.ndim != 2 or result.shape[0] == 0 or result.shape[1] not in (2, 3):
        raise ValueError("nodes must be a non-empty 2D or 3D array")
    return result

def propagation_delays(nodes, source_pos, c=343.0):
    positions = _positions(nodes); source = np.asarray(source_pos, dtype=float)
    if source.shape != (positions.shape[1],): raise ValueError("source and node positions must have matching dimensions")
    if not np.isfinite(c) or c <= 0: raise ValueError("sound speed must be finite and positive")
    return np.linalg.norm(positions - source, axis=1) / float(c)

def _class(value): return value.node_class if isinstance(value, SimNode) else value

def simulate_forward_model(source_pos, node_positions, signal, *, node_classes=None, t0_s=0.0,
                           sound_speed_mps=343.0, c=None, add_clock_noise=True, rng=None,
                           capture_path_bias_s=None, source_sample_rate_hz=None):
    positions = _positions(node_positions)
    source = np.asarray(source_pos, dtype=float)
    if source.shape != (positions.shape[1],): raise ValueError("source and node positions must have matching dimensions")
    if c is not None: sound_speed_mps = c
    classes = list(node_classes) if node_classes is not None else [_class(n) for n in node_positions]
    if len(classes) != len(positions): raise ValueError("node_classes must match nodes")
    classes = [_class(item) for item in classes]
    rates = tuple(float(getattr(item, "fs_hz", 16000.0)) for item in classes)
    source_rate = rates[0] if source_sample_rate_hz is None else float(source_sample_rate_hz)
    if not np.isfinite(source_rate) or source_rate <= 0: raise ValueError("source sample rate must be finite and positive")
    delays = np.linalg.norm(positions-source, axis=1)/float(sound_speed_mps)
    arrivals = float(t0_s)+delays
    if capture_path_bias_s is not None: arrivals += np.asarray(capture_path_bias_s, float)
    if add_clock_noise:
        generator = rng if rng is not None else np.random.default_rng(0)
        sigmas = np.asarray([float(getattr(item, "t_sigma_s", 0.0)) for item in classes])
        arrivals += generator.normal(0.0, sigmas)
    source_signal=np.asarray(signal,float)
    if source_signal.ndim == 1: source_signal = source_signal[None, :]
    if source_signal.ndim != 2 or source_signal.shape[1] == 0 or not np.isfinite(source_signal).all(): raise ValueError("signal must be finite and non-empty")
    audio=[]
    for delay, rate, distance in zip(delays, rates, np.linalg.norm(positions-source,axis=1)):
        rendered = source_signal if rate == source_rate else np.stack([resample_poly(ch, rate, source_rate) for ch in source_signal])
        taps=33; integer=int(np.ceil(max(0.0, delay*rate))); frac=max(0.0, delay*rate)-integer
        kernel=np.sinc(np.arange(taps)-(taps-1)/2-frac)*np.hanning(taps); kernel/=kernel.sum()
        delayed=[]
        for channel in rendered:
            filtered=fftconvolve(channel, kernel, mode="full")
            filtered=filtered[(taps-1)//2:(taps-1)//2+channel.size]
            delayed.append(np.concatenate((np.zeros(integer), filtered)))
        audio.append(np.asarray(delayed)/max(float(distance),1.0))
    return ForwardModelResult(np.linalg.norm(positions-source,axis=1), delays, arrivals, tuple(audio), rates)

def simulate(source_pos, nodes, signal, **kwargs):
    return simulate_forward_model(source_pos, nodes, signal, **kwargs)

def arrival_times(nodes, source_pos, c=343.0, emission_time_s=0.0, timing_noise_s=0.0, seed=0):
    delays=propagation_delays(nodes, source_pos, c); noise=float(timing_noise_s)
    if not np.isfinite(noise) or noise<0: raise ValueError("timing noise must be finite and non-negative")
    if noise: delays += np.random.default_rng(seed).normal(0.0, noise, len(delays))
    return float(emission_time_s)+delays

def tdoa(arrivals):
    values=np.asarray(arrivals,float)
    if values.ndim!=1 or len(values)<2 or not np.isfinite(values).all(): raise ValueError("arrivals must be a finite one-dimensional sequence of at least two values")
    return values-values.mean()
