#!/usr/bin/env python3
"""Detect human speech in a clip, destroy the clip, and keep only the proof that it happened.

    from hear.privacy import purge
    report = purge.scan_pool("~/hear-clips", audit_log="~/hear-clips/purged_receipts.jsonl")
    report.purged            # how many WAVs no longer exist
    report.receipts[0].as_record()["zero_audio_retained"]   # True, always

WHAT THIS MODULE IS FOR. Clips are captured for acoustics -- a gunshot's arrival time, a clap's
envelope -- and a microphone that hears a gunshot also hears whoever was talking beside it. A
voice clip is not evidence this project may hold, so the only defensible handling is: notice it,
destroy it, and be able to say afterwards exactly what was destroyed without being able to say
anything about its contents.

⚠️THE RECEIPT IS THE ONLY SURVIVOR, AND IT IS DELIBERATELY NEARLY EMPTY. A purge that leaves no
record is indistinguishable from a lost file, from a crashed drain, and from a cover-up; an audit
that cannot count destructions cannot prove the policy runs. So one JSONL line is written per
purge and it carries `{timestamp, node, duration_s, peak_speech_prob, purged_sha256,
purge_reason, zero_audio_retained}` -- when, where, how long, how sure, and the digest of bytes
that no longer exist anywhere. The digest is a *tombstone*, not an index: nothing can be
recovered from it, and it is what lets a later question ("was THIS file purged?") be answered by
whoever still holds the file, without this repository holding it.

⚠️EVERY FIELD IS ALLOW-LISTED, BECAUSE THE FAILURE MODE HERE IS ADDITIVE. The way a privacy
pipeline leaks is not a decision to leak; it is one more "harmless" field -- a transcript
snippet for debugging, an mfcc vector to tune the threshold, an embedding to dedupe speakers --
added by someone solving a real problem. `assert_privacy_safe()` refuses any key that is not on
`RECEIPT_KEYS`, and refuses any value that is a sequence of numbers, so a feature vector cannot
be smuggled into a field that is allowed to exist. Widening that list is a deliberate act with a
reviewer attached, which is the whole point.

⚠️NO TRANSCRIPT, NO FEATURES, NO EMBEDDINGS, NO SAMPLES -- INCLUDING IN MEMORY, AFTER THE
DECISION. The VAD sees the waveform because it must; nothing downstream of the decision does.
The sample buffer is zeroed as soon as a purge is decided, the frame probabilities are collapsed
to a peak and a set of second-resolution spans before they leave `detect()`, and no code path
here writes audio anywhere. Speech *timestamps* are kept because "0.4 s of speech at 2.1 s" is
an operational measurement about a file that no longer exists; it is not content.

⚠️DESTRUCTION IS OVERWRITE-THEN-UNLINK, AND IT IS NOT CRYPTOGRAPHIC ERASURE. The file is
overwritten in place (random pass, then zeros), fsynced, truncated and unlinked, so the bytes
are gone from any reader that goes through the filesystem. On a copy-on-write or log-structured
filesystem, on flash with wear levelling, and in any snapshot or backup taken before the purge,
the original blocks may still exist physically. That is a storage-layer property this module
cannot fix and must not pretend to: the claim made in the receipt is `zero_audio_retained`, i.e.
this pipeline retained nothing, not `unrecoverable`.

⚠️DRY RUN TOUCHES NOTHING, AND THAT INCLUDES THE AUDIT LOG. `--dry-run` answers "what would go"
and must be safe to run on a pool someone else is draining. It hashes and detects, marks the
receipt `dry_run: true`, and neither destroys a file nor appends a line.

⚠️THE DETECTOR IS PLUGGABLE AND ITS NAME IS ON EVERY RECEIPT. `load_vad()` prefers the Silero
v5 ONNX engine (`hear/privacy/silero_vad.py`) when it is installed and otherwise falls back to
`BandEnergyVAD`, a stdlib+numpy voiced-band/harmonicity detector with no model file. They do not
have the same recall, so `vad_engine` is recorded on every receipt: a fleet-wide claim about
what was purged is only as strong as the weakest detector that produced it, and that has to be
visible in the record rather than inferred from a deployment date.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import wave
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

#: Bumped when the receipt's meaning changes, never when a field is merely reordered.
RECEIPT_SCHEMA_VERSION = 1

#: The one reason this pipeline destroys anything. A second reason is a second code path with a
#: second review, not a free-text string an operator can invent at the command line.
#:
#: ⚠️IT NAMES THE POLICY, NOT THE ENGINE THAT RAN. The reason a clip was destroyed is always
#: "the speech-detection policy fired"; WHICH detector fired is `vad_engine`, on the same line.
#: Varying the reason per engine would make a fleet-wide count of purges depend on which node
#: had onnxruntime installed, and the count is the thing an auditor reads.
PURGE_REASON = "silero_vad_speech_detected"

#: Default audit sink, relative to the pool root being scanned.
RECEIPT_NAME = "purged_receipts.jsonl"

#: EVERY key a receipt may carry. `assert_privacy_safe()` refuses anything else -- see the
#: module docstring on why the failure mode here is additive.
RECEIPT_KEYS = (
    "schema_version",
    "timestamp",
    "node",
    "clip",
    "duration_s",
    "peak_speech_prob",
    "speech_s",
    "speech_spans_s",
    "purged_sha256",
    "purge_reason",
    "vad_engine",
    "zero_audio_retained",
    "dry_run",
)

#: Keys that have leaked from a pipeline like this one before, spelled out so the refusal names
#: the thing rather than saying "unknown key". Checked case-insensitively, substring-wise, with
#: `NAME_EXEMPT` below holding the allow-listed keys whose own names contain one of these words.
FORBIDDEN_SUBSTRINGS = (
    "transcript", "text", "words", "tokens", "embedding", "embed", "mfcc", "mel",
    "spectrogram", "feature", "samples", "audio", "waveform", "pcm", "voiceprint",
    "speaker", "identity", "content", "excerpt", "snippet",
)

#: Allow-listed keys that legitimately spell a forbidden word. `zero_audio_retained` is the
#: pipeline's central CLAIM about audio, not audio; the speech fields are measurements in
#: seconds and a scalar probability.
NAME_EXEMPT = ("zero_audio_retained", "speech_s", "speech_spans_s", "peak_speech_prob")

#: A voice, in Hz: the band a telephone was built around, which is where speech energy is.
VOICE_BAND_HZ = (300.0, 3400.0)
#: Fundamental frequency search range for the harmonicity test, adult male floor to child ceiling.
PITCH_HZ = (70.0, 350.0)
#: Analysis geometry. 32 ms frames at 10 ms hops: long enough for a pitch period at 70 Hz to
#: appear twice, short enough that a 250 ms utterance is 25 frames rather than 8.
FRAME_MS, HOP_MS = 32.0, 10.0
#: Two speech-labelled frames closer than this are one utterance with a pause between. 200 ms,
#: not 120: at 120 the voiced peaks of one modulated utterance in noise stay separate 140 ms
#: fragments, every fragment falls under `min_speech_ms`, and a clip of someone talking through
#: traffic is KEPT. Merging first and length-testing after is what makes the length test mean
#: "how long were they speaking" rather than "how long was the loudest syllable".
JOIN_GAP_MS = 200.0

DEFAULT_THRESHOLD = 0.5
DEFAULT_MIN_SPEECH_MS = 250.0

#: A clip larger than this is refused rather than read into memory. 5.0 s of 48 kHz mono PCM16
#: is 480 044 B; 64 MiB is ~11 minutes, far past anything the firmware writes.
MAX_WAV_BYTES = 64 * 1024 * 1024


class PurgeError(RuntimeError):
    """A clip could not be decided about. Never raised to mean "no speech"."""


class UnreadableClip(PurgeError):
    """The WAV could not be parsed. The file is left alone: undecided is not innocent."""


class PrivacyLeak(AssertionError):
    """A record carried a field this pipeline is not allowed to emit. Fail loudly, never trim."""


# --------------------------------------------------------------------------- reading WAVs

def read_wav_mono(path: str) -> Tuple[np.ndarray, int]:
    """`path` -> (float32 samples in [-1, 1], sample rate). Mono by channel mean.

    Stdlib `wave` only: this must work on a bench with no soundfile/libsndfile, and the firmware
    writes plain 16-bit PCM. Anything else -- float WAVs, 24-bit, IEEE extensible -- raises
    rather than being guessed at, because a misread body is a misread *decision*.
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise UnreadableClip("%s: cannot stat: %s" % (path, exc)) from exc
    if size > MAX_WAV_BYTES:
        raise UnreadableClip("%s: %d B exceeds the %d B read cap" % (path, size, MAX_WAV_BYTES))
    try:
        with wave.open(path, "rb") as wav:
            channels, width, rate, frames = (wav.getnchannels(), wav.getsampwidth(),
                                             wav.getframerate(), wav.getnframes())
            if width != 2:
                raise UnreadableClip("%s: sample width %d B, only 16-bit PCM is read here"
                                     % (path, width))
            if rate <= 0 or channels <= 0:
                raise UnreadableClip("%s: header claims %d Hz / %d channels"
                                     % (path, rate, channels))
            raw = wav.readframes(frames)
    except UnreadableClip:
        raise
    except (wave.Error, EOFError, OSError, ValueError) as exc:
        raise UnreadableClip("%s: %s" % (path, exc)) from exc

    pcm = np.frombuffer(raw, dtype="<i2")
    usable = (pcm.size // channels) * channels
    pcm = pcm[:usable]
    if pcm.size == 0:
        raise UnreadableClip("%s: no sample frames in body" % path)
    samples = pcm.reshape(-1, channels).astype(np.float32).mean(axis=1) / 32768.0
    return samples, rate


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """Digest of the bytes on disk, streamed. This is the tombstone the receipt carries."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(chunk), b""):
                h.update(block)
    except OSError as exc:
        raise UnreadableClip("%s: cannot read for digest: %s" % (path, exc)) from exc
    return h.hexdigest()


# --------------------------------------------------------------------------- the detector

@dataclass(frozen=True)
class SpeechSpan:
    """One stretch of speech, in seconds from the start of the clip."""
    start_s: float
    end_s: float
    peak_prob: float

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    def as_pair(self) -> List[float]:
        return [round(self.start_s, 3), round(self.end_s, 3)]


@dataclass(frozen=True)
class VadDecision:
    """What the detector concluded, and nothing that could reconstruct the sound.

    `peak_prob` is the maximum per-frame speech probability, kept because an operator tuning a
    threshold needs to know whether a clip was refused at 0.49 or at 0.02. The per-frame vector
    itself is NOT kept: it is a coarse envelope of the utterance and it leaves this function
    collapsed.
    """
    speech: bool
    peak_prob: float
    speech_s: float
    spans: Tuple[SpeechSpan, ...]
    engine: str


class BandEnergyVAD:
    """Voiced-band energy + harmonicity, stdlib and numpy, no model file.

    ⚠️THIS IS THE FALLBACK, NOT THE STANDARD. Silero v5 is the engine this pipeline is specified
    against; this exists so a node with no onnxruntime still enforces the policy instead of
    silently keeping voice clips, and so the pipeline's own tests do not depend on a model
    download. Its name is written to every receipt it produces for exactly that reason.

    Three things have to agree before a frame is called speech, because any one of them alone
    fires on something the field is full of:

      * **energy above the clip's own noise floor, or above an absolute one** -- alone this
        labels wind and a passing car;
      * **voiced-band concentration** (300-3400 Hz share of frame energy) -- alone this labels
        any mid-band tone, e.g. an engine harmonic or a siren;
      * **harmonicity**, a normalised autocorrelation peak at a plausible pitch period -- alone
        this labels a whistle, a hum and a tyre resonance;
      * **spectral spread**, i.e. no single FFT bin owning the frame -- this is what separates
        a voice from the sirens and alarms the first three gates happily call speech.

    A gunshot fails the first two (broadband, impulsive), rain and hiss fail the second and
    third, a tonal machine fails the flatness/pitch-stability part of the third. A voice passes
    all three, which is the only claim being made.
    """

    name = "band_energy_fallback_v1"

    def __init__(self, threshold: float = DEFAULT_THRESHOLD,
                 min_speech_ms: float = DEFAULT_MIN_SPEECH_MS) -> None:
        self.threshold = float(threshold)
        self.min_speech_ms = float(min_speech_ms)

    # -- framing ---------------------------------------------------------------------------

    @staticmethod
    def _frames(samples: np.ndarray, rate: int) -> Tuple[np.ndarray, int, int]:
        n = max(16, int(round(rate * FRAME_MS / 1000.0)))
        hop = max(1, int(round(rate * HOP_MS / 1000.0)))
        if samples.size < n:
            pad = np.zeros(n - samples.size, dtype=np.float32)
            samples = np.concatenate([samples, pad])
        count = 1 + (samples.size - n) // hop
        idx = np.arange(n)[None, :] + hop * np.arange(count)[:, None]
        return samples[idx], n, hop

    def _frame_probs(self, samples: np.ndarray, rate: int) -> Tuple[np.ndarray, int]:
        frames, n, hop = self._frames(samples.astype(np.float32, copy=False), rate)
        window = np.hanning(n).astype(np.float32)
        win = frames * window

        spec = np.abs(np.fft.rfft(win, axis=1)) ** 2
        freqs = np.fft.rfftfreq(n, 1.0 / rate)
        total = spec.sum(axis=1) + 1e-12
        band = spec[:, (freqs >= VOICE_BAND_HZ[0]) & (freqs <= VOICE_BAND_HZ[1])].sum(axis=1)
        band_ratio = band / total

        # ⚠️THE ENERGY GATE TAKES THE BETTER OF TWO MEASURES, NOT THE RELATIVE ONE. A clip that
        # is speech end to end has its own 20th percentile sitting INSIDE the utterance, so a
        # purely relative SNR reports ~0 dB and the loudest possible privacy failure -- someone
        # talking for the whole five seconds -- is the one that scores lowest. The absolute term
        # is what catches it; the relative term is what still finds one quiet word in silence.
        rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1)) + 1e-12
        floor = np.percentile(rms, 20)
        snr_db = 20.0 * np.log10(rms / max(floor, 1e-9))
        level_db = 20.0 * np.log10(rms)

        harm = self._harmonicity(win, rate)

        # ⚠️A NARROW TONE IS NOT A VOICE. A siren, a whistle and a reversing alarm all sit in
        # the voice band and all autocorrelate beautifully, so band + harmonicity alone purge
        # them -- destroying exactly the acoustic events this project exists to record. Speech
        # spreads its energy over a pitch comb and formants; no single bin owns it.
        peak_share = spec.max(axis=1) / total
        top_share = np.sort(spec, axis=1)[:, -8:].sum(axis=1) / total

        # Each term is a soft gate in [0, 1]; the product is the probability. A product, not a
        # mean: a frame that fails one test outright must not be rescued by the others.
        p_energy = np.maximum(_ramp(snr_db, 4.0, 12.0), _ramp(level_db, -52.0, -38.0))
        p_band = _ramp(band_ratio, 0.25, 0.55)
        p_harm = _ramp(harm, 0.25, 0.55)
        p_spread = (1.0 - _ramp(peak_share, 0.32, 0.55)) * (1.0 - _ramp(top_share, 0.88, 0.98))
        probs = np.clip(p_energy * p_band * p_harm * p_spread, 0.0, 1.0) ** (1.0 / 4.0)
        # Absolute silence is never speech, whatever the clip's own floor says.
        probs[rms < 1e-4] = 0.0
        return probs.astype(np.float32), hop

    @staticmethod
    def _harmonicity(win: np.ndarray, rate: int) -> np.ndarray:
        """Normalised autocorrelation peak inside the pitch range, per frame.

        FFT-based: a 32 ms frame at 48 kHz is 1 536 samples, and the direct correlation over 600
        candidate lags per frame is what made an earlier version of this too slow to run on a
        whole pool.
        """
        n = win.shape[1]
        size = 1 << int(np.ceil(np.log2(2 * n)))
        spec = np.fft.rfft(win, n=size, axis=1)
        acf = np.fft.irfft(np.abs(spec) ** 2, n=size, axis=1)[:, :n]
        zero = acf[:, :1] + 1e-12
        lo = max(1, int(rate / PITCH_HZ[1]))
        hi = min(n - 1, int(rate / PITCH_HZ[0]))
        if hi <= lo:
            return np.zeros(win.shape[0], dtype=np.float32)
        return np.clip(acf[:, lo:hi + 1].max(axis=1) / zero[:, 0], 0.0, 1.0).astype(np.float32)

    # -- decision --------------------------------------------------------------------------

    def detect(self, samples: np.ndarray, rate: int) -> VadDecision:
        if rate <= 0:
            raise PurgeError("sample rate %r is not a rate" % (rate,))
        if samples.size == 0:
            return VadDecision(False, 0.0, 0.0, (), self.name)
        probs, hop = self._frame_probs(samples, rate)
        return decision_from_probs(probs, hop / float(rate), FRAME_MS / 1000.0,
                                   self.threshold, self.min_speech_ms, self.name)


def _ramp(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Linear soft gate: 0 at or below `lo`, 1 at or above `hi`."""
    return np.clip((np.asarray(x, dtype=np.float64) - lo) / max(hi - lo, 1e-9), 0.0, 1.0)


def decision_from_probs(probs: Sequence[float], hop_s: float, frame_s: float,
                        threshold: float, min_speech_ms: float, engine: str) -> VadDecision:
    """Per-frame probabilities -> spans, a peak and a verdict.

    Shared by every engine so that "what counts as speech" is one rule: frames at or above
    `threshold`, merged across gaps shorter than `JOIN_GAP_MS` (a stop consonant is a gap), and
    any merged span shorter than `min_speech_ms` discarded (a door latch is not a word).
    """
    arr = np.asarray(probs, dtype=np.float64)
    if arr.size == 0:
        return VadDecision(False, 0.0, 0.0, (), engine)
    peak = float(arr.max())
    hot = arr >= float(threshold)

    raw: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for i, on in enumerate(hot):
        if on and start is None:
            start = i
        elif not on and start is not None:
            raw.append((start, i - 1))
            start = None
    if start is not None:
        raw.append((start, int(hot.size) - 1))

    merged: List[Tuple[int, int]] = []
    gap_frames = max(1, int(round((JOIN_GAP_MS / 1000.0) / max(hop_s, 1e-9))))
    for span in raw:
        if merged and span[0] - merged[-1][1] <= gap_frames:
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)

    spans: List[SpeechSpan] = []
    for lo, hi in merged:
        start_s = lo * hop_s
        end_s = hi * hop_s + frame_s
        if (end_s - start_s) * 1000.0 < float(min_speech_ms):
            continue
        spans.append(SpeechSpan(start_s, end_s, float(arr[lo:hi + 1].max())))

    speech_s = float(sum(s.duration_s for s in spans))
    return VadDecision(bool(spans), peak, speech_s, tuple(spans), engine)


def load_vad(engine: str = "auto", threshold: float = DEFAULT_THRESHOLD,
             min_speech_ms: float = DEFAULT_MIN_SPEECH_MS):
    """Return a detector with a `.detect(samples, rate) -> VadDecision` and a `.name`.

    `auto` prefers Silero v5 (`hear/privacy/silero_vad.py`, which needs onnxruntime and the
    pinned model) and falls back to `BandEnergyVAD` when it is not installed -- a node without
    the runtime must still enforce the policy rather than quietly keep voice clips. `silero`
    demands it and raises if it is missing; `band_energy` demands the fallback. Whichever runs,
    its name is on every receipt.
    """
    engine = (engine or "auto").strip().lower()
    if engine in ("band_energy", "fallback"):
        return BandEnergyVAD(threshold, min_speech_ms)
    if engine in ("auto", "silero"):
        try:
            from . import silero_vad  # type: ignore[attr-defined]
        except ImportError as exc:
            if engine == "silero":
                raise PurgeError("the silero engine was demanded and is not installed: %s" % exc)
            return BandEnergyVAD(threshold, min_speech_ms)
        return silero_vad.load_vad(threshold=threshold, min_speech_ms=min_speech_ms)
    raise PurgeError("unknown vad engine %r; known: auto, silero, band_energy" % (engine,))


# --------------------------------------------------------------------------- the receipt

@dataclass(frozen=True)
class PurgeReceipt:
    """What is allowed to outlive a voice clip.

    Read it as a death certificate: it says a file existed, on which node, for how long, that a
    detector was this sure it held speech, and that its bytes hashed to this. It says nothing
    that could be turned back into sound, and `assert_privacy_safe()` enforces that on the way
    out rather than trusting this class to stay honest as it is edited.
    """
    timestamp: str
    node: str
    clip: str
    duration_s: float
    peak_speech_prob: float
    speech_s: float
    spans: Tuple[SpeechSpan, ...]
    purged_sha256: str
    vad_engine: str
    dry_run: bool = False
    purge_reason: str = PURGE_REASON

    def as_record(self) -> Dict[str, Any]:
        rec: Dict[str, Any] = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "timestamp": self.timestamp,
            "node": self.node,
            "clip": self.clip,
            "duration_s": round(float(self.duration_s), 3),
            "peak_speech_prob": round(float(self.peak_speech_prob), 4),
            "speech_s": round(float(self.speech_s), 3),
            "speech_spans_s": [s.as_pair() for s in self.spans],
            "purged_sha256": self.purged_sha256,
            "purge_reason": self.purge_reason,
            "vad_engine": self.vad_engine,
            "zero_audio_retained": True,
            "dry_run": bool(self.dry_run),
        }
        assert_privacy_safe(rec)
        return rec


def assert_privacy_safe(record: Dict[str, Any]) -> Dict[str, Any]:
    """Raise `PrivacyLeak` unless `record` is on the allow-list AND carries no vector.

    Two refusals, because there are two ways the leak arrives. A NEW key is the obvious one:
    `transcript`, `mfcc`, `speaker_id`. A new VALUE under an old key is the quiet one -- a
    `speech_spans_s` that grows from pairs of seconds into a per-frame probability envelope is
    still a legal key and is a coarse recording of the utterance's rhythm. So any numeric
    sequence longer than a span pair, and any nested structure, is refused too.
    """
    for key, value in record.items():
        if key not in RECEIPT_KEYS:
            raise PrivacyLeak(
                "receipt field %r is not on RECEIPT_KEYS. If this is genuinely needed, add it "
                "there in a reviewed change and say why it cannot reconstruct audio." % (key,))
        low = key.lower()
        for bad in FORBIDDEN_SUBSTRINGS:
            if bad in low and key not in NAME_EXEMPT:
                raise PrivacyLeak("receipt field %r names %r, which this pipeline never "
                                  "retains" % (key, bad))
        if key == "speech_spans_s":
            if not isinstance(value, list):
                raise PrivacyLeak("speech_spans_s must be a list of [start, end] pairs")
            for pair in value:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise PrivacyLeak("speech_spans_s carries %r, not a [start, end] pair"
                                      % (pair,))
                for edge in pair:
                    if not isinstance(edge, (int, float)) or isinstance(edge, bool):
                        raise PrivacyLeak("speech span edge %r is not a number" % (edge,))
            continue
        if isinstance(value, (list, tuple, dict, bytes, bytearray, np.ndarray)):
            raise PrivacyLeak(
                "receipt field %r carries a %s. A per-frame vector is a coarse recording; "
                "receipts hold scalars and span pairs only." % (key, type(value).__name__))
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise PrivacyLeak("receipt field %r carries an unserialisable %s"
                              % (key, type(value).__name__))
    return record


def append_receipt(path: str, receipt: PurgeReceipt) -> str:
    """Append one JSONL line, flushed and fsynced before the call returns.

    The audit line is written AFTER the bytes are gone and is fsynced on its own, because the
    failure that matters is a destroyed clip with no record of the destruction. A duplicate line
    after a crash is recoverable by a reader; a missing one is not recoverable by anybody.
    """
    path = os.path.expanduser(path)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    line = json.dumps(receipt.as_record(), sort_keys=True, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
    return path


def read_receipts(path: str) -> List[Dict[str, Any]]:
    """Every receipt in an audit log. Blank and unparseable lines are skipped, not guessed at."""
    path = os.path.expanduser(path)
    out: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


# --------------------------------------------------------------------------- destruction

def shred_file(path: str, passes: int = 1) -> None:
    """Overwrite in place, fsync, truncate, unlink. See the module docstring's caveat.

    The overwrite happens before the unlink so that a reader holding the path sees zeros rather
    than audio even if the unlink fails; the truncate-to-zero is what makes a partially
    overwritten file unusable if the process dies between the two.
    """
    path = os.path.abspath(os.path.expanduser(path))
    try:
        size = os.path.getsize(path)
        flags = os.O_WRONLY
        fd = os.open(path, flags)
        try:
            for i in range(max(1, int(passes)) + 1):
                os.lseek(fd, 0, os.SEEK_SET)
                written = 0
                while written < size:
                    block = min(1 << 20, size - written)
                    chunk = os.urandom(block) if i == 0 else b"\x00" * block
                    os.write(fd, chunk)
                    written += block
                os.fsync(fd)
            os.ftruncate(fd, 0)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise PurgeError("%s: could not overwrite before unlink: %s" % (path, exc)) from exc
    try:
        os.unlink(path)
    except OSError as exc:
        raise PurgeError("%s: overwritten but not unlinked: %s" % (path, exc)) from exc
    _fsync_dir(os.path.dirname(path))


def _fsync_dir(path: str) -> None:
    """Make the unlink itself durable; without it a crash can resurrect the directory entry."""
    try:
        fd = os.open(path or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _zero(samples: np.ndarray) -> None:
    """Drop the waveform the moment the decision is made. Cheap, and it is the stated policy."""
    try:
        samples[...] = 0.0
    except (ValueError, TypeError):
        pass


# --------------------------------------------------------------------------- the pipeline

def node_of(path: str, default: str = "unknown") -> str:
    """Node id out of a clip name, via the one parser that already knows every shipped shape.

    `hear.clips.parse_clip_name` takes the NODE-side string (`/clips/<name>`), so the basename is
    put back under that prefix rather than the name being re-parsed here with a second regex
    that would drift from the firmware's four naming generations.
    """
    try:
        from .. import clips as C
        return str(C.parse_clip_name(C.CLIP_DIR + "/" + os.path.basename(path))["node"])
    except Exception:
        return default


def _now_iso(now: Optional[dt.datetime] = None) -> str:
    stamp = now or dt.datetime.now(dt.timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


@dataclass
class ClipOutcome:
    """One clip's fate: kept, purged, would-be-purged, or undecidable."""
    path: str
    status: str                       # "kept" | "purged" | "would_purge" | "error"
    receipt: Optional[PurgeReceipt] = None
    detail: str = ""


@dataclass
class PurgeReport:
    """The run, as counts plus the receipts it produced. No audio, by construction."""
    scanned: int = 0
    purged: int = 0
    would_purge: int = 0
    kept: int = 0
    errors: int = 0
    dry_run: bool = False
    vad_engine: str = ""
    audit_log: Optional[str] = None
    receipts: List[PurgeReceipt] = field(default_factory=list)
    outcomes: List[ClipOutcome] = field(default_factory=list)

    def as_record(self) -> Dict[str, Any]:
        return {"scanned": self.scanned, "purged": self.purged, "would_purge": self.would_purge,
                "kept": self.kept, "errors": self.errors, "dry_run": self.dry_run,
                "vad_engine": self.vad_engine, "audit_log": self.audit_log}


def inspect_samples(samples: np.ndarray, rate: int, vad=None, *, threshold: float = DEFAULT_THRESHOLD,
                    min_speech_ms: float = DEFAULT_MIN_SPEECH_MS) -> VadDecision:
    """Run a detector over a waveform already in memory. Nothing is written, nothing is kept."""
    vad = vad or load_vad("auto", threshold, min_speech_ms)
    return vad.detect(np.asarray(samples, dtype=np.float32), int(rate))


def purge_wav_bytes(data: bytes, node: str = "stream", clip: str = "<stream>", vad=None, *,
                    threshold: float = DEFAULT_THRESHOLD,
                    min_speech_ms: float = DEFAULT_MIN_SPEECH_MS,
                    audit_log: Optional[str] = None,
                    now: Optional[dt.datetime] = None) -> Optional[PurgeReceipt]:
    """Decide about a WAV that never reached the disk. Returns a receipt iff it held speech.

    This is the streaming door: a drain that has bytes in hand can ask "may I store this?"
    before it writes anything, which is strictly better than writing and then shredding. The
    digest is over the bytes as handed in, so the receipt means the same thing either way, and
    the caller's `data` is the only copy -- this function keeps none.
    """
    digest = hashlib.sha256(data).hexdigest()
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels, width, rate, frames = (wav.getnchannels(), wav.getsampwidth(),
                                             wav.getframerate(), wav.getnframes())
            if width != 2:
                raise UnreadableClip("stream: sample width %d B, only 16-bit PCM is read here"
                                     % width)
            raw = wav.readframes(frames)
    except UnreadableClip:
        raise
    except (wave.Error, EOFError, ValueError) as exc:
        raise UnreadableClip("stream: %s" % exc) from exc

    pcm = np.frombuffer(raw, dtype="<i2")
    usable = (pcm.size // max(channels, 1)) * max(channels, 1)
    samples = (pcm[:usable].reshape(-1, max(channels, 1)).astype(np.float32).mean(axis=1)
               / 32768.0)
    duration_s = samples.size / float(rate or 1)
    decision = inspect_samples(samples, rate, vad, threshold=threshold,
                               min_speech_ms=min_speech_ms)
    _zero(samples)
    if not decision.speech:
        return None
    receipt = PurgeReceipt(timestamp=_now_iso(now), node=node, clip=clip,
                           duration_s=duration_s, peak_speech_prob=decision.peak_prob,
                           speech_s=decision.speech_s, spans=decision.spans,
                           purged_sha256=digest, vad_engine=decision.engine, dry_run=False)
    if audit_log:
        append_receipt(audit_log, receipt)
    return receipt


def purge_clip(path: str, vad=None, *, threshold: float = DEFAULT_THRESHOLD,
               min_speech_ms: float = DEFAULT_MIN_SPEECH_MS, dry_run: bool = False,
               audit_log: Optional[str] = None, node: Optional[str] = None,
               now: Optional[dt.datetime] = None) -> ClipOutcome:
    """Decide about one WAV on disk, and destroy it if it holds speech.

    Order is deliberate and is the audit's whole basis: **digest, detect, destroy, record.** The
    digest is taken before anything else so that the receipt describes the bytes that existed;
    detection happens on the buffer already read; destruction happens before the audit line, so
    a record can never claim a purge that did not happen.
    """
    path = os.path.abspath(os.path.expanduser(path))
    vad = vad or load_vad("auto", threshold, min_speech_ms)
    try:
        digest = sha256_file(path)
        samples, rate = read_wav_mono(path)
    except PurgeError as exc:
        return ClipOutcome(path, "error", None, str(exc))

    duration_s = samples.size / float(rate or 1)
    try:
        decision = vad.detect(samples, rate)
    except Exception as exc:                                  # a detector fault is undecided
        _zero(samples)
        return ClipOutcome(path, "error", None, "vad failed: %s" % exc)
    _zero(samples)
    del samples

    if not decision.speech:
        return ClipOutcome(path, "kept", None,
                           "no speech (peak %.3f < %.3f)" % (decision.peak_prob, threshold))

    receipt = PurgeReceipt(timestamp=_now_iso(now), node=node or node_of(path),
                           clip=os.path.basename(path), duration_s=duration_s,
                           peak_speech_prob=decision.peak_prob, speech_s=decision.speech_s,
                           spans=decision.spans, purged_sha256=digest,
                           vad_engine=decision.engine, dry_run=dry_run)
    if dry_run:
        return ClipOutcome(path, "would_purge", receipt, "dry run: file untouched")
    try:
        shred_file(path)
    except PurgeError as exc:
        return ClipOutcome(path, "error", None, str(exc))
    if audit_log:
        append_receipt(audit_log, receipt)
    return ClipOutcome(path, "purged", receipt, "shredded and unlinked")


def iter_clips(root: str) -> Iterator[str]:
    """Every `.wav` under `root`, depth-first and sorted, so two runs report in one order."""
    root = os.path.abspath(os.path.expanduser(root))
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if name.lower().endswith(".wav"):
                yield os.path.join(dirpath, name)


def scan_pool(root: str, *, engine: str = "auto", threshold: float = DEFAULT_THRESHOLD,
              min_speech_ms: float = DEFAULT_MIN_SPEECH_MS, dry_run: bool = False,
              audit_log: Optional[str] = None, vad=None,
              now: Optional[dt.datetime] = None,
              on_outcome=None) -> PurgeReport:
    """Walk a clip pool, purging every WAV that holds speech.

    `audit_log` defaults to `<root>/purged_receipts.jsonl` for a live run and is never written
    in a dry run. The audit log itself is never a scan target -- it is JSONL, not a WAV -- so a
    run cannot purge its own evidence.
    """
    root = os.path.abspath(os.path.expanduser(root))
    vad = vad or load_vad(engine, threshold, min_speech_ms)
    if audit_log is None and not dry_run:
        audit_log = os.path.join(root, RECEIPT_NAME)
    report = PurgeReport(dry_run=dry_run, vad_engine=getattr(vad, "name", engine),
                         audit_log=None if dry_run else audit_log)
    for path in iter_clips(root):
        outcome = purge_clip(path, vad, threshold=threshold, min_speech_ms=min_speech_ms,
                             dry_run=dry_run, audit_log=None if dry_run else audit_log, now=now)
        report.scanned += 1
        report.outcomes.append(outcome)
        if outcome.receipt is not None:
            report.receipts.append(outcome.receipt)
        if outcome.status == "purged":
            report.purged += 1
        elif outcome.status == "would_purge":
            report.would_purge += 1
        elif outcome.status == "kept":
            report.kept += 1
        else:
            report.errors += 1
        if on_outcome is not None:
            on_outcome(outcome)
    return report
