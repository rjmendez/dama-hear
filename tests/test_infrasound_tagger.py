#!/usr/bin/env python3
import numpy as np
import pytest

from hear.infrasound import InfrasoundSeismicTagger


def test_infrasound_tagger_init():
    tagger = InfrasoundSeismicTagger()
    assert tagger.name == "infrasound-seismic"
    assert tagger.analysis_rate_hz == 400.0


def test_infrasound_tagger_silence():
    tagger = InfrasoundSeismicTagger()
    res = tagger.tag(np.zeros(100), fs_hz=100.0)
    assert "scores" in res
    assert "feature_metrics" in res


def test_infrasound_tagger_microbarom():
    tagger = InfrasoundSeismicTagger()
    t = np.linspace(0, 20, 2000)
    # 0.2 Hz ocean microbarom wave
    signal = np.sin(2 * np.pi * 0.2 * t)
    res = tagger.tag(signal, fs_hz=100.0)
    assert "scores" in res
    assert res["scores"].get("infrasound.microbarom", 0.0) > 0.3


def test_infrasound_tagger_footstep_thump():
    tagger = InfrasoundSeismicTagger()
    t = np.linspace(0, 5, 2000)
    signal = 0.01 * np.random.randn(len(t))
    # Inject impulsive footstep stomps at 2 Hz
    for i in range(10):
        idx = int(i * 200 + 50)
        signal[idx : idx + 10] += 2.0 * np.exp(-np.linspace(0, 2, 10))
    res = tagger.tag(signal, fs_hz=400.0)
    assert res["scores"].get("infrasound.footstep_thump", 0.0) > 0.3


def test_infrasound_tagger_imu_dict_input():
    tagger = InfrasoundSeismicTagger()
    t = np.linspace(0, 2, 800)
    imu_dict = {
        "imu_z": np.sin(2 * np.pi * 5.0 * t) + 0.1 * np.random.randn(len(t)),
        "fs_hz": 400.0,
    }
    res = tagger.tag(imu_dict)
    assert "feature_metrics" in res
    assert res["feature_metrics"]["source"] == "imu"
