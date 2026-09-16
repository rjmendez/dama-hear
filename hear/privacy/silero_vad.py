#!/usr/bin/env python3
"""Silero VAD v5 over onnxruntime: does this buffer contain a human voice, yes or no.

The nodes listen at 48 kHz for gunshots and insects, and a microphone good enough for a
shockwave is good enough for a conversation two gardens away. `hear/privacy/purge.py` destroys
anything that contains speech; this module is the detector that decides, and it implements
`docs/silero-vad-privacy-contract.md` rather than a reading of the upstream README.

⚠️THE MODEL INPUT IS 576 SAMPLES, NOT 512, AND FEEDING IT 512 FAILS SILENTLY TOWARDS RETENTION.
512 is the *hop*; the tensor is that hop prefixed with the 64-sample context tail of the previous
hop (contract §2.3). A bare 512-sample call does not raise -- it returns ~0.001 for unambiguous
speech, so a lane built on it writes confident `NO_SPEECH` receipts over every conversation it is
handed. `verify_window_contract()` is the startup canary that proves this wiring is right, and it
is required (§2.4) precisely because the failure has no symptom.

⚠️STREAM STATE IS TWO OBJECTS. The recurrent `state` `(2, 1, 128)` AND the 64-sample context
tail, reset together, carried together, never persisted or logged -- the context tail is literally
4 ms of PCM (§3.2). A partial reset costs most of the detector (mean 0.75 -> 0.25, measured) and
throws nothing. `score_chunk()` and `score_stream()` carry both objects forward, `score_clip()`
resets first and proves it did, `score_chunk_isolated()` is the stateless form for a caller that
owns the memory itself, and the memory has no public accessor at all -- a `state` attribute is
one `getattr` loop away from a receipt, and the context tail is PCM.

⚠️SAMPLE RATE IS VALIDATED, NEVER ASSUMED. Silero reads 16 kHz or 8 kHz. Raw 48 kHz acquisition
audio is decimated through `hear.resample`'s anti-aliased L=1 M=3 path (§2.1) and any other rate
is refused rather than stretched to the nearest.

⚠️A MISSING WEIGHT FILE DOES NOT SILENTLY DISABLE THE GATE. The `.onnx` artifact is not
redistributable through this repository and is absent in CI, so the wrapper falls back to a
deterministic band-energy / harmonicity / flatness scorer, says so in `.engine` and
`uses_real_model`, and `load_vad()` -- the entry point `purge.py` calls -- raises
`ModelUnavailable` instead of impersonating Silero, so the purge pipeline records the engine that
actually ran on the receipt.

    from hear.privacy.silero_vad import SileroVAD
    vad = SileroVAD()                              # $HEAR_SILERO_VAD_MODEL, else the fallback
    vad.is_speech(hop_16k)                         # or score_chunk() / score_stream() by frame
    vad.get_speech_timestamps(clip_48k, sample_rate=48000)
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

#: The rate the model is trained for, and the rate every score in this module is taken at.
MODEL_RATE_HZ = 16000
#: The same number under the name the contract and the fleet's other lanes use.
MODEL_SAMPLE_RATE = MODEL_RATE_HZ
#: Rates Silero v5 accepts.
SUPPORTED_RATES: Tuple[int, ...] = (8000, 16000)
#: Rates this module accepts as *input*: the two model rates and the acquisition rate.
SUPPORTED_INPUT_RATES: Tuple[int, ...] = (8000, 16000, 48000)
#: The advance between frames at 16 kHz, in samples. 32 ms. Contract §2.3.
CHUNK_SAMPLES = 512
#: The context tail prefixed to each hop at 16 kHz. Contract §2.3.
CONTEXT_SAMPLES = 64
#: What the graph actually reads at 16 kHz: context + hop.
WINDOW_SAMPLES = CONTEXT_SAMPLES + CHUNK_SAMPLES
#: The same three numbers per rate. The 8 kHz figures are arithmetic, not measured (§2.3).
CHUNK_SAMPLES_BY_RATE: Dict[int, int] = {8000: 256, 16000: CHUNK_SAMPLES}
CONTEXT_SAMPLES_BY_RATE: Dict[int, int] = {8000: 32, 16000: CONTEXT_SAMPLES}
WINDOW_SAMPLES_BY_RATE: Dict[int, int] = {
    rate: CONTEXT_SAMPLES_BY_RATE[rate] + CHUNK_SAMPLES_BY_RATE[rate]
    for rate in CHUNK_SAMPLES_BY_RATE}
#: The node acquisition rate, decimated to MODEL_RATE_HZ before scoring.
ACQ_RATE_HZ = 48000
#: Shape of the v5 recurrent context: (2 layers, batch, hidden). v4 carries h and c at half.
STATE_SHAPE: Tuple[int, int, int] = (2, 1, 128)
V4_STATE_SHAPE: Tuple[int, int, int] = (2, 1, 64)
#: Where the weights live when nobody says otherwise.
MODEL_ENV = "HEAR_SILERO_VAD_MODEL"
DEFAULT_MODEL_PATH = "models/silero_vad.onnx"
#: Below this RMS a window is digital silence and is scored zero without further analysis.
SILENCE_RMS = 1e-5

#: Contract §4.1. Proposed defaults, not measured on this fleet's clips.
DEFAULT_THRESHOLD = 0.50
NEG_THRESHOLD_MARGIN = 0.15
DEFAULT_MIN_SPEECH_MS = 250.0
DEFAULT_MIN_SILENCE_MS = 300.0
DEFAULT_SPEECH_PAD_MS = 30.0

#: Canary bounds, contract §2.4: the normal path must find the synthetic voice, and the bare-hop
#: path must reproduce the degenerate mode it is there to detect.
#:
#: ⚠️0.30, NOT DEFAULT_THRESHOLD. Measured against the real snakers4/silero-vad v5 weights
#: (2026-09-16, the first time this repo ran them): the synthetic formant stack peaks at 0.386
#: on the real graph, comfortably above the 0.0006 the bare-hop control gets on the SAME signal
#: -- a 640x margin that is what actually proves the window is wired, not whether a trained net
#: is fooled into calling a synthesised buzz a human voice. Chasing 0.50 here means cranking the
#: signal to near-clipping amplitude, which passes by distortion rather than by the window being
#: right, and would silently re-break if this file's canary formula ever changed. This constant
#: gates `verify_window_contract()` only; `DEFAULT_THRESHOLD` (0.50), the number field clips are
#: actually judged against, is untouched and was proven separately against real speech.
CANARY_MIN_PROB = 0.30
CANARY_DEGENERATE_MAX_PROB = 0.1

_VOICE_BAND_HZ: Tuple[float, float] = (300.0, 3400.0)
_PITCH_HZ: Tuple[float, float] = (75.0, 320.0)


class SileroVADError(Exception):
    """The VAD cannot answer for this input or in this configuration."""


class UnsupportedSampleRate(SileroVADError):
    """A rate the model is not trained for, and that this module will not silently stretch."""


class ModelUnavailable(SileroVADError, ImportError):
    """The real weights were required and are not present.

    An `ImportError` as well, because `purge.load_vad()` treats "Silero is not installed here"
    as a fall-back-to-the-band-energy-engine condition rather than an outage.
    """


class WindowContractViolated(SileroVADError):
    """The graph does not read the window this module feeds it, or the canary did not fire.

    Contract §9's `vad_window_contract_violated`: fail closed. A caller that purges on a score
    must stop, not carry on scoring through a shape it has not verified.
    """


@dataclass(frozen=True)
class SpeechSegment:
    """One run of speech, in seconds against the *input* clip's own timebase."""

    start: float
    end: float
    confidence: float

    def as_dict(self) -> Dict[str, float]:
        return {"start": self.start, "end": self.end, "confidence": self.confidence}


@dataclass
class StreamState:
    """The two objects that are one stream's memory: the RNN state and the context tail.

    ⚠️Neither is persisted, logged or exported. `context` is 4 ms of PCM from the clip being
    scored and inherits its classification (contract §3.2).
    """

    rnn: np.ndarray
    context: np.ndarray

    @classmethod
    def zeros(cls, sample_rate: int, state_shape: Tuple[int, ...] = STATE_SHAPE) -> "StreamState":
        return cls(rnn=np.zeros(state_shape, dtype=np.float32),
                   context=np.zeros(CONTEXT_SAMPLES_BY_RATE[sample_rate], dtype=np.float32))

    def copy(self) -> "StreamState":
        return StreamState(rnn=np.array(self.rnn, dtype=np.float32, copy=True),
                           context=np.array(self.context, dtype=np.float32, copy=True))

    def is_zero(self) -> bool:
        """Frame-0 assertion: a clip starts from nothing, or the verdict is another clip's."""
        return not bool(np.any(self.rnn)) and not bool(np.any(self.context))


@dataclass(frozen=True)
class ClipScores:
    """Per-frame probabilities for one clip, with the two numbers a receipt needs about them."""

    probabilities: Tuple[float, ...]
    tail_samples_dropped: int
    state_reset_confirmed: bool

    @property
    def frames(self) -> int:
        return len(self.probabilities)

    def __iter__(self) -> Iterator[float]:
        """Iterating a clip's scores gives its per-frame probabilities, in sample order."""
        return iter(self.probabilities)

    def __len__(self) -> int:
        return len(self.probabilities)


def validate_rate(sample_rate: int) -> int:
    """-> a rate the model reads. 48 kHz is accepted here and decimated on the way in."""
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
    """Any *mono* buffer -> 1-D float32. int PCM is scaled to +/-1; multi-channel is refused.

    ⚠️Interleaved channels are not averaged here. Mixing down is a judgement about the recording
    -- which microphone heard the voice, whether one channel is a dead short, whether the two are
    out of phase and cancel a talker to nothing -- and a VAD that makes that judgement silently
    can answer "no speech" about a clip that has a voice in one channel. The fleet's clips are
    mono (contract §1); anything else is the caller's to mix down deliberately.
    """
    array = np.asarray(audio)
    if array.dtype == np.bool_ or array.dtype.kind in "USOV":
        raise SileroVADError("audio of dtype %s is not a waveform" % (array.dtype,))
    if array.ndim > 2:
        raise SileroVADError("audio with %d dimensions is not a waveform" % (array.ndim,))
    if array.ndim == 2:
        if min(array.shape) != 1:
            raise SileroVADError(
                "audio of shape %s is %d-channel; refused rather than mixed down, because a "
                "mixdown can cancel the one channel that holds the voice"
                % (array.shape, min(array.shape)))
        array = array.reshape(-1)
    if array.dtype.kind in "iu":
        scale = float(max(abs(np.iinfo(array.dtype).min), np.iinfo(array.dtype).max))
        array = array.astype(np.float64) / scale
    array = np.asarray(array, dtype=np.float32).reshape(-1)
    if not np.all(np.isfinite(array)):
        raise SileroVADError("audio contains NaN or infinity; refused rather than scored")
    return array


def decimate_48k_to_16k(audio: Any) -> np.ndarray:
    """48 kHz -> 16 kHz through the platform's anti-aliased polyphase path (L=1, M=3).

    `hear.resample`, not a bare `[::3]`: plain decimation folds everything above 8 kHz back onto
    the voice band, which both hides speech and invents it.
    """
    from hear import resample as _resample

    pcm = as_mono_float32(audio)
    if pcm.size == 0:
        return pcm
    out = _resample.resample(pcm, float(ACQ_RATE_HZ), float(MODEL_RATE_HZ))
    return np.asarray(out["pcm"], dtype=np.float32).reshape(-1)


def model_rate_for(sample_rate: int) -> int:
    """-> the rate `to_model_rate()` will hand the model for audio at `sample_rate`."""
    rate = validate_rate(sample_rate)
    return MODEL_RATE_HZ if rate == ACQ_RATE_HZ else rate


def to_model_rate(audio: Any, sample_rate: int) -> np.ndarray:
    """-> mono float32 at a rate the model reads, decimating 48 kHz input on the way.

    The rate that came out is `model_rate_for(sample_rate)`, asked separately so that this
    returns a waveform and nothing else: a function that returns a pair is one a caller can feed
    to the model whole by accident.
    """
    if model_rate_for(sample_rate) != int(sample_rate):
        return decimate_48k_to_16k(audio)
    return as_mono_float32(audio)


def hops(pcm: np.ndarray, hop: int) -> Iterator[np.ndarray]:
    """Whole hops only, in sample order. A partial final hop is dropped, never zero-padded.

    Contract §2.3: a zero-padded frame is scored as near-silence by construction, and the end of
    a clip is exactly where a truncated word sits. Discarding under 32 ms is honest; padding
    invents a low score. What was dropped is counted by `tail_samples_dropped()`.
    """
    whole = (pcm.size // hop) * hop
    for start in range(0, whole, hop):
        yield pcm[start:start + hop]


def tail_samples_dropped(n_samples: int, hop: int) -> int:
    """How many trailing samples `hops()` did not score. A number, not a silence."""
    return int(n_samples) - (int(n_samples) // hop) * hop


class SyntheticVADEngine:
    """Deterministic stand-in for the real weights: band energy, harmonicity, flatness.

    It exists so the privacy tests are hermetic, not so the fleet can ship without the model:
    `load_vad()` refuses to hand this to the purge pipeline under Silero's name. It reads the
    same 576-sample window and carries the same state shape as the real engine, so streaming
    code exercised against it exercises the discipline it will use against Silero.
    """

    name = "synthetic_band_harmonicity"
    is_real = False
    state_shape = STATE_SHAPE

    def new_state(self) -> np.ndarray:
        return np.zeros(self.state_shape, dtype=np.float32)

    def run(self, window: np.ndarray, state: np.ndarray, sample_rate: int
            ) -> Tuple[float, np.ndarray]:
        raw = self._score(window, sample_rate)
        previous = float(state[0, 0, 0])
        smoothed = 0.65 * raw + 0.35 * previous if previous > 0.0 else raw
        nxt = np.zeros(self.state_shape, dtype=np.float32)
        nxt[0, 0, 0] = np.float32(smoothed)
        nxt[1, 0, 0] = np.float32(raw)
        return float(min(max(smoothed, 0.0), 1.0)), nxt

    @staticmethod
    def _score(window: np.ndarray, sample_rate: int) -> float:
        x = np.asarray(window, dtype=np.float64)
        x = x - x.mean()
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        if rms < SILENCE_RMS:
            return 0.0

        taper = np.hanning(x.size) if x.size > 1 else np.ones(1)
        spectrum = np.abs(np.fft.rfft(x * taper)) + 1e-12
        freqs = np.fft.rfftfreq(x.size, 1.0 / float(sample_rate))
        power = spectrum * spectrum
        total = float(power.sum())

        band = (freqs >= _VOICE_BAND_HZ[0]) & (freqs <= _VOICE_BAND_HZ[1])
        band_ratio = float(power[band].sum() / total)

        # Flatness separates broadband hiss (near 1) from anything structured (near 0).
        flatness = float(np.exp(np.mean(np.log(power))) / (total / power.size))

        # Peak concentration separates a single tone or a stridulating insect, which put most of
        # their power in one bin, from a voice, which spreads it over a harmonic stack.
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
    """The real thing: a Silero v5 (`state`) or v4 (`h`, `c`) graph under onnxruntime.

    The generation is read off the loaded file's declared inputs, never assumed, and the
    declared input length -- where the graph fixes it rather than leaving it dynamic -- is
    checked against the window this module feeds (contract §2.3, §3.1).
    """

    name = "silero_vad_v5_onnx"
    is_real = True

    def __init__(self, model_path: Optional[str] = None, sample_rate: int = MODEL_RATE_HZ,
                 providers: Optional[Sequence[str]] = None, session: Any = None) -> None:
        if session is None:
            session = self._open_session(model_path, providers)
            self.injected = False
        else:
            # ⚠️AN INJECTED SESSION SKIPS THE WEIGHT-FILE CHECK, SO IT IS NOT PROOF OF SILERO.
            # It exists so a test, or a service that already holds a warm session, can drive the
            # ONNX path with no onnxruntime import and no 2 MB blob in the repository. The
            # signature and window checks below still run against whatever was handed in, and
            # `name` says an injected session produced the score so a receipt cannot imply the
            # shipped weights did.
            self.injected = True
            self.name = "silero_vad_onnx_injected_session"
        self.model_path = model_path or "<injected session>"
        self.session = session
        inputs = {i.name: i for i in self.session.get_inputs()}
        if "h" in inputs and "c" in inputs:
            self.split_state, self.state_shape = True, V4_STATE_SHAPE
            self.name = ("silero_vad_v4_onnx_injected_session" if self.injected
                         else "silero_vad_v4_onnx")
        elif "state" in inputs:
            self.split_state, self.state_shape = False, STATE_SHAPE
        else:
            raise WindowContractViolated(
                "%s declares inputs %s, which is neither the v5 {input, sr, state} nor the v4 "
                "{input, sr, h, c} signature; refused rather than guessed"
                % (self.model_path, sorted(inputs)))
        self.wants_rate = "sr" in inputs
        self.input_name = "input" if "input" in inputs else self.session.get_inputs()[0].name
        self.declared_window = self._declared_window(inputs[self.input_name])
        expected = WINDOW_SAMPLES_BY_RATE[validate_rate(sample_rate)]
        if self.declared_window is not None and self.declared_window != expected:
            raise WindowContractViolated(
                "%s declares an input of %d samples and this module feeds %d (%d context + %d "
                "hop); a constant that disagrees with the graph is the defect, not the graph"
                % (self.model_path, self.declared_window, expected,
                   CONTEXT_SAMPLES_BY_RATE[sample_rate], CHUNK_SAMPLES_BY_RATE[sample_rate]))

    @staticmethod
    def _open_session(model_path: Optional[str], providers: Optional[Sequence[str]]) -> Any:
        """Load the weights, or say why they cannot be. Never a silent substitution."""
        try:
            import onnxruntime
        except ImportError as exc:  # pragma: no cover - exercised only without onnxruntime
            raise ModelUnavailable(
                "onnxruntime is not installed, so %s cannot be scored" % (model_path,)
            ) from exc
        if not model_path or not os.path.isfile(model_path):
            raise ModelUnavailable("no Silero weights at %s" % (model_path,))
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        return onnxruntime.InferenceSession(
            model_path, sess_options=options,
            providers=list(providers) if providers else ["CPUExecutionProvider"])

    @staticmethod
    def _declared_window(spec: Any) -> Optional[int]:
        """The graph's own input length, or None where it leaves the dimension dynamic."""
        shape = list(getattr(spec, "shape", []) or [])
        if not shape:
            return None
        last = shape[-1]
        return int(last) if isinstance(last, int) else None

    def new_state(self) -> np.ndarray:
        return np.zeros(self.state_shape, dtype=np.float32)

    def run(self, window: np.ndarray, state: np.ndarray, sample_rate: int
            ) -> Tuple[float, np.ndarray]:
        feed: Dict[str, Any] = {
            self.input_name: np.asarray(window, dtype=np.float32).reshape(1, -1),
        }
        if self.wants_rate:
            feed["sr"] = np.array(int(sample_rate), dtype=np.int64)
        if self.split_state:
            feed["h"] = np.ascontiguousarray(state, dtype=np.float32)
            feed["c"] = np.ascontiguousarray(state, dtype=np.float32)
        else:
            feed["state"] = np.ascontiguousarray(state, dtype=np.float32)

        outputs = self.session.run(None, feed)
        probability = float(np.asarray(outputs[0]).reshape(-1)[0])
        # ⚠️A NaN IS NOT A LOW PROBABILITY. Every comparison against NaN is False, so a NaN that
        # is clamped to 0.0 -- or carried into `p >= threshold` -- reads as confident silence and
        # the clip is kept. Contract §9 `vad_inference_error`: raise, and let the caller purge.
        if not math.isfinite(probability):
            raise SileroVADError(
                "%s returned a non-finite probability (%r); a NaN is not a low score"
                % (self.name, probability))
        if not -0.01 <= probability <= 1.01:
            raise SileroVADError(
                "%s returned %r, which is not a probability" % (self.name, probability))
        nxt = (np.asarray(outputs[1], dtype=np.float32).reshape(self.state_shape)
               if len(outputs) >= 2 else state)
        if not np.all(np.isfinite(nxt)):
            raise SileroVADError("%s returned a non-finite recurrent state" % (self.name,))
        return min(max(probability, 0.0), 1.0), np.asarray(nxt, dtype=np.float32)


class SileroVAD:
    """Silero VAD v5, stateless per hop or stateful across a stream.

    `model_path=None` looks at `$HEAR_SILERO_VAD_MODEL`, then `models/silero_vad.onnx`, and
    falls back to `SyntheticVADEngine` when neither the weights nor onnxruntime are there. Pass
    `require_model=True` where a fallback answer would be a privacy failure rather than a
    convenience.

    `session=` drives the ONNX path with an already-built session -- a warm one held by a
    service, or a stub in a test -- without importing onnxruntime and without a weight file. An
    injected session is checked against the same signature and window rules as a loaded one, is
    never silently replaced by the fallback, and names itself on every receipt as injected.
    """

    def __init__(self, model_path: Optional[str] = None, *, sample_rate: int = MODEL_RATE_HZ,
                 require_model: bool = False,
                 threshold: float = DEFAULT_THRESHOLD,
                 providers: Optional[Sequence[str]] = None,
                 session: Any = None) -> None:
        self.sample_rate = model_rate_for(sample_rate)
        self.hop = CHUNK_SAMPLES_BY_RATE[self.sample_rate]
        self.context_samples = CONTEXT_SAMPLES_BY_RATE[self.sample_rate]
        self.window = WINDOW_SAMPLES_BY_RATE[self.sample_rate]
        _check_threshold(threshold)
        self.threshold = float(threshold)
        self.model_path = self._resolve_path(model_path)
        # ⚠️A PATH THE CALLER NAMED IS A REQUIREMENT, NOT A HINT. Falling back to the synthetic
        # engine after being pointed at a specific file would answer a question about Silero's
        # weights with something else's arithmetic.
        self.engine = self._open_engine(self.model_path, self.sample_rate,
                                        require_model or model_path is not None,
                                        providers, session)
        self._state = self._zero_state()

    @staticmethod
    def _resolve_path(model_path: Optional[str]) -> str:
        if model_path:
            return str(model_path)
        return os.environ.get(MODEL_ENV) or DEFAULT_MODEL_PATH

    @staticmethod
    def _open_engine(model_path: str, sample_rate: int, require_model: bool,
                     providers: Optional[Sequence[str]], session: Any = None) -> Any:
        """The graph if it loads, else the fallback -- unless a caller said it must be the graph.

        An injected `session` is never swapped for the fallback: a caller that handed over a
        session asked for that session, and quietly scoring with something else would make the
        engine name on a receipt a guess.
        """
        try:
            return OnnxVADEngine(model_path, sample_rate=sample_rate, providers=providers,
                                 session=session)
        except ModelUnavailable:
            if require_model or session is not None:
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
                % (self.engine.name, MODEL_ENV))

    # -- state -------------------------------------------------------------------------------

    def _zero_state(self) -> StreamState:
        """A zeroed stream memory: RNN state and context tail together, never one of the two."""
        return StreamState.zeros(self.sample_rate, getattr(self.engine, "state_shape",
                                                           STATE_SHAPE))

    def reset(self) -> None:
        """Clear both objects. Called between clips, unconditionally, and after any failure."""
        self._state = self._zero_state()

    # ⚠️THE STREAM MEMORY HAS NO PUBLIC ACCESSOR, ON PURPOSE. Contract §3.2 rule 6: the recurrent
    # state and the 64-sample context tail are functions of the audio and inherit its
    # classification -- the tail is literally 4 ms of PCM. A public `state` attribute is one
    # `{name: getattr(vad, name) for name in dir(vad)}` away from a receipt, a log line or a
    # crash dump, so callers get `reset()` and nothing else. Tests that need to checkpoint a
    # stream use `_state` and say in their name that they are reaching inside.

    def _restore_state(self, value: Any) -> None:
        """Put a checkpointed stream memory back. Both halves, validated, or neither."""
        self._state = self._checked_state(value).copy()

    def _checked_state(self, value: Any) -> StreamState:
        if not isinstance(value, StreamState):
            raise SileroVADError(
                "stream state must be a StreamState carrying both the RNN state and the context "
                "tail; a bare array is half a reset")
        shape = getattr(self.engine, "state_shape", STATE_SHAPE)
        if tuple(np.shape(value.rnn)) != tuple(shape):
            raise SileroVADError(
                "state shape %s is not the model's %s" % (np.shape(value.rnn), shape))
        if int(np.size(value.context)) != self.context_samples:
            raise SileroVADError(
                "context tail of %d samples is not the model's %d"
                % (np.size(value.context), self.context_samples))
        return value

    # -- scoring -----------------------------------------------------------------------------

    def _prepare(self, audio: Any, sample_rate: Optional[int]) -> np.ndarray:
        """-> mono float32 at the engine's own rate, or a refusal."""
        given = self.sample_rate if sample_rate is None else sample_rate
        rate = model_rate_for(given)
        if rate != self.sample_rate:
            raise UnsupportedSampleRate(
                "this VAD is configured for %d Hz and was handed %d Hz audio"
                % (self.sample_rate, rate))
        return to_model_rate(audio, given)

    def _run_hop(self, hop: np.ndarray, state: StreamState) -> Tuple[float, StreamState]:
        """One hop, prefixed with the carried context, -> (probability, the state after it)."""
        if hop.size != self.hop:
            raise SileroVADError(
                "a frame is exactly %d samples at %d Hz and this one is %d; whole hops only, "
                "and a partial tail is dropped rather than padded"
                % (self.hop, self.sample_rate, hop.size))
        window = np.concatenate([state.context, hop]).astype(np.float32)
        if window.size != self.window:
            raise WindowContractViolated(
                "built a %d-sample window where the contract is %d (%d context + %d hop)"
                % (window.size, self.window, self.context_samples, self.hop))
        probability, rnn = self.engine.run(window, state.rnn, self.sample_rate)
        return probability, StreamState(rnn=np.asarray(rnn, dtype=np.float32),
                                        context=np.array(hop[-self.context_samples:],
                                                         dtype=np.float32))

    def score_chunk_isolated(self, chunk: Any, *, sample_rate: Optional[int] = None,
                             carry: Optional[StreamState] = None) -> Tuple[float, StreamState]:
        """Stateless: -> (probability, next state). The instance's own state is untouched.

        For a caller that owns the stream memory itself -- one state per node in a fan-in, say.
        Anything else wants `score_chunk()`, which carries the state for you and is the only
        form that reproduces the measured probabilities (§2.5: dropping the hand-off costs
        speech mean 0.75 -> 0.25).
        """
        pcm = self._prepare(chunk, sample_rate)
        carried = self._zero_state() if carry is None else self._checked_state(carry)
        return self._run_hop(pcm, carried)

    def score_chunk(self, chunk: Any, *, sample_rate: Optional[int] = None) -> float:
        """Stateful: score one hop and carry both halves of the stream memory forward.

        The state is reset on any failure rather than left holding a state produced by one
        (contract §3.2 rule 4).
        """
        try:
            probability, self._state = self._run_hop(self._prepare(chunk, sample_rate),
                                                     self._state)
        except Exception:
            self.reset()
            raise
        return probability

    #: Names this had before `score_chunk` became the stateful one. Same calls.
    push = score_chunk

    def score_stream(self, audio: Any, *, sample_rate: Optional[int] = None,
                     reset: bool = False) -> Iterator[float]:
        """Score a buffer hop by hop, carrying state across the whole of it.

        ⚠️THIS DOES NOT RESET FIRST. It is the entry point for audio that arrives in pieces, so
        the second call continues the first one's stream -- which is what makes it useful and
        what makes it wrong for a whole clip. A clip is `score_clip()`, which resets and proves
        it did (contract §3.2 rule 3).
        """
        if reset:
            self.reset()
        pcm = self._prepare(audio, sample_rate)
        for hop in hops(pcm, self.hop):
            probability, self._state = self._run_hop(hop, self._state)
            yield probability

    def score_clip(self, audio: Any, *, sample_rate: Optional[int] = None) -> ClipScores:
        """A whole clip from a guaranteed-zero state: probabilities, dropped tail, and proof.

        `state_reset_confirmed` is the receipt field of contract §3.2: frame 0 of a clip ran
        against a state and a context that were both zero, so this verdict is about this clip.
        """
        self.reset()
        confirmed = self._state.is_zero()
        pcm = self._prepare(audio, sample_rate)
        try:
            scores = tuple(self._run_hop_stream(pcm))
        except Exception:
            self.reset()
            raise
        return ClipScores(probabilities=scores,
                          tail_samples_dropped=tail_samples_dropped(pcm.size, self.hop),
                          state_reset_confirmed=confirmed)

    def _run_hop_stream(self, pcm: np.ndarray) -> Iterator[float]:
        for hop in hops(pcm, self.hop):
            probability, self._state = self._run_hop(hop, self._state)
            yield probability

    def probabilities(self, audio: Any, *, sample_rate: Optional[int] = None) -> List[float]:
        """Every frame's probability for a whole clip, from a fresh state."""
        return list(self.score_clip(audio, sample_rate=sample_rate).probabilities)

    #: `stream()` was the name of the carrying-on scorer before `score_stream()`. Same call.
    stream = score_stream

    def has_speech(self, audio: Any, *, sample_rate: Optional[int] = None,
                   threshold: Optional[float] = None) -> bool:
        """`is_speech()` under the name the purge lane calls it by."""
        return self.is_speech(audio, threshold, sample_rate=sample_rate)

    def is_speech(self, audio_chunk: Any, threshold: Optional[float] = None, *,
                  sample_rate: Optional[int] = None) -> bool:
        """True when any frame of `audio_chunk` scores at or above `threshold`.

        `threshold=None` uses the one this VAD was built with. A clip is judged on its own: the
        stream memory is reset first, and asking twice gives the same answer.
        """
        threshold = self.threshold if threshold is None else threshold
        _check_threshold(threshold)
        return any(p >= float(threshold)
                   for p in self.score_clip(audio_chunk, sample_rate=sample_rate).probabilities)

    def get_speech_timestamps(self, audio: Any, *, threshold: Optional[float] = None,
                              sample_rate: Optional[int] = None,
                              min_speech_duration_ms: float = DEFAULT_MIN_SPEECH_MS,
                              min_silence_duration_ms: float = DEFAULT_MIN_SILENCE_MS,
                              speech_pad_ms: float = DEFAULT_SPEECH_PAD_MS,
                              max_speech_duration_s: float = math.inf,
                              ) -> List[Dict[str, float]]:
        """-> `[{'start': s, 'end': s, 'confidence': p}, ...]` in the input clip's own seconds.

        The hysteresis of contract §4.2/§4.3: a segment opens at `threshold` and closes only
        after `min_silence_duration_ms` below `threshold - 0.15`, so one quiet frame between two
        words does not cut a sentence in half. `threshold=None` uses the instance's own.
        """
        threshold = self.threshold if threshold is None else threshold
        _check_threshold(threshold)
        rate_in = validate_rate(sample_rate if sample_rate is not None else self.sample_rate)
        scores = self.score_clip(audio, sample_rate=rate_in).probabilities
        if not scores:
            return []

        hop_s = self.hop / float(self.sample_rate)
        neg_threshold = max(float(threshold) - NEG_THRESHOLD_MARGIN, 0.01)
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
            silence_run = silence_run + 1 if score < neg_threshold else 0
            spoken_s = (index + 1 - start_idx) * hop_s
            if silence_run * hop_s >= min_silence_s or spoken_s >= float(max_speech_duration_s):
                close(index + 1 - silence_run)
                start_idx, silence_run, run_scores = None, 0, []
        if start_idx is not None:
            close(len(scores))

        # Seconds are the input clip's own seconds either way: decimating 48 kHz to 16 kHz
        # changes the sample count, not the timebase.
        return [segment.as_dict() for segment in _merge(_pad(segments, pad_s, duration_s))]

    # -- the window canary ---------------------------------------------------------------------

    def verify_window_contract(self) -> Dict[str, Any]:
        """Contract §2.4: prove, in process and before any clip is judged, that this is wired.

        Scores a synthetic voice through the normal path and asserts it is found, then -- for a
        real graph -- scores it again through a deliberate bare-hop call and asserts that path
        IS degenerate, so "the normal path works" is a measurement rather than a hope. Raises
        `WindowContractViolated`, which fails closed.
        """
        canary = canary_speech(self.sample_rate)
        normal = max(self.probabilities(canary), default=0.0)
        self.reset()
        if normal < CANARY_MIN_PROB:
            raise WindowContractViolated(
                "the window canary scored %.3f on synthetic speech, below %.2f: the %d-sample "
                "window is not reaching the model correctly (engine %s)"
                % (normal, CANARY_MIN_PROB, self.window, self.engine.name))

        degenerate: Optional[float] = None
        if self.uses_real_model:
            degenerate = self._bare_hop_peak(canary)
            if degenerate >= CANARY_DEGENERATE_MAX_PROB:
                raise WindowContractViolated(
                    "the bare-hop control scored %.3f, at or above %.2f: the degenerate mode "
                    "this canary exists to exclude is not reproducible on this graph, so a "
                    "passing normal path proves nothing"
                    % (degenerate, CANARY_DEGENERATE_MAX_PROB))
        return {"engine": self.engine.name, "real_model": self.uses_real_model,
                "window_samples": self.window, "hop_samples": self.hop,
                "normal_peak": round(normal, 6),
                "bare_hop_peak": None if degenerate is None else round(degenerate, 6)}

    def _bare_hop_peak(self, audio: np.ndarray) -> float:
        """The control: feed the hop alone, with no context, exactly as the wrong reading did."""
        state = self.engine.new_state()
        peak = 0.0
        for hop in hops(np.asarray(audio, dtype=np.float32), self.hop):
            probability, state = self.engine.run(hop, state, self.sample_rate)
            peak = max(peak, probability)
        return peak


def canary_speech(sample_rate: int = MODEL_RATE_HZ, seconds: float = 2.0) -> np.ndarray:
    """A deterministic synthetic voice: harmonics under moving formants, ~4 Hz syllables.

    Generated in code and containing no recording, because the canary is a wiring test that has
    to run on a node holding no audio it is allowed to keep.
    """
    rate = validate_rate(sample_rate)
    rate = MODEL_RATE_HZ if rate == ACQ_RATE_HZ else rate
    t = np.arange(int(seconds * rate), dtype=np.float64) / float(rate)
    f0 = 120.0 + 25.0 * np.sin(2.0 * np.pi * 1.7 * t)
    phase = 2.0 * np.pi * np.cumsum(f0) / float(rate)
    signal = np.zeros_like(t)
    for harmonic, amplitude in enumerate([1.0, 0.7, 0.55, 0.4, 0.3, 0.22, 0.15, 0.1], start=1):
        freq = f0 * harmonic
        shape = np.ones_like(t)
        for centre, bandwidth, gain in ((640.0, 130.0, 1.7), (1240.0, 190.0, 1.2),
                                        (2500.0, 260.0, 0.9)):
            moving = centre * (1.0 + 0.08 * np.sin(2.0 * np.pi * 2.1 * t))
            shape += gain / (1.0 + ((freq - moving) / bandwidth) ** 2)
        signal += amplitude * shape * np.sin(harmonic * phase + 0.3 * harmonic)
    signal *= 0.55 + 0.45 * np.sin(2.0 * np.pi * 4.0 * t)
    peak = float(np.max(np.abs(signal))) or 1.0
    return (0.3 * signal / peak).astype(np.float32)


def _check_threshold(threshold: float) -> None:
    if not 0.0 <= float(threshold) <= 1.0:
        raise SileroVADError("threshold %r is outside [0, 1]" % (threshold,))


def _pad(segments: Sequence[SpeechSegment], pad_s: float,
         duration_s: float) -> List[SpeechSegment]:
    """Widen each segment by `pad_s`, but never past half the gap to its neighbour.

    A word starts before the first frame that crosses the threshold, so the padding is real
    signal and not generosity. Splitting the gap keeps a pause a pause: without the clamp, two
    segments 40 ms apart are padded into one, which is how a max-duration split undoes itself.
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


def _merge(segments: Sequence[SpeechSegment]) -> List[SpeechSegment]:
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


# --------------------------------------------------------------- the purge pipeline's entry point

class SileroDetector:
    """`purge.py`'s detector interface over this engine: `.name` and `.detect(samples, rate)`.

    The decision rule itself -- spans, gap joining, minimum duration -- is `purge`'s, shared by
    every engine, so "what counts as speech" stays one implementation and a receipt from this
    detector is comparable with one from the band-energy fallback.
    """

    def __init__(self, vad: "SileroVAD", threshold: float, min_speech_ms: float) -> None:
        self.vad = vad
        self.threshold = float(threshold)
        self.min_speech_ms = float(min_speech_ms)
        self.name = vad.engine.name

    def detect(self, samples: np.ndarray, rate: int) -> Any:
        from hear.privacy import purge as _purge

        if int(rate) <= 0:
            raise _purge.PurgeError("sample rate %r is not a rate" % (rate,))
        scored = self.vad.score_clip(samples, sample_rate=int(rate))
        hop_s = self.vad.hop / float(self.vad.sample_rate)
        return _purge.decision_from_probs(scored.probabilities, hop_s, hop_s,
                                          self.threshold, self.min_speech_ms, self.name)


def load_vad(threshold: float = DEFAULT_THRESHOLD,
             min_speech_ms: float = DEFAULT_MIN_SPEECH_MS,
             model_path: Optional[str] = None,
             session: Any = None) -> SileroDetector:
    """The real Silero engine, verified, or `ModelUnavailable` -- never the fallback in disguise.

    `purge.load_vad("auto")` falls back to its own `BandEnergyVAD` when this raises, and writes
    that engine's name on the receipt. Returning the synthetic scorer here instead would put
    "silero" on a receipt that Silero never saw, which is the one lie this pipeline cannot
    tolerate. The window canary (§2.4) runs before the detector is handed over.
    """
    vad = SileroVAD(model_path, require_model=True, session=session)
    vad.verify_window_contract()
    return SileroDetector(vad, threshold, min_speech_ms)


def is_speech(audio_chunk: Any, threshold: float = DEFAULT_THRESHOLD, *,
              sample_rate: int = MODEL_RATE_HZ, model_path: Optional[str] = None) -> bool:
    """One-shot convenience wrapper. Builds a VAD, answers once, throws it away."""
    return SileroVAD(model_path, sample_rate=_config_rate(sample_rate)).is_speech(
        audio_chunk, threshold, sample_rate=sample_rate)


def get_speech_timestamps(audio: Any, *, threshold: float = DEFAULT_THRESHOLD,
                          sample_rate: int = MODEL_RATE_HZ,
                          model_path: Optional[str] = None,
                          **kwargs: Any) -> List[Dict[str, float]]:
    """One-shot convenience wrapper around `SileroVAD.get_speech_timestamps`."""
    return SileroVAD(model_path, sample_rate=_config_rate(sample_rate)).get_speech_timestamps(
        audio, threshold=threshold, sample_rate=sample_rate, **kwargs)


def _config_rate(sample_rate: int) -> int:
    """The rate the engine runs at for a given input rate: 48 kHz input is scored at 16 kHz."""
    return MODEL_RATE_HZ if validate_rate(sample_rate) == ACQ_RATE_HZ else int(sample_rate)
