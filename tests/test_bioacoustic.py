"""Sustained tonal detection. The band ceiling and the cannot-go-deaf proof are the point."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modules.bioacoustic import detect as BA  # noqa: E402

FS = 16000.0            # the node's PDM rate. Nyquist 8 kHz, and that is the whole band story.
CICADA_HZ = 6000.0      # mid-band, well clear of both walls
NOISE_SD = 30.0         # 2026-09-07 capture: ambient median 29.4 counts over 11.33 h
BAND = BA.CICADA_BAND_HZ


def _noise(n, sd=NOISE_SD, seed=1):
    return np.random.RandomState(seed).normal(0.0, sd, n)


def _amp_for(snr_db, sd=NOISE_SD, band=BAND):
    """Sine amplitude giving `snr_db` of in-band signal power over white noise of this sd.

    White noise of variance sd^2 spreads over 0..fs/2, so the fraction inside the band is
    (f_hi - f_lo)/(fs/2); a sine of amplitude a carries a^2/2.
    """
    pn = sd ** 2 * (band[1] - band[0]) / (FS / 2.0)
    return float(np.sqrt(2.0 * pn * 10.0 ** (snr_db / 10.0)))


def _buzz(n, f=CICADA_HZ, snr_db=9.0, sd=NOISE_SD, seed=3):
    """Cicada-like: a continuous tone in noise. Narrowband, and barely modulated."""
    t = np.arange(n) / FS
    return _noise(n, sd, seed) + _amp_for(snr_db, sd) * np.sin(2 * np.pi * f * t)


def _band_noise(n, gain, seed=2, band=BAND):
    x = np.random.RandomState(seed).normal(0.0, NOISE_SD, n)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1.0 / FS)
    out = np.zeros_like(X)
    m = (f >= band[0]) & (f <= band[1])
    out[m] = X[m] * gain
    return np.fft.irfft(out, n)


def _clicks(n, rate_hz=20.0, gain=10.0, duty=0.25, seed=4):
    """Katydid-like: a pulse train. Broadband inside the band, and strongly modulated.

    Deliberately NOT a modulated sine. The two target signals differ on which structure they
    have, and a modulated sine has both, so it would not test the periodicity path on its own.
    """
    t = np.arange(n) / FS
    return _noise(n, NOISE_SD, seed + 10) + _band_noise(n, gain, seed) * \
        (np.mod(t * rate_hz, 1.0) < duty)


def _impulse(n, at, amp=25000.0):
    """Broadband impulse with a decay tail -- the supersonic module's signal, as a negative."""
    x = _noise(n, NOISE_SD, 5)
    x[at:at + 20] += amp * np.hanning(20)
    tail = np.arange(880)
    x[at + 20:at + 900] += amp * 0.25 * np.exp(-tail / 200.0) * \
        np.random.RandomState(6).normal(0, 1, 880)
    return x


def _run(gate, *segments, start=0):
    """Feed segments in order and return every event, including the one flush() closes.

    `start` is the absolute index of the first sample of the first segment. It exists because
    process() now honours block_start on every call: a test that has already fed a gate and then
    calls _run() again must say where it got to, or it is declaring that the stream went
    backwards -- which is exactly what the gate refuses.
    """
    out, i = [], int(start)
    for s in segments:
        out += gate.process(s, i)
        i += len(s)
    return out + gate.flush()


class TestBandCeiling:
    """⚠️The whole reason this module exists in the 4-8 kHz band and not above it."""

    def test_nyquist_is_the_ceiling_at_the_node_rate(self):
        b = BA.band_limit(4000.0, 8000.0, FS)
        assert b["limit"] == "nyquist"
        assert b["f_hi_eff"] == pytest.approx(FS / 2.0 * BA.NYQUIST_USABLE)

    def test_the_microphone_is_the_ceiling_once_sampling_is_fast_enough(self):
        # 96 kHz puts Nyquist at 47 kHz, so the ICS-43434's own 15 kHz roll-off binds instead --
        # and that one is not fixed by sampling faster, which is why the two are named apart.
        b = BA.band_limit(4000.0, 20000.0, 96000.0)
        assert b["limit"] == "microphone"
        assert b["f_hi_eff"] == pytest.approx(BA.ICS43434_F_HI_HZ)

    def test_cicadas_are_reachable(self):
        assert BA.band_limit(*BA.CICADA_BAND_HZ, fs=FS)["reachable"]

    def test_ultrasonic_katydids_are_not_reachable_at_any_rate(self):
        # Not at the node rate...
        assert not BA.band_limit(*BA.KATYDID_ULTRASONIC_BAND_HZ, fs=FS)["reachable"]
        # ...and not at 192 kHz either, because the microphone stops at 15 kHz. Sampling faster
        # is the obvious move and it does not work.
        b = BA.band_limit(*BA.KATYDID_ULTRASONIC_BAND_HZ, fs=192000.0)
        assert not b["reachable"] and b["limit"] == "microphone"

    def test_an_unreachable_band_is_refused_not_silently_empty(self):
        # A gate that returns nothing for ever is indistinguishable from a quiet night.
        with pytest.raises(ValueError, match="unreachable"):
            BA.TonalGate(FS, band=BA.KATYDID_ULTRASONIC_BAND_HZ)

    def test_a_partly_reachable_band_is_clamped_and_says_so(self):
        g = BA.TonalGate(FS, band=(4000.0, 20000.0))
        assert g.f_hi == pytest.approx(FS / 2.0 * BA.NYQUIST_USABLE)
        assert g.band_truncated, "a clamped band must be flagged, not quietly delivered"

    def test_the_truncation_flag_is_a_property_of_the_gate_not_of_an_event(self):
        # It used to be copied onto every event as `band_limited`. At the shipped band and rate
        # f_hi is ALWAYS clamped to 7840 Hz, so that copy was True on every event the gate could
        # ever emit -- a flag that cannot be false is not a flag. It lives on the gate now, and
        # the event carries `limit`, which says WHICH ceiling bit.
        g = BA.TonalGate(FS)
        assert g.band_truncated and g.limit["limit"] == "nyquist"
        d = _run(g, _noise(int(3 * FS)), _buzz(int(4 * FS)))[0]
        assert "band_limited" not in d
        assert d["limit"] == "nyquist"

    def test_too_few_bins_is_refused(self):
        # Flatness over a handful of bins is periodogram scatter, not a measurement.
        with pytest.raises(ValueError, match="bins"):
            BA.TonalGate(FS, band=(6000.0, 6050.0))


class TestFlatness:
    def test_gain_does_not_move_it(self):
        # THE reason this statistic was chosen. The failure being designed against is a loud
        # broadband event passing as a call, and amplitude is what the level gate already got
        # wrong; a statistic that cannot see gain cannot be fooled by it.
        p = np.random.RandomState(9).gamma(2.0, 1.0, 256)
        assert BA.spectral_flatness(p) == pytest.approx(BA.spectral_flatness(p * 1e6), abs=1e-12)

    def test_a_tone_is_far_from_flat_and_noise_is_near_flat(self):
        p = np.full(256, 1.0)
        assert BA.spectral_flatness(p) == pytest.approx(1.0)
        p[100] = 1e6
        assert BA.spectral_flatness(p) < 0.2

    def test_a_single_periodogram_is_not_enough_and_that_is_why_the_gate_averages(self):
        # exp(E[log X])/E[X] for exponential X is e^-gamma = 0.561, so ONE frame of white noise
        # scores 0.44 "tonal" -- above the 0.20 threshold. Averaging the run's frames is not an
        # optimisation, it is what makes the number mean anything.
        one = np.random.RandomState(12).exponential(1.0, 512)
        assert BA.spectral_flatness(one) < 0.7
        many = np.mean([np.random.RandomState(s).exponential(1.0, 512)
                        for s in range(60)], axis=0)
        assert BA.spectral_flatness(many) > 0.97

    def test_two_bins_is_the_minimum(self):
        with pytest.raises(ValueError):
            BA.spectral_flatness([1.0])


class TestPulseRate:
    @pytest.mark.parametrize("rate", [8.0, 12.0, 20.0, 45.0])
    def test_recovers_the_fundamental_not_a_subharmonic(self, rate):
        # THE REGRESSION. Autocorrelation peaks at every multiple of the period, and the
        # (n-lag)/n bias correction lifts the longer lags, so argmax lands on a subharmonic:
        # measured 10 Hz for a 20 Hz train and 4 Hz for a 12 Hz one before the fundamental rule.
        n = int(3 * FS)
        env, fs_env = BA.decimate(BA.band_envelope(_clicks(n, rate), FS, *BAND), FS)
        p, got = BA.pulse_rate(env, fs_env)
        assert p > 0.5
        assert got == pytest.approx(rate, rel=0.05)

    def test_an_unmodulated_band_is_not_periodic(self):
        env, fs_env = BA.decimate(BA.band_envelope(_band_noise(int(3 * FS), 8.0), FS, *BAND), FS)
        p, _ = BA.pulse_rate(env, fs_env)
        assert p < BA.PERIODICITY_MIN

    def test_a_window_too_short_for_two_periods_is_none_not_zero(self):
        # Zero reads as "not periodic"; the truth is "not measurable", and those must not look
        # alike -- same rule as classify.score() returning None for a missing feature.
        p, rate = BA.pulse_rate(np.random.RandomState(2).normal(0, 1, 500), 1000.0,
                               rate_lo=1.0, rate_hi=2.0)
        assert p is None and rate is None

    @pytest.mark.parametrize("dur_s", [0.3, 0.4, 1.0])
    def test_the_short_window_guard_is_against_the_SLOWEST_rate_asked_for(self, dur_s):
        # THE REGRESSION. The guard used to compare against lag_lo, the lag of the FASTEST rate,
        # while the docstring promised the slowest. A window that cannot hold one period of the
        # true modulation was searched anyway, and the search reported a confident wrong answer.
        # Measured on the shipped code before the fix, on this exact envelope at the default
        # (2, 100) Hz range: 0.4 s -> (0.988, 100.0 Hz) and 0.3 s -> (1.000, 100.0 Hz), against a
        # true rate of 3 Hz. Two periods at rate_lo=2 Hz need just over 1 s, so all three of
        # these are unmeasurable and must say so.
        t = np.arange(int(dur_s * 1000.0)) / 1000.0
        env = 1.0 + 0.9 * np.sin(2 * np.pi * 3.0 * t)
        assert BA.pulse_rate(env, 1000.0) == (None, None)

    def test_naming_a_faster_rate_lo_is_what_buys_a_short_window(self):
        # The cost of the guard, stated as a test rather than left as a surprise: the caller may
        # have an answer from a short window, but only by declaring it is no longer looking for
        # slow modulation. 2 periods at rate_lo=8 Hz is 0.25 s.
        t = np.arange(int(0.4 * 1000.0)) / 1000.0
        env = 1.0 + 0.9 * np.sin(2 * np.pi * 12.0 * t)
        p, rate = BA.pulse_rate(env, 1000.0, rate_lo=8.0, rate_hi=100.0)
        assert p > 0.5 and rate == pytest.approx(12.0, rel=0.1)

    def test_a_genuine_slow_rate_is_recovered_once_the_window_is_long_enough(self):
        t = np.arange(int(2.0 * 1000.0)) / 1000.0
        env = 1.0 + 0.9 * np.sin(2 * np.pi * 3.0 * t)
        p, rate = BA.pulse_rate(env, 1000.0)
        assert p > 0.5 and rate == pytest.approx(3.0, rel=0.05)


class TestSustainedTonal:
    def test_finds_a_cicada_like_buzz(self):
        g = BA.TonalGate(FS)
        ev = _run(g, _noise(int(3 * FS)), _buzz(int(4 * FS)))
        assert len(ev) == 1
        d = ev[0]
        assert d["t_s"] == pytest.approx(3.0, abs=0.2)
        assert d["duration_s"] == pytest.approx(4.0, abs=0.3)
        assert d["tonal"] and not d["pulsed"]
        assert d["peak_hz"] == pytest.approx(CICADA_HZ, abs=2 * FS / BA.NFFT)

    def test_finds_a_katydid_like_pulse_train_on_periodicity_alone(self):
        # The carrier is band-limited NOISE, so tonality is near zero and only the envelope
        # autocorrelation can carry this detection. That is why structure is an OR, not an AND.
        g = BA.TonalGate(FS)
        ev = _run(g, _noise(int(3 * FS)), _clicks(int(4 * FS), 20.0))
        assert len(ev) == 1
        d = ev[0]
        assert d["pulsed"] and not d["tonal"]
        assert d["tonality"] < BA.TONALITY_MIN
        assert d["pulse_rate_hz"] == pytest.approx(20.0, rel=0.05)

    def test_confidence_rises_with_signal_to_noise(self):
        got = []
        for snr in (7.0, 12.0, 20.0):
            g = BA.TonalGate(FS)
            ev = _run(g, _noise(int(3 * FS)), _buzz(int(4 * FS), snr_db=snr))
            got.append(ev[0]["confidence"])
        assert got == sorted(got)
        assert 0.0 < got[0] < got[-1] < 1.0, "confidence is bounded and never certain"

    def test_a_short_gap_does_not_split_one_song_into_many(self):
        # The 2026-09-07 capture is the cautionary case. Measured on its dets.csv, over the 48
        # rows that carry a usable sketch (11 of the 59 set flags bit 1, insufficient context):
        # 16 of the 47 inter-detection intervals are under a second, so grouping at a 1 s gap
        # turns 48 rows into 32 events -- a 50% inflation in the raw count.
        n = int(2 * FS)
        gap = _noise(int(0.1 * FS))
        g = BA.TonalGate(FS)
        ev = _run(g, _noise(int(3 * FS)), _buzz(n), gap, _buzz(n, seed=7))
        assert len(ev) == 1, "a 100 ms gap is not the end of a song"


class TestRejection:
    def test_a_loud_impulse_is_not_a_call(self):
        # The signal the supersonic module is built for, at an amplitude that dwarfs any insect.
        # Rejected on duration, and COUNTED: "saw nothing" and "saw it and threw it away" are
        # different statements about a site.
        g = BA.TonalGate(FS)
        ev = _run(g, _noise(int(3 * FS)), _impulse(int(2 * FS), int(0.5 * FS)))
        assert ev == []
        assert g.n_short >= 1

    def test_sustained_broadband_noise_is_not_a_call(self):
        # Loud and long but shapeless: wind, traffic, a generator. Without structure as a hard
        # requirement this scored confidence 0.52, because a geometric mean of three terms cannot
        # be dragged low enough by one of them alone.
        g = BA.TonalGate(FS)
        n = int(4 * FS)
        ev = _run(g, _noise(int(3 * FS)), _noise(n, seed=8) + _band_noise(n, 8.0))
        assert ev == []
        assert g.n_unstructured >= 1

    def test_a_tone_below_the_snr_trigger_does_not_fire(self):
        g = BA.TonalGate(FS)
        assert _run(g, _noise(int(3 * FS)), _buzz(int(4 * FS), snr_db=2.0)) == []


class TestBandEdge:
    """Reporting when the signal is pressed against a wall, which the 2026-09-07 capture did not.

    46 of that night's 48 usable detections peak in mel band 0. Band 0's nonzero rfft bins run
    312.5-500 Hz, so a peak there is equally consistent with a source inside band 0 and with one
    below 300 Hz that the filterbank cannot see; the sketch cannot separate those. The conclusion
    that survives is the weaker one -- the energy sat at the bottom edge of the representation --
    and nothing in the output said even that. These fields exist to say it.
    """

    def test_a_peak_at_nyquist_is_flagged(self):
        g = BA.TonalGate(FS)
        ev = _run(g, _noise(int(3 * FS)), _buzz(int(4 * FS), f=7800.0))
        assert ev[0]["at_band_edge"] and ev[0]["edge"] == "high"
        assert ev[0]["limit"] == "nyquist", "the top of this band IS the Nyquist wall"

    def test_a_peak_at_the_bottom_of_the_band_is_flagged(self):
        g = BA.TonalGate(FS)
        ev = _run(g, _noise(int(3 * FS)), _buzz(int(4 * FS), f=4020.0))
        assert ev[0]["at_band_edge"] and ev[0]["edge"] == "low"

    def test_a_peak_in_the_middle_is_not_flagged(self):
        g = BA.TonalGate(FS)
        ev = _run(g, _noise(int(3 * FS)), _buzz(int(4 * FS)))
        assert not ev[0]["at_band_edge"] and ev[0]["edge"] is None

    def test_the_hardware_limit_is_not_folded_into_the_confidence(self):
        # Two different facts with two different remedies. Averaging them into one number destroys
        # the distinction the operator needs, so the flag is reported and the score left alone.
        edge = _run(BA.TonalGate(FS), _noise(int(3 * FS)), _buzz(int(4 * FS), f=7800.0))[0]
        mid = _run(BA.TonalGate(FS), _noise(int(3 * FS)), _buzz(int(4 * FS)))[0]
        assert edge["confidence"] == pytest.approx(mid["confidence"], abs=0.15)


class TestCannotGoDeaf:
    """The failure this module must not inherit.

    hear/node/detect.py updates its ambient only on the armed-and-below-threshold branch, so once
    a rising floor disarms it, it stops learning the very level that keeps it disarmed. The
    firmware measured the result in the field and fixed it there -- see `gate()` in
    firmware/night_node/night_node.ino and the `ALPHA_UP` constant above it, whose comment records
    the measurement: "156 s solid disarmed, envelope 1400-1600 against thr 800, ambient frozen at
    73.2, two detections all night". A chorus IS a floor that rises for hours, so this detector
    cannot afford it. These tests are the proof that it does not have it.
    """

    def test_the_floor_is_tracked_during_a_detection_not_only_between_them(self):
        g = BA.TonalGate(FS, floor_tau_up_s=5.0)
        g.process(_noise(int(3 * FS)), 0)
        quiet = g.floor_db
        g.process(_noise(int(60 * FS), sd=1500.0, seed=2), int(3 * FS))
        assert g.floor_db > quiet + 25.0, \
            "the floor must learn a level that rose while the gate was busy"

    def test_a_call_on_a_risen_floor_is_still_detected(self):
        g = BA.TonalGate(FS, floor_tau_up_s=5.0)
        n = 0
        for seg in (_noise(int(3 * FS)), _noise(int(60 * FS), sd=1500.0, seed=2)):
            g.process(seg, n)
            n += len(seg)
        ev = _run(g, _buzz(int(4 * FS), snr_db=12.0, sd=1500.0), start=n)
        assert len(ev) >= 1, "60 s of a raised floor must not leave the detector permanently deaf"

    def test_a_run_cannot_stay_open_for_ever(self):
        # The other way to be deaf: while a run is open no new one can start, so an unbounded run
        # is an unbounded deaf period. Runs are force-closed and reopened on the same frame, so a
        # 25 s song comes back as measured segments rather than one open-ended one.
        g = BA.TonalGate(FS, max_duration_s=10.0)
        ev = _run(g, _noise(int(3 * FS)), _buzz(int(25 * FS)))
        assert len(ev) == 3
        assert [d["end_reason"] for d in ev] == ["max_duration", "max_duration", "stream_end"]
        assert all(d["truncated"] for d in ev), "a segment's duration is a bound, not a length"

    def test_the_seam_belongs_to_exactly_one_segment(self):
        # The reopen used to close at last_hot + nfft and reopen at the same frame's start, so
        # 1024 samples (64 ms at the defaults) sat in two segments at once and were measured, and
        # reported, twice. Segments must tile the run, not overlap it.
        g = BA.TonalGate(FS, max_duration_s=10.0)
        ev = _run(g, _noise(int(3 * FS)), _buzz(int(25 * FS)))
        for a, b in zip(ev, ev[1:]):
            assert a["end_index"] == b["index"], "segments must be contiguous, not overlapping"
        assert sum(d["duration_s"] for d in ev) == pytest.approx(
            (ev[-1]["end_index"] - ev[0]["index"]) / FS, abs=1e-9)

    def test_a_chopped_song_says_which_segments_are_continuations(self):
        # A census that counted these as three events would be counting max_duration_s.
        g = BA.TonalGate(FS, max_duration_s=10.0)
        ev = _run(g, _noise(int(3 * FS)), _buzz(int(25 * FS)))
        assert [d["continues"] for d in ev] == [False, True, True]

    def test_one_shapeless_stretch_is_one_unstructured_count_whatever_the_chop(self):
        # n_unstructured used to increment once per SEGMENT, so a single wind gust scored 3 at
        # max_duration_s=10 and 2 at 20 -- a number about the setting, dressed up as a number
        # about the site. The stretch count is now invariant; the segment count is reported
        # separately and is expected to differ.
        n = int(25 * FS)
        loud = _noise(n, seed=8) + _band_noise(n, 8.0)
        got = {}
        for md in (10.0, 20.0):
            g = BA.TonalGate(FS, max_duration_s=md)
            assert _run(g, _noise(int(3 * FS)), loud) == []
            got[md] = (g.n_unstructured, g.n_unstructured_segments)
        assert got[10.0][0] == got[20.0][0] == 1, "one stretch of wind is one unstructured event"
        assert got[10.0][1] > got[20.0][1], "the segment count IS a function of max_duration_s"


class TestHonesty:
    """Every test in this class runs at the SHIPPED defaults.

    The one it replaces did not: it overrode floor_tau_up_s to 2.0 AND max_duration_s to 60.0 --
    values the module ships nowhere -- to make a `floor_absorbed` flag fire, and then presented
    the flag as a live safeguard. At the shipped 300 s and 10 s it cannot fire at all.
    """

    def test_the_floor_cannot_end_a_run_at_the_shipped_configuration(self):
        # The arithmetic, from the constants rather than from a memory of them: over one
        # full-length run the up-limb closes ~3.3% of the gap, so ending a run early would need
        # ~92 dB of in-band SNR. The `floor_absorbed` boolean that claimed to report this was
        # deleted rather than left as a flag that is structurally always False.
        g = BA.TonalGate(FS)
        frames = int(round(BA.MAX_DURATION_S / g.hop_s))
        closed = 1.0 - (1.0 - g._a_up) ** frames
        assert closed == pytest.approx(0.0327, abs=5e-4)
        assert BA.CLOSE_HYST_DB / closed == pytest.approx(91.7, abs=1.0)

    @pytest.mark.parametrize("snr", [12.0, 30.0, 60.0, 90.0])
    def test_the_measured_floor_rise_matches_that_arithmetic(self, snr):
        # And it is a MEASUREMENT on every event, not a verdict: floor_rise_db, the dB the floor
        # actually moved while the run was open. Even at a physically absurd 90 dB it stays under
        # close_hyst_db, which is the same statement as the test above, made against real frames.
        g = BA.TonalGate(FS)
        ev = _run(g, _noise(int(3 * FS)), _buzz(int(30 * FS), snr_db=snr))
        assert ev, "the buzz must be detected before its floor rise means anything"
        assert all(0.0 < d["floor_rise_db"] < BA.CLOSE_HYST_DB for d in ev)
        assert ev[0]["floor_rise_db"] == pytest.approx(0.0327 * snr, rel=0.35)

    def test_absorption_is_visible_across_events_not_inside_one(self):
        # What the deleted flag was reaching for, in the place it can actually be seen: a sound
        # that outlasts the run limit comes back as segments whose floor_db climbs and whose
        # snr_db shrinks. Measured at the shipped defaults on a 25 s 9 dB buzz: floor_db
        # 79.40 -> 79.72 -> 80.02 and snr_db 9.55 -> 9.25 -> 8.92.
        g = BA.TonalGate(FS)
        ev = _run(g, _noise(int(3 * FS)), _buzz(int(25 * FS)))
        assert len(ev) == 3
        floors = [d["floor_db"] for d in ev]
        snrs = [d["snr_db"] for d in ev]
        assert floors == sorted(floors) and floors[0] < floors[-1]
        assert snrs == sorted(snrs, reverse=True) and snrs[0] > snrs[-1]

    def test_every_event_carries_what_it_was_measured_against(self):
        d = _run(BA.TonalGate(FS), _noise(int(3 * FS)), _buzz(int(4 * FS)))[0]
        for k in ("band_hz", "floor_db", "snr_db", "n_frames", "confidence", "limit"):
            assert k in d
        assert d["band_hz"] == (pytest.approx(BAND[0]), pytest.approx(FS / 2.0 * BA.NYQUIST_USABLE))
        assert d["snr_db"] == pytest.approx(d["band_db"] - d["floor_db"], abs=1e-9)


class TestAbsoluteIndex:
    """⚠️THE ONE ERROR A TDoA SOLVE CANNOT SURVIVE IS A WRONG TIME.

    process() takes the absolute index of block[0]. It used to re-anchor to it only when the
    residual buffer happened to be empty, which is almost never, so on essentially every call the
    caller's index was read and discarded. This matters because real streams have holes: the
    2026-09-07 capture had 18 one-second windows come up short (health.csv, final drop_s=18),
    totalling drop_samples=42749 -- about 2.7 s of audio across 11.33 h, not 18 s,
    and every hole shifted every subsequent index, t_s and end_index by the size of the hole with
    nothing in the output saying so.
    """

    def test_block_start_is_honoured_on_every_call_not_just_the_first(self):
        # Measured on the shipped code before the fix: these two calls left the internal anchor
        # at 1024, so the second block was framed as samples 1000-1999 of the recording -- ten
        # million samples early.
        g = BA.TonalGate(FS)
        g.process(_noise(1000), 0)
        g.process(_noise(1000, seed=2), 10_000_000)
        assert g._buf_start == 10_000_000

    def test_an_event_after_a_gap_is_timestamped_where_the_caller_says_it_is(self):
        # The whole point, end to end: warm up, drop an hour of audio, then call. The event's
        # t_s must be an hour later, not three seconds later.
        g = BA.TonalGate(FS)
        g.process(_noise(int(3 * FS)), 0)
        skip = int(3600 * FS)
        ev = _run(g, _buzz(int(4 * FS)), start=int(3 * FS) + skip)
        assert ev, "the detector must still work after a gap"
        assert ev[0]["t_s"] == pytest.approx(3603.0, abs=0.3)

    def test_a_gap_is_counted_and_the_orphaned_residual_is_not_silently_reused(self):
        g = BA.TonalGate(FS)
        g.process(_noise(1000), 0)                       # 1000 < nfft, so all of it is residual
        g.process(_noise(1000, seed=2), 10_000)
        assert g.n_gaps == 1
        assert g.n_gap_samples == 10_000 - 1000
        assert g.n_discarded_samples == 1000, \
            "samples on the far side of a hole cannot be framed with samples on this side"

    def test_a_run_open_across_a_gap_is_closed_and_flagged_not_extended(self):
        # A run that spans a hole has a duration nobody measured. It is closed at the hole with
        # end_reason "stream_gap" and truncated True, which is the same contract as a run cut off
        # by the end of the stream.
        g = BA.TonalGate(FS)
        n = int(3 * FS) + int(4 * FS)
        g.process(_noise(int(3 * FS)), 0)
        out = g.process(_buzz(int(4 * FS)), int(3 * FS))
        assert g.in_run and out == []
        after = g.process(_noise(int(1 * FS)), n + int(60 * FS))
        assert len(after) == 1
        assert after[0]["end_reason"] == "stream_gap" and after[0]["truncated"]
        assert not g.in_run

    def test_a_stream_that_goes_backwards_is_refused_not_reconciled(self):
        # Overlapping blocks mean two candidate values for the same sample, and choosing one is
        # an invention. The module refuses instead -- same rule as hear/wire.py, which raises
        # rather than wrapping or clamping anything on its way to the wire.
        g = BA.TonalGate(FS)
        g.process(_noise(int(1 * FS)), 0)
        with pytest.raises(ValueError, match="behind"):
            g.process(_noise(int(1 * FS)), int(0.5 * FS))

    def test_contiguous_blocks_of_any_size_give_the_same_events(self):
        # The contract has to hold for a caller whose blocks do not divide into frames, which was
        # exactly the condition under which the old anchor was never refreshed.
        seg = [_noise(int(3 * FS)), _buzz(int(4 * FS))]
        one = _run(BA.TonalGate(FS), np.concatenate(seg))
        g, i, many = BA.TonalGate(FS), 0, []
        x = np.concatenate(seg)
        for k in range(0, x.size, 999):
            many += g.process(x[k:k + 999], i)
            i += len(x[k:k + 999])
        many += g.flush()
        assert [d["index"] for d in one] == [d["index"] for d in many]
        assert [round(d["duration_s"], 9) for d in one] == \
               [round(d["duration_s"], 9) for d in many]
