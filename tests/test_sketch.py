"""Log-mel sketch: wire format, quantisation, and the Meshtastic size ceiling."""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import sketch as S  # noqa: E402

FS = 48000.0


def _impulse(n=4096, at=100, amp=20000.0):
    x = np.random.RandomState(0).normal(0, 30, n)
    x[at:at + 12] += amp * np.hanning(12)
    return x


class TestSize:
    def test_default_fits_one_meshtastic_packet(self):
        # 237 B payload; the default must leave room for protobuf + portnum framing
        assert S.wire_size() == 172
        assert S.fits_meshtastic()

    def test_bigger_geometries_are_correctly_refused(self):
        for b, f in ((24, 8), (32, 6), (40, 8)):
            assert not S.fits_meshtastic(b, f), "%dx%d should not fit" % (b, f)

    def test_pack_length_matches_wire_size(self):
        q, ref = S.sketch(_impulse(), FS)
        assert len(S.pack(123456, ref, 20000, q)) == S.wire_size()


class TestRoundTrip:
    def test_unpack_recovers_the_header(self):
        q, ref = S.sketch(_impulse(), FS)
        got = S.unpack(S.pack(999999, ref, 31000, q, flags=3))
        assert got["node_us"] == 999999
        assert got["peak"] == 31000
        assert got["flags"] == 3
        assert got["ref_db"] == pytest.approx(ref, abs=0.25)   # 0.25 dB header quantisation

    def test_unpack_recovers_the_spectrogram_shape(self):
        q, ref = S.sketch(_impulse(), FS)
        got = S.unpack(S.pack(1, ref, 1, q))
        assert got["q"].shape == (S.MEL_BANDS, S.FRAMES)
        assert np.array_equal(got["q"], q)

    def test_absolute_db_is_recoverable(self):
        # THE POINT of sending ref_db. Amplitude alone reached AUC 0.90 on real events; a sketch
        # that carried only shape would throw that away.
        q, ref = S.sketch(_impulse(amp=20000.0), FS)
        got = S.unpack(S.pack(1, ref, 1, q))
        assert got["db"].max() == pytest.approx(ref, abs=0.5)


class TestQuantisation:
    def test_loud_and_quiet_events_differ_in_ref_not_in_shape(self):
        # per-event scaling: the same sound 20 dB down must keep its shape and move its reference
        loud = _impulse(amp=20000.0)
        quiet = loud * 0.1
        ql, rl = S.sketch(loud, FS)
        qq, rq = S.sketch(quiet, FS)
        assert rq == pytest.approx(rl - 20.0, abs=1.5)
        assert np.abs(ql.astype(int) - qq.astype(int)).mean() < 6

    def test_int8_range_is_respected(self):
        q, _ = S.sketch(_impulse(), FS)
        assert q.dtype == np.int8 and q.max() <= 127 and q.min() >= -128

    def test_silence_does_not_blow_up(self):
        q, ref = S.sketch(np.zeros(4096), FS)
        assert np.isfinite(ref) and np.isfinite(q).all()


class TestFilterbank:
    def test_bands_are_normalised_and_ordered(self):
        fb = S.mel_filterbank(FS)
        assert fb.shape[0] == S.MEL_BANDS
        assert (fb >= 0).all()
        peaks = [int(np.argmax(fb[b])) for b in range(S.MEL_BANDS) if fb[b].max() > 0]
        assert peaks == sorted(peaks), "mel band centres must increase"

    def test_upper_edge_respects_nyquist(self):
        # a 16 kHz node must not be handed 20 kHz band edges
        fb = S.mel_filterbank(16000.0)
        freqs = np.fft.rfftfreq(S.NFFT, 1.0 / 16000.0)
        top = max(freqs[fb[b] > 0].max() for b in range(S.MEL_BANDS) if fb[b].max() > 0)
        assert top <= 8000.0
