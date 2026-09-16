"""hear/privacy/silero_vad.py: the speech gate answers the same way twice, and refuses guesses.

Hermetic by construction. The Silero `.onnx` binary is not in this repository, so every test
here runs against the deterministic fallback engine unless a real model is pointed at by
`HEAR_SILERO_VAD_MODEL`; the onnxruntime plumbing is exercised separately against a tiny graph
built at test time, which is skipped where onnx/onnxruntime are not installed (CI pins neither).

No recorded audio and no coordinates: every waveform below is synthesised from its own formula.
"""
from __future__ import annotations

import math
import pathlib
import sys

import numpy as np
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hear.privacy import silero_vad as V                                      # noqa: E402

FS = V.MODEL_RATE_HZ
SEED = 20260915


def _rng() -> np.random.Generator:
    return np.random.default_rng(SEED)


def voice(seconds: float = 1.0, f0: float = 130.0, fs: int = FS) -> np.ndarray:
    """A voiced vowel: harmonic stack under three formants, with a 4 Hz syllabic envelope."""
    t = np.arange(int(seconds * fs)) / fs
    signal = np.zeros_like(t)
    for harmonic, amplitude in enumerate([1.0, 0.7, 0.55, 0.4, 0.3, 0.22, 0.15, 0.1], start=1):
        freq = f0 * harmonic
        shape = 1.0
        for centre, bandwidth, gain in ((700.0, 130.0, 1.6), (1220.0, 180.0, 1.2),
                                        (2600.0, 250.0, 0.9)):
            shape += gain / (1.0 + ((freq - centre) / bandwidth) ** 2)
        signal += amplitude * shape * np.sin(2 * np.pi * freq * t + harmonic * 0.3)
    signal *= 0.55 + 0.45 * np.sin(2 * np.pi * 4.0 * t)
    signal += 0.01 * _rng().standard_normal(t.size) * np.max(np.abs(signal))
    return (0.3 * signal / np.max(np.abs(signal))).astype(np.float32)


def hiss(seconds: float = 1.0, fs: int = FS) -> np.ndarray:
    """Wind and self-noise: broadband, unstructured."""
    return (0.15 * _rng().standard_normal(int(seconds * fs))).astype(np.float32)


def tone(seconds: float = 1.0, freq: float = 1000.0, fs: int = FS) -> np.ndarray:
    """A single tone inside the voice band, which a naive band-energy gate calls speech."""
    t = np.arange(int(seconds * fs)) / fs
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def cicada(seconds: float = 1.0, fs: int = FS) -> np.ndarray:
    """What the bioacoustic module listens for: narrowband, high, amplitude-modulated."""
    t = np.arange(int(seconds * fs)) / fs
    carrier = np.sin(2 * np.pi * 6500 * t) + 0.6 * np.sin(2 * np.pi * 7400 * t)
    return (0.3 * carrier * (0.6 + 0.4 * np.sin(2 * np.pi * 40 * t))).astype(np.float32)


def shockwave(seconds: float = 1.0, fs: int = FS) -> np.ndarray:
    """Two impulsive cracks in an otherwise quiet clip."""
    out = np.zeros(int(seconds * fs), dtype=np.float32)
    noise = _rng().standard_normal(out.size)
    length = int(0.015 * fs)
    for fraction in (0.12, 0.55):
        onset = min(int(fraction * out.size), max(out.size - length, 0))
        decay = np.exp(-np.arange(length) / (0.0025 * fs))[: out.size - onset]
        out[onset:onset + decay.size] = (
            decay * noise[onset:onset + decay.size]).astype(np.float32)
    return out


def silence(seconds: float = 1.0, fs: int = FS) -> np.ndarray:
    return np.zeros(int(seconds * fs), dtype=np.float32)


def upsample_to_48k(x: np.ndarray) -> np.ndarray:
    """A 16 kHz waveform rendered at 48 kHz, so the decimation path has something real to eat."""
    t16 = np.arange(x.size) / float(FS)
    t48 = np.arange(x.size * 3) / float(V.ACQ_RATE_HZ)
    return np.interp(t48, t16, x).astype(np.float32)


@pytest.fixture()
def vad() -> V.SileroVAD:
    return V.SileroVAD()


# -- the fallback is announced, never silently substituted -------------------------------------

def test_absent_weights_fall_back_to_the_synthetic_engine_and_say_so(tmp_path):
    engine = V.SileroVAD(str(tmp_path / "not-here.onnx"))
    assert isinstance(engine.engine, V.SyntheticVADEngine)
    assert engine.uses_real_model is False


def test_a_caller_that_destroys_data_can_refuse_the_fallback(tmp_path):
    with pytest.raises(V.ModelUnavailable):
        V.SileroVAD(str(tmp_path / "not-here.onnx"), require_model=True)
    with pytest.raises(V.ModelUnavailable):
        V.SileroVAD(str(tmp_path / "not-here.onnx")).assert_real_model()


def test_the_model_path_comes_from_the_environment_when_nobody_passes_one(monkeypatch, tmp_path):
    monkeypatch.setenv(V.MODEL_ENV, str(tmp_path / "from-env.onnx"))
    assert V.SileroVAD().model_path == str(tmp_path / "from-env.onnx")
    monkeypatch.delenv(V.MODEL_ENV)
    assert V.SileroVAD().model_path == V.DEFAULT_MODEL_PATH


# -- sample rate is validated, and 48 kHz is decimated rather than assumed ----------------------

@pytest.mark.parametrize("rate", [8000, 16000, 48000])
def test_supported_and_acquisition_rates_are_accepted(rate):
    assert V.validate_rate(rate) == rate


@pytest.mark.parametrize("rate", [0, -16000, 22050, 44100, 32000, 96000, "16k", None])
def test_every_other_rate_is_refused_rather_than_stretched(rate):
    with pytest.raises(V.UnsupportedSampleRate):
        V.validate_rate(rate)


def test_a_48k_clip_is_decimated_to_the_model_rate_by_length_and_by_band():
    clip = upsample_to_48k(voice(0.5))
    out = V.decimate_48k_to_16k(clip)
    assert abs(out.size - clip.size / 3) <= 2
    assert out.dtype == np.float32
    # A 12 kHz tone is above the 16 kHz Nyquist and must be attenuated, not folded into the
    # voice band, which is the whole reason this is not `clip[::3]`.
    t = np.arange(V.ACQ_RATE_HZ // 2) / float(V.ACQ_RATE_HZ)
    folded = V.decimate_48k_to_16k(0.5 * np.sin(2 * np.pi * 12000 * t).astype(np.float32))
    assert float(np.sqrt(np.mean(folded ** 2))) < 0.05


def test_48k_speech_is_still_speech_after_the_decimation_helper(vad):
    assert vad.is_speech(upsample_to_48k(voice(1.0)), sample_rate=V.ACQ_RATE_HZ) is True
    assert vad.is_speech(upsample_to_48k(hiss(1.0)), sample_rate=V.ACQ_RATE_HZ) is False


def test_an_unsupported_rate_reaches_the_caller_from_the_helpers(vad):
    with pytest.raises(V.UnsupportedSampleRate):
        vad.is_speech(voice(0.2), sample_rate=44100)
    with pytest.raises(V.UnsupportedSampleRate):
        vad.get_speech_timestamps(voice(0.2), sample_rate=22050)


def test_an_8k_vad_refuses_16k_audio_instead_of_scoring_it_at_the_wrong_rate():
    engine = V.SileroVAD(sample_rate=8000)
    assert engine.window == V.CHUNK_SAMPLES[8000]
    with pytest.raises(V.UnsupportedSampleRate):
        engine.push(np.zeros(256, dtype=np.float32), sample_rate=16000)


# -- voice against everything else the fleet actually hears -------------------------------------

@pytest.mark.parametrize("f0", [95.0, 130.0, 210.0])
def test_voice_is_detected_across_pitches(vad, f0):
    assert vad.is_speech(voice(1.0, f0=f0)) is True


@pytest.mark.parametrize("maker", [hiss, tone, cicada, shockwave, silence])
def test_environmental_sound_is_not_called_speech(vad, maker):
    assert vad.is_speech(maker(1.0)) is False


def test_voice_scores_well_above_everything_else(vad):
    speech = max(vad.probabilities(voice(1.0)))
    for maker in (hiss, tone, cicada, shockwave, silence):
        assert speech > max(vad.probabilities(maker(1.0))) + 0.4


def test_digital_silence_scores_exactly_zero(vad):
    assert vad.probabilities(silence(0.5)) == [0.0] * len(vad.probabilities(silence(0.5)))


def test_a_threshold_outside_zero_to_one_is_refused(vad):
    for bad in (-0.1, 1.5):
        with pytest.raises(V.SileroVADError):
            vad.is_speech(voice(0.2), bad)
        with pytest.raises(V.SileroVADError):
            vad.get_speech_timestamps(voice(0.2), threshold=bad)


# -- stateless scoring is reproducible, streaming state is carried -------------------------------

def test_score_chunk_is_stateless_and_repeatable(vad):
    chunk = voice(0.2)[:vad.window]
    first, state_a = vad.score_chunk(chunk)
    second, state_b = vad.score_chunk(chunk)
    assert first == second
    assert np.array_equal(state_a, state_b)
    assert np.array_equal(vad.state, np.zeros(V.STATE_SHAPE, dtype=np.float32))


def test_score_chunk_accepts_a_carried_state_without_touching_the_instance(vad):
    chunk = voice(0.2)[:vad.window]
    _, state = vad.score_chunk(chunk)
    carried, _ = vad.score_chunk(chunk, state=state)
    fresh, _ = vad.score_chunk(chunk)
    assert carried != fresh or state.any()
    assert np.array_equal(vad.state, np.zeros(V.STATE_SHAPE, dtype=np.float32))


def test_push_advances_the_streaming_state_and_reset_clears_it(vad):
    chunk = voice(0.2)[:vad.window]
    vad.push(chunk)
    assert vad.state.any()
    vad.reset()
    assert not vad.state.any()


def test_streaming_a_clip_chunk_by_chunk_matches_scoring_it_whole(vad):
    clip = voice(1.0)
    whole = vad.probabilities(clip)
    vad.reset()
    piecewise = [vad.push(clip[i:i + vad.window])
                 for i in range(0, (clip.size // vad.window) * vad.window, vad.window)]
    assert piecewise == pytest.approx(whole[:len(piecewise)])


def test_state_is_a_copy_so_a_caller_cannot_mutate_it_from_underneath(vad):
    vad.push(voice(0.2)[:vad.window])
    snapshot = vad.state
    snapshot[:] = 0.0
    assert vad.state.any()


def test_a_state_of_the_wrong_shape_is_refused(vad):
    with pytest.raises(V.SileroVADError):
        vad.state = np.zeros((1, 1, 8), dtype=np.float32)
    with pytest.raises(V.SileroVADError):
        vad.score_chunk(voice(0.2)[:vad.window], state=np.zeros((3, 3), dtype=np.float32))


def test_a_restored_state_reproduces_the_stream_that_produced_it(vad):
    clip = voice(1.0)
    first = list(vad.stream(clip, reset=True))
    checkpoint = vad.state
    tail_a = list(vad.stream(clip, reset=False))
    vad.state = checkpoint
    tail_b = list(vad.stream(clip, reset=False))
    assert tail_a == tail_b
    assert len(first) == len(tail_a)


# -- chunk boundaries and malformed buffers ------------------------------------------------------

def test_a_chunk_longer_than_the_window_is_refused_not_truncated(vad):
    with pytest.raises(V.SileroVADError):
        vad.score_chunk(np.zeros(vad.window + 1, dtype=np.float32))


def test_a_short_chunk_is_extended_to_the_window_without_a_zero_step(vad):
    padded = V._fit_chunk(voice(0.05)[:100], vad.window)
    assert padded.size == vad.window
    assert padded.dtype == np.float32
    assert np.count_nonzero(padded) > 100


def test_a_clip_that_is_not_a_whole_number_of_windows_still_scores_every_sample(vad):
    clip = voice(1.0)[: vad.window * 3 + 17]
    scores = vad.probabilities(clip)
    assert len(scores) == 4
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_a_tail_window_does_not_invent_speech_out_of_padding(vad):
    assert max(vad.probabilities(tone(1.0)[: vad.window * 31 + 128])) < 0.5


@pytest.mark.parametrize("bad", [np.array(["a", "b"]), np.zeros((2, 2, 2)),
                                 np.array([np.nan, 0.0]), np.array([np.inf, 0.0])])
def test_malformed_audio_is_refused(vad, bad):
    with pytest.raises(V.SileroVADError):
        vad.is_speech(bad)


def test_an_empty_buffer_is_an_empty_answer_not_a_crash(vad):
    assert vad.probabilities(np.zeros(0, dtype=np.float32)) == []
    assert vad.get_speech_timestamps(np.zeros(0, dtype=np.float32)) == []
    assert vad.is_speech(np.zeros(0, dtype=np.float32)) is False


def test_int16_pcm_is_scaled_rather_than_scored_as_thousands(vad):
    clip = voice(1.0)
    as_int16 = (clip * 32767).astype(np.int16)
    assert V.as_mono_float32(as_int16) == pytest.approx(clip, abs=1e-3)
    assert vad.is_speech(as_int16) is True


def test_stereo_is_mixed_to_mono_on_the_way_in(vad):
    clip = voice(1.0)
    stereo = np.stack([clip, clip])
    assert V.as_mono_float32(stereo) == pytest.approx(clip, abs=1e-6)
    assert vad.is_speech(stereo) is True


def test_a_python_list_is_as_good_as_an_array(vad):
    assert vad.is_speech(list(map(float, voice(1.0)))) is True


# -- timestamps ----------------------------------------------------------------------------------

def test_timestamps_find_the_speech_and_not_the_gaps(vad):
    clip = np.concatenate([silence(0.5), voice(1.0), silence(0.6), voice(0.8)])
    stamps = vad.get_speech_timestamps(clip)
    assert len(stamps) == 2
    assert set(stamps[0]) == {"start", "end", "confidence"}
    assert stamps[0]["start"] == pytest.approx(0.5, abs=0.12)
    assert stamps[0]["end"] == pytest.approx(1.5, abs=0.12)
    assert stamps[1]["start"] == pytest.approx(2.1, abs=0.12)
    assert stamps[1]["end"] == pytest.approx(2.9, abs=0.12)
    for stamp in stamps:
        assert 0.0 <= stamp["confidence"] <= 1.0
        assert stamp["confidence"] >= 0.5
        assert stamp["end"] > stamp["start"]


def test_timestamps_are_ordered_and_never_overlap(vad):
    clip = np.concatenate([voice(0.6), hiss(0.5), voice(0.6), silence(0.4), voice(0.6)])
    stamps = vad.get_speech_timestamps(clip)
    assert stamps == sorted(stamps, key=lambda s: s["start"])
    for earlier, later in zip(stamps, stamps[1:]):
        assert earlier["end"] <= later["start"]


def test_timestamps_stay_inside_the_clip(vad):
    clip = np.concatenate([voice(0.8), silence(0.1)])
    duration = clip.size / float(FS)
    for stamp in vad.get_speech_timestamps(clip):
        assert stamp["start"] >= 0.0
        assert stamp["end"] <= duration + 1e-6


def test_a_clip_with_no_speech_has_no_timestamps(vad):
    assert vad.get_speech_timestamps(np.concatenate([hiss(0.5), shockwave(0.5)])) == []


def test_a_short_burst_below_the_minimum_duration_is_dropped(vad):
    clip = np.concatenate([silence(0.3), voice(0.1), silence(0.6)])
    assert vad.get_speech_timestamps(clip, min_speech_duration_ms=400.0) == []
    assert vad.get_speech_timestamps(clip, min_speech_duration_ms=30.0) != []


def test_a_single_quiet_window_does_not_cut_a_sentence_in_half(vad):
    clip = np.concatenate([voice(0.5), silence(0.03), voice(0.5)])
    stamps = vad.get_speech_timestamps(clip, min_silence_duration_ms=200.0)
    assert len(stamps) == 1


def test_max_speech_duration_splits_a_long_run(vad):
    stamps = vad.get_speech_timestamps(voice(3.0), max_speech_duration_s=1.0)
    assert len(stamps) >= 2


def test_timestamps_of_a_48k_clip_are_in_the_clips_own_seconds(vad):
    clip = np.concatenate([silence(0.5), voice(1.0), silence(0.5)])
    at_48k = upsample_to_48k(clip)
    stamps = vad.get_speech_timestamps(at_48k, sample_rate=V.ACQ_RATE_HZ)
    assert len(stamps) == 1
    assert stamps[0]["start"] == pytest.approx(0.5, abs=0.15)
    assert stamps[0]["end"] == pytest.approx(1.5, abs=0.15)


def test_the_module_level_helpers_agree_with_the_class(vad):
    clip = np.concatenate([silence(0.3), voice(0.9)])
    assert V.is_speech(clip) is vad.is_speech(clip)
    assert V.get_speech_timestamps(clip) == vad.get_speech_timestamps(clip)


def test_scoring_is_deterministic_across_instances():
    clip = voice(1.0)
    assert V.SileroVAD().probabilities(clip) == V.SileroVAD().probabilities(clip)


# -- the onnxruntime plumbing, against a graph built here ----------------------------------------

def _tiny_silero_like_model(path: pathlib.Path, split_state: bool) -> pathlib.Path:
    """A graph with Silero's I/O contract and arithmetic simple enough to predict by hand.

    output = mean(|input|) clipped to [0, 1] plus the carried state's first element; the next
    state is the state plus that mean. Enough to prove the feed, the state round-trip and the
    v4/v5 input-name split are wired correctly, and nothing about speech.
    """
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper, numpy_helper

    state_shape = [1, 1, V.STATE_SHAPE[2]] if split_state else list(V.STATE_SHAPE)
    inputs = [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, None]),
              helper.make_tensor_value_info("sr", TensorProto.INT64, [])]
    names = ["h", "c"] if split_state else ["state"]
    inputs += [helper.make_tensor_value_info(n, TensorProto.FLOAT, state_shape) for n in names]

    nodes = [
        helper.make_node("Abs", ["input"], ["absolute"]),
        helper.make_node("ReduceMean", ["absolute"], ["mean"], keepdims=1),
        helper.make_node("Clip", ["mean", "zero", "one"], ["output"]),
    ]
    outputs = [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 1])]
    for name in names:
        nodes.append(helper.make_node("Add", [name, "output"], [name + "n"]))
        outputs.append(helper.make_tensor_value_info(name + "n", TensorProto.FLOAT, state_shape))

    initialisers = [numpy_helper.from_array(np.array(0.0, dtype=np.float32), "zero"),
                    numpy_helper.from_array(np.array(1.0, dtype=np.float32), "one")]
    graph = helper.make_graph(nodes, "silero_like", inputs, outputs, initialisers)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    onnx.checker.check_model(model)
    path.write_bytes(model.SerializeToString())
    return path


@pytest.mark.parametrize("split_state", [False, True])
def test_the_onnx_engine_feeds_the_graph_and_carries_its_state(tmp_path, split_state):
    pytest.importorskip("onnxruntime")
    path = _tiny_silero_like_model(tmp_path / ("m%d.onnx" % split_state), split_state)
    engine = V.SileroVAD(str(path))
    assert engine.uses_real_model is True
    engine.assert_real_model()

    chunk = np.full(engine.window, 0.25, dtype=np.float32)
    probability, state = engine.score_chunk(chunk)
    assert probability == pytest.approx(0.25, abs=1e-5)
    assert state.shape == V.STATE_SHAPE
    assert float(state[0, 0, 0]) == pytest.approx(0.25, abs=1e-5)

    # The second window sees the state the first one produced, which is the whole point.
    assert engine.push(chunk) == pytest.approx(0.25, abs=1e-5)
    assert float(engine.state[0, 0, 0]) == pytest.approx(0.25, abs=1e-5)
    engine.reset()
    assert not engine.state.any()


def test_a_corrupt_model_file_is_a_clear_failure_not_a_silent_fallback(tmp_path):
    pytest.importorskip("onnxruntime")
    broken = tmp_path / "broken.onnx"
    broken.write_bytes(b"this is not a protobuf")
    with pytest.raises(Exception) as caught:
        V.SileroVAD(str(broken))
    assert not isinstance(caught.value, V.ModelUnavailable) or "no Silero weights" not in str(
        caught.value)


def test_the_window_size_is_the_one_silero_reads():
    assert V.CHUNK_SAMPLES == {8000: 256, 16000: 512}
    assert V.SileroVAD().window == 512
    assert math.isclose(512 / float(FS), 0.032)
