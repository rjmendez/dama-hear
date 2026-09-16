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

def test_absent_weights_fall_back_to_the_synthetic_engine_and_say_so(monkeypatch, tmp_path):
    monkeypatch.delenv(V.MODEL_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    engine = V.SileroVAD()
    assert isinstance(engine.engine, V.SyntheticVADEngine)
    assert engine.uses_real_model is False


def test_a_model_path_the_caller_named_must_load_rather_than_fall_back(tmp_path):
    """Being pointed at a file and answering with something else's arithmetic is not a fallback."""
    with pytest.raises(V.ModelUnavailable):
        V.SileroVAD(str(tmp_path / "not-here.onnx"))


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
    assert (engine.hop, engine.context_samples, engine.window) == (256, 32, 288)
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


# -- the window contract: 576 samples, not 512 ---------------------------------------------------

def test_the_window_is_the_hop_prefixed_with_a_context_tail():
    assert (V.CHUNK_SAMPLES, V.CONTEXT_SAMPLES, V.WINDOW_SAMPLES) == (512, 64, 576)
    assert V.MODEL_SAMPLE_RATE == V.MODEL_RATE_HZ == 16000
    assert V.CHUNK_SAMPLES_BY_RATE == {8000: 256, 16000: 512}
    assert V.CONTEXT_SAMPLES_BY_RATE == {8000: 32, 16000: 64}
    assert V.WINDOW_SAMPLES_BY_RATE == {8000: 288, 16000: 576}
    engine = V.SileroVAD()
    assert (engine.hop, engine.context_samples, engine.window) == (512, 64, 576)
    assert math.isclose(engine.hop / float(FS), 0.032)


def test_the_engine_is_fed_576_samples_of_context_plus_hop(vad, monkeypatch):
    seen = []

    def spy(window, state, rate):
        seen.append(np.array(window, dtype=np.float32))
        return 0.0, state

    monkeypatch.setattr(vad.engine, "run", spy)
    clip = voice(0.2)
    list(vad.stream(clip, reset=True))
    assert seen and all(w.size == vad.window for w in seen)
    # Frame 0 is zero context; frame 1 carries the literal last 64 samples of frame 0's hop.
    assert not seen[0][: vad.context_samples].any()
    assert seen[1][: vad.context_samples] == pytest.approx(
        clip[vad.hop - vad.context_samples:vad.hop], abs=1e-6)
    assert seen[1][vad.context_samples:] == pytest.approx(clip[vad.hop:2 * vad.hop], abs=1e-6)


def test_the_window_canary_passes_and_reports_what_it_measured(vad):
    report = vad.verify_window_contract()
    assert report["window_samples"] == 576
    assert report["hop_samples"] == 512
    assert report["normal_peak"] >= V.CANARY_MIN_PROB


def test_the_canary_fails_closed_when_the_normal_path_stops_finding_speech(vad, monkeypatch):
    monkeypatch.setattr(vad.engine, "run",
                        lambda window, state, rate: (0.001, state))
    with pytest.raises(V.WindowContractViolated):
        vad.verify_window_contract()


def test_the_canary_signal_is_generated_and_is_speech_shaped(vad):
    canary = V.canary_speech()
    assert canary.dtype == np.float32
    assert canary.size == 2 * FS
    assert np.array_equal(canary, V.canary_speech())
    assert vad.is_speech(canary) is True


# -- stateless scoring is reproducible, streaming state is carried -------------------------------

def test_a_fresh_state_is_zero_in_both_halves(vad):
    state = vad._zero_state()
    assert state.rnn.shape == V.STATE_SHAPE
    assert state.context.size == vad.context_samples
    assert state.is_zero() is True


def test_score_chunk_isolated_is_repeatable_and_leaves_the_stream_alone(vad):
    chunk = voice(0.2)[:vad.hop]
    first, state_a = vad.score_chunk_isolated(chunk)
    second, state_b = vad.score_chunk_isolated(chunk)
    assert first == second
    assert np.array_equal(state_a.rnn, state_b.rnn)
    assert np.array_equal(state_a.context, state_b.context)
    assert vad._state.is_zero() is True


def test_the_next_state_carries_the_hops_own_tail_as_context(vad):
    chunk = voice(0.2)[:vad.hop]
    _, state = vad.score_chunk_isolated(chunk)
    assert state.context == pytest.approx(chunk[-vad.context_samples:], abs=1e-6)


def test_score_chunk_advances_both_halves_and_reset_clears_both(vad):
    assert isinstance(vad.score_chunk(voice(0.2)[:vad.hop]), float)
    assert vad._state.rnn.any()
    assert vad._state.context.any()
    vad.reset()
    assert vad._state.is_zero() is True


def test_the_stream_memory_is_not_a_public_attribute(vad):
    """Contract §3.2 rule 6: the context tail is 4 ms of PCM, so nothing can reach it by name."""
    list(vad.score_stream(voice(0.5)))
    public = {name for name in dir(vad) if not name.startswith("_")}
    assert not {name for name in public if "state" in name.lower()}


def test_streaming_a_clip_hop_by_hop_matches_scoring_it_whole(vad):
    clip = voice(1.0)
    whole = vad.probabilities(clip)
    vad.reset()
    piecewise = [vad.score_chunk(clip[i:i + vad.hop])
                 for i in range(0, (clip.size // vad.hop) * vad.hop, vad.hop)]
    assert piecewise == pytest.approx(whole[:len(piecewise)])


def test_a_dropped_context_changes_the_answer_so_it_is_not_a_buffering_detail(vad):
    """A partial reset is measured upstream to cost most of the detector; it must not be free."""
    clip = voice(1.0)
    carried = vad.probabilities(clip)
    vad.reset()
    partial = []
    for i in range(0, (clip.size // vad.hop) * vad.hop, vad.hop):
        state = vad._state.copy()
        state.context[:] = 0.0
        vad._restore_state(state)
        partial.append(vad.score_chunk(clip[i:i + vad.hop]))
    assert partial != pytest.approx(carried[:len(partial)])


def test_a_restored_state_is_a_copy_so_a_caller_cannot_mutate_it_from_underneath(vad):
    vad.score_chunk(voice(0.2)[:vad.hop])
    snapshot = vad._state.copy()
    vad._restore_state(snapshot)
    snapshot.rnn[:] = 0.0
    snapshot.context[:] = 0.0
    assert vad._state.rnn.any()
    assert vad._state.context.any()


def test_half_a_state_is_refused_because_a_partial_reset_is_the_silent_failure(vad):
    with pytest.raises(V.SileroVADError):
        vad._restore_state(np.zeros(V.STATE_SHAPE, dtype=np.float32))
    with pytest.raises(V.SileroVADError):
        vad._restore_state(V.StreamState(rnn=np.zeros((1, 1, 8), dtype=np.float32),
                                         context=np.zeros(64, dtype=np.float32)))
    with pytest.raises(V.SileroVADError):
        vad._restore_state(V.StreamState(rnn=np.zeros(V.STATE_SHAPE, dtype=np.float32),
                                         context=np.zeros(16, dtype=np.float32)))


def test_a_restored_state_reproduces_the_stream_that_produced_it(vad):
    clip = voice(1.0)
    first = list(vad.score_stream(clip, reset=True))
    checkpoint = vad._state.copy()
    tail_a = list(vad.score_stream(clip, reset=False))
    vad._restore_state(checkpoint)
    tail_b = list(vad.score_stream(clip, reset=False))
    assert tail_a == tail_b
    assert len(first) == len(tail_a)


def test_a_clip_is_scored_from_a_state_proven_zero(vad):
    vad.score_chunk(voice(0.2)[:vad.hop])
    scored = vad.score_clip(voice(1.0))
    assert scored.state_reset_confirmed is True
    assert scored.frames == len(scored.probabilities)


def test_a_failed_call_resets_the_stream_rather_than_resuming_from_it(vad):
    vad.score_chunk(voice(0.2)[:vad.hop])
    with pytest.raises(V.SileroVADError):
        vad.score_chunk(np.zeros(vad.hop + 1, dtype=np.float32))
    assert vad._state.is_zero() is True


# -- frame boundaries and malformed buffers ------------------------------------------------------

def test_a_chunk_that_is_not_a_whole_hop_is_refused_not_padded(vad):
    for size in (vad.hop + 1, vad.hop - 1, vad.window):
        with pytest.raises(V.SileroVADError):
            vad.score_chunk_isolated(np.zeros(size, dtype=np.float32))


def test_a_partial_final_hop_is_dropped_and_counted(vad):
    clip = voice(1.0)[: vad.hop * 3 + 17]
    scored = vad.score_clip(clip)
    assert scored.frames == 3
    assert scored.tail_samples_dropped == 17
    assert V.tail_samples_dropped(clip.size, vad.hop) == 17


def test_a_buffer_shorter_than_one_hop_scores_no_frames_rather_than_a_padded_one(vad):
    scored = vad.score_clip(voice(1.0)[: vad.hop - 1])
    assert scored.probabilities == ()
    assert scored.tail_samples_dropped == vad.hop - 1


def test_a_tail_frame_does_not_invent_speech_out_of_padding(vad):
    assert max(vad.probabilities(tone(1.0)[: vad.hop * 31 + 128])) < 0.5


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


def test_a_single_channel_2d_buffer_is_flattened_but_real_stereo_is_refused(vad):
    """A mixdown can cancel the one channel that holds the voice, so it is the caller's call."""
    clip = voice(1.0)
    assert V.as_mono_float32(clip.reshape(1, -1)) == pytest.approx(clip, abs=1e-6)
    assert V.as_mono_float32(clip.reshape(-1, 1)) == pytest.approx(clip, abs=1e-6)
    with pytest.raises(V.SileroVADError):
        V.as_mono_float32(np.stack([clip, clip]))
    with pytest.raises(V.SileroVADError):
        vad.is_speech(np.stack([clip, clip], axis=1))


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

def _tiny_silero_like_model(path: pathlib.Path, split_state: bool = False,
                            declared_window: object = None) -> pathlib.Path:
    """A graph with Silero's I/O contract and arithmetic simple enough to predict by hand.

    `output = clip(mean(|input|), 0, 1)`, and the next state is the state plus that mean. It
    proves the feed, the state round-trip, the declared-window check and the v4/v5 signature
    split are wired correctly, and says nothing about speech.
    """
    onnx = pytest.importorskip("onnx")
    from onnx import TensorProto, helper, numpy_helper

    state_shape = list(V.V4_STATE_SHAPE if split_state else V.STATE_SHAPE)
    length = declared_window if declared_window is not None else "n"
    inputs = [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, length]),
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
    assert engine.engine.split_state is split_state
    assert engine.engine.state_shape == (V.V4_STATE_SHAPE if split_state else V.STATE_SHAPE)

    # A quarter-amplitude hop against a zero context: the graph sees 576 samples, 64 of which
    # are zero, so the mean it returns is the one predicted for that window and not for the hop.
    hop = np.full(engine.hop, 0.25, dtype=np.float32)
    expected = 0.25 * engine.hop / float(engine.window)
    probability, state = engine.score_chunk_isolated(hop)
    assert probability == pytest.approx(expected, abs=1e-6)
    assert state.rnn.shape == engine.engine.state_shape
    assert float(state.rnn[0, 0, 0]) == pytest.approx(expected, abs=1e-6)
    assert state.context == pytest.approx(hop[-engine.context_samples:])

    # The second hop sees the first one's state and context, which is the whole point.
    assert engine.score_chunk(hop) == pytest.approx(expected, abs=1e-6)
    assert engine.score_chunk(hop) == pytest.approx(0.25, abs=1e-6)
    engine.reset()
    assert engine._state.is_zero() is True


def test_a_graph_that_declares_the_wrong_window_is_refused(tmp_path):
    pytest.importorskip("onnxruntime")
    wrong = _tiny_silero_like_model(tmp_path / "w512.onnx", declared_window=512)
    with pytest.raises(V.WindowContractViolated):
        V.SileroVAD(str(wrong))
    right = _tiny_silero_like_model(tmp_path / "w576.onnx", declared_window=576)
    assert V.SileroVAD(str(right)).uses_real_model is True


def test_a_graph_with_neither_signature_is_refused_rather_than_guessed(tmp_path):
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    from onnx import TensorProto, helper

    graph = helper.make_graph(
        [helper.make_node("Abs", ["input"], ["output"])], "not_silero",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, "n"])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, "n"])])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 9
    path = tmp_path / "not-silero.onnx"
    path.write_bytes(model.SerializeToString())
    with pytest.raises(V.WindowContractViolated):
        V.SileroVAD(str(path))


def test_load_vad_refuses_to_hand_the_fallback_to_the_purge_pipeline(tmp_path):
    with pytest.raises(V.ModelUnavailable):
        V.load_vad(model_path=str(tmp_path / "absent.onnx"))
    # purge.load_vad("auto") must see that refusal as "not installed here" and use its own
    # engine, so a receipt never carries Silero's name for a score Silero did not produce.
    from hear.privacy import purge

    detector = purge.load_vad("auto", 0.5, 250.0)
    assert isinstance(detector, purge.BandEnergyVAD)


def test_load_vad_runs_the_canary_and_returns_a_purge_detector(tmp_path, monkeypatch):
    pytest.importorskip("onnxruntime")
    from hear.privacy import purge

    path = _tiny_silero_like_model(tmp_path / "canary.onnx")
    monkeypatch.setattr(V.SileroVAD, "verify_window_contract", lambda self: {"ok": True})
    detector = V.load_vad(model_path=str(path))
    assert detector.name == "silero_vad_v5_onnx"
    decision = detector.detect(voice(1.0), FS)
    assert isinstance(decision, purge.VadDecision)
    assert decision.engine == "silero_vad_v5_onnx"
    assert 0.0 <= decision.peak_prob <= 1.0


# -- an injected session: the ONNX path with no onnxruntime and no weight file -------------------

class _Spec:
    """The two attributes this module reads off an onnxruntime input spec."""

    def __init__(self, name: str, shape: list) -> None:
        self.name = name
        self.shape = shape


class StubSession:
    """A scripted Silero v5 session, degenerate on a bare hop exactly as the real graph is.

    Records what it was fed, so the 576-sample window and the context tail inside it can be
    asserted without a 2 MB blob in the repository or onnxruntime on the runner.
    """

    def __init__(self, probabilities=(0.9,), declared_window="n", split_state=False,
                 window: int = 576, degenerate: float = 0.001) -> None:
        self.script = list(probabilities)
        self.window = window
        self.degenerate = degenerate
        self.calls: list = []
        shape = list(V.V4_STATE_SHAPE if split_state else V.STATE_SHAPE)
        names = ["h", "c"] if split_state else ["state"]
        self._inputs = [_Spec("input", [1, declared_window]), _Spec("sr", [])]
        self._inputs += [_Spec(n, shape) for n in names]
        self._state_names = names
        self._shape = shape

    def get_inputs(self):
        return self._inputs

    def run(self, _outputs, feed):
        self.calls.append({k: np.array(v, copy=True) for k, v in feed.items()})
        window = np.asarray(feed["input"]).shape[-1]
        probability = (self.script[min(len(self.calls) - 1, len(self.script) - 1)]
                       if window == self.window else self.degenerate)
        carried = np.asarray(feed[self._state_names[0]], dtype=np.float32)
        return [np.array([[probability]], dtype=np.float32),
                (carried + np.float32(0.01)).reshape(self._shape)]


def test_an_injected_session_drives_the_onnx_path_without_onnxruntime():
    stub = StubSession(probabilities=[0.02, 0.97, 0.97, 0.02])
    vad = V.SileroVAD(session=stub)
    assert vad.uses_real_model is True
    assert vad.engine.injected is True
    assert vad.engine.name == "silero_vad_onnx_injected_session"

    scores = vad.probabilities(voice(1.0))
    assert scores[:4] == pytest.approx([0.02, 0.97, 0.97, 0.02], abs=1e-6)
    fed = stub.calls[1]
    assert fed["input"].shape == (1, 576)
    assert fed["sr"].dtype == np.int64 and int(fed["sr"]) == 16000
    assert fed["state"].shape == V.STATE_SHAPE
    # Frame 1's window opens with the literal last 64 samples of frame 0's hop.
    assert fed["input"][0, :64] == pytest.approx(voice(1.0)[448:512], abs=1e-6)


def test_an_injected_session_is_never_swapped_for_the_fallback():
    stub = StubSession(declared_window=512)
    with pytest.raises(V.WindowContractViolated):
        V.SileroVAD(session=stub)
    with pytest.raises(V.ModelUnavailable):
        V.SileroVAD("no-such-model.onnx")


def test_an_injected_v4_session_is_recognised_by_its_signature():
    vad = V.SileroVAD(session=StubSession(split_state=True))
    assert vad.engine.split_state is True
    assert vad.engine.state_shape == V.V4_STATE_SHAPE
    assert vad.engine.name == "silero_vad_v4_onnx_injected_session"
    assert vad._zero_state().rnn.shape == V.V4_STATE_SHAPE


def test_load_vad_accepts_an_injected_session_and_the_canary_runs_against_it():
    detector = V.load_vad(session=StubSession(probabilities=[0.95]))
    assert detector.name == "silero_vad_onnx_injected_session"
    decision = detector.detect(voice(1.0), FS)
    assert decision.speech is True
    assert decision.engine == "silero_vad_onnx_injected_session"


def test_an_injected_session_that_never_fires_fails_the_canary():
    """The bare-hop control is the point: a session degenerate everywhere is not wired proof."""
    with pytest.raises(V.WindowContractViolated):
        V.load_vad(session=StubSession(probabilities=[0.001]))


def test_a_corrupt_model_file_is_a_clear_failure_not_a_silent_fallback(tmp_path):
    pytest.importorskip("onnxruntime")
    broken = tmp_path / "broken.onnx"
    broken.write_bytes(b"this is not a protobuf")
    with pytest.raises(Exception) as caught:
        V.SileroVAD(str(broken))
    assert not isinstance(caught.value, V.ModelUnavailable) or "no Silero weights" not in str(
        caught.value)




# ===================================================================================
# What the real graph actually answered, recorded once and asserted here.
#
# The tests above are hermetic by construction and therefore cannot say whether Silero
# separates a voice from a bird. `testdata/silero_vad_golden.json` holds the per-frame
# probabilities the real v5 graph produced for the fixtures in `tests/privacy_signals.py`,
# measured under onnxruntime with the model sha256, the runtime version and every waveform's
# digest recorded beside them. CI re-asserts the separation and the digests; where the weights
# exist, the same engine is re-run and compared to the record frame by frame.
#
# Regenerate with:
#     HEAR_VAD_MODEL=/path/to/silero_vad.onnx python3 tools/gen_silero_vad_golden.py \
#         > testdata/silero_vad_golden.json
# ===================================================================================

import json     # noqa: E402
import hashlib  # noqa: E402
import os       # noqa: E402

from tests import privacy_signals as SIG  # noqa: E402

GOLDEN_PATH = ROOT / "testdata" / "silero_vad_golden.json"
GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
SHARED = tuple(SIG.catalogue())

#: The model is real, so the bar is set where the measurement is, not where a docstring wishes
#: it were. Speech peaked at 0.999 on 71% of frames; the loudest thing that is not speech
#: peaked at 0.041.
SPEECH_MIN_PEAK = 0.9
SPEECH_MIN_FRACTION = 0.5
AMBIENT_MAX_PEAK = 0.1


def _probs(name):
    return np.asarray(GOLDEN["fixtures"][name]["probs"], dtype=float)


def _fraction_over(probs, threshold=V.DEFAULT_THRESHOLD):
    return float(np.mean(probs >= threshold)) if len(probs) else 0.0


def test_the_golden_names_the_model_the_runtime_and_the_geometry_it_was_measured_under():
    """A recorded number without its provenance is a number somebody typed."""
    assert GOLDEN["schema"] == "hear.vad.fixture_golden.v1"
    assert len(GOLDEN["model"]["sha256"]) == 64
    assert GOLDEN["model"]["version"] == "v5"
    assert GOLDEN["runtime"]["name"] == "onnxruntime" and GOLDEN["runtime"]["version"]


def test_the_geometry_the_golden_was_measured_under_is_the_geometry_the_module_uses():
    """If the module's window moves, the recorded probabilities stop describing it."""
    frame = GOLDEN["frame"]
    rate = frame["rate_hz"]
    assert rate == V.MODEL_RATE_HZ
    assert frame["chunk_samples"] == V.CHUNK_SAMPLES[rate]
    assert frame["context_samples"] == V.CONTEXT_SAMPLES[rate]
    assert tuple(frame["state_shape"]) == tuple(V.STATE_SHAPE)
    assert frame["chunk_samples"] + frame["context_samples"] == V.WINDOW_SAMPLES[rate]


@pytest.mark.parametrize("name", [f[0] for f in SHARED])
def test_the_waveform_scored_then_is_the_waveform_synthesised_now(name):
    """The digest is what stops a fixture drifting under its own recorded answers."""
    recorded = GOLDEN["fixtures"][name]
    waveform = np.asarray(SIG.by_name(name), dtype=np.float32)
    assert waveform.size == recorded["samples"]
    assert hashlib.sha256(waveform.tobytes()).hexdigest() == recorded["sha256"], (
        "%s changed; regenerate the golden with tools/gen_silero_vad_golden.py rather than "
        "editing the numbers" % name)


def test_the_real_model_called_the_voice_speech():
    probs = _probs("speech_like")
    assert probs.max() >= SPEECH_MIN_PEAK
    assert _fraction_over(probs) >= SPEECH_MIN_FRACTION, (
        "speech cleared the threshold on only %.0f%% of frames" % (100 * _fraction_over(probs)))


@pytest.mark.parametrize("name", [f[0] for f in SHARED if not f[2]])
def test_the_real_model_refused_everything_that_is_not_a_voice(name):
    """Silence, gaussian and pink noise, bird chirp sweeps, a 1 kHz tone."""
    probs = _probs(name)
    assert probs.max() <= AMBIENT_MAX_PEAK, \
        "%s peaked at %.3f, which is not a refusal" % (name, probs.max())
    assert _fraction_over(probs) == 0.0


def test_the_separation_is_a_gap_and_not_a_hair():
    quietest_speech = _probs("speech_like").max()
    loudest_ambient = max(_probs(n).max() for n, _x, speech in SHARED if not speech)
    assert quietest_speech > 10 * loudest_ambient, \
        "speech %.3f against ambient %.3f" % (quietest_speech, loudest_ambient)


def test_a_static_formant_buzz_is_not_a_usable_stand_in_for_speech():
    """Why the fixture is synthesised the hard way, recorded so nobody simplifies it back.

    A held vowel with unmoving formants reaches 0.90 on a fifth of its frames. It is a control,
    not a speech fixture: a suite built on it would pass while a detector that only fires on
    drones shipped.
    """
    drone = _probs("formant_drone") if "formant_drone" in GOLDEN["fixtures"] else None
    if drone is None:
        pytest.skip("the drone was not recorded in this golden")
    assert _fraction_over(drone) < _fraction_over(_probs("speech_like")) / 2


def test_dropping_the_context_window_silently_stops_finding_speech():
    """Contract §2.3, as a measurement: 512 samples where the graph declares 576.

    No exception, no log line -- just a confident 0.001 on a clip of speech, which in this
    pipeline means a voice clip that is never purged.
    """
    without = np.asarray(GOLDEN["ablations"]["speech_like_without_context"], dtype=float)
    assert without.max() < AMBIENT_MAX_PEAK, \
        "the ablation no longer demonstrates the failure it documents (max %.3f)" % without.max()
    assert _probs("speech_like").max() > 50 * without.max()


def test_resetting_the_state_every_frame_loses_most_of_the_speech():
    """Contract §3.2: the state is not a buffering detail, it is the memory of the utterance."""
    reset = np.asarray(GOLDEN["ablations"]["speech_like_state_reset_every_frame"], dtype=float)
    assert _fraction_over(reset) < _fraction_over(_probs("speech_like")) / 2


def test_decimating_48k_by_slicing_every_third_sample_is_not_decimation():
    """Aliasing is not free: the naive path scores a different clip than the filtered one."""
    naive = np.asarray(GOLDEN["ablations"]["speech_like_48k_decimated_naively"], dtype=float)
    proper = _probs("speech_like")
    assert naive.shape == proper.shape
    assert np.max(np.abs(naive - proper)) > 0.05, \
        "the aliased clip scores identically, so this ablation proves nothing"


# ---------------------------------------------------------------- the fallback, measured

@pytest.mark.parametrize("name,_x,holds_speech", SHARED, ids=[f[0] for f in SHARED])
def test_the_fallback_engine_reaches_the_same_verdict_as_the_recorded_model(name, _x,
                                                                           holds_speech):
    """The synthetic engine is not Silero, but it may not disagree about what a voice is.

    Probabilities are its own -- pink noise is its closest call at 0.42 against Silero's 0.028 --
    so only the verdict is compared. A node running the fallback destroys the same clips.
    """
    engine = V.SileroVAD()
    assert not engine.uses_real_model
    verdict = engine.is_speech(np.asarray(SIG.by_name(name), dtype=np.float32))
    recorded = bool(_probs(name).max() >= V.DEFAULT_THRESHOLD)
    assert verdict is holds_speech, "%s: fallback says speech=%s" % (name, verdict)
    assert verdict is recorded, \
        "%s: fallback says %s, the recorded model says %s" % (name, verdict, recorded)


# ---------------------------------------------------------------- against the weights

MODEL = os.environ.get("HEAR_VAD_MODEL") or os.environ.get(V.MODEL_ENV) or ""
needs_model = pytest.mark.skipif(
    not (MODEL and os.path.exists(MODEL)),
    reason="set HEAR_VAD_MODEL (or %s) to a silero_vad.onnx and install onnxruntime" % V.MODEL_ENV)


@pytest.fixture(scope="module")
def real_vad():
    engine = V.SileroVAD(MODEL, require_model=True)
    engine.assert_real_model()
    return engine


@needs_model
def test_the_weights_on_disk_are_the_weights_the_golden_was_measured_from():
    digest = hashlib.sha256(open(MODEL, "rb").read()).hexdigest()
    if digest != GOLDEN["model"]["sha256"]:
        pytest.skip("a different silero_vad.onnx (%s...); the golden is not about this file"
                    % digest[:12])


@needs_model
@pytest.mark.parametrize("name", [f[0] for f in SHARED])
def test_this_engine_reproduces_the_recorded_probabilities_frame_for_frame(real_vad, name):
    """The end-to-end claim: the shipped code, the real graph, the recorded answers.

    Everything else in this file tests a part. This tests that the parts, assembled, still
    produce what was measured -- and it is what would catch a window, state or decimation
    regression that every hermetic test agreed with.
    """
    if hashlib.sha256(open(MODEL, "rb").read()).hexdigest() != GOLDEN["model"]["sha256"]:
        pytest.skip("different weights")
    measured = np.asarray(real_vad.probabilities(np.asarray(SIG.by_name(name), dtype=np.float32)),
                          dtype=float)
    recorded = _probs(name)
    assert measured.shape == recorded.shape
    assert np.max(np.abs(measured - recorded)) < 1e-4, \
        "%s drifted by %.5f" % (name, np.max(np.abs(measured - recorded)))


@needs_model
def test_the_real_model_finds_the_voice_and_refuses_the_ambient_battery(real_vad):
    for name, waveform, holds_speech in SHARED:
        assert real_vad.is_speech(np.asarray(waveform, dtype=np.float32)) is holds_speech, \
            "%s: the real model disagrees with the fixture's own label" % name


@needs_model
def test_a_48k_clip_of_the_same_voice_is_still_speech_through_the_real_model(real_vad):
    assert real_vad.is_speech(np.asarray(SIG.speech_like_48k(), dtype=np.float32),
                              sample_rate=V.ACQ_RATE_HZ)
