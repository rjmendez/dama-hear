"""The node's compiled filterbank must BE hear.sketch's, and its frames must say which one.

⚠️A NODE CANNOT JUST SET THE LAYOUT BIT. Under the legacy layout the bank is rescaled to each
node's Nyquist, so band 12 is 3072 Hz at 16 kHz and 5826 Hz at 48 kHz. Setting bit 12 without
regenerating the header would ship a frame CLAIMING the shared axis while carrying rescaled data
-- worse than leaving it unset, because the far side would then stack it with a phone's. The bit
and the table come from one call to hear.sketch in firmware/gen_mel.py so they cannot disagree.

There is no ESP32 toolchain in CI, so this reads the generated header and reimplements
sketch_frame() from it -- the same technique the Kotlin port is checked with.

TWO BANKS, TWO RATES. night_node's own sketch moved to the acquisition rate (48 kHz); path_test's
did not (firmware/path_test/path_test.ino:143 feeds its MEL16_FS straight into i2s.begin, so
regenerating it would silently move a second sketch's microphone). The renamed file
(firmware/night_node/mel_impulse.h, prefix MELIMP_ -- not mel16.h/MEL16_ any more, deliberately:
see test_the_two_banks_are_at_different_rates_and_only_one_of_them_moved) is what makes a stale
reference to the old name fail loudly instead of reading a real, valid, wrong-rate header.
"""
import pathlib
import re
import sys

import numpy as np
import pytest

from hear import sketch as SK

ROOT = pathlib.Path(__file__).resolve().parents[1]
IMPULSE = ROOT / "firmware" / "night_node" / "mel_impulse.h"
PATH_TEST = ROOT / "firmware" / "path_test" / "mel16.h"
BOARD = ROOT / "firmware" / "boards" / "xiao_s3_sense.h"
INO = ROOT / "firmware" / "night_node" / "night_node.ino"

#: (header, its #define prefix). Every test below is parametrized over this pair rather than
#: hardcoding either, so a rate or a name checked for one bank is checked for both.
HDRS = [(IMPULSE, "MELIMP"), (PATH_TEST, "MEL16")]


def _load(path, prefix):
    if not path.exists():
        pytest.skip("%s not in this checkout" % path)
    h = path.read_text()

    def d(suffix):
        n = "%s_%s" % (prefix, suffix)
        m = re.search(r"#define\s+%s\s+(0x[0-9a-fA-F]+|[0-9.]+f?)" % n, h)
        assert m, "%s missing from %s" % (n, path.name)
        v = m.group(1)
        return int(v, 16) if v.startswith("0x") else float(v.rstrip("f"))

    def arr(suffix):
        n = "%s_%s" % (prefix, suffix)
        m = re.search(r"\b%s\[\d+\]\s*=\s*\{(.*?)\};" % n, h, re.S)
        assert m, "%s missing" % n
        return [float(t) for t in re.findall(r"-?[0-9.]+(?:e[-+]?\d+)?", m.group(1).replace("f", ""))]

    return d, arr


@pytest.mark.parametrize("path,prefix", HDRS, ids=[p.parent.name for p, _ in HDRS])
def test_the_compiled_bank_is_hears_fixed_axis_bank(path, prefix):
    d, arr = _load(path, prefix)
    fs, nfft, bands = d("FS"), int(d("NFFT")), int(d("BANDS"))
    lo = [int(v) for v in arr("FB_LO")]
    n = [int(v) for v in arr("FB_N")]
    w = np.array(arr("FB_W"))
    fb = SK.mel_filterbank(fs, nfft, bands, layout=SK.LAYOUT_FIXED)
    off = 0
    empty = []
    for b in range(bands):
        if n[b] == 0:
            empty.append(b)
            assert not fb[b].any(), "band %d empty on the node, populated in hear" % b
            continue
        got, want = w[off:off + n[b]], fb[b][lo[b]:lo[b] + n[b]]
        off += n[b]
        assert np.abs(got - want).max() < 1e-6, "band %d weights differ" % b
        assert not fb[b][:lo[b]].any() and not fb[b][lo[b] + n[b]:].any(), \
            "band %d support runs outside the stored span" % b
    assert bands - len(empty) == int(d("VALID_BANDS"))
    assert np.allclose(np.array(arr("WIN")), np.hanning(nfft), atol=1e-7)


@pytest.mark.parametrize("path,prefix", HDRS, ids=[p.parent.name for p, _ in HDRS])
def test_the_header_declares_the_rate_and_the_layout(path, prefix):
    d, _ = _load(path, prefix)
    fs = d("FS")
    assert int(d("FS_CODE")) == SK.fs_code(fs) != 0, "an unstated rate is not usable"
    assert int(d("LAYOUT_BIT")) == SK.LAYOUT_BIT
    assert int(d("VALID_BANDS")) == SK.valid_bands(fs, layout=SK.LAYOUT_FIXED)
    assert int(d("FRAME_BYTES")) == SK.wire_size()


def test_the_two_banks_are_at_different_rates_and_only_one_of_them_moved():
    """Fails against the un-renamed two-file layout this replaced: mel16.h regenerated for BOTH
    night_node and path_test at one shared rate, which is the exact defect (D1) that made
    path_test's own i2s.begin(MEL16_FS) move with it."""
    fs_nominal = float(re.search(r"#define\s+FS_NOMINAL\s+(\d+)", BOARD.read_text()).group(1))
    decim = int(re.search(r"#define\s+DECIM\s+(\d+)", INO.read_text()).group(1))
    d_impulse, _ = _load(IMPULSE, "MELIMP")
    d_path, _ = _load(PATH_TEST, "MEL16")
    assert d_path("FS") == fs_nominal
    assert d_impulse("FS") == fs_nominal * decim
    assert not any(re.search(r"\bMEL16_FS\b", p.read_text())
                  for p in ROOT.glob("firmware/night_node/*.h")), \
        "a night_node header still defines MEL16_FS: the old name is back"


def test_the_node_arithmetic_reproduces_hear_sketch_byte_for_byte():
    """sketch_frame() reimplemented from the compiled table. Any drift between the two is a
    classifier trained on one side's bytes applied to the other's -- checked at BOTH rates, since
    they are now different measurements, not just different numbers."""
    for path, prefix in HDRS:
        d, arr = _load(path, prefix)
        fs, nfft, hop = d("FS"), int(d("NFFT")), int(d("HOP"))
        bands, frames = int(d("BANDS")), int(d("FRAMES"))
        win = np.array(arr("WIN"))
        lo = [int(v) for v in arr("FB_LO")]
        n = [int(v) for v in arr("FB_N")]
        w = np.array(arr("FB_W"))

        def node_sketch(x):
            db = np.zeros(bands * frames)
            for t in range(frames):
                seg = x[t * hop:t * hop + nfft]
                if len(seg) < nfft:
                    seg = np.pad(seg, (0, nfft - len(seg)))
                P = np.abs(np.fft.rfft(seg * win, nfft)) ** 2
                off = 0
                for b in range(bands):
                    acc = float((w[off:off + n[b]] * P[lo[b]:lo[b] + n[b]]).sum()) if n[b] else 0.0
                    off += n[b]
                    db[b * frames + t] = 10.0 * np.log10(acc + 1e-12)
            ref = db.max()
            return (np.clip(np.round((db - ref) * 2.0), -128, 127)
                   .astype(np.int8).reshape(bands, frames), float(ref))

        rng = np.random.default_rng(20260908)
        for i in range(6):
            x = rng.normal(0, 3000, 4096)
            x[500:520] += 9000                       # an impulse, not just noise
            qn, rn = node_sketch(x)
            qp, rp = SK.sketch(x, fs, layout=SK.LAYOUT_FIXED)
            assert np.array_equal(qn, qp), \
                "%s case %d: %d of %d bytes differ" % (prefix, i, int((qn != qp).sum()), qn.size)
            assert abs(rn - rp) < 1e-9


def test_a_frame_with_those_flags_decodes_and_the_fleet_model_takes_it():
    """The point of the original change: a 16 kHz node's frames were refused by every shipped
    model. Now split in two (D1/T2), because the two headers no longer agree on anything a model
    cares about: night_node's 48 kHz frame is what DEFAULT_SKETCH_MODEL (20-band) was fitted on
    and path_test's 16 kHz frame is what still needs FLEET_SKETCH_MODEL (15-band)."""
    sys.path.insert(0, str(ROOT / "modules" / "supersonic"))
    import classify as CL

    def _framed(path, prefix, no_context_bit=0x0002):
        d, _ = _load(path, prefix)
        fs = d("FS")
        flags = (int(d("FS_CODE")) << SK.FS_SHIFT) | int(d("LAYOUT_BIT")) | no_context_bit
        q, ref = SK.sketch(np.random.default_rng(3).normal(0, 3000, 4096), fs, layout=SK.LAYOUT_FIXED)
        raw = bytearray(SK.pack(1, ref, 800, q, fs=fs, layout=SK.LAYOUT_FIXED))
        raw[10], raw[11] = flags & 0xFF, (flags >> 8) & 0xFF      # the node writes flags verbatim
        return SK.unpack(bytes(raw))

    u48 = _framed(IMPULSE, "MELIMP")
    assert u48["fs_hz"] == 48000.0 and u48["layout"] == SK.LAYOUT_FIXED and u48["valid_bands"] == 20
    assert u48["event_flags"] == 0x02 and u48["retrigger"] is False
    assert 0.0 <= CL.score_sketch(u48, CL.load_model(CL.DEFAULT_SKETCH_MODEL)) <= 1.0

    u16 = _framed(PATH_TEST, "MEL16")
    assert u16["fs_hz"] == 16000.0 and u16["layout"] == SK.LAYOUT_FIXED and u16["valid_bands"] == 15
    assert (u16["q"][15:] == u16["q"].min()).all(), "the bands above Nyquist must sit at the floor"
    # the fleet model takes the 16 kHz frame; the 20-band one still refuses it, naming what to use
    assert 0.0 <= CL.score_sketch(u16, CL.load_model(CL.FLEET_SKETCH_MODEL)) <= 1.0
    with pytest.raises(CL.SketchMismatch, match="15 of this frame's bands"):
        CL.score_sketch(u16, CL.load_model(CL.DEFAULT_SKETCH_MODEL))
