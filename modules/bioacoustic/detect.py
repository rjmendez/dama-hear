#!/usr/bin/env python3
"""Sustained tonal detection: cicadas, and the katydids that call low enough to hear.

WHY THIS IS NOT hear/node/detect.py WITH DIFFERENT NUMBERS. That gate is a broadband level gate
sized for an impulse, and the 2026-09-07 12.08 h capture measured exactly how far it is from this
job. Its floor is 800 counts against a median ambient of 28.95 -- 28.8 dB over the background, and
19.6 dB over ambient p95 (84.11). A chorus sitting a plausible 10 dB over ambient is ~19 dB under
that gate and cannot fire it at any hour of the night. Worse, the gate's envelope is broadband,
and the night's own sketches say what that costs.

    RECIPE, so the numbers below are reproducible rather than asserted. Capture is
    ~/dama-hear-night-2026-09-07/final/. health.csv holds 1450 rows; its utc_us spans
    07:16:23.7 to 19:21:15.9 UTC and its uptime_s runs 28 s to 43521 s -- 12.081 h either way,
    which is where "12.08 h" comes from. The ambient figures above are the median and the 95th
    percentile of that file's `ambient` column over all 1450 rows, in counts, against the
    firmware's fixed floor of 800: 20*log10(800/28.95) = 28.8 dB, 20*log10(800/84.11) = 19.6 dB.

    dets.csv holds 62 rows. 14 carry uptime_s == 12 -- the boot-time burst -- and 11 of those 14
    also set flags bit 1 (insufficient context: the sketch is of a ring that had not filled).
    Dropping the 14 leaves the 48 in-run detections, and on this file the two filters agree: all
    48 survivors have flags == 0. (An earlier revision of this docstring said "59 rows; 11 set
    flags bit 1 ... leaving 48". The file has 62 rows and 62 - 11 = 51, so that arithmetic never
    worked.) Decode each `frame_hex` with hear.sketch.unpack and take the `db` field, a 20x8
    array. Reduce each band to its MAXIMUM over the 8 frames. Then:
      - ALL 48 peak in mel band 0, and band 0 exceeds band 1 in 48 of 48 (median 5.50 dB), so the
        spectrum is still climbing where the filterbank stops. At fs=16 kHz and nfft=256 the only
        rfft bins with nonzero weight in band 0 are 312.5-500 Hz (bins 5-8; the mel triangle
        nominally spans 300.0-526.6 Hz).
      - median over the 48 of (band 0) - (mean of bands 16-19) = 24.6 dB. Bands 16-19 are the
        four whose triangular support lies entirely above 4 kHz: mel edges 4425-7840 Hz, nonzero
        bins 4437.5-7812.5 Hz.
    The reduction moves the dB but not the count: per-band MEAN over frames instead of max still
    puts all 48 in band 0, at 17.6 dB; comparing against the LOUDEST of bands 16-19 rather than
    their mean gives 19.0 dB under the max reduction and 14.0 dB under the mean. Two earlier
    revisions claimed "22.3 dB", then "46 of the 48 ... 20.4 dB" with "45 of 48 ... 16.5 dB" for
    the mean reduction; none of those five figures reproduces under any reduction tried, and they
    are gone.

So low-frequency rumble sets the envelope and anything in the insect band is masked before the
threshold is even consulted. And it is an amplitude detector: a sustained tonal source has a low
crest factor, so it raises the mean without producing the peak the gate looks for. Three
independent reasons, none of which retuning `floor` fixes.

So this detector asks a different question, on the three axes a sustained tonal source actually
differs on:

  band       power inside one chosen band only, so the rumble that dominates the broadband
             envelope is out of the measurement rather than being thresholded against.
  structure  the band must be either narrowband (spectral flatness) or pulsed (envelope
             autocorrelation). Wind and traffic are neither.
  time       it must hold for at least `min_duration_s` (default 1 s), not peak for 1 ms.

WHAT IT DOES NOT DO. It does not identify a species, and it does not count callers. It says a
structured band-limited sound persisted here, for this long, this far over the local floor, with
this much confidence. Species work needs labels, which is the supersonic module's pattern
(modules/supersonic/classify.py) and the same one applies here once labels exist.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------------------------
# ⚠️BANDWIDTH IS A HARDWARE FACT. NO AMOUNT OF INTEGRATION MOVES IT.
#
# A sustained source is periodic, so you can integrate over many cycles instead of living or dying
# on one 100 us transient. That buys TIMING precision -- it does not buy BANDWIDTH. Integration
# improves the estimate of energy that reached the anti-alias filter; it cannot recover energy the
# filter removed. Two hard ceilings, and the lower one always wins:
#
#   fs = 16000 Hz  ->  Nyquist 8000 Hz. Nothing above 8 kHz exists in the samples at all.
#   ICS-43434 (the planned I2S part; docs/node-hardware.md, "What the ICS-43434 can and cannot
#   do") is 50 Hz - 15 kHz, low-passed above 24 kHz: NO ultrasonic content AT ANY SAMPLE RATE.
#   Re-clocking the part changes nothing.
#
# Consequence, stated once so nobody has to work it out again:
#   cicadas, 4-8 kHz            -> REACHABLE, but 8 kHz IS the Nyquist wall (see `at_band_edge`).
#   crickets / low katydids     -> REACHABLE, most call in 3-8 kHz.
#   ultrasonic katydids, 15-60 kHz -> NOT REACHABLE. Not at 16 kHz, and not on the ICS-43434 at
#   any rate. Detecting those needs a different microphone, not a different detector.
# ---------------------------------------------------------------------------------------------
ICS43434_F_HI_HZ = 15000.0
# 0.98 of Nyquist, the same clamp hear/sketch.py:mel_filterbank uses, so a band this module
# accepts is a band the sketch can also represent.
NYQUIST_USABLE = 0.98

CICADA_BAND_HZ = (4000.0, 8000.0)
KATYDID_AUDIBLE_BAND_HZ = (3000.0, 8000.0)
KATYDID_ULTRASONIC_BAND_HZ = (15000.0, 60000.0)   # here to be refused, not to be used

NFFT = 1024                  # 64 ms at 16 kHz; 15.6 Hz bins, 256 of them across 4-8 kHz
HOP_S = 0.032
SNR_DB = 6.0                 # over the tracked in-band floor
MIN_DURATION_S = 1.0
CLOSE_HYST_DB = 3.0
CLOSE_S = 0.25               # a gap this short does not end a song
# ⚠️A RUN MUST BE ALLOWED TO END. While one is open no new one can start, so an unbounded run is
# an unbounded deaf period -- a different mechanism from the impulse gate's frozen-ambient
# deadlock, with the same symptom. A run this long is force-closed at the current frame's first
# sample and a fresh one opened there, so the segments are contiguous and share no audio,
# structure is re-measured every 10 s, and the deaf window is bounded by construction. 10x
# MIN_DURATION_S, so it never chops a call of ordinary length. Segments after the first carry
# `continues` True: they are one sound cut up, and a census that counted them separately would
# be counting this constant rather than the site.
MAX_DURATION_S = 10.0
# Structure thresholds, measured on this box against synthetic references. They are NOT measured
# on field data: the 2026-09-07 capture contains no biological example to measure them on, and
# saying so is the point -- these are provisional numbers to be refitted the first night this
# detector records something a person has listened to.
# RECIPE for both thresholds: the helpers in tests/test_bioacoustic.py, at the shipped defaults,
# on this box. `noise` is _band_noise(n, gain=8.0, seed=2); `buzz` is _buzz(4 s, snr_db=S) fed to
# a TonalGate(16000) after 3 s of _noise, and the quoted tonality/periodicity/SNR are the fields
# of the event the gate emits; `clicks` is _clicks(4 s, 20.0, gain=G) fed the same way.
#
# Band-limited white noise, averaged the way the gate averages -- frame at nfft=1024 and hop=512
# through the Hanning window, keep the in-band bins, mean the periodograms, then take
# 1 - spectral_flatness -- gives tonality 0.016 over a 1 s run (30 complete frames, not 31; 31 is
# the hop count and the last hop does not hold a whole frame) and 0.0055 over 3 s (92 frames).
# The shipped path agrees: a loud 4 s band-noise burst comes out at tonality 0.004. The weakest
# buzz the gate will open a run on at all is snr_db=5.0, which it reports at 6.19 dB: tonality
# 0.752. So 0.20 sits 12.5x above the worst noise case and 3.8x below the weakest tonal case,
# which is the widest gap available.
TONALITY_MIN = 0.20
# The same band noise, unmodulated, measured over ANALYSIS_S = 2 s -- because that is all the raw
# audio a run retains (see analysis_n), so no run's periodicity is ever measured over more,
# whatever its duration: 0.056 at seed 2, and 0.073 worst over seeds 0-7. The shipped path agrees:
# the loud band-noise burst comes out at periodicity 0.064. The weakest click train the gate will
# open a run on is gain=2.5, reported at 5.15 dB: periodicity 0.785, rising to 0.907 at gain=10.
# So 0.30 sits 4.1x above the worst noise case and 2.6x below the weakest pulsed case.
#
# ⚠️0.30 IS NOT THE WIDEST-GAP POINT, WHICH IS sqrt(0.073*0.785) = 0.24, AND IT IS HELD ANYWAY.
# An unmodulated tone's envelope correlates with itself more strongly the further it is out of the
# noise: the buzz reports periodicity 0.217 at 10.45 dB, 0.301 at 12.31 dB, 0.531 at 20.10 dB. So
# `pulsed` is already loose on a plain tone above ~12 dB, and every dB taken off this threshold
# makes it looser. It costs nothing in the gate -- structure is an OR and tonality carries those
# events regardless -- but `pulsed` on a high-SNR event is not evidence of modulation, and the
# threshold is not lowered to the widest-gap point for that reason.
#
# The justification this replaces read "periodicity 0.14 over 1 s ... the short-window value is
# the one that has to be cleared". A 1 s envelope at the default (2, 100) Hz range returns
# (None, None) from the shipped code -- two periods of the slowest rate asked for need just over
# 1 s -- so that number was unobtainable, and 0.065 for a 3 s window described a window the gate
# never uses.
PERIODICITY_MIN = 0.30
# Floor time constants. Fast down, slow up, and applied on EVERY frame -- see _update_floor.
FLOOR_TAU_DOWN_S = 10.0
# ⚠️THIS IS 30x MAX_DURATION_S, AND THAT RATIO IS WHY NO RUN IS EVER ENDED BY THE FLOOR.
# At hop_s = 0.032 the per-frame coefficient is a_up = 1 - exp(-0.032/300) = 1.0666e-4, so over
# the 312 frames of a full-length run the floor closes 1 - (1 - a_up)^312 = 3.27% of the gap
# between it and the signal. Moving the floor the close_hyst_db = 3.0 dB that would end a run
# early therefore needs 3.0/0.0327 = 91.7 dB of in-band SNR, which the ADC does not have. A run
# is always ended by max_duration_s, by a gap, or by the end of the stream -- never by absorption.
# Every event still reports `floor_rise_db`, the dB the floor actually moved while it was open,
# so this stays a measurement instead of a claim. See TonalGate's docstring for what absorption
# does instead: it shows up ACROSS events, in floor_db climbing from one segment to the next.
FLOOR_TAU_UP_S = 300.0
WARMUP_S = 2.0
PULSE_RATE_HZ = (2.0, 100.0)
ENV_FS = 1000.0              # envelope is decimated to this before autocorrelation
ANALYSIS_S = 2.0             # raw audio retained per run for the periodicity measurement
# Flatness over too few bins is dominated by the periodogram's own chi-squared scatter and stops
# meaning anything. 8 bins is the point below which this module refuses to answer.
MIN_BAND_BINS = 8
# A peak this close to the tallest autocorrelation peak is taken to be the fundamental.
FUND_FRAC = 0.85


def band_limit(f_lo: float, f_hi: float, fs: float,
               mic_f_hi: float = ICS43434_F_HI_HZ) -> Dict:
    """What of [f_lo, f_hi] the hardware can actually deliver, and which ceiling bound it.

    Returns `f_hi_eff`, `reachable`, `limit` in {None, "nyquist", "microphone"} and a note. The
    caller is told WHICH limit bit, because the two have different remedies: "nyquist" is fixed by
    sampling faster, "microphone" is not fixed by anything short of a different part.
    """
    f_lo, f_hi, fs = float(f_lo), float(f_hi), float(fs)
    nyq = fs / 2.0 * NYQUIST_USABLE
    mic = float(mic_f_hi) if mic_f_hi else float("inf")
    f_hi_eff = min(f_hi, nyq, mic)
    limit = None
    if nyq < f_hi and nyq <= mic:
        limit = "nyquist"
    elif mic < f_hi:
        limit = "microphone"
    reachable = f_hi_eff > f_lo
    if not reachable:
        note = ("nothing of %.0f-%.0f Hz survives: the usable ceiling is %.0f Hz (%s)"
                % (f_lo, f_hi, f_hi_eff, limit))
    elif limit == "nyquist":
        note = "band truncated to %.0f Hz by Nyquist at fs=%.0f Hz" % (f_hi_eff, fs)
    elif limit == "microphone":
        note = ("band truncated to %.0f Hz by the microphone, which no sample rate changes"
                % f_hi_eff)
    else:
        note = ""
    return {"f_lo": f_lo, "f_hi": f_hi, "f_hi_eff": f_hi_eff,
            "reachable": bool(reachable), "limit": limit, "note": note}


def spectral_flatness(p: Sequence[float]) -> float:
    """Wiener entropy of a power spectrum: geometric mean over arithmetic mean, in [0, 1].

    WHY THIS STATISTIC AND NOT ANOTHER. Three properties decide it.

    It is SCALE-INVARIANT. Multiply the signal by any gain and both means scale together, so the
    ratio does not move. That matters more here than anywhere else in the repo: the failure mode
    being designed against is a loud broadband event passing as a call, and amplitude is precisely
    what the existing level gate already got wrong. A statistic that cannot see gain cannot be
    fooled by it.

    It needs NO model of the source. Waveform autocorrelation was the obvious alternative and is
    wrong for a chorus: many callers at slightly different frequencies sum to something with no
    single period, so a periodicity-of-the-waveform test goes to zero exactly when the site is
    busiest. Harmonic product spectrum was the other, and assumes a harmonic stack -- cicada song
    is a noisy carrier with a formant-like peak, not a fundamental with overtones.

    It costs one log and two means over the in-band bins, which is what a node could afford later.

    ⚠️AVERAGE THE PERIODOGRAM FIRST. On a SINGLE frame this is near-useless: each bin of a white
    periodogram is exponentially distributed, and exp(E[log X])/E[X] = e^-gamma = 0.5615, so white
    noise scores 0.44 "tonal" on one frame. Averaging k frames pulls it to exp(psi(k) - ln k) --
    0.983 at k=30, which is how many complete frames fit in one second at the default nfft and
    hop ((16000 - 1024) // 512 + 1). The caller must hand this an averaged spectrum, and TonalGate
    does.
    """
    p = np.asarray(p, float)
    if p.size < 2:
        raise ValueError("flatness of fewer than 2 bins is not defined")
    a = float(p.mean())
    if not np.isfinite(a) or a <= 0.0:
        return 1.0
    n = p / a                                    # scale out first; the eps below is then relative
    g = float(np.exp(np.mean(np.log(n + 1e-30))))
    return float(min(1.0, max(0.0, g)))


def band_envelope(x: Sequence[float], fs: float, f_lo: float, f_hi: float) -> np.ndarray:
    """Analytic-signal envelope of x band-passed to [f_lo, f_hi]. Same length as x.

    Built by zeroing everything outside the band in the full FFT and doubling what is left, which
    is the analytic signal by construction -- so the magnitude is the true envelope rather than a
    rectified-and-smoothed approximation whose smoothing window would itself set an upper limit on
    the pulse rate measurable. hear/node/detect.py's 1 ms boxcar is the right tool for an impulse
    and the wrong one here for exactly that reason.
    """
    x = np.asarray(x, float)
    n = x.size
    if n < 2:
        return np.abs(x)
    X = np.fft.fft(x)
    f = np.fft.fftfreq(n, 1.0 / float(fs))
    keep = (f >= float(f_lo)) & (f <= float(f_hi))
    Xa = np.zeros(n, complex)
    Xa[keep] = 2.0 * X[keep]
    return np.abs(np.fft.ifft(Xa))


def decimate(x: Sequence[float], fs: float, fs_out: float = ENV_FS) -> Tuple[np.ndarray, float]:
    """Block-mean decimation, for the envelope only. Returns (y, realised fs).

    An envelope carries nothing above a few hundred Hz -- pulse rates, not carriers -- so a block
    mean is an adequate anti-alias filter and the autocorrelation that follows gets 16x shorter.
    """
    x = np.asarray(x, float)
    k = max(1, int(round(float(fs) / float(fs_out))))
    if k == 1:
        return x, float(fs)
    m = (x.size // k) * k
    if m == 0:
        return x, float(fs)
    return x[:m].reshape(-1, k).mean(axis=1), float(fs) / k


def pulse_rate(env: Sequence[float], fs_env: float,
               rate_lo: float = PULSE_RATE_HZ[0],
               rate_hi: float = PULSE_RATE_HZ[1]) -> Tuple[Optional[float], Optional[float]]:
    """(periodicity in [0,1], rate in Hz) from the envelope's autocorrelation, or (None, None).

    None rather than 0.0 when the window is too short to hold two periods of the slowest rate
    asked for: a zero there reads as "not periodic" when the truth is "not measurable", and the
    repo's standing rule (modules/supersonic/classify.py:score) is that those must not look alike.

    ⚠️THE GUARD IS AGAINST rate_lo, THE SLOWEST RATE, AND IT USED TO BE AGAINST rate_hi. Guarding
    on the fastest rate lets a window that cannot hold one period of the true modulation be
    searched anyway; the search then lands on some multiple of the true rate, or on the edge of
    the searched range, and reports it with full confidence.

    RECIPE for the three pre-fix numbers below, because they cannot be got from this file as it
    stands. Restore the old guard and the old clip -- replace the `n < 2 * lag_hi + 2` test with
    `n < 2 * lag_lo + 2` and insert `lag_hi = min(lag_hi, n // 2)` above it -- and leave the rest
    of the body alone. Then, at fs_env = 1000 Hz and the default (2, 100) Hz range:
        env = 1 + 0.9*sin(2*pi*3*t), 0.4 s  ->  (0.988, 100.0 Hz)  true rate 3 Hz, so 33x wrong
        the same, 0.3 s                     ->  (1.000, 100.0 Hz)  periodicity 1.000, top of range
        np.random.RandomState(0).normal(0, 1, 300)
                                            ->  (0.137, 12.3 Hz)  where this docstring promised
                                                                  None
    The two sine rows do not depend on the clip; the white-noise row does, and reproduces only
    with `n // 2`. All three now return (None, None).

    The cost is stated rather than hidden: at the default rate_lo = 2 Hz a measurement needs
    2/2 Hz + 2 samples of envelope, i.e. just over 1 s, so a run near min_duration_s can come
    back with periodicity None and be judged on tonality alone. A caller that wants an answer
    from a shorter window must raise rate_lo and accept that it is no longer looking for slow
    modulation -- which is a decision, not a default.

    The autocorrelation is bias-corrected by (n - lag)/n. Without it the raw estimate falls off
    linearly with lag, so a slow pulse train scores lower than a fast one purely as an artefact.

    ⚠️THE REPORTED LAG IS THE FUNDAMENTAL, NOT THE TALLEST PEAK. A periodic envelope correlates
    with itself at every multiple of its period, and the bias correction lifts the longer lags,
    so argmax lands on a subharmonic. RECIPE: take the argmax of `seg` instead of running the
    FUND_FRAC loop below, on the envelope of 3 s of tests/test_bioacoustic.py's own
    _clicks(n, rate) at fs = 16 kHz, decimated the way this module decimates it, at the default
    (2, 100) Hz range:
        true  8 Hz -> 4.00 Hz     true 12 Hz -> 4.00 Hz
        true 20 Hz -> 2.86 Hz     true 45 Hz -> 2.50 Hz
    Never the true rate, and for the faster trains it falls to within a hair of rate_lo, the
    bottom of the searched range. (An earlier revision of this docstring said "10 Hz for a 20 Hz
    train". No window length or variant tried reproduces 10 Hz; the 12 Hz row is the half of that
    sentence that does reproduce, which is probably why the other half survived so long. Dropping
    the bias correction instead of the FUND_FRAC rule recovers 20.00 Hz -- at the cost the
    correction exists to prevent, a slow train scoring lower than a fast one for free.)
    The first peak within FUND_FRAC of the tallest is taken instead: 8.00, 12.05, 20.00, 45.45 Hz
    on those same four inputs.
    """
    e = np.asarray(env, float)
    n = e.size
    fs_env = float(fs_env)
    lag_lo = int(round(fs_env / float(rate_hi)))
    lag_hi = int(round(fs_env / float(rate_lo)))
    lag_lo = max(1, lag_lo)
    # Two periods of the SLOWEST rate asked for. lag_hi <= (n - 2)/2 follows, so the searched
    # range never has to be clipped to the window -- clipping it is what produced the harmonic.
    if lag_hi <= lag_lo or n < 2 * lag_hi + 2:
        return None, None
    e = e - e.mean()
    if not np.any(e):
        return 0.0, None
    nf = 1 << int(np.ceil(np.log2(2 * n)))
    ac = np.fft.irfft(np.abs(np.fft.rfft(e, nf)) ** 2, nf)[:n]
    if ac[0] <= 0:
        return 0.0, None
    ac = ac / ac[0] * (n / np.maximum(n - np.arange(n), 1.0))
    seg = ac[lag_lo:lag_hi + 1]
    top = float(seg.max())
    k = int(np.argmax(seg))
    if top > 0.0:
        for j in range(1, seg.size - 1):
            if seg[j] >= FUND_FRAC * top and seg[j] >= seg[j - 1] and seg[j] >= seg[j + 1]:
                k = j
                break
    return float(min(1.0, max(0.0, seg[k]))), float(fs_env / (lag_lo + k))


def _logistic(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-max(-60.0, min(60.0, float(x)))))


class TonalGate:
    """Streaming detector for sustained band-limited structured sound.

    Frames of `nfft` samples every `hop_s`. Per frame: in-band power against a tracked in-band
    floor. A run opens at `snr_db` over the floor and closes when it has been `close_hyst_db`
    below that for `close_s`. On close the run is measured -- flatness of its averaged in-band
    spectrum, autocorrelation of its band envelope -- and either reported, or discarded as too
    short (`n_short`) or as shapeless (`n_unstructured`). Both discards are counted, because
    "saw nothing" and "saw something and threw it away" are different facts about a site.

    ⚠️THE FLOOR IS UPDATED ON EVERY FRAME, INCLUDING DURING A DETECTION. hear/node/detect.py
    updates its ambient only on the armed-and-under-threshold branch, which is safe for impulses
    and deadlocks on a rising floor: once disarmed it stops learning, so it can never learn the
    level that is keeping it disarmed. The firmware hit this in the field and fixed it -- see
    `gate()` in firmware/night_node/night_node.ino and the `ALPHA_UP` constant above it, whose
    comment records the measurement: "156 s solid disarmed, envelope 1400-1600 against thr 800,
    ambient frozen at 73.2, two detections all night". A chorus IS a floor that rises for hours,
    so this module cannot afford that failure and does not have the branch.

    ⚠️THE PRICE OF THAT IS PAID ACROSS EVENTS, NOT INSIDE ONE, AND THIS CLASS USED TO CLAIM THE
    OPPOSITE. It shipped a `floor_absorbed` boolean described as "the floor climbed out from
    under a song that was still going". With the shipped configuration that boolean is
    structurally unreachable: floor_tau_up_s is 300 s and max_duration_s is 10 s, so the floor
    can close only 3.27% of the gap before the run is force-closed anyway, and clearing
    close_hyst_db would take 91.7 dB of SNR (the arithmetic is beside FLOOR_TAU_UP_S). MEASURED,
    on 3 s of _noise then 30 s of _buzz(snr_db=S) from tests/test_bioacoustic.py at the shipped
    defaults: the three segments' floor_rise_db come to 0.41/0.39/0.38 dB at S=12, 0.99/0.96/0.92
    at 30, 1.98/1.91/1.84 at 60 and 2.96/2.87/2.76 at 90 -- under close_hyst_db = 3.0 even at a
    physically absurd 90 dB, which is the same statement as the arithmetic. A flag that cannot
    fire is worse than no flag, so it is gone, replaced by `floor_rise_db` -- the dB the floor
    actually moved while the run was open, a number rather than a verdict.

    What absorption really looks like is a sequence: a chorus that outlasts floor_tau_up_s comes
    back as successive max_duration segments whose `floor_db` climbs and whose `snr_db` shrinks,
    until the gate stops opening runs at all. Reading `floor_db` across events is how an operator
    sees it, and there is no single event on which it can be seen.
    """

    def __init__(self, fs: float, band: Tuple[float, float] = CICADA_BAND_HZ,
                 nfft: int = NFFT, hop_s: float = HOP_S, snr_db: float = SNR_DB,
                 min_duration_s: float = MIN_DURATION_S,
                 tonality_min: float = TONALITY_MIN,
                 periodicity_min: float = PERIODICITY_MIN,
                 close_hyst_db: float = CLOSE_HYST_DB, close_s: float = CLOSE_S,
                 max_duration_s: float = MAX_DURATION_S,
                 floor_tau_down_s: float = FLOOR_TAU_DOWN_S,
                 floor_tau_up_s: float = FLOOR_TAU_UP_S,
                 warmup_s: float = WARMUP_S,
                 pulse_rate_hz: Tuple[float, float] = PULSE_RATE_HZ,
                 analysis_s: float = ANALYSIS_S,
                 mic_f_hi: float = ICS43434_F_HI_HZ):
        self.fs = float(fs)
        self.nfft = int(nfft)
        self.hop = max(1, int(round(float(hop_s) * self.fs)))
        self.hop_s = self.hop / self.fs
        self.snr_db = float(snr_db)
        self.min_duration_s = float(min_duration_s)
        self.tonality_min = float(tonality_min)
        self.periodicity_min = float(periodicity_min)
        self.close_hyst_db = float(close_hyst_db)
        self.close_frames = max(1, int(round(float(close_s) / self.hop_s)))
        self.max_duration_s = float(max_duration_s)
        self.pulse_rate_hz = (float(pulse_rate_hz[0]), float(pulse_rate_hz[1]))
        self.analysis_n = max(self.nfft, int(round(float(analysis_s) * self.fs)))

        self.limit = band_limit(band[0], band[1], self.fs, mic_f_hi)
        if not self.limit["reachable"]:
            # Refuse rather than return nothing forever. A gate asked for 15-60 kHz at 16 kHz will
            # never fire, and silence is indistinguishable from a quiet night.
            raise ValueError("band unreachable: " + self.limit["note"])
        self.f_lo = self.limit["f_lo"]
        self.f_hi = self.limit["f_hi_eff"]
        # ⚠️A PROPERTY OF THE CONFIGURATION, NOT OF ANY EVENT. It says the band that was ASKED FOR
        # did not fit under the hardware ceilings, which is decided here in __init__ and cannot
        # change afterwards. It used to be copied onto every event as `band_limited`, where it
        # read as a per-event finding; at the shipped band and rate f_hi is always clamped to
        # 7840 Hz, so that copy was True on every event the gate could ever emit. Events carry
        # `limit` (None | "nyquist" | "microphone"), which is the same fact plus which ceiling
        # bit, and `at_band_edge`/`edge`, which ARE per-event.
        self.band_truncated = self.limit["limit"] is not None

        freqs = np.fft.rfftfreq(self.nfft, 1.0 / self.fs)
        self._k0 = int(np.searchsorted(freqs, self.f_lo, "left"))
        self._k1 = int(np.searchsorted(freqs, self.f_hi, "right"))
        if self._k1 - self._k0 < MIN_BAND_BINS:
            raise ValueError("band holds %d bins at nfft=%d; flatness needs at least %d"
                             % (self._k1 - self._k0, self.nfft, MIN_BAND_BINS))
        self._freqs = freqs[self._k0:self._k1]
        self._bin_hz = float(self.fs) / self.nfft
        self._win = np.hanning(self.nfft)

        # tau is a TIME, so it carries across sample rates and hop sizes unchanged; the equivalent
        # per-frame coefficient does not, and is derived here rather than written down.
        self._a_down = 1.0 - float(np.exp(-self.hop_s / max(1e-9, float(floor_tau_down_s))))
        self._a_up = 1.0 - float(np.exp(-self.hop_s / max(1e-9, float(floor_tau_up_s))))
        self._warm_frames = max(1, int(round(float(warmup_s) / self.hop_s)))

        self.floor_db: Optional[float] = None
        self.n_frames = 0
        self.n_seen = 0
        self.n_short = 0        # runs discarded as too short; counted, never silently dropped
        # ⚠️TWO COUNTERS, BECAUSE ONE OF THEM IS A FUNCTION OF max_duration_s AND THE OTHER IS
        # NOT. A single shapeless stretch longer than max_duration_s is chopped into segments and
        # each segment is judged separately, so counting segments makes a 60 s wind gust score 6
        # at max_duration_s=10 and 3 at 20 -- measured on 3 s of _noise then 60 s of
        # _noise(seed=8) + _band_noise(gain=8.0) from tests/test_bioacoustic.py, which emits no
        # events and leaves n_unstructured_segments at 6 and 3 while n_unstructured stays 1 for
        # both. (The same signal at 25 s gives 3 and 2, and that is the case the tests pin.)
        # `n_unstructured` counts STRETCHES -- a segment that
        # continues the previous one does not increment it -- and is the number that says
        # something about the site. `n_unstructured_segments` counts the discards and is a number
        # about this detector's settings.
        self.n_unstructured = 0
        self.n_unstructured_segments = 0
        # Stream discontinuities seen on the `block_start` the caller declares. Not cosmetic: the
        # node had 20 one-second windows come up short across the 2026-09-07 capture. drop_s counts
        # WINDOWS, not seconds of audio: drop_samples=57597 is 3.60 s at 16 kHz (health.csv, last
        # row: drop_s=20, drop_samples=57597, both monotonic over the file). Either way a real
        # stream HAS gaps. An earlier revision quoted drop_s=18 / drop_samples=42749 / 2.7 s as
        # the final values; that pair is real but mid-run -- it first appears 1259 rows in, at
        # uptime_s=37818 of 43521.
        self.n_gaps = 0
        self.n_gap_samples = 0
        self.n_discarded_samples = 0   # residual thrown away because a gap orphaned it
        self._buf = np.zeros(0)
        self._buf_start = 0
        self._next_index: Optional[int] = None
        self._run: Optional[Dict] = None

    # -- floor ---------------------------------------------------------------------------------

    def _update_floor(self, band_db: float) -> None:
        """Two-speed limb, unconditional. See the class docstring for why there is no branch.

        Down fast (10 s) so the floor follows a chorus that stops. Up slow (300 s) so one event
        cannot lift the floor out from under itself -- the property the impulse gate gets by
        skipping the update entirely, obtained here without the deadlock that costs.
        """
        if self.floor_db is None:
            self.floor_db = band_db
            return
        a = self._a_down if band_db < self.floor_db else self._a_up
        self.floor_db += a * (band_db - self.floor_db)

    # -- streaming -----------------------------------------------------------------------------

    def process(self, block: Sequence[float], block_start: int) -> List[Dict]:
        """Events whose run CLOSED inside this block. `block_start` indexes block[0] absolutely.

        ⚠️`block_start` IS HONOURED ON EVERY CALL, AND IT USED TO BE HONOURED ONLY ON THE FIRST.
        The old code re-anchored under `if self._buf.size == 0`, which once framing has started
        is never true: the residual always holds between nfft-hop and nfft-1 samples (512 to 1023
        at the defaults), because `pos` stops at the last frame that fits. So from the second call
        onward the caller's absolute index was read and discarded, and the detector kept counting
        from wherever the first block put it. Measured against the shipped code, at fs=16000 and the
        default nfft/hop: process(1000 samples, 0) then process(1000 samples, 10_000_000) left
        the internal anchor at 1024 -- the second block was framed as samples 1000-1999 of the
        recording, ten million samples early, with nothing in the output saying so. Every
        `index`, `t_s` and `end_index` after a dropped block was wrong by the size of the drop,
        and a wrong arrival time is the one error a TDoA solve cannot survive.

        A DISCONTINUITY IS NEVER SILENT. `block_start` greater than where the last block ended
        means the caller lost samples, and this method:
          - force-closes any open run with end_reason "stream_gap" (its duration is a bound set
            by the drop, and `truncated` is True), returning that event;
          - discards the residual buffer, which can no longer be framed against the new samples
            because they are not adjacent to it, and adds it to `n_discarded_samples`;
          - re-anchors the frame grid to `block_start` and counts the gap in `n_gaps` /
            `n_gap_samples`.
        The frame grid restarts at the resume point, so hop phase before and after a gap is not
        the same. That is correct -- there is no phase relationship across missing samples -- and
        it is the reason the residual cannot simply be kept.

        `block_start` BEHIND where the last block ended raises ValueError. A backwards or
        overlapping stream cannot be reconciled into one timeline, and guessing which copy of a
        sample is the real one is exactly the invention this module refuses to make.

        Events are emitted at the END of a run, not at its onset, because duration and structure
        are the measurement and neither exists until the run is over. A caller that needs to know
        a song is in progress should read `in_run`.
        """
        block = np.asarray(block, float)
        block_start = int(block_start)
        out: List[Dict] = []

        if self._next_index is None:
            self._buf_start = block_start
        elif block_start < self._next_index:
            raise ValueError(
                "block_start %d is behind the end of the last block (%d): a stream that goes "
                "backwards or overlaps cannot be placed on one timeline"
                % (block_start, self._next_index))
        elif block_start > self._next_index:
            out += self._on_gap(block_start - self._next_index, block_start)

        self._next_index = block_start + int(block.size)
        self._buf = np.concatenate([self._buf, block])
        pos = 0
        while pos + self.nfft <= self._buf.size:
            ev = self._frame(self._buf[pos:pos + self.nfft], self._buf_start + pos,
                             self._buf[pos:pos + self.hop])
            if ev is not None:
                out.append(ev)
            pos += self.hop
        self._buf = self._buf[pos:]
        self._buf_start += pos
        self.n_seen += int(block.size)
        return out

    def _on_gap(self, gap: int, block_start: int) -> List[Dict]:
        """Samples the caller did not deliver. Close, drop, re-anchor, count -- in that order."""
        out: List[Dict] = []
        if self._run is not None:
            ev = self._close("stream_gap")
            if ev is not None:
                out.append(ev)
        self.n_gaps += 1
        self.n_gap_samples += int(gap)
        self.n_discarded_samples += int(self._buf.size)
        self._buf = np.zeros(0)
        self._buf_start = int(block_start)
        return out

    def flush(self) -> List[Dict]:
        """Close an open run at the end of the stream.

        The event carries end_reason "stream_end": its duration is bounded by the end of the
        recording rather than by the end of the song.
        """
        if self._run is None:
            return []
        ev = self._close("stream_end")
        return [] if ev is None else [ev]

    @property
    def in_run(self) -> bool:
        return self._run is not None

    # -- per frame -----------------------------------------------------------------------------

    def _frame(self, frame: np.ndarray, start: int, fresh: np.ndarray) -> Optional[Dict]:
        p = np.abs(np.fft.rfft(frame * self._win, self.nfft)) ** 2
        b = p[self._k0:self._k1]
        band_db = 10.0 * np.log10(float(b.sum()) + 1e-20)
        floor_before = self.floor_db
        self._update_floor(band_db)
        self.n_frames += 1
        snr = band_db - (floor_before if floor_before is not None else band_db)

        if self._run is None:
            # A floor built from fewer than warmup_s of frames is one frame's worth of noise, and
            # a run opened against it is measuring the startup transient.
            if self.n_frames >= self._warm_frames and snr >= self.snr_db:
                self._open(start, b, band_db, floor_before, fresh)
            return None

        r = self._run
        hot = snr >= self.snr_db - self.close_hyst_db

        # ⚠️THE SEAM IS TAKEN BEFORE THIS FRAME IS FOLDED IN, NOT AFTER, OR THE FRAME LANDS IN
        # BOTH SEGMENTS. The old code closed at last_hot + nfft and reopened at the same frame's
        # `start`, so segment n ended nfft samples (64 ms at the defaults) after segment n+1
        # began and that audio was measured, and reported, twice. Closing at `start` makes the
        # segments exactly contiguous: seg[n]["end_index"] == seg[n+1]["index"].
        if (start - r["start"]) / self.fs >= self.max_duration_s:
            ev = self._close("max_duration", end=start)
            if hot:
                # Reopen on this same frame rather than the next: a chorus must come back as a
                # series of measured segments, not lose a frame at every seam.
                self._open(start, b, band_db, floor_before, fresh, continues=True)
            return ev

        if hot:
            r["below"] = 0
            r["last_hot"] = start
            r["psum"] += b
            r["n"] += 1
            r["db"].append(band_db)
        else:
            r["below"] += 1
        if r["nx"] < self.analysis_n:
            r["x"].append(np.asarray(fresh, float))
            r["nx"] += int(fresh.size)
        if r["below"] >= self.close_frames:
            return self._close("gap")
        return None

    def _open(self, start: int, b: np.ndarray, band_db: float,
              floor_before: Optional[float], fresh: np.ndarray,
              continues: bool = False) -> None:
        self._run = {"start": start, "psum": b.copy(), "n": 1,
                     "db": [band_db],
                     "floor0": floor_before, "last_hot": start, "below": 0,
                     "continues": bool(continues),
                     "x": [np.asarray(fresh, float)], "nx": int(fresh.size)}

    # -- run close -----------------------------------------------------------------------------

    def _close(self, end_reason: str, end: Optional[int] = None) -> Optional[Dict]:
        r = self._run
        self._run = None
        # `end` is passed only for a max_duration seam, where it is the next segment's first
        # sample. Everywhere else the run ends at the end of its last hot frame's window.
        end = int(r["last_hot"] + self.nfft) if end is None else int(end)
        duration_s = (end - r["start"]) / self.fs
        if duration_s < self.min_duration_s:
            # An impulse. Counted, because "the detector saw nothing" and "the detector saw
            # something and threw it away" are different statements about a site. A CONTINUATION
            # is not counted: a short tail after a max_duration seam is the end of a long sound,
            # not an impulse, and calling it one would make n_short depend on max_duration_s.
            if not r["continues"]:
                self.n_short += 1
            return None

        psd = r["psum"] / max(1, r["n"])
        flat = spectral_flatness(psd)
        tonality = 1.0 - flat
        peak_hz = float(self._freqs[int(np.argmax(psd))])

        x = np.concatenate(r["x"]) if r["x"] else np.zeros(0)
        env, fs_env = decimate(band_envelope(x, self.fs, self.f_lo, self.f_hi), self.fs)
        periodicity, rate_hz = pulse_rate(env, fs_env, *self.pulse_rate_hz)

        band_db = float(np.median(r["db"]))
        floor0 = float(r["floor0"]) if r["floor0"] is not None else band_db
        snr_db = band_db - floor0

        # STRUCTURE IS A GATE, NOT A WEIGHT. Narrowband OR pulsed -- either will do, because the
        # two target signals differ on which one they have: a cicada's continuous buzz is tonal
        # and barely modulated, a katydid's pulse train is strongly modulated and its carrier can
        # be broad. Wind and traffic are neither. Without this as a hard requirement a band-noise
        # burst 17.95 dB over the floor scores confidence 0.534, because a geometric mean of three
        # terms cannot be dragged low enough by one of them alone. RECIPE: 3 s of _noise then 4 s
        # of _noise(seed=8) + _band_noise(gain=8.0) from tests/test_bioacoustic.py, through a
        # TonalGate(16000) whose `if structure < 0.0: return None` branch below is removed; the
        # event comes back tonality 0.004, periodicity 0.064, structure -0.245. (An earlier
        # revision rounded this to "18 dB" and "0.52"; 0.52 does not reproduce here.)
        ton_n = (tonality - self.tonality_min) / max(1e-9, 1.0 - self.tonality_min)
        if periodicity is None:
            per_n = None
            structure = ton_n
        else:
            per_n = (periodicity - self.periodicity_min) / max(1e-9, 1.0 - self.periodicity_min)
            structure = max(ton_n, per_n)
        if structure < 0.0:
            # Loud, sustained, and shapeless. Counted, not silently dropped. Two counters, and
            # only one of them is about the site: see their definitions in __init__, which carry
            # the recipe. A 60 s wind gust is ONE unstructured stretch and six unstructured
            # segments at the shipped max_duration_s; the old single counter reported six and
            # called that a fact about the site, when six is a fact about max_duration_s.
            self.n_unstructured_segments += 1
            if not r["continues"]:
                self.n_unstructured += 1
            return None

        # Confidence is the geometric mean of three logistics, one per axis the detector actually
        # tested. Geometric so that a weakness on any one axis cannot be bought off by strength on
        # another -- a very loud, very long, completely unstructured event must not score high.
        # Each logistic is 0.5 at its own threshold, so confidence 0.5 means precisely "sitting on
        # the trigger boundary; believe nothing from this alone".
        c_snr = _logistic((snr_db - self.snr_db) / 3.0)
        c_str = _logistic(structure / 0.15)
        c_dur = _logistic((duration_s - self.min_duration_s) / max(1e-9, self.min_duration_s))
        confidence = float((c_snr * c_str * c_dur) ** (1.0 / 3.0))

        # Peak against a wall. The 2026-09-07 capture is the cautionary case: all 48 of its in-run
        # detections peak in mel band 0, whose nonzero bins run 312.5-500 Hz. That LOCATES THE
        # PEAK NO MORE PRECISELY THAN "at or below 500 Hz" -- it is equally consistent with a peak
        # inside band 0 and with one below 300 Hz, outside the representation entirely, and the
        # sketch cannot tell those apart. The conclusion worth drawing is the weaker one: the
        # energy sat at the bottom edge of what the node represents, and nothing in the output
        # said so. That is what these two fields exist to say.
        edge_hz = max(2.0 * self._bin_hz, 0.02 * (self.f_hi - self.f_lo))
        edge = None
        if peak_hz >= self.f_hi - edge_hz:
            edge = "high"
        elif peak_hz <= self.f_lo + edge_hz:
            edge = "low"

        # Deliberately NOT folded into `confidence`. A hardware ceiling and a marginal signal are
        # different facts with different remedies, and averaging them into one number destroys the
        # distinction the operator needs. Same rule as score() returning None for a missing
        # feature rather than a plausible default.
        return {
            "index": int(r["start"]),
            "onset_index": float(r["start"]),   # FRAME resolution, +-hop/2. Not sub-sample: an
            "t_s": r["start"] / self.fs,        # onset that takes 100 ms to rise has no edge to
            "end_index": int(end),              # interpolate, so claiming sub-sample would lie.
            "duration_s": float(duration_s),
            "band_hz": (float(self.f_lo), float(self.f_hi)),
            "band_db": band_db,
            "floor_db": floor0,
            "snr_db": float(snr_db),
            "tonality": float(tonality),
            "periodicity": periodicity,
            "pulse_rate_hz": rate_hz,
            "peak_hz": peak_hz,
            "n_frames": int(r["n"]),
            "confidence": confidence,
            "tonal": bool(tonality >= self.tonality_min),
            "pulsed": bool(periodicity is not None and periodicity >= self.periodicity_min),
            # Which hardware ceiling truncated the requested band, or None. Constant for the life
            # of the gate -- see `band_truncated` in __init__ for why that is stated rather than
            # dressed up as a per-event flag.
            "limit": self.limit["limit"],
            "at_band_edge": edge is not None,
            "edge": edge,
            # `duration_s` is a measurement only when end_reason is "gap"; otherwise it is a
            # lower bound set by this detector ("max_duration"), by the end of the recording
            # ("stream_end"), or by samples the caller lost ("stream_gap"), and a census that
            # averaged those in without noticing would be wrong.
            "end_reason": end_reason,
            "truncated": bool(end_reason != "gap"),
            # True when this segment continues the previous event across a max_duration seam.
            # A census must merge these before counting: they are one sound, cut up so that the
            # gate cannot be held open -- and therefore deaf -- indefinitely.
            "continues": bool(r["continues"]),
            # How far the tracked floor moved while this run was open. Replaces a `floor_absorbed`
            # boolean that could not fire at the shipped configuration; the class docstring has
            # the arithmetic and what absorption looks like instead.
            "floor_rise_db": float(self.floor_db - r["floor0"]) if (
                self.floor_db is not None and r["floor0"] is not None) else None,
        }
