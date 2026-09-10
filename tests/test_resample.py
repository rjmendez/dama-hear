"""48 kHz acquisition -> the model's 32 kHz: what survives, and what is refused."""

import numpy as np
import pytest

from hear import resample as R


def tone(f, fs, secs=2.0, amp=0.5):
    t = np.arange(int(fs * secs)) / float(fs)
    return amp * np.sin(2 * np.pi * f * t)


class TestTheRateIsSnappedOrRefused:

    def test_a_measured_acquisition_rate_snaps(self):
        assert R.snap(48000) == 48000.0
        assert R.snap(47973.0) == 48000.0

    @pytest.mark.parametrize("bad", [16000.0, 22624.0, 32000.0, 44100.0])
    def test_any_other_rate_is_refused_not_stretched(self, bad):
        with pytest.raises(R.RateRefused):
            R.snap(bad)

    @pytest.mark.parametrize("bad", [0, 0.0, None, -48000.0])
    def test_zero_and_none_are_refused(self, bad):
        with pytest.raises(R.RateRefused):
            R.snap(bad)


class TestTheFilter:

    def _response(self):
        h = R.design(2, 3)
        w = np.linspace(0, 0.5, 8001)
        return w, np.abs(np.exp(-2j * np.pi * np.outer(w, np.arange(len(h)))) @ h) / 2

    def test_stopband_is_below_the_int16_floor(self):
        w, H = self._response()
        assert 20 * np.log10(H[w >= (0.5 / 3) * (1 + R.TRANSITION)].max()) < -65.0

    def test_passband_is_flat(self):
        w, H = self._response()
        pb = H[w <= (0.5 / 3) * (1 - R.TRANSITION)]
        assert 20 * np.log10(pb.max() / pb.min()) < 0.1

    def test_the_taps_are_odd(self):
        assert len(R.design(2, 3)) % 2 == 1


class TestWhatComesOut:

    def test_a_tone_keeps_its_frequency_and_level(self):
        y = R.resample(tone(1000, 48000), 48000, 32000)
        p = y["pcm"][len(y["pcm"]) // 4: -len(y["pcm"]) // 4]
        sp = np.abs(np.fft.rfft(p * np.hanning(len(p))))
        assert abs(np.fft.rfftfreq(len(p), 1 / 32000.0)[int(np.argmax(sp))] - 1000.0) < 2.0
        assert abs(p.std() - 0.5 / np.sqrt(2)) < 0.01

    def test_content_above_the_output_nyquist_does_not_fold_back(self):
        y = R.resample(tone(20000, 48000), 48000, 32000)["pcm"]
        p = y[len(y) // 4: -len(y) // 4]
        assert 20 * np.log10(p.std() * np.sqrt(2) / 0.5 + 1e-12) < -60.0

    def test_the_length_follows_the_ratio(self):
        assert abs(len(R.resample(np.zeros(240000), 48000, 32000)["pcm"]) - 160000) <= 2

    def test_the_measured_source_rate_is_recorded(self):
        y = R.resample(np.zeros(1000), 47973.0, 32000)
        assert y["fs_source_hz"] == pytest.approx(47973.0)
        assert (y["L"], y["M"]) == (2, 3)

    def test_a_same_rate_clip_passes_through_untouched(self):
        x = np.random.default_rng(1).normal(0, 0.2, 999)
        y = R.resample(x, 48000, 48000)
        assert y["taps"] == 0 and np.allclose(y["pcm"], x.astype(np.float32))
