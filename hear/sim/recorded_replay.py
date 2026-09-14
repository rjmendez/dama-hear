"""Recorded-sample replay harness for TDoA simulations.

This module lets the harness play back a real multi-node or multi-channel recording, detect
onsets in each stream, bundle those arrivals, and feed them through the project's TDoA solver.
The implementation keeps the interface compact while still covering the main deployment modes:

- a directory of WAV/PCM files, one per node
- a single multi-channel WAV file split by channel
- synthetic multi-node recordings generated from a known source position
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np
from scipy.io import wavfile
from scipy.signal import correlate, correlation_lags

from hear.backend import associate as backend_associate
from hear.node import detect as detect_mod
from hear.solve import point as point_solve
from hear.sim.forward_model import SimNode


def _as_vector(value, *, name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=float)
    if arr.shape not in ((2,), (3,)):
        raise ValueError(f"{name} must be a 2D or 3D vector, got {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} must contain only finite values")
    return arr.astype(float)


def _node_position(node: SimNode) -> np.ndarray:
    pos = np.asarray(node.position, dtype=float)
    if pos.shape not in ((2,), (3,)):
        raise ValueError(f"node {node.node_id!r} position must be 2D or 3D")
    return pos.astype(float)


def _sound_speed_mps(temp_c: float = 20.0) -> float:
    return 331.3 + 0.606 * float(temp_c)


def _read_wav_or_pcm(path: str | Path) -> tuple[float, np.ndarray]:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in {".wav", ".wave"}:
        rate, data = wavfile.read(str(p))
        arr = np.asarray(data, dtype=float)
        if arr.ndim == 1:
            return float(rate), arr
        return float(rate), arr
    if suffix == ".pcm":
        raw = np.fromfile(str(p), dtype=np.int16)
        return 48000.0, raw.astype(float)
    raise ValueError(f"unsupported audio format for {p!s}; expected WAV or PCM")


class RecordedReplayHarness:
    """Replay recorded or synthetic audio against a known node layout."""

    def __init__(self, nodes: Sequence[SimNode], audio_by_node: Mapping[object, np.ndarray],
                 sample_rate_hz: float = 48000.0, source_class: str = "blast",
                 temp_c: float = 20.0):
        self.nodes = list(nodes)
        self.audio_by_node = {k: np.asarray(v, dtype=float).reshape(-1) for k, v in audio_by_node.items()}
        self.sample_rate_hz = float(sample_rate_hz)
        self.source_class = source_class
        self.temp_c = float(temp_c)
        self._node_by_id = {int(node.node_id): node for node in self.nodes}
        if not self.nodes:
            raise ValueError("at least one node is required")
        for node in self.nodes:
            node_id = int(node.node_id)
            if node_id not in self.audio_by_node:
                raise ValueError(f"missing audio for node {node_id}")
            if not np.all(np.isfinite(self.audio_by_node[node_id])):
                raise ValueError(f"audio for node {node_id} contains non-finite samples")

    @classmethod
    def from_wav_set(cls, wav_source, nodes: Sequence[SimNode] | None = None, *,
                     source_class: str = "blast", temp_c: float = 20.0,
                     sample_rate_hz: float | None = None):
        """Load one recording per node, or split a multi-channel WAV into node streams."""
        if isinstance(wav_source, (str, Path)):
            p = Path(wav_source)
            if p.is_dir():
                files = sorted(p.glob("*.wav")) + sorted(p.glob("*.pcm")) + sorted(p.glob("*.wave"))
                if not files:
                    raise FileNotFoundError(f"no WAV/PCM recordings found in {p}")
                items = files
            else:
                items = [p]
        else:
            items = list(wav_source)

        if len(items) == 1:
            rate, data = _read_wav_or_pcm(items[0])
            if data.ndim == 2 and data.shape[1] > 1:
                channels = data.shape[1]
                if nodes is not None:
                    if len(nodes) != channels:
                        raise ValueError(f"multi-channel WAV contains {channels} channels but {len(nodes)} nodes were supplied")
                    node_list = list(nodes)
                else:
                    node_list = [SimNode(idx + 1, (float(idx) * 10.0, 0.0, 0.0), node_class="xiao-s3-pps")
                                 for idx in range(channels)]
                audio_by_node = {}
                for ch_idx in range(channels):
                    audio_by_node[int(node_list[ch_idx].node_id)] = np.asarray(data[:, ch_idx], dtype=float)
                return cls(node_list, audio_by_node, sample_rate_hz=float(rate),
                           source_class=source_class, temp_c=temp_c)

        if nodes is not None:
            node_list = list(nodes)
            if len(node_list) != len(items):
                raise ValueError("node count must match the number of input recordings")
        else:
            node_list = [SimNode(idx + 1, (float(idx) * 10.0, 0.0, 0.0), node_class="xiao-s3-pps")
                         for idx in range(len(items))]

        audio_by_node: dict[int, np.ndarray] = {}
        for idx, item in enumerate(items):
            rate, data = _read_wav_or_pcm(item)
            if sample_rate_hz is None:
                sample_rate_hz = rate
            elif abs(rate - sample_rate_hz) > 1e-6:
                raise ValueError(f"recordings disagree on sample rate: {rate} vs {sample_rate_hz}")
            if data.ndim == 2:
                channels = data.shape[1]
                if channels == 1:
                    audio_by_node[int(node_list[idx].node_id)] = data[:, 0].astype(float)
                else:
                    for ch_idx in range(channels):
                        channel_id = int(node_list[idx].node_id) * 100 + ch_idx
                        audio_by_node[channel_id] = data[:, ch_idx].astype(float)
            else:
                audio_by_node[int(node_list[idx].node_id)] = np.asarray(data, dtype=float)

        if nodes is None:
            node_list = [SimNode(node_id, (float(node_id), 0.0, 0.0), node_class="xiao-s3-pps")
                         for node_id in sorted(audio_by_node)]
        else:
            node_list = list(nodes)

        return cls(node_list, audio_by_node, sample_rate_hz=float(sample_rate_hz or 48000.0),
                   source_class=source_class, temp_c=temp_c)

    @classmethod
    def from_synthetic(cls, node_positions, source_position, *, fs_hz: float = 48000.0,
                       source_class: str = "blast", temp_c: float = 20.0,
                       duration_s: float = 0.25, noise_std: float = 0.01,
                       waveform: np.ndarray | None = None, rng_seed: int | None = None):
        """Construct a synthetic multi-node recording set from a known source geometry."""
        positions = {int(k): _as_vector(v, name=f"node_position[{k}]") for k, v in dict(node_positions).items()}
        source = _as_vector(source_position, name="source_position")
        rng = np.random.default_rng(rng_seed)
        if waveform is None:
            n = int(round(duration_s * fs_hz))
            t = np.arange(n, dtype=float) / float(fs_hz)
            pulse = np.exp(-((t - 0.01) / 0.004) ** 2) * np.sin(2.0 * math.pi * 1200.0 * t)
        else:
            pulse = np.asarray(waveform, dtype=float).reshape(-1)
            if pulse.size == 0:
                raise ValueError("waveform must not be empty")

        c = _sound_speed_mps(temp_c)
        audio_by_node: dict[int, np.ndarray] = {}
        for node_id, pos in positions.items():
            distance_m = float(np.linalg.norm(source - pos))
            n_delay = int(round((distance_m / c) * fs_hz))
            buf = np.zeros(max(len(pulse), n_delay + len(pulse)), dtype=float)
            start = max(0, n_delay)
            buf[start:start + len(pulse)] = pulse
            if noise_std > 0:
                buf += rng.normal(0.0, float(noise_std), size=buf.shape)
            audio_by_node[int(node_id)] = buf

        nodes = [SimNode(node_id, tuple(map(float, pos)), node_class="xiao-s3-pps")
                 for node_id, pos in sorted(positions.items())]
        return cls(nodes, audio_by_node, sample_rate_hz=float(fs_hz), source_class=source_class,
                   temp_c=temp_c)

    def _node_lookup(self) -> Mapping[int, SimNode]:
        return {int(node.node_id): node for node in self.nodes}

    def _onset_for_node(self, node_id: int) -> float:
        x = self.audio_by_node[int(node_id)]
        env = detect_mod.envelope(x, self.sample_rate_hz)
        if env.size == 0:
            raise ValueError(f"audio for node {node_id} is empty")
        peak_idx = int(np.argmax(env))
        back = max(1, int(0.03 * self.sample_rate_hz))
        onset_idx = float(detect_mod.onset_index(env, peak_idx, back=back))
        if not np.isfinite(onset_idx):
            onset_idx = float(peak_idx)
        return onset_idx / self.sample_rate_hz

    def _gcc_phat_for_node(self, node_id: int, reference_node_id: int) -> float:
        x = self.audio_by_node[int(node_id)]
        ref = self.audio_by_node[int(reference_node_id)]
        corr = correlate(x, ref, mode="full")
        lags = correlation_lags(len(x), len(ref), mode="full")
        lag_samples = int(lags[int(np.argmax(np.abs(corr)))])
        return float(lag_samples) / self.sample_rate_hz

    def detect_arrivals(self, method: str = "onset", reference_node_id: int | None = None):
        """Return a mapping node_id -> arrival time in seconds relative to record start."""
        if method not in {"onset", "gcc_phat"}:
            raise ValueError(f"unsupported detection method {method!r}")
        chosen_ref = int(reference_node_id) if reference_node_id is not None else min(
            self._node_lookup(), key=lambda n: float(np.linalg.norm(_node_position(self._node_lookup()[n])))
        )
        arrivals = {}
        for node_id in sorted(self._node_lookup()):
            if method == "onset":
                arrivals[node_id] = self._onset_for_node(node_id)
            else:
                arrivals[node_id] = self._gcc_phat_for_node(node_id, chosen_ref)
        return arrivals

    def bundle_arrivals(self, method: str = "onset", reference_node_id: int | None = None):
        """Bundle arrival times as candidate arrivals for the multilateration pipeline."""
        arrivals = self.detect_arrivals(method=method, reference_node_id=reference_node_id)
        bundles = []
        for node_id in sorted(arrivals):
            node = self._node_lookup()[node_id]
            bundles.append({
                "node_id": int(node_id),
                "t_arrival_s": float(arrivals[node_id]),
                "sigma_s": 1.0 / self.sample_rate_hz,
                "node_class": node.node_class,
            })
        return bundles

    def solve_bundles(self, bundles=None, *, fixed_up_m: float = 0.0):
        if bundles is None:
            bundles = self.bundle_arrivals()
        positions = []
        arrivals = []
        for bundle in bundles:
            node_id = int(bundle["node_id"])
            positions.append(_node_position(self._node_lookup()[node_id]))
            arrivals.append(float(bundle["t_arrival_s"]))
        return point_solve.solve(positions, arrivals, self.source_class,
                                temp_c=self.temp_c, fixed_up_m=fixed_up_m)

    def associate_bundles(self, bundles=None, *, temp_c: float | None = None):
        if bundles is None:
            bundles = self.bundle_arrivals()
        if temp_c is None:
            temp_c = self.temp_c
        detections = []
        for bundle in bundles:
            node_id = int(bundle["node_id"])
            detections.append({
                "node_id": node_id,
                "seq": 1,
                "t_utc_s": float(bundle["t_arrival_s"]),
                "node_class": bundle.get("node_class", self._node_lookup()[node_id].node_class),
                "onset_found": True,
                "utc_trusted": True,
                "tdoa_capable": True,
                "timestamp_domain": "utc_gps_pps",
            })
        survey = type("SimpleSurvey", (), {
            "position": lambda self, nid: _node_position(self._node_lookup()[int(nid)]),
            "__contains__": lambda self, nid: int(nid) in self._node_lookup(),
            "diameter_m": lambda self: float(np.linalg.norm(
                np.asarray([_node_position(n) for n in self._node_lookup().values()]).max(axis=0)
                - np.asarray([_node_position(n) for n in self._node_lookup().values()]).min(axis=0)
            ))
        })()
        survey._node_lookup = self._node_lookup
        return backend_associate.associate(detections, survey, temp_c=temp_c, min_nodes=3, margin_s=0.03)


__all__ = ["RecordedReplayHarness"]


def replay_impulse(path, nodes, source_pos, c=343.0, snr_db=None):
    """Replay a single recorded PCM/WAV impulse using its peak as emission time."""
    rate, data = _read_wav_or_pcm(path)
    signal = np.asarray(data, dtype=float)
    if signal.ndim == 2: signal = signal.mean(axis=1)
    if signal.size == 0 or not np.any(np.abs(signal)): raise ValueError("WAV contains no impulse energy")
    index = int(np.argmax(np.abs(signal)))
    delays = np.asarray([np.linalg.norm(np.asarray(n.position, float) - np.asarray(source_pos, float)) / float(c) for n in nodes])
    arrivals = index / rate + delays
    return arrivals, {"sample": str(path), "sample_rate_hz": rate, "impulse_index": index, "impulse_time_s": index / rate, "snr_db": snr_db}
