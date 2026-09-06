#!/usr/bin/env python3
"""Log-mel sketch: what a node ships instead of audio, sized to one Meshtastic packet.

A Meshtastic payload is 237 bytes, ~200 usable after protobuf framing. The sketch is
MEL_BANDS x FRAMES int8 values -- 160 B at the default 20x8 -- leaving room for a header.

WHY A SKETCH AND NOT A LEARNED EMBEDDING. A learned embedding is only as general as the classes
it was trained on, and it goes stale the day you add a new sound. A log-mel sketch is just a
coarse spectrogram: the node commits to no interpretation, and the central side can train new
classifiers on the same bytes forever -- gunshots today, cicadas next, whatever after. That is
the whole point of putting the hard maths elsewhere.

⚠️QUANTISATION IS PER-EVENT AND THE SCALE MUST TRAVEL. Levels span the noise floor to a clipped
rifle report; a fixed int8 scale would either saturate or waste its range. The reference level is
sent in the header so the central side can undo it. Without that the sketch carries shape but not
amplitude, and amplitude is the single strongest feature this project has measured.
"""
from __future__ import annotations

import math
import struct
from typing import Dict, Optional, Tuple

import numpy as np

MEL_BANDS = 20
FRAMES = 8
F_LO, F_HI = 300.0, 20000.0
HOP_S = 0.004
NFFT = 256
MESHTASTIC_PAYLOAD = 237


def _hz_to_mel(f):
    return 2595.0 * np.log10(1.0 + np.asarray(f, float) / 700.0)


def _mel_to_hz(m):
    return 700.0 * (10 ** (np.asarray(m, float) / 2595.0) - 1.0)


def mel_filterbank(fs: float, nfft: int = NFFT, bands: int = MEL_BANDS,
                   f_lo: float = F_LO, f_hi: float = F_HI) -> np.ndarray:
    hi = min(f_hi, fs / 2.0 * 0.98)
    edges = _mel_to_hz(np.linspace(_hz_to_mel(f_lo), _hz_to_mel(hi), bands + 2))
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    fb = np.zeros((bands, len(freqs)))
    for b in range(bands):
        lo, mid, up = edges[b], edges[b + 1], edges[b + 2]
        if up <= lo:
            continue
        rise = (freqs - lo) / max(mid - lo, 1e-9)
        fall = (up - freqs) / max(up - mid, 1e-9)
        fb[b] = np.clip(np.minimum(rise, fall), 0.0, None)
        s = fb[b].sum()
        if s > 0:
            fb[b] /= s
    return fb


def sketch(x: np.ndarray, fs: float, bands: int = MEL_BANDS, frames: int = FRAMES,
           hop_s: float = HOP_S, nfft: int = NFFT) -> Tuple[np.ndarray, float]:
    """(int8 [bands x frames], ref_db). Frames start at the sample the caller passes as index 0."""
    x = np.asarray(x, float)
    fb = mel_filterbank(fs, nfft, bands)
    hop = max(1, int(hop_s * fs))
    win = np.hanning(nfft)
    out = np.zeros((bands, frames))
    for t in range(frames):
        s = t * hop
        seg = x[s:s + nfft]
        if len(seg) < nfft:
            seg = np.pad(seg, (0, nfft - len(seg)))
        P = np.abs(np.fft.rfft(seg * win, nfft)) ** 2
        out[:, t] = fb @ P
    db = 10.0 * np.log10(out + 1e-12)
    ref = float(db.max())
    # 0.5 dB per step over a 64 dB window: enough to hold a spectrum's shape, and 64 dB is
    # wider than the useful dynamic range between this site's noise floor and a clipped report.
    q = np.clip(np.round((db - ref) * 2.0), -128, 127).astype(np.int8)
    return q, ref


def pack(node_us: int, ref_db: float, peak: int, q: np.ndarray, flags: int = 0) -> bytes:
    """Wire format. Header is 12 B; the sketch is bands*frames int8.

    `node_us` is microseconds within the PPS second -- the whole second comes from the mesh's own
    clock, so the timestamp costs 4 bytes rather than 8 and still resolves to 1 us.
    """
    body = q.astype(np.int8).tobytes()
    hdr = struct.pack("<IhHBBH", int(node_us) & 0xFFFFFFFF, int(round(ref_db * 4)),
                      min(int(peak), 0xFFFF), q.shape[0], q.shape[1], flags & 0xFFFF)
    return hdr + body


def unpack(b: bytes) -> Dict:
    node_us, ref4, peak, bands, frames, flags = struct.unpack("<IhHBBH", b[:12])
    q = np.frombuffer(b[12:12 + bands * frames], dtype=np.int8).reshape(bands, frames)
    return {"node_us": node_us, "ref_db": ref4 / 4.0, "peak": peak,
            "flags": flags, "q": q, "db": q.astype(float) / 2.0 + ref4 / 4.0}


def wire_size(bands: int = MEL_BANDS, frames: int = FRAMES) -> int:
    return 12 + bands * frames


def fits_meshtastic(bands: int = MEL_BANDS, frames: int = FRAMES, overhead: int = 37) -> bool:
    """237 B payload minus protobuf/portnum overhead. Default 20x8 leaves headroom."""
    return wire_size(bands, frames) <= MESHTASTIC_PAYLOAD - overhead
