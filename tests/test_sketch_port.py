"""Golden vectors for the Kotlin port, and a mirror of its exact logic.

The Kotlin in dama-gotchi (`AcousticSketch.kt`) uses a direct DFT and an explicit Hann window
where this module uses numpy. A classifier trained on one side's bytes is applied to the other's,
so the port has to agree on every one of the 160 quantised bytes, not approximately.

This file cannot run Kotlin. What it does is (a) assert the golden vectors are SELF-CONSISTENT --
the stored int16 PCM must reproduce the stored sketch -- and (b) re-implement the Kotlin's exact
arithmetic in Python and check that too. The first version of the generator failed (a): it
sketched the float signal and stored the rounded int16, unreproducible by 24 of 160 bytes on an
impulse and 0.0005 dB on a tone.
"""
import base64
import json
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import sketch as S  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "testdata", "sketch_golden.json")


def _g():
    with open(GOLDEN) as fh:
        return json.load(fh)


def _pcm(c):
    return np.frombuffer(base64.b64decode(c["pcm_b64"]), dtype="<i2").astype(np.float64)


def _q(c):
    return np.frombuffer(base64.b64decode(c["q_b64"]), dtype=np.int8).reshape(
        S.MEL_BANDS, S.FRAMES)


# ---- the Kotlin's exact arithmetic, transcribed ------------------------------------------
def _kt_mel(fs, nfft=S.NFFT, bands=S.MEL_BANDS, f_lo=S.F_LO, f_hi=S.F_HI, fixed=False):
    hi = f_hi if fixed else min(f_hi, fs / 2.0 * 0.98)
    nb = nfft // 2 + 1
    h = lambda f: 2595.0 * math.log10(1.0 + f / 700.0)          # noqa: E731
    m = lambda v: 700.0 * (10.0 ** (v / 2595.0) - 1.0)          # noqa: E731
    edges = [m(h(f_lo) + (h(hi) - h(f_lo)) * i / (bands + 1.0)) for i in range(bands + 2)]
    freqs = [k * fs / nfft for k in range(nb)]
    out = []
    for b in range(bands):
        lo, mid, up = edges[b], edges[b + 1], edges[b + 2]
        row = [0.0] * nb
        if up > lo:
            s = 0.0
            for k in range(nb):
                v = max(0.0, min((freqs[k] - lo) / max(mid - lo, 1e-9),
                                 (up - freqs[k]) / max(up - mid, 1e-9)))
                row[k] = v
                s += v
            if s > 0:
                row = [v / s for v in row]
        out.append(row)
    return out


def _kt_sketch(x, fs, fixed=False):
    fb = _kt_mel(fs, fixed=fixed)
    hop = max(1, int(S.HOP_S * fs))
    w = [0.5 - 0.5 * math.cos(2.0 * math.pi * i / (S.NFFT - 1)) for i in range(S.NFFT)]
    db = [0.0] * (S.MEL_BANDS * S.FRAMES)
    ref = -1e30
    for t in range(S.FRAMES):
        s0 = t * hop
        xx = [(x[s0 + i] if s0 + i < len(x) else 0.0) * w[i] for i in range(S.NFFT)]
        p = []
        for k in range(S.NFFT // 2 + 1):
            re = im = 0.0
            for n in range(S.NFFT):
                a = -2.0 * math.pi * k * n / S.NFFT
                re += xx[n] * math.cos(a)
                im += xx[n] * math.sin(a)
            p.append(re * re + im * im)
        for b in range(S.MEL_BANDS):
            v = 10.0 * math.log10(sum(fb[b][k] * p[k] for k in range(len(p))) + 1e-12)
            db[b * S.FRAMES + t] = v
            ref = max(ref, v)
    q = [max(-128, min(127, int(round((v - ref) * 2.0)))) for v in db]
    return np.array(q, dtype=np.int8).reshape(S.MEL_BANDS, S.FRAMES), ref


class TestGoldenSelfConsistency:
    """The stored PCM must reproduce the stored sketch. This is what caught the generator bug."""

    def test_file_exists_and_declares_this_geometry(self):
        g = _g()
        assert g["mel_bands"] == S.MEL_BANDS and g["frames"] == S.FRAMES
        assert g["wire_size"] == S.wire_size()

    @pytest.mark.parametrize("i", range(9))
    def test_reference_reproduces_each_vector_from_its_own_pcm(self, i):
        c = _g()["cases"][i]
        q, ref = S.sketch(_pcm(c), c["fs"], layout=c.get("layout", S.LAYOUT_NYQUIST))
        assert np.array_equal(q, _q(c)), "%s: sketch not reproducible from stored PCM" % c["name"]
        assert abs(ref - c["ref_db"]) < 1e-12

    def test_stored_frame_matches_a_fresh_pack(self):
        for c in _g()["cases"]:
            x = _pcm(c)
            lay = c.get("layout", S.LAYOUT_NYQUIST)
            q, ref = S.sketch(x, c["fs"], layout=lay)
            frame = S.pack(123456, ref, int(min(np.abs(x).max(), 65535)), q,
                           fs=c["fs"], layout=lay)
            assert frame == base64.b64decode(c["frame_b64"]), c["name"]

    def test_frame_states_its_own_sample_rate(self):
        """20 bands span 300 Hz-20 kHz at 48 kHz and 300 Hz-7.84 kHz at 16 kHz. Two frames that
        do not say which are not comparable, and used to be indistinguishable."""
        for c in _g()["cases"]:
            d = S.unpack(base64.b64decode(c["frame_b64"]))
            assert d["fs_code"] == c["fs_code"], c["name"]
            assert d["fs_hz"] == c["fs"], c["name"]
            assert np.allclose(d["band_edges_hz"], c["band_edges_hz"], atol=1e-3), c["name"]

    def test_the_sample_rate_code_does_not_disturb_the_event_flags(self):
        q, ref = S.sketch(np.zeros(4096), 48000.0)
        for flags in (0, 1, 0xFF):
            d = S.unpack(S.pack(1, ref, 0, q, flags=flags, fs=48000.0))
            assert d["event_flags"] == flags
            assert d["retrigger"] is bool(flags & 1)
            assert d["fs_hz"] == 48000.0
        # an unstated rate is not a guess
        assert S.unpack(S.pack(1, ref, 0, q))["fs_hz"] is None


class TestKotlinArithmetic:
    """The port uses a direct DFT and an explicit Hann; numpy must agree byte-for-byte."""

    @pytest.mark.parametrize("i", range(9))
    def test_kotlin_logic_matches_numpy_exactly(self, i):
        c = _g()["cases"][i]
        q, ref = _kt_sketch(list(_pcm(c)), c["fs"],
                            fixed=c.get("layout") == S.LAYOUT_FIXED)
        assert np.array_equal(q, _q(c)), \
            "%s: %d of 160 bytes differ" % (c["name"], int((q != _q(c)).sum()))
        assert abs(ref - c["ref_db"]) < 1e-9

    def test_the_16k_case_is_present(self):
        # a phone pulled at 16 kHz and one at 48 must both port correctly; the mel bank's
        # Nyquist clamp is the only thing that differs between them
        assert any(c["fs"] == 16000.0 for c in _g()["cases"])
