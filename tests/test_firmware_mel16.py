"""The node's compiled filterbank must BE hear.sketch's, and its frames must say which one.

⚠️A NODE CANNOT JUST SET THE LAYOUT BIT. Under the legacy layout the bank is rescaled to each
node's Nyquist, so band 12 is 3072 Hz at 16 kHz and 5826 Hz at 48 kHz. Setting bit 12 without
regenerating mel16.h would ship a frame CLAIMING the shared axis while carrying rescaled data --
worse than leaving it unset, because the far side would then stack it with a phone's. The bit and
the table come from one call to hear.sketch in firmware/gen_mel.py so they cannot disagree.

There is no ESP32 toolchain in CI, so this reads the generated header and reimplements
sketch_frame() from it -- the same technique the Kotlin port is checked with.
"""
import pathlib
import re
import sys

import numpy as np
import pytest

from hear import sketch as SK

ROOT = pathlib.Path(__file__).resolve().parents[1]
HDRS = [ROOT / "firmware" / sub / "mel16.h" for sub in ("night_node", "path_test")]


def _load(path):
    if not path.exists():
        pytest.skip("%s not in this checkout" % path)
    h = path.read_text()

    def d(n):
        m = re.search(r"#define\s+%s\s+(0x[0-9a-fA-F]+|[0-9.]+f?)" % n, h)
        assert m, "%s missing from %s" % (n, path.name)
        v = m.group(1)
        return int(v, 16) if v.startswith("0x") else float(v.rstrip("f"))

    def arr(n):
        m = re.search(r"\b%s\[\d+\]\s*=\s*\{(.*?)\};" % n, h, re.S)
        assert m, "%s missing" % n
        return [float(t) for t in re.findall(r"-?[0-9.]+(?:e[-+]?\d+)?", m.group(1).replace("f", ""))]

    return d, arr


@pytest.mark.parametrize("path", HDRS, ids=lambda p: p.parent.name)
def test_the_compiled_bank_is_hears_fixed_axis_bank(path):
    d, arr = _load(path)
    fs, nfft, bands = d("MEL16_FS"), int(d("MEL16_NFFT")), int(d("MEL16_BANDS"))
    lo = [int(v) for v in arr("MEL16_FB_LO")]
    n = [int(v) for v in arr("MEL16_FB_N")]
    w = np.array(arr("MEL16_FB_W"))
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
    assert bands - len(empty) == int(d("MEL16_VALID_BANDS"))
    assert np.allclose(np.array(arr("MEL16_WIN")), np.hanning(nfft), atol=1e-7)


@pytest.mark.parametrize("path", HDRS, ids=lambda p: p.parent.name)
def test_the_header_declares_the_rate_and_the_layout(path):
    d, _ = _load(path)
    fs = d("MEL16_FS")
    assert int(d("MEL16_FS_CODE")) == SK.fs_code(fs) != 0, "an unstated rate is not usable"
    assert int(d("MEL16_LAYOUT_BIT")) == SK.LAYOUT_BIT
    assert int(d("MEL16_VALID_BANDS")) == SK.valid_bands(fs, layout=SK.LAYOUT_FIXED)
    assert int(d("MEL16_FRAME_BYTES")) == SK.wire_size()


def test_the_node_arithmetic_reproduces_hear_sketch_byte_for_byte():
    """sketch_frame() reimplemented from the compiled table. Any drift between the two is a
    classifier trained on one side's bytes applied to the other's."""
    d, arr = _load(HDRS[0])
    fs, nfft, hop = d("MEL16_FS"), int(d("MEL16_NFFT")), int(d("MEL16_HOP"))
    bands, frames = int(d("MEL16_BANDS")), int(d("MEL16_FRAMES"))
    win = np.array(arr("MEL16_WIN"))
    lo = [int(v) for v in arr("MEL16_FB_LO")]
    n = [int(v) for v in arr("MEL16_FB_N")]
    w = np.array(arr("MEL16_FB_W"))

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
        return np.clip(np.round((db - ref) * 2.0), -128, 127).astype(np.int8).reshape(bands, frames), float(ref)

    rng = np.random.default_rng(20260908)
    for i in range(6):
        x = rng.normal(0, 3000, 4096)
        x[500:520] += 9000                       # an impulse, not just noise
        qn, rn = node_sketch(x)
        qp, rp = SK.sketch(x, fs, layout=SK.LAYOUT_FIXED)
        assert np.array_equal(qn, qp), "case %d: %d of %d bytes differ" % (i, int((qn != qp).sum()), qn.size)
        assert abs(rn - rp) < 1e-9


def test_a_frame_with_those_flags_decodes_and_the_fleet_model_takes_it():
    """The point of the change: these nodes' frames were refused by every shipped model."""
    sys.path.insert(0, str(ROOT / "modules" / "supersonic"))
    import classify as CL
    d, _ = _load(HDRS[0])
    fs = d("MEL16_FS")
    flags = (int(d("MEL16_FS_CODE")) << SK.FS_SHIFT) | int(d("MEL16_LAYOUT_BIT")) | 0x0002
    q, ref = SK.sketch(np.random.default_rng(3).normal(0, 3000, 4096), fs, layout=SK.LAYOUT_FIXED)
    raw = bytearray(SK.pack(1, ref, 800, q, fs=fs, layout=SK.LAYOUT_FIXED))
    raw[10], raw[11] = flags & 0xFF, (flags >> 8) & 0xFF      # the node writes flags verbatim
    u = SK.unpack(bytes(raw))
    assert u["fs_hz"] == 16000.0 and u["layout"] == SK.LAYOUT_FIXED and u["valid_bands"] == 15
    assert u["event_flags"] == 0x02 and u["retrigger"] is False
    assert (u["q"][15:] == u["q"].min()).all(), "the bands above Nyquist must sit at the floor"
    # the fleet model takes it; the 20-band one still refuses, naming what to use
    assert 0.0 <= CL.score_sketch(u, CL.load_model(CL.FLEET_SKETCH_MODEL)) <= 1.0
    with pytest.raises(CL.SketchMismatch, match="15 of this frame's bands"):
        CL.score_sketch(u, CL.load_model(CL.DEFAULT_SKETCH_MODEL))
