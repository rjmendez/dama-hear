#!/usr/bin/env python3
"""Silero VAD v5 over onnxruntime: does this buffer contain a human voice, yes or no.

The nodes listen at 48 kHz for gunshots and insects, and a microphone good enough for a
shockwave is good enough for a conversation two gardens away. Everything the fleet keeps has to
be provably not speech, so the speech decision is a single small module with one scoring path,
one state discipline and no hidden network access.

Three things this file is careful about, because each of them is a way the privacy gate silently
stops working:

⚠️SAMPLE RATE IS VALIDATED, NEVER ASSUMED. Silero v5 is trained on 16 kHz (and 8 kHz) and reads a
fixed 512-sample (256 at 8 kHz) window. Handing it 48 kHz audio does not fail -- it scores a
buffer three times too fast, mis-reads every formant, and returns a plausible number. Raw 48 kHz
is therefore decimated through `hear.resample`'s anti-aliased polyphase path before scoring, and
any other rate is refused rather than stretched.

⚠️STREAMING STATE IS EXPLICIT. The model is an RNN: its answer for a chunk depends on the chunks
before it. `score_chunk()` is stateless and reproducible (it never touches the instance state),
`push()`/`stream()` carry the recurrent context forward, and `reset()` is what you call between
two unrelated clips. Mixing those up is what makes a VAD look like it works on a test clip and
leak on a real one.

⚠️A MISSING WEIGHT FILE DOES NOT SILENTLY DISABLE THE GATE. The `.onnx` binary is not
redistributable through this repository and is absent in CI, so this module falls back to a
deterministic synthetic scorer with the same interface, records that it did in `.engine`, and
`assert_real_model()` is available to any caller (a purge pipeline, say) that must refuse to run
on the fallback. The fallback is a real signal-processing scorer -- band energy, harmonicity and
spectral flatness -- not a random number, so tests are hermetic and meaningful, but it is not
Silero and never claims to be.

    from hear.privacy.silero_vad import SileroVAD
    vad = SileroVAD()                                  # env HEAR_SILERO_VAD_MODEL, or fallback
    vad.is_speech(chunk_16k)                           # one 512-sample window
    vad.get_speech_timestamps(clip_48k, sample_rate=48000)
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np

#: The rate the model is trained for, and the rate every score in this module is taken at.
MODEL_RATE_HZ = 16000
#: Rates Silero v5 accepts directly.
SUPPORTED_RATES: Tuple[int, ...] = (8000, 16000)
#: The window the model reads, per rate. Silero v5 refuses anything else.
CHUNK_SAMPLES: Dict[int, int] = {8000: 256, 16000: 512}
#: The node acquisition rate, decimated to MODEL_RATE_HZ before scoring.
ACQ_RATE_HZ = 48000
#: Shape of the v5 recurrent context: (2 layers, batch, hidden).
STATE_SHAPE: Tuple[int, int, int] = (2, 1, 128)
#: Where the weights live when nobody says otherwise.
MODEL_ENV = "HEAR_SILERO_VAD_MODEL"
DEFAULT_MODEL_PATH = "models/silero_vad.onnx"
#: Below this RMS a window is digital silence and is scored zero without further analysis.
SILENCE_RMS = 1e-5

_VOICE_BAND_HZ: Tuple[float, float] = (300.0, 3400.0)
_PITCH_HZ: Tuple[float, float] = (75.0, 320.0)


class SileroVADError(Exception):
    """The VAD cannot answer for this input or in this configuration."""


class UnsupportedSampleRate(SileroVADError):
    """A rate the model is not trained for, and that this module will not silently stretch."""


class ModelUnavailable(SileroVADError):
    """The real weights were required and are not present."""


@dataclass(frozen=True)
class SpeechSegment:
    """One run of speech, in seconds against the *input* clip's own timebase."""

    start: float
    end: float
    confidence: float

    def as_dict(self) -> Dict[str, float]:
        return {"start": self.start, "end": self.end, "confidence": self.confidence}


def validate_rate(sample_rate: int) -> int:
    """-> a rate the model reads. 48 kHz is accepted here and decimated by the caller."""
    try:
        rate = int(sample_rate)
    except (TypeError, ValueError) as exc:
        raise UnsupportedSampleRate("%r is not a sample rate" % (sample_rate,)) from exc
    if rate in SUPPORTED_RATES or rate == ACQ_RATE_HZ:
        return rate
    raise UnsupportedSampleRate(
        "%d Hz is neither a Silero rate %s nor the %d Hz acquisition rate; refused rather than "
        "resampled by guess" % (rate, SUPPORTED_RATES, ACQ_RATE_HZ)
    )


def as_mono_float32(audio: Any) -> np.ndarray:
    """Any buffer -> 1-D float32. Multi-channel is averaged; int PCM is scaled to +/-1."""
    array = np.asarray(audio)
    if array.dtype == np.bool_ or array.dtype.kind in "USOV":
        raise SileroVADError("audio of dtype %s is not a waveform" % (array.dtype,))
    if array.ndim > 2:
        raise SileroVADError("audio with %d dimensions is not a waveform" % (array.ndim,))
    if array.ndim == 2:
        axis = 0 if array.shape[0] < array.shape[1] else 1
        array = array.mean(axis=axis)
    if array.dtype.kind in "iu":
        scale = float(max(abs(np.iinfo(array.dtype).min), np.iinfo(array.dtype).max))
        array = array.astype(np.float64) / scale
    array = np.asarray(array, dtype=np.float32).reshape(-1)
    if not np.all(np.isfinite(array)):
        raise SileroVADError("audio contains NaN or infinity; refused rather than scored")
    return array


def decimate_48k_to_16k(audio: Any) -> np.ndarray:
    """48 kHz -> 16 kHz through the platform's anti-aliased polyphase path (L=1, M=3).

    Uses `hear.resample` rather than a bare `[::3]`: plain decimation folds everything above
    8 kHz back onto the voice band, which both hides speech and invents it.
    """
    from hear import resample as _resample

    pcm = as_mono_float32(audio)
    if pcm.size == 0:
        return pcm
    out = _resample.resample(pcm, float(ACQ_RATE_HZ), float(MODEL_RATE_HZ))
    return np.asarray(out["pcm"], dtype=np.float32).reshape(-1)


def to_model_rate(audio: Any, sample_rate: int) -> Tuple[np.ndarray, int]:
    """-> (pcm, rate) at a rate the model reads, decimating 48 kHz input on the way."""
    rate = validate_rate(sample_rate)
    if rate == ACQ_RATE_HZ:
        return decimate_48k_to_16k(audio), MODEL_RATE_HZ
    return as_mono_float32(audio), rate


def _fit_chunk(chunk: np.ndarray, window: int) -> np.ndarray:
    """Extend a short window to the model window; refuse a long one, which would be truncation.

    The tail is mirrored, not zero-filled: a zero tail is a step discontinuity that smears the
    spectrum of whatever preceded it and reads as a broadband onset, which is exactly the shape
    a voice onset has. Mirroring keeps the window's own spectral character.
    """
    if chunk.size == window:
        return chunk
    if chunk.size > window:
        raise SileroVADError(
            "chunk of %d samples is longer than the model window of %d; split it rather than "
            "letting the tail go unscored" % (chunk.size, window)
        )
    if chunk.size == 0:
        return np.zeros(window, dtype=np.float32)
    padded = np.zeros(window, dtype=np.float32)
    mirrored = np.concatenate([chunk, chunk[::-1]])
    repeats = int(math.ceil(window / mirrored.size))
    filler = np.tile(mirrored, repeats)[:window]
    padded[:] = filler.astype(np.float32)
    padded[: chunk.size] = chunk
    return padded


def _windows(pcm: np.ndarray, window: int) -> Iterator[np.ndarray]:
    """Cut a buffer into model windows, covering the tail without inventing a boundary.

    A remainder shorter than the window is covered by sliding the last window back to the end
    of the buffer rather than zero-filling it, so every sample is scored inside a window made of
    real audio. Only a buffer shorter than one window is mirrored out to length.
    """
    if pcm.size == 0:
        return
    full = (pcm.size // window) * window
    for start in range(0, full, window):
        yield pcm[start:start + window]
    if pcm.size > full:
        tail = pcm[-window:] if pcm.size >= window else pcm
        yield _fit_chunk(tail, window)


class SyntheticVADEngine:
    """Deterministic stand-in for the real weights: band energy, harmonicity, flatness.

    It exists so the privacy tests are hermetic, not so the fleet can ship without the model. It
    is stateful in the same shape as the real engine -- the returned probability is smoothed
    against the previous window through the carried state -- so streaming code exercised against
    it exercises the same state discipline it will use against Silero.
    """

    name = "synthetic"
    is_real = False

    def new_state(self) -> np.ndarray:
        return np.zeros(STATE_SHAPE, dtype=np.float32)

    def run(self, chunk: np.ndarray, state: np.ndarray, sample_rate: int
            ) -> Tuple[float, np.ndarray]:
        raw = self._score(chunk, sample_rate)
        previous = float(state[0, 0, 0])
        smoothed = 0.65 * raw + 0.35 * previous if previous > 0.0 else raw
        nxt = np.zeros(STATE_SHAPE, dtype=np.float32)
        nxt[0, 0, 0] = np.float32(smoothed)
        nxt[1, 0, 0] = np.float32(raw)
        return float(min(max(smoothed, 0.0), 1.0)), nxt

    @staticmethod
    def _score(chunk: np.ndarray, sample_rate: int) -> float:
        x = np.asarray(chunk, dtype=np.float64)
        x = x - x.mean()
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        if rms < SILENCE_RMS:
            return 0.0

        window = np.hanning(x.size) if x.size > 1 else np.ones(1)
        spectrum = np.abs(np.fft.rfft(x * window)) + 1e-12
        freqs = np.fft.rfftfreq(x.size, 1.0 / float(sample_rate))
        power = spectrum * spectrum
        total = float(power.sum())

        band = (freqs >= _VOICE_BAND_HZ[0]) & (freqs <= _VOICE_BAND_HZ[1])
        band_ratio = float(power[band].sum() / total)

        # Flatness separates broadband hiss (near 1) from anything structured (near 0).
        flatness = float(np.exp(np.mean(np.log(power))) / (total / power.size))

        # Peak concentration separates a single tone or a stridulating insect, which put most
        # of their power in one bin, from a voice, which spreads it over a harmonic stack.
        peak_ratio = float(power.max() / total)

        # Harmonicity: a voiced window autocorrelates at its pitch period, noise does not.
        norm = float(np.dot(x, x))
        lo = max(int(sample_rate / _PITCH_HZ[1]), 1)
        hi = min(int(sample_rate / _PITCH_HZ[0]), x.size - 1)
        if hi > lo and norm > 0.0:
            corr = np.correlate(x, x, mode="full")[x.size - 1:]
            harmonic = float(np.max(corr[lo:hi]) / norm)
        else:
            harmonic = 0.0
        harmonic = min(max(harmonic, 0.0), 1.0)

        logit = (4.5 * (band_ratio - 0.35)
                 + 5.0 * (harmonic - 0.35)
                 - 10.0 * max(0.0, peak_ratio - 0.30) / 0.30
                 - 6.0 * max(0.0, flatness - 0.30) / 0.30
                 - 0.4)
        return 1.0 / (1.0 + math.exp(-max(min(logit, 30.0), -30.0)))


class OnnxVADEngine:
    """The real thing: a Silero v5 (`state`) or v4 (`h`, `c`) graph under onnxruntime."""

    name = "onnx"
    is_real = True

    def __init__(self, model_path: str, providers: Optional[Sequence[str]] = None) -> None:
        try:
            import onnxruntime  # noqa: WPS433 -- optional dependency, absent in CI
        except ImportError as exc:  # pragma: no cover - exercised only without onnxruntime
            raise ModelUnavailable(
                "onnxruntime is not installed, so %s cannot be scored" % (model_path,)
            ) from exc
        if not os.path.isfile(model_path):
            raise ModelUnavailable("no Silero weights at %s" % (model_path,))

        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self.model_path = model_path
        self.session = onnxruntime.InferenceSession(
            model_path, sess_options=options,
            providers=list(providers) if providers else ["CPUExecutionProvider"],
        )
        names = {i.name for i in self.session.get_inputs()}
        self.split_state = "h" in names and "c" in names
        self.wants_rate = "sr" in names
        self.input_name = "input" if "input" in names else self.session.get_inputs()[0].name

    def new_state(self) -> np.ndarray:
        return np.zeros(STATE_SHAPE, dtype=np.float32)

    def run(self, chunk: np.ndarray, state: np.ndarray, sample_rate: int
            ) -> Tuple[float, np.ndarray]:
        feed: Dict[str, Any] = {
            self.input_name: np.asarray(chunk, dtype=np.float32).reshape(1, -1),
        }
        if self.wants_rate:
            feed["sr"] = np.array(int(sample_rate), dtype=np.int64)
        if self.split_state:
            feed["h"] = np.ascontiguousarray(state[0:1], dtype=np.float32)
            feed["c"] = np.ascontiguousarray(state[1:2], dtype=np.float32)
        else:
            feed["state"] = np.ascontiguousarray(state, dtype=np.float32)

        outputs = self.session.run(None, feed)
        probability = float(np.asarray(outputs[0]).reshape(-1)[0])
        if self.split_state and len(outputs) >= 3:
            nxt = np.concatenate(
                [np.asarray(outputs[1], dtype=np.float32).reshape(1, *STATE_SHAPE[1:]),
                 np.asarray(outputs[2], dtype=np.float32).reshape(1, *STATE_SHAPE[1:])], axis=0)
        elif len(outputs) >= 2:
            nxt = np.asarray(outputs[1], dtype=np.float32).reshape(STATE_SHAPE)
        else:
            nxt = state
        return min(max(probability, 0.0), 1.0), np.asarray(nxt, dtype=np.float32)


class SileroVAD:
    """Silero VAD v5, stateless per chunk or stateful across a stream.

    `model_path=None` looks at `$HEAR_SILERO_VAD_MODEL` then `models/silero_vad.onnx`, and falls
    back to `SyntheticVADEngine` when neither the weights nor onnxruntime are there. Pass
    `require_model=True` where a fallback answer would be a privacy failure rather than a
    convenience.
    """

    def __init__(self, model_path: Optional[str] = None, *, sample_rate: int = MODEL_RATE_HZ,
                 require_model: bool = False,
                 providers: Optional[Sequence[str]] = None) -> None:
        rate = validate_rate(sample_rate)
        self.sample_rate = MODEL_RATE_HZ if rate == ACQ_RATE_HZ else rate
        self.window = CHUNK_SAMPLES[self.sample_rate]
        self.model_path = self._resolve_path(model_path)
        self.engine = self._open_engine(self.model_path, require_model, providers)
        self._state = self.engine.new_state()

    @staticmethod
    def _resolve_path(model_path: Optional[str]) -> str:
        if model_path:
            return str(model_path)
        return os.environ.get(MODEL_ENV) or DEFAULT_MODEL_PATH

    @staticmethod
    def _open_engine(model_path: str, require_model: bool,
                     providers: Optional[Sequence[str]]) -> Any:
        try:
            return OnnxVADEngine(model_path, providers=providers)
        except ModelUnavailable:
            if require_model:
                raise
            return SyntheticVADEngine()

    @property
    def uses_real_model(self) -> bool:
        """True only when the scores came out of the Silero graph."""
        return bool(getattr(self.engine, "is_real", False))

    def assert_real_model(self) -> None:
        """Raise unless the real weights are loaded. For callers that destroy data on a score."""
        if not self.uses_real_model:
            raise ModelUnavailable(
                "scoring is running on the %s fallback engine, not Silero weights; set %s"
                % (self.engine.name, MODEL_ENV)
            )

    # -- state -------------------------------------------------------------------------------

    def reset(self) -> None:
        """Drop the recurrent context. Call between two unrelated clips, never inside one."""
        self._state = self.engine.new_state()

    @property
    def state(self) -> np.ndarray:
        """A copy of the streaming context, so a caller cannot mutate it from underneath."""
        return np.array(self._state, dtype=np.float32, copy=True)

    @state.setter
    def state(self, value: Any) -> None:
        array = np.asarray(value, dtype=np.float32)
        if array.shape != STATE_SHAPE:
            raise SileroVADError(
                "state shape %s is not the model's %s" % (array.shape, STATE_SHAPE))
        self._state = np.array(array, dtype=np.float32, copy=True)

    # -- scoring -----------------------------------------------------------------------------

    def score_chunk(self, chunk: Any, *, sample_rate: Optional[int] = None,
                    state: Optional[np.ndarray] = None) -> Tuple[float, np.ndarray]:
        """Stateless: -> (probability, next_state). The instance's own state is untouched."""
        pcm, rate = to_model_rate(chunk, sample_rate if sample_rate is not None
                                  else self.sample_rate)
        if rate != self.sample_rate:
            raise UnsupportedSampleRate(
                "this VAD is configured for %d Hz and was handed %d Hz audio"
                % (self.sample_rate, rate))
        window = _fit_chunk(pcm, self.window)
        carried = self.engine.new_state() if state is None else np.asarray(state,
                                                                          dtype=np.float32)
        if carried.shape != STATE_SHAPE:
            raise SileroVADError(
                "state shape %s is not the model's %s" % (carried.shape, STATE_SHAPE))
        return self.engine.run(window, carried, self.sample_rate)

    def push(self, chunk: Any, *, sample_rate: Optional[int] = None) -> float:
        """Stateful: score one window and carry the recurrent context forward."""
        probability, self._state = self.score_chunk(chunk, sample_rate=sample_rate,
                                                    state=self._state)
        return probability

    def _prepare(self, audio: Any, sample_rate: Optional[int]) -> np.ndarray:
        """-> mono float32 at the engine's own rate, or a refusal."""
        pcm, rate = to_model_rate(audio, sample_rate if sample_rate is not None
                                  else self.sample_rate)
        if rate != self.sample_rate:
            raise UnsupportedSampleRate(
                "this VAD is configured for %d Hz and was handed %d Hz audio"
                % (self.sample_rate, rate))
        return pcm

    def stream(self, audio: Any, *, sample_rate: Optional[int] = None,
               reset: bool = False) -> Iterator[float]:
        """Score a buffer window by window, carrying state across the whole of it."""
        if reset:
            self.reset()
        for window in _windows(self._prepare(audio, sample_rate), self.window):
            probability, self._state = self.engine.run(window, self._state, self.sample_rate)
            yield probability

    def probabilities(self, audio: Any, *, sample_rate: Optional[int] = None
                      ) -> List[float]:
        """Every window's probability for a whole clip, from a fresh state."""
        return list(self.stream(audio, sample_rate=sample_rate, reset=True))

    def is_speech(self, audio_chunk: Any, threshold: float = 0.5, *,
                  sample_rate: Optional[int] = None) -> bool:
        """True when any window of `audio_chunk` scores at or above `threshold`.

        Stateless with respect to the instance: a clip is judged on its own, and asking twice
        gives the same answer.
        """
        if not 0.0 <= float(threshold) <= 1.0:
            raise SileroVADError("threshold %r is outside [0, 1]" % (threshold,))
        state = self.engine.new_state()
        for window in _windows(self._prepare(audio_chunk, sample_rate), self.window):
            probability, state = self.engine.run(window, state, self.sample_rate)
            if probability >= float(threshold):
                return True
        return False

    def get_speech_timestamps(self, audio: Any, *, threshold: float = 0.5,
                              sample_rate: Optional[int] = None,
                              min_speech_duration_ms: float = 250.0,
                              min_silence_duration_ms: float = 100.0,
                              speech_pad_ms: float = 30.0,
                              max_speech_duration_s: float = math.inf,
                              ) -> List[Dict[str, float]]:
        """-> `[{'start': s, 'end': s, 'confidence': p}, ...]` in the input clip's own seconds.

        Hysteresis, as Silero's own helper does it: a segment opens at `threshold` and closes
        only after `min_silence_duration_ms` below `threshold - 0.15`, so one quiet window
        between two words does not cut a sentence in half.
        """
        if not 0.0 <= float(threshold) <= 1.0:
            raise SileroVADError("threshold %r is outside [0, 1]" % (threshold,))
        rate_in = validate_rate(sample_rate if sample_rate is not None else self.sample_rate)
        scores = self.probabilities(audio, sample_rate=rate_in)
        if not scores:
            return []

        hop_s = self.window / float(self.sample_rate)
        neg_threshold = max(float(threshold) - 0.15, 0.01)
        min_speech_s = max(float(min_speech_duration_ms), 0.0) / 1000.0
        min_silence_s = max(float(min_silence_duration_ms), 0.0) / 1000.0
        pad_s = max(float(speech_pad_ms), 0.0) / 1000.0
        duration_s = len(scores) * hop_s

        segments: List[SpeechSegment] = []
        start_idx: Optional[int] = None
        silence_run = 0
        run_scores: List[float] = []

        def close(end_idx: int) -> None:
            assert start_idx is not None
            start_s = start_idx * hop_s
            end_s = min(end_idx * hop_s, duration_s)
            if end_s - start_s >= min_speech_s:
                segments.append(SpeechSegment(start=start_s, end=end_s,
                                              confidence=float(max(run_scores))))

        for index, score in enumerate(scores):
            if start_idx is None:
                if score >= float(threshold):
                    start_idx, silence_run, run_scores = index, 0, [score]
                continue
            run_scores.append(score)
            if score < neg_threshold:
                silence_run += 1
            else:
                silence_run = 0
            spoken_s = (index + 1 - start_idx) * hop_s
            if silence_run * hop_s >= min_silence_s or spoken_s >= float(max_speech_duration_s):
                close(index + 1 - silence_run)
                start_idx, silence_run, run_scores = None, 0, []
        if start_idx is not None:
            close(len(scores))

        # Seconds are the input clip's own seconds either way: decimating 48 kHz to 16 kHz
        # changes the sample count, not the timebase.
        return [segment.as_dict() for segment in _merge(_pad(segments, pad_s, duration_s))]


def _pad(segments: Sequence[SpeechSegment], pad_s: float,
         duration_s: float) -> List[SpeechSegment]:
    """Widen each segment by `pad_s`, but never past half the gap to its neighbour.

    A word starts before the first window that crosses the threshold, so the padding is real
    signal and not generosity. Splitting the gap keeps a pause a pause: without the clamp, two
    segments 40 ms apart would be padded into one, which is how a max-duration split quietly
    undoes itself.
    """
    out: List[SpeechSegment] = []
    for index, segment in enumerate(segments):
        previous_end = segments[index - 1].end if index else 0.0
        next_start = segments[index + 1].start if index + 1 < len(segments) else duration_s
        lead = min(pad_s, max(segment.start - previous_end, 0.0) / 2.0)
        trail = min(pad_s, max(next_start - segment.end, 0.0) / 2.0)
        out.append(SpeechSegment(start=round(max(segment.start - lead, 0.0), 6),
                                 end=round(min(segment.end + trail, duration_s), 6),
                                 confidence=round(segment.confidence, 6)))
    return out


def _merge(segments: Iterable[SpeechSegment]) -> List[SpeechSegment]:
    """Join segments whose padding made them overlap, keeping the strongest confidence."""
    merged: List[SpeechSegment] = []
    for segment in segments:
        if merged and segment.start < merged[-1].end:
            previous = merged[-1]
            merged[-1] = SpeechSegment(previous.start, max(previous.end, segment.end),
                                       max(previous.confidence, segment.confidence))
        else:
            merged.append(segment)
    return merged


def is_speech(audio_chunk: Any, threshold: float = 0.5, *, sample_rate: int = MODEL_RATE_HZ,
              model_path: Optional[str] = None) -> bool:
    """One-shot convenience wrapper. Builds a VAD, answers once, throws it away."""
    return SileroVAD(model_path, sample_rate=_config_rate(sample_rate)).is_speech(
        audio_chunk, threshold, sample_rate=sample_rate)


def get_speech_timestamps(audio: Any, *, threshold: float = 0.5,
                          sample_rate: int = MODEL_RATE_HZ,
                          model_path: Optional[str] = None,
                          **kwargs: Any) -> List[Dict[str, float]]:
    """One-shot convenience wrapper around `SileroVAD.get_speech_timestamps`."""
    return SileroVAD(model_path, sample_rate=_config_rate(sample_rate)).get_speech_timestamps(
        audio, threshold=threshold, sample_rate=sample_rate, **kwargs)


def _config_rate(sample_rate: int) -> int:
    """The rate the engine runs at for a given input rate: 48 kHz input is scored at 16 kHz."""
    return MODEL_RATE_HZ if validate_rate(sample_rate) == ACQ_RATE_HZ else int(sample_rate)
