import math
import os
import sys

import numpy as np
import pytest
from scipy.io import wavfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hear.sim.forward_model import SimNode
from hear.sim.recorded_replay import RecordedReplayHarness


def _pulse(fs_hz=8000.0, duration_s=0.08):
    t = np.arange(int(round(duration_s * fs_hz)), dtype=float) / float(fs_hz)
    return np.exp(-((t - 0.010) / 0.004) ** 2) * np.sin(2.0 * math.pi * 1200.0 * t)


class TestRecordedReplayHarness:
    def test_from_synthetic_builds_arrivals_and_solves(self):
        node_positions = {
            1: (0.0, 0.0, 0.0),
            2: (18.0, 0.0, 0.0),
            3: (0.0, 18.0, 0.0),
            4: (18.0, 18.0, 0.0),
        }
        source = (32.0, 22.0, 0.0)
        harness = RecordedReplayHarness.from_synthetic(
            node_positions,
            source,
            fs_hz=16000.0,
            duration_s=0.2,
            noise_std=0.0,
            waveform=_pulse(16000.0, 0.2),
        )

        bundles = harness.bundle_arrivals()
        assert {bundle["node_id"] for bundle in bundles} == set(node_positions)
        assert all(bundle["sigma_s"] > 0.0 for bundle in bundles)

        solution = harness.solve_bundles(bundles, fixed_up_m=0.0)
        assert solution["position_observable"] is True
        assert solution["east_m"] == pytest.approx(source[0], abs=2.0)
        assert solution["north_m"] == pytest.approx(source[1], abs=2.0)

    def test_from_wav_set_loads_multi_node_recordings(self, tmp_path):
        fs_hz = 8000.0
        source = (26.0, 24.0, 0.0)
        node_positions = [
            SimNode(1, (0.0, 0.0, 0.0), node_class="xiao-s3-pps"),
            SimNode(2, (12.0, 0.0, 0.0), node_class="xiao-s3-pps"),
            SimNode(3, (0.0, 12.0, 0.0), node_class="xiao-s3-pps"),
        ]
        c = 331.3 + 0.606 * 20.0
        pulse = _pulse(fs_hz, 0.05)

        for node in node_positions:
            pos = np.asarray(node.position, dtype=float)
            dist_m = float(np.linalg.norm(np.asarray(source, dtype=float) - pos))
            delay_samples = int(round((dist_m / c) * fs_hz))
            signal = np.zeros(max(len(pulse), delay_samples + len(pulse)), dtype=float)
            signal[delay_samples:delay_samples + len(pulse)] = pulse
            wavfile.write(str(tmp_path / f"node{node.node_id}.wav"), int(fs_hz),
                          (signal * 32767.0).astype(np.int16))

        harness = RecordedReplayHarness.from_wav_set(
            tmp_path,
            nodes=node_positions,
            source_class="blast",
            temp_c=20.0,
        )

        bundles = harness.bundle_arrivals()
        assert {bundle["node_id"] for bundle in bundles} == {1, 2, 3}
        assert all(bundle["node_class"] == "xiao-s3-pps" for bundle in bundles)

        solution = harness.solve_bundles(bundles, fixed_up_m=0.0)
        assert solution["position_observable"] is True
        assert solution["east_m"] == pytest.approx(source[0], abs=5.0)
        assert solution["north_m"] == pytest.approx(source[1], abs=5.0)
