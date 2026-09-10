"""Rational resampling to a model's native rate, and the refusal when it cannot be honest.

⚠️THE CLASSIFIER'S RATE IS NOT THE FLEET'S RATE AND NEVER WILL BE. EfficientAT wants 32 kHz; the
nodes clock the mic at 48 kHz and, before that, 16 kHz. Both directions have to be crossed, and
they are not the same operation:

    48000 -> 32000   L=2 M=3   a DECIMATION. Every band the model reads is real measurement;
                               the mic gave 24 kHz of band and 16 kHz of it survives.
    16000 -> 32000   L=2 M=1   an INTERPOLATION. The model's mel bank runs to 15 kHz and the
                               recording stops at 8. Everything above 8 kHz that the model sees
                               is a property of this filter, not of the night.

⚠️THE SECOND CASE IS THE ONE THAT LIES, SO IT IS THE ONE THAT IS LABELLED. `band_limit_hz` rides
with every resampled clip and every score derived from it. §4.6 of docs/acoustic-stack.md forbids
exactly this operation one layer down, where presenting measured silence as measurement cost
0.9473 -> 0.9141 on identical audio; here it is unavoidable (the corpus is what it is) so the
requirement is that it can never be mistaken for anything else downstream.

⚠️THE RATIO COMES FROM THE NOMINAL RATE, NOT THE MEASURED ONE. A node reports fs_clean like
16004.741 Hz; the exact ratio 32000/16004.741 is not rational in any useful sense and chasing it
would build a 32000-tap filter to correct 0.03 % of pitch -- four cents, ~1/50th of the model's
mel bin spacing. The nominal rate is snapped to, the measured rate is RECORDED, and the snap is
refused outright when the measured rate is not near a rate this fleet actually clocks.

⚠️A RATE NOBODY CONFIGURED IS REFUSED, NOT SNAPPED TO THE NEAREST. mach shipped an entire boot
headed 22624 Hz. Resampling that as though it were 16 kHz or 48 kHz would launder a known
firmware defect into audio a model answers confidently about.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

#: Rates a dama-hear node is ever configured to clock the microphone at. FS_NOMINAL, its 48 kHz
#: replacement, and the 32 kHz target that was the acquisition rate for one firmware generation.
FLEET_RATES_HZ = (16000.0, 32000.0, 48000.0)
#: How far a MEASURED rate may sit from a nominal one and still be snapped to it. The observed
#: header spread on the pool is 15968-16050 Hz, so 0.5 % (80 Hz at 16 kHz) covers it with room,
#: while 22624 Hz is 41 % away and stays refused.
SNAP_TOLERANCE = 0.005
#: Kaiser stopband attenuation for the anti-alias/interpolation filter, in dB. 72 dB puts the
#: fold below the 16-bit quantisation floor of the source, so the filter is not what limits the
#: clip. Matches the discipline of firmware/gen_decim.py, which measured -63.4 dB and said so.
STOPBAND_DB = 72.0
#: Transition width as a fraction of the passband edge. 0.10 with 72 dB gives ~130 taps at L=2,
#: which is 4 ms of group delay at 32 kHz -- immaterial for a tagger, and the clip is not used
#: for timing.
TRANSITION = 0.10


class RateRefused(Exception):
    """The clip's rate is not one this fleet clocks, so it is not resampled at all."""


def snap(fs_hz: float) -> float:
    """Measured rate -> the nominal rate it is. Raises RateRefused for anything else."""
    if not fs_hz or fs_hz <= 0:
        raise RateRefused("rate %r is not a rate" % (fs_hz,))
    for r in FLEET_RATES_HZ:
        if abs(float(fs_hz) - r) <= r * SNAP_TOLERANCE:
            return r
    raise RateRefused(
        "%.1f Hz is not within %.1f%% of any rate this fleet clocks (%s). It is not resampled: "
        "a rate nobody configured is evidence of a defect, and snapping it to the nearest would "
        "hide that defect behind a confident answer."
        % (fs_hz, SNAP_TOLERANCE * 100, ", ".join("%g" % r for r in FLEET_RATES_HZ)))


def _kaiser_beta(att_db: float) -> float:
    if att_db > 50:
        return 0.1102 * (att_db - 8.7)
    if att_db >= 21:
        return 0.5842 * (att_db - 21) ** 0.4 + 0.07886 * (att_db - 21)
    return 0.0


def design(L: int, M: int):
    """Windowed-sinc low-pass for an L/M polyphase resampler, as a numpy array.

    Cutoff is 1/(2*max(L, M)) of the L-upsampled rate: the tighter of "do not alias when
    decimating by M" and "do not admit the images the L-upsample created".
    """
    import numpy as np
    cutoff = 0.5 / max(L, M)
    width = cutoff * TRANSITION * 2.0
    n = int(math.ceil((STOPBAND_DB - 8.0) / (2.285 * 2.0 * math.pi * width)))
    n = max(n, 8)
    if n % 2 == 0:
        n += 1                                   # odd -> integer group delay, exactly (n-1)/2
    k = np.arange(n) - (n - 1) / 2.0
    h = 2.0 * cutoff * np.sinc(2.0 * cutoff * k) * np.kaiser(n, _kaiser_beta(STOPBAND_DB))
    return (h * L / h.sum()).astype(np.float64)  # unity gain THROUGH the zero-stuffing


def resample(x, fs_in: float, fs_out: float) -> Dict[str, Any]:
    """-> {"pcm", "fs_hz", "fs_source_hz", "band_limit_hz", "upsampled", "L", "M", "taps"}.

    `band_limit_hz` is the highest frequency that carries measurement: min(Nyquist in, Nyquist
    out). When it is below the output Nyquist the extra band is filter output, not sound, and
    `upsampled` says so.
    """
    import numpy as np
    src = snap(fs_in)
    dst = snap(fs_out)
    g = math.gcd(int(src), int(dst))
    L, M = int(dst) // g, int(src) // g
    x = np.asarray(x, dtype=np.float64)
    if L == 1 and M == 1:
        y = x.copy()
        h = np.zeros(0)
    else:
        h = design(L, M)
        up = np.zeros(len(x) * L, dtype=np.float64)
        up[::L] = x
        y = np.convolve(up, h, mode="same")[::M]
    band = min(src, dst) / 2.0
    return {
        "pcm": y.astype(np.float32),
        "fs_hz": dst,
        "fs_source_hz": float(fs_in),
        "fs_source_nominal_hz": src,
        "band_limit_hz": band,
        "upsampled": bool(dst > src),
        "L": L, "M": M, "taps": int(len(h)),
    }
