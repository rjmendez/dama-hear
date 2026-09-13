"""Rational resampling from the node's acquisition rate to a model's native rate.

The nodes acquire at 48 kHz and EfficientAT wants 32 kHz: L=2 M=3, a decimation, so every band the
model reads is measurement. The header rate is snapped to 48 kHz within SNAP_TOLERANCE; a rate
nobody configured is refused rather than stretched to the nearest.
"""

from __future__ import annotations

import math
from typing import Any, Dict

#: The rate the nodes clock the microphone at.
ACQ_RATE_HZ = 48000.0
#: How far a measured header rate may sit from ACQ_RATE_HZ and still be snapped to it.
SNAP_TOLERANCE = 0.005
#: Kaiser stopband attenuation, dB. Puts the fold under the int16 floor of the source.
STOPBAND_DB = 72.0
#: Transition width as a fraction of the passband edge.
TRANSITION = 0.10


class RateRefused(Exception):
    """The clip's rate is not the acquisition rate, so it is not resampled at all."""


def snap(fs_hz: float) -> float:
    """Measured header rate -> ACQ_RATE_HZ. Raises RateRefused for anything else."""
    if not fs_hz or fs_hz <= 0:
        raise RateRefused("rate %r is not a rate" % (fs_hz,))
    if abs(float(fs_hz) - ACQ_RATE_HZ) <= ACQ_RATE_HZ * SNAP_TOLERANCE:
        return ACQ_RATE_HZ
    raise RateRefused("%.1f Hz is not within %.1f%% of the %g Hz acquisition rate; refused "
                      "rather than resampled" % (fs_hz, SNAP_TOLERANCE * 100, ACQ_RATE_HZ))


def _kaiser_beta(att_db: float) -> float:
    if att_db > 50:
        return 0.1102 * (att_db - 8.7)
    if att_db >= 21:
        return 0.5842 * (att_db - 21) ** 0.4 + 0.07886 * (att_db - 21)
    return 0.0


def design(L: int, M: int):
    """Windowed-sinc low-pass for an L/M polyphase resampler, unity gain through zero-stuffing."""
    import numpy as np
    cutoff = 0.5 / max(L, M)
    width = cutoff * TRANSITION * 2.0
    n = max(int(math.ceil((STOPBAND_DB - 8.0) / (2.285 * 2.0 * math.pi * width))), 8)
    if n % 2 == 0:
        n += 1
    k = np.arange(n) - (n - 1) / 2.0
    h = 2.0 * cutoff * np.sinc(2.0 * cutoff * k) * np.kaiser(n, _kaiser_beta(STOPBAND_DB))
    return (h * L / h.sum()).astype(np.float64)


def resample(x, fs_in: float, fs_out: float) -> Dict[str, Any]:
    """-> {"pcm", "fs_hz", "fs_source_hz", "L", "M", "taps"}. fs_in must snap to ACQ_RATE_HZ."""
    import numpy as np
    src = snap(fs_in)
    dst = float(fs_out)
    if dst <= 0 or dst != int(dst):
        raise RateRefused("output rate %r is not a positive integer rate" % (fs_out,))
    g = math.gcd(int(src), int(dst))
    L, M = int(dst) // g, int(src) // g
    x = np.asarray(x, dtype=np.float64)
    if L == 1 and M == 1:
        y, h = x.copy(), np.zeros(0)
    else:
        h = design(L, M)
        up = np.zeros(len(x) * L, dtype=np.float64)
        up[::L] = x
        y = np.convolve(up, h, mode="same")[::M]
    return {"pcm": y.astype(np.float32), "fs_hz": dst, "fs_source_hz": float(fs_in),
            "L": L, "M": M, "taps": int(len(h))}
