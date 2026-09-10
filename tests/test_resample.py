"""Crossing to the classifier's rate: what survives, what is manufactured, and what is refused."""

import numpy as np
import pytest

from hear import resample as R


def tone(f, fs, secs=2.0, amp=0.5):
    t = np.arange(int(fs * secs)) / float(fs)
    return amp * np.sin(2 * np.pi * f * t)


class TestTheRateIsSnappedOrRefused:

    def test_a_measured_rate_snaps_to_the_nominal_one(self):
        """Nodes report fs_clean like 16004.741. Chasing that exactly would build a 32000-tap
        filter to correct four cents of pitch."""
        assert R.snap(16004.741) == 16000.0
        assert R.snap(15968.0) == 16000.0
        assert R.snap(47973.0) == 48000.0

    def test_machs_22624_hz_boot_is_refused_not_snapped(self):
        """⚠️mach shipped a whole boot headed 22624 Hz. Snapping it to 16 kHz would launder a
        firmware defect into audio a model answers confidently about."""
        for bad in (22624.0, 22848.0):
            with pytest.raises(R.RateRefused):
                R.snap(bad)

    def test_the_refusal_names_the_rates_it_would_have_accepted(self):
        with pytest.raises(R.RateRefused) as e:
            R.snap(22624.0)
        for r in ("16000", "48000"):
            assert r in str(e.value)

    def test_zero_and_none_are_refused_rather_than_dividing_by_zero(self):
        for bad in (0, 0.0, None, -16000.0):
            with pytest.raises(R.RateRefused):
                R.snap(bad)


class TestTheFilterIsMeasuredNotAssumed:

    @pytest.mark.parametrize("L,M", [(2, 3), (2, 1)])
    def test_stopband_is_below_the_16_bit_floor_of_the_source(self, L, M):
        """72 dB target. A source clip is int16, so a fold at -71 dB is not what limits it."""
        h = R.design(L, M)
        w = np.linspace(0, 0.5, 8001)
        H = np.abs(np.exp(-2j * np.pi * np.outer(w, np.arange(len(h)))) @ h) / L
        cut = 0.5 / max(L, M)
        sb = H[w >= cut * (1 + R.TRANSITION)]
        assert 20 * np.log10(sb.max()) < -65.0

    @pytest.mark.parametrize("L,M", [(2, 3), (2, 1)])
    def test_passband_is_flat(self, L, M):
        h = R.design(L, M)
        w = np.linspace(0, 0.5, 8001)
        H = np.abs(np.exp(-2j * np.pi * np.outer(w, np.arange(len(h)))) @ h) / L
        pb = H[w <= 0.5 / max(L, M) * (1 - R.TRANSITION)]
        assert 20 * np.log10(pb.max() / pb.min()) < 0.1

    def test_the_taps_are_odd_so_the_group_delay_is_a_whole_sample(self):
        for L, M in ((2, 3), (2, 1), (1, 3)):
            assert len(R.design(L, M)) % 2 == 1


class TestWhatComesOut:

    @pytest.mark.parametrize("src", [16000, 48000])
    def test_a_tone_keeps_its_frequency_and_its_level(self, src):
        y = R.resample(tone(1000, src), src, 32000)
        p = y["pcm"][len(y["pcm"]) // 4: -len(y["pcm"]) // 4]
        sp = np.abs(np.fft.rfft(p * np.hanning(len(p))))
        f = np.fft.rfftfreq(len(p), 1 / 32000.0)[int(np.argmax(sp))]
        assert abs(f - 1000.0) < 2.0
        assert abs(p.std() - 0.5 / np.sqrt(2)) < 0.01

    def test_content_above_the_output_nyquist_does_not_fold_back(self):
        """20 kHz at 48 kHz is above the 16 kHz output Nyquist. Without the filter it would
        reappear as 12 kHz and the tagger would score a tone that was never there."""
        y = R.resample(tone(20000, 48000), 48000, 32000)["pcm"]
        p = y[len(y) // 4: -len(y) // 4]
        assert 20 * np.log10(p.std() * np.sqrt(2) / 0.5 + 1e-12) < -60.0

    def test_the_length_follows_the_ratio(self):
        for src, dst in ((48000, 32000), (16000, 32000)):
            n = src * 5
            y = R.resample(np.zeros(n), src, dst)
            assert abs(len(y["pcm"]) - n * dst / src) <= 2


class TestTheManufacturedBandIsLabelled:
    """⚠️The whole reason this module returns a dict instead of an array."""

    def test_upsampling_from_16k_declares_its_band_limit_and_flags_itself(self):
        y = R.resample(np.zeros(16000), 16000, 32000)
        assert y["upsampled"] is True
        assert y["band_limit_hz"] == 8000.0
        assert y["fs_hz"] == 32000.0

    def test_decimating_from_48k_is_not_flagged_and_keeps_the_full_output_band(self):
        y = R.resample(np.zeros(48000), 48000, 32000)
        assert y["upsampled"] is False
        assert y["band_limit_hz"] == 16000.0

    def test_the_band_above_the_limit_really_is_empty_on_an_upsampled_clip(self):
        """Not a label check -- the actual spectrum. If the interpolation filter let images
        through, `band_limit_hz` would be a claim the audio contradicts."""
        rng = np.random.default_rng(3)
        x = rng.normal(0, 0.1, 16000 * 2)
        y = R.resample(x, 16000, 32000)["pcm"]
        p = y[len(y) // 4: -len(y) // 4]
        sp = np.abs(np.fft.rfft(p * np.hanning(len(p)))) ** 2
        f = np.fft.rfftfreq(len(p), 1 / 32000.0)
        below = sp[(f > 200) & (f < 7000)].mean()
        above = sp[f > 9000].mean()
        assert 10 * np.log10(above / below) < -60.0

    def test_the_source_rate_survives_into_the_record(self):
        y = R.resample(np.zeros(1000), 16004.741, 32000)
        assert y["fs_source_hz"] == pytest.approx(16004.741)
        assert y["fs_source_nominal_hz"] == 16000.0

    def test_a_same_rate_clip_is_passed_through_untouched(self):
        rng = np.random.default_rng(1)
        x = rng.normal(0, 0.2, 999).astype(np.float64)
        y = R.resample(x, 32000, 32000)
        assert y["taps"] == 0 and y["L"] == 1 and y["M"] == 1
        assert np.allclose(y["pcm"], x.astype(np.float32))
