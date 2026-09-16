"""Synthetic acoustic fixtures for the speech-privacy tests: voice-like, and deliberately not.

⚠️CONSTRUCTED, NEVER RECORDED. Nothing here is captured audio. Every waveform is generated from a
stated seed by this file, so a probability asserted in a test is a statement about the detector
and not about whoever was standing near a node on the day a clip was cut. That is the only kind
of speech fixture this repository is allowed to hold: a speech test corpus made of real voices is
the exact artefact `hear/privacy` exists to destroy.

⚠️THE SPEECH SIGNAL IS NOT A TONE, AND THAT MATTERS. Measured against the real Silero VAD v5 ONNX
on 2 s at 16 kHz (62 chunks of 512, streaming state kept):

    signal                                    max p     mean p   frac p>0.5
    speech_like()   (this file)               0.998     0.747      0.82
    formant_drone() (static formants)         0.863     0.233      0.16
    silence()                                 0.009     0.004      0.00
    gaussian_noise() quiet / loud             0.041     0.012      0.00
    pink_noise()    quiet / loud              0.028     0.013      0.00
    bird_chirps()   3.5 -> 7 kHz up-sweeps    0.012     0.003      0.00
    tone(1000)                                0.006     0.001      0.00

A stationary source-filter buzz is NOT enough: `formant_drone` holds its formants still and the
detector is unconvinced (max 0.86, but only 16 % of chunks over threshold). What earns 0.998 is
the time structure -- formant trajectories that glide between vowel targets, fricative and plosive
consonants between them, and a syllable rate near 4 Hz. `formant_drone` is kept precisely so a
test can show the difference, and so nobody "simplifies" `speech_like` into a buzz and leaves a
detector test that passes on a signal the detector does not actually call speech.
"""
from __future__ import annotations

import struct
from typing import Iterable, Sequence, Tuple

import numpy as np

MODEL_RATE = 16000
ACQUISITION_RATE = 48000

# (start Hz, end Hz, bandwidth Hz, gain) tracks that glide across one syllable: rough
# a->e, i->a and o->u trajectories. Numbers are round on purpose; nothing here is measured
# off a person.
VOWEL_TRACKS: Tuple[Tuple[Tuple[float, float, float, float], ...], ...] = (
    ((700.0, 500.0, 80.0, 1.0), (1220.0, 1700.0, 100.0, 0.7), (2600.0, 2500.0, 140.0, 0.4)),
    ((300.0, 700.0, 70.0, 1.0), (2300.0, 1200.0, 110.0, 0.7), (3000.0, 2600.0, 160.0, 0.35)),
    ((450.0, 320.0, 75.0, 1.0), (900.0, 800.0, 100.0, 0.8), (2400.0, 2300.0, 150.0, 0.3)),
)


# ------------------------------------------------------------------ primitives

def _norm(x: np.ndarray, peak: float = 0.6) -> np.ndarray:
    m = float(np.max(np.abs(x))) if x.size else 0.0
    if m == 0.0:
        return np.zeros_like(x, dtype=np.float32)
    return (peak * x / m).astype(np.float32)


def _resonate(src: np.ndarray, fc: float, bw: float, fs: int) -> np.ndarray:
    """One two-pole resonator, written out rather than imported.

    scipy.signal.lfilter would do this, but the fixtures are also loaded by the firmware-side
    checks that run on a bare interpreter, and a resonator is four lines.
    """
    r = float(np.exp(-np.pi * bw / fs))
    a1, a2 = -2.0 * r * np.cos(2.0 * np.pi * fc / fs), r * r
    y = np.zeros(len(src), dtype=np.float64)
    y1 = y2 = 0.0
    g = 1.0 - r
    for i, s in enumerate(src):
        cur = g * s - a1 * y1 - a2 * y2
        y[i] = cur
        y2, y1 = y1, cur
    return y


def _glide(src: np.ndarray, tracks: Sequence[Tuple[float, float, float, float]],
           fs: int, block: int = 128) -> np.ndarray:
    """Formants that move: the filter is re-tuned every `block` samples across the syllable."""
    n = len(src)
    out = np.zeros(n, dtype=np.float64)
    for start in range(0, n, block):
        seg = src[start:start + block]
        if not len(seg):
            break
        frac = start / max(n - 1, 1)
        acc = np.zeros(len(seg), dtype=np.float64)
        for f_start, f_end, bw, gain in tracks:
            acc += gain * _resonate(seg, f_start + (f_end - f_start) * frac, bw, fs)
        out[start:start + len(seg)] = acc
    return out


def _glottal(n: int, f0: float, fs: int, rng: np.random.Generator) -> np.ndarray:
    """An impulse train with a little period jitter, which is what a larynx approximately is."""
    src = np.zeros(n, dtype=np.float64)
    i = 0
    while i < n:
        src[i] = 1.0
        i += max(8, int(fs / max(f0 * (1.0 + rng.normal(0.0, 0.02)), 40.0)))
    return src - src.mean()


# ------------------------------------------------------------------ the fixtures

def silence(seconds: float = 2.0, fs: int = MODEL_RATE) -> np.ndarray:
    return np.zeros(int(seconds * fs), dtype=np.float32)


def tone(freq: float = 1000.0, seconds: float = 2.0, fs: int = MODEL_RATE,
         amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * fs)) / float(fs)
    return (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def gaussian_noise(seconds: float = 2.0, fs: int = MODEL_RATE, seed: int = 3,
                   peak: float = 0.1) -> np.ndarray:
    """Stationary white noise: the wind-and-preamp floor a node hears most of the time."""
    x = np.random.default_rng(seed).normal(0.0, 1.0, int(seconds * fs))
    return _norm(x, peak)


def pink_noise(seconds: float = 2.0, fs: int = MODEL_RATE, seed: int = 4,
               peak: float = 0.3) -> np.ndarray:
    """1/f noise: rain, traffic hum and distant water, which white noise does not stand in for."""
    n = int(seconds * fs)
    rng = np.random.default_rng(seed)
    half = n // 2 + 1
    spec = rng.normal(0.0, 1.0, half) + 1j * rng.normal(0.0, 1.0, half)
    k = np.arange(half, dtype=np.float64)
    k[0] = 1.0
    return _norm(np.fft.irfft(spec / np.sqrt(k), n), peak)


def bird_chirps(seconds: float = 2.0, fs: int = MODEL_RATE, f_low: float = 3500.0,
                f_high: float = 7000.0, sweep_s: float = 0.06,
                repeat_s: float = 0.18) -> np.ndarray:
    """Repeated linear up-sweeps well above the formant range: the ambient event we must keep.

    This is the fixture that protects the purge from eating its own corpus. A VAD that fires on
    birdsong deletes the recordings the project exists to collect.
    """
    n = int(seconds * fs)
    out = np.zeros(n, dtype=np.float64)
    m = int(sweep_s * fs)
    ts = np.arange(m) / float(fs)
    rate = (f_high - f_low) / sweep_s
    phase = 2.0 * np.pi * (f_low * ts + 0.5 * rate * ts ** 2)
    sweep = np.sin(phase) * np.hanning(m)
    for start in range(0, max(n - m, 1), max(int(repeat_s * fs), 1)):
        out[start:start + m] += sweep
    return _norm(out)


def formant_drone(seconds: float = 2.0, fs: int = MODEL_RATE, f0: float = 120.0,
                  seed: int = 1) -> np.ndarray:
    """Voiced source through THREE STATIC FORMANTS. Vowel-coloured, but not speech.

    Kept as the near-miss: see the module docstring's table. It reaches 0.86 on single chunks and
    still fails to read as speech overall, which is the distinction `speech_like` has to earn.
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * fs)
    src = _glottal(n, f0, fs, rng)
    out = np.zeros(n, dtype=np.float64)
    for fc, bw, gain in ((700.0, 90.0, 1.0), (1220.0, 110.0, 0.6), (2600.0, 160.0, 0.35)):
        out += gain * _resonate(src, fc, bw, fs)
    t = np.arange(n) / float(fs)
    env = 0.3 + 0.7 * 0.5 * (1.0 - np.cos(2.0 * np.pi * 4.0 * t))
    return _norm(out * env)


def speech_like(seconds: float = 2.0, fs: int = MODEL_RATE, seed: int = 1,
                f0: float = 130.0) -> np.ndarray:
    """Consonant-vowel syllables with gliding formants: what the detector actually answers to.

    Measured mean probability 0.75 and 82 % of chunks above threshold against real Silero v5.
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * fs)
    out = np.zeros(n, dtype=np.float64)
    pos = int(0.05 * fs)
    k = 0
    while pos < n - int(0.3 * fs):
        if k % 2 == 0:                                  # fricative: high, noisy, unvoiced
            m = int(0.05 * fs)
            c = _glide(rng.normal(0.0, 1.0, m), ((4000.0, 4500.0, 1500.0, 1.0),), fs)
            c = c / (np.max(np.abs(c)) + 1e-9) * np.hanning(m) * 0.35
        else:                                           # plosive: a burst that decays
            m = int(0.02 * fs)
            c = rng.normal(0.0, 1.0, m) * np.exp(-np.arange(m) / (0.004 * fs)) * 0.5
        out[pos:pos + len(c)] += c
        pos += len(c)

        vn = int((0.12 + 0.1 * rng.random()) * fs)      # vowel: gliding formant targets
        v = _glide(_glottal(vn, f0 * (1.0 + 0.15 * rng.normal()), fs, rng),
                   VOWEL_TRACKS[k % len(VOWEL_TRACKS)], fs)
        v = v / (np.max(np.abs(v)) + 1e-9) * np.hanning(vn)
        out[pos:pos + len(v)] += v
        pos += len(v) + int((0.02 + 0.06 * rng.random()) * fs)
        k += 1
    return _norm(out)


def speech_like_48k(seconds: float = 2.0, seed: int = 1) -> np.ndarray:
    """The same syllables, band-limited, carried on the 48 kHz acquisition rate.

    Synthesised at 16 kHz and interpolated rather than synthesised at 48 kHz, so that a test of
    the 48 -> 16 decimator compares like with like: the ideal decimation of this signal is the
    16 kHz original, and any difference the test sees belongs to the decimator.
    """
    base = speech_like(seconds, MODEL_RATE, seed)
    return upsample_3x(base)


def upsample_3x(x: np.ndarray) -> np.ndarray:
    """Band-limited 16 -> 48 kHz interpolation, done in the frequency domain (no scipy needed)."""
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    spec = np.fft.rfft(x)
    out = np.zeros(3 * (n // 2) + 1, dtype=complex)
    out[:len(spec)] = spec
    y = np.fft.irfft(out, 3 * n) * 3.0
    return _norm(y, float(np.max(np.abs(x))) if n else 0.0)


def ultrasonic_marker(seconds: float = 2.0, fs: int = ACQUISITION_RATE,
                      freq: float = 20000.0, amp: float = 0.4) -> np.ndarray:
    """A 20 kHz tone that only exists above the 16 kHz model band.

    Decimated properly it disappears; decimated by throwing away two samples in three it folds to
    4 kHz and lands in the middle of the speech band. That fold is the bug this marker catches.
    """
    return tone(freq, seconds, fs, amp)


# ------------------------------------------------------------------ WAV containers

def wav_bytes(pcm: np.ndarray, fs: int = ACQUISITION_RATE, channels: int = 1) -> bytes:
    """The 44-byte canonical header the firmware writes, plus int16 samples.

    Assembled by hand rather than with `wave` so a test can state a header that disagrees with
    the payload, which is the whole point of the malformed-container cases.
    """
    d = np.clip(np.asarray(pcm) * 32767.0, -32768, 32767).astype("<i2").tobytes()
    return (b"RIFF" + struct.pack("<I", 36 + len(d)) + b"WAVEfmt " + struct.pack("<I", 16)
            + struct.pack("<HHIIHH", 1, channels, fs, fs * 2 * channels, 2 * channels, 16)
            + b"data" + struct.pack("<I", len(d)) + d)


def write_wav(path: str, pcm: np.ndarray, fs: int = ACQUISITION_RATE) -> str:
    with open(path, "wb") as fh:
        fh.write(wav_bytes(pcm, fs))
    return path


def truncated_wav(pcm: np.ndarray, fs: int = ACQUISITION_RATE, keep: int = 30) -> bytes:
    """A header cut off mid-field: the shape an interrupted SD write leaves behind."""
    return wav_bytes(pcm, fs)[:keep]


def lying_wav(pcm: np.ndarray, fs: int = ACQUISITION_RATE, claim: int = 1 << 20) -> bytes:
    """A data chunk that claims more bytes than the file holds."""
    body = bytearray(wav_bytes(pcm, fs))
    body[40:44] = struct.pack("<I", claim)
    return bytes(body)


def not_a_wav(n: int = 4096, seed: int = 9) -> bytes:
    return np.random.default_rng(seed).integers(0, 256, n, dtype=np.uint8).tobytes()


# ------------------------------------------------------------------ description

def by_name(name: str) -> np.ndarray:
    """The waveform behind a catalogue name, including the parameterised variants."""
    variants = {
        "gaussian_noise_loud": lambda: gaussian_noise(seed=11, peak=0.9),
        "pink_noise_loud": lambda: pink_noise(seed=12, peak=0.9),
        "tone_1k": tone,
    }
    if name in variants:
        return variants[name]()
    fn = globals().get(name)
    if fn is None or not callable(fn):
        raise KeyError("no fixture named %r" % (name,))
    return fn()


def catalogue() -> Iterable[Tuple[str, np.ndarray, bool]]:
    """(name, 16 kHz waveform, is it speech) for every fixture a discrimination test sweeps."""
    return tuple((n, by_name(n), n == "speech_like") for n in (
        "speech_like", "silence", "gaussian_noise", "gaussian_noise_loud", "pink_noise",
        "pink_noise_loud", "bird_chirps", "tone_1k"))
