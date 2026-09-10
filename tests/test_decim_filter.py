"""The anti-alias filter's claims, checked rather than asserted in a comment.

⚠️THE FILTER REPLACED ONE THE MICROPHONE USED TO PROVIDE. Running the PDM mic at 16 kHz let its
internal decimator do the anti-aliasing; acquiring at 32 kHz and decimating in software moved that
job into decim.h. A filter that is quietly worse than the one it displaced degrades the top of the
scene band -- and the corpus has 228k rows measured through the old path, so the degradation would
show up as a trend, not as an error.

⚠️AND IT IS MEASURED ON THE QUANTISED TAPS. The float design reads -74.8 dB; the int16 filter that
actually runs on the node reads -64.0 dB, and an earlier version of the generator shipped -39.0 dB
without anyone noticing, because it was only ever checked in float. Every number below comes from
DECIM_H as the firmware will use it.
"""
import os
import pathlib
import re
import subprocess
import sys

import numpy as np
import pytest

scipy_signal = pytest.importorskip("scipy.signal")

ROOT = pathlib.Path(__file__).resolve().parents[1]
HDR = ROOT / "firmware" / "hear_node" / "decim.h"
GEN = ROOT / "firmware" / "gen_decim.py"

#: The band the scene mel bank actually uses.
TOP_HZ = 7812.5
FS_ACQ = 48000.0
FS_DEC = 16000.0


def _strip_c_comments(src):
    """A source-scanning guard that does not strip comments matches its own prose: hear_node.ino's
    prose names both banks and both rates in the paragraphs that explain the asserts."""
    return re.sub(r"//.*", "", re.sub(r"/\*.*?\*/", " ", src, flags=re.S))


def _defines():
    txt = HDR.read_text()
    d = {k: int(v) for k, v in re.findall(r"#define\s+(DECIM_\w+)\s+(\d+)", txt)}
    taps = [int(x) for x in re.findall(r"(-?\d+),", txt.split("DECIM_H[DECIM_TAPS] = {")[1])]
    return d, np.array(taps, dtype=np.int64)


def _response(taps, shift):
    w, H = scipy_signal.freqz(taps / float(1 << shift), worN=32768, fs=FS_ACQ)
    return w, np.abs(H)


def test_the_header_is_what_the_generator_produces():
    """Hand-editing decim.h would put a filter on the node that no measurement here describes."""
    out = subprocess.run([sys.executable, str(GEN)], capture_output=True, text=True,
                         cwd=str(ROOT))
    assert out.returncode == 0, out.stderr
    assert out.stdout == HDR.read_text(), (
        "decim.h has drifted from gen_decim.py. Regenerate:\n"
        "    python3 firmware/gen_decim.py > firmware/hear_node/decim.h")


def test_the_taps_sum_to_unity_so_the_band_levels_do_not_drift():
    d, taps = _defines()
    assert int(taps.sum()) == (1 << d["DECIM_SHIFT"]), (
        "DC gain is not exactly 1; every scene band would be scaled by %.4f"
        % (taps.sum() / float(1 << d["DECIM_SHIFT"])))


def test_every_tap_fits_int16():
    _d, taps = _defines()
    assert int(np.abs(taps).max()) < 32768


def test_the_group_delay_is_a_whole_number_of_acquisition_samples():
    """⚠️acq_of() SUBTRACTS THIS. A fractional delay could not be compensated by an integer index,
    so a clip would start a fraction of a sample early or late for ever."""
    d, _t = _defines()
    assert d["DECIM_DELAY"] == (d["DECIM_TAPS"] - 1) // 2
    assert (d["DECIM_TAPS"] - 1) % 2 == 0, "an even-length FIR has a half-sample delay"
    # It need NOT be a multiple of DECIM: acq_of() subtracts it in the acquisition domain.


def test_nothing_above_the_fold_lands_in_the_used_band():
    """The whole reason the filter is 257 taps and not a cheap halfband.

    ⚠️THE THRESHOLD IS TUNED TO THE SHIPPED FILTER, NOT TO ONE HISTORICAL BUG. It was first set
    at -40 dB, which sat neatly between the -39.0 dB a normalisation bug produced at /2 and the
    -64.0 dB the good design achieved. Retargeting to /3 moved that bug to -46.3 dB and the guard
    went silently blind to it -- a threshold picked against one failure stops testing when the
    configuration moves. -55 dB is below the -63.4 dB the shipped taps measure with headroom for
    honest design variation, and above every degraded variant seen so far.
    """
    d, taps = _defines()
    w, a = _response(taps, d["DECIM_SHIFT"])
    db = lambda f: 20 * np.log10(np.interp(f, w, a) + 1e-12)          # noqa: E731
    # ⚠️/3 FOLDS MORE THAN ONE IMAGE. Checking only (fs_d - f) was right for /2 and would miss the
    # k=2 image entirely at /3 -- a guard that passes because it looked in one place.
    decim = int(round(FS_ACQ / FS_DEC))
    grid = np.linspace(62.5, TOP_HZ, 400)
    worst = max(max(db(k * FS_DEC - f), db(k * FS_DEC + f))
                for f in grid for k in range(1, decim) if k * FS_DEC + f <= FS_ACQ / 2)
    assert worst < -55.0, "worst fold into 62.5-%.1f Hz is %+.1f dB" % (TOP_HZ, worst)


def test_the_passband_is_flat_enough_not_to_tilt_the_corpus():
    d, taps = _defines()
    w, a = _response(taps, d["DECIM_SHIFT"])
    db = lambda f: 20 * np.log10(np.interp(f, w, a) + 1e-12)          # noqa: E731
    ripple = max(abs(db(f)) for f in np.linspace(62.5, TOP_HZ, 400))
    assert ripple < 0.5, "passband ripple %.2f dB would tilt band levels against the stored rows" % ripple


def test_the_firmware_derives_the_acquisition_rate_and_never_hardcodes_it():
    """FS_ACQ must stay FS_NOMINAL * DECIM. Two independent constants would be free to disagree,
    which is the exact failure the mel-bank static_asserts exist to catch one layer down.

    ⚠️AIMED AT THE PROPERTY, NOT AT ONE CONFIGURATION. This used to match the literal string
    `static_assert((int)MEL16_FS == FS_NOMINAL` for both banks. That went blind the moment the
    banks stopped sharing a rate: the sketch bank moved to FS_ACQ, and a guard pinned to the old
    text can only say the old text is gone -- it cannot say the new binding is right, and it would
    have passed just as happily if the SCENE assert had been the one repointed. So: every mel bank
    the sketch includes is pinned by a static_assert to a rate SYMBOL, and that symbol's value must
    equal the rate baked into that bank's own generated header."""
    ino = _strip_c_comments((ROOT / "firmware" / "hear_node" / "hear_node.ino").read_text())
    assert "#define FS_ACQ     (FS_NOMINAL * DECIM)" in ino
    nominal = float(re.search(r"#define\s+FS_NOMINAL\s+(\d+)",
                             (ROOT / "firmware" / "boards" / "xiao_s3_sense.h").read_text()).group(1))
    decim = int(re.search(r"#define\s+DECIM\s+(\d+)", ino).group(1))
    rates = {"FS_NOMINAL": nominal, "FS_ACQ": nominal * decim}

    banks = {}
    for inc in re.findall(r'#include\s+"([a-z_0-9]+\.h)"', ino):
        h = ROOT / "firmware" / "hear_node" / inc
        if not h.exists():
            continue
        m = re.search(r"#define\s+(MEL[A-Z0-9]*)_FS\s+([0-9.]+)f", h.read_text())
        if m:
            banks[m.group(1)] = float(m.group(2))
    assert len(banks) >= 2, "expected the sketch bank and the scene bank, found %s" % sorted(banks)

    for pre, fs in sorted(banks.items()):
        m = re.search(r"static_assert\(\(int\)%s_FS\s*==\s*(\w+)" % pre, ino)
        assert m, "%s_FS is not pinned to a rate by a static_assert" % pre
        sym = m.group(1)
        assert sym in rates, "%s_FS is pinned to %s, which is not a rate this sketch defines" % (pre, sym)
        assert rates[sym] == fs, \
            "%s_FS is %.1f Hz but the guard pins it to %s = %.1f Hz" % (pre, fs, sym, rates[sym])

    # The two banks read two different streams; a build where both name the same rate is the
    # pre-change one, and the decimator has nothing left to do.
    pinned = {pre: re.search(r"static_assert\(\(int\)%s_FS\s*==\s*(\w+)" % pre, ino).group(1)
              for pre in banks}
    assert len(set(pinned.values())) == 2, \
        "both mel banks are pinned to the same rate: %s" % pinned


# ---------------------------------------------------------------- the folded implementation

def _sat(y):
    return 32767 if y > 32767 else (-32768 if y < -32768 else y)


def _run(H, x, hist, decim, shift, fold, flat=False):
    """The forms of the same filter, in the same arithmetic the firmware uses.

    `flat` models the SHIPPED loop: history and input copied into one contiguous span so there is no
    per-tap conditional. Measured on rankine at 24.2 cycles per multiply against 36.0 for the
    conditional form -- 36% of a core for one microphone rather than 54%.
    """
    if flat:
        buf = list(hist) + list(x)
        n, half, out = len(H), len(H) // 2, []
        base = len(H) - 1
        for k in range(0, len(x) - decim + 1, decim):
            w = base + k + decim - 1
            acc = 0
            for t in range(half):
                acc += H[t] * (buf[w - t] + buf[w - (n - 1 - t)])
            acc += H[half] * buf[w - half]
            out.append(_sat(acc >> shift))
        return out
    n, half, out = len(H), len(H) // 2, []
    for k in range(0, len(x) - decim + 1, decim):
        acc = 0
        if fold:
            for t in range(half):
                ia, ib = k + decim - 1 - t, k + decim - 1 - (n - 1 - t)
                a = x[ia] if ia >= 0 else hist[(n - 1) + ia]
                b = x[ib] if ib >= 0 else hist[(n - 1) + ib]
                acc += H[t] * (a + b)
            ic = k + decim - 1 - half
            acc += H[half] * (x[ic] if ic >= 0 else hist[(n - 1) + ic])
        else:
            for t in range(n):
                idx = k + decim - 1 - t
                acc += H[t] * (x[idx] if idx >= 0 else hist[(n - 1) + idx])
        out.append(_sat(acc >> shift))
    return out


def test_the_taps_are_symmetric_which_is_what_licenses_folding():
    _d, taps = _defines()
    n = len(taps)
    assert all(taps[t] == taps[n - 1 - t] for t in range(n // 2)), (
        "the filter is not symmetric, so hear_node's folded decimate() is computing something else")
    assert n % 2 == 1, "an even-length filter has no centre tap for the folded loop to add"


def test_folding_changes_the_cost_and_not_one_output_sample():
    """⚠️THE WHOLE RISK OF FOLDING. Half the multiplies is worthless if it is half a different
    filter, and a fold bug would be inaudible in a spectrum plot and wrong in every clip."""
    import random
    d, taps = _defines()
    H = [int(t) for t in taps]
    rng = random.Random(7)
    for _ in range(4):
        hist = [rng.randint(-32768, 32767) for _ in range(len(H) - 1)]
        x = [rng.randint(-32768, 32767) for _ in range(768)]
        a = _run(H, x, hist, d["DECIM_TAPS"] and 3, d["DECIM_SHIFT"], fold=False)
        b = _run(H, x, hist, 3, d["DECIM_SHIFT"], fold=True)
        assert a == b, "folded and unfolded disagree on %d of %d outputs" % (
            sum(1 for p, q in zip(a, b) if p != q), len(a))


def test_the_accumulator_really_does_need_64_bits():
    """⚠️KEEPS int64 HONEST. 'It is a unity-gain lowpass so it fits int32' predicts sum|h| ~ 1.05;
    the real filter is 2.54 in Q15 because a sharp design has large tap ripple. If a future design
    genuinely fits, this test fails and says so rather than leaving int64 as folklore."""
    d, taps = _defines()
    worst = int(sum(abs(int(t)) for t in taps)) * 32768
    assert worst > 2**31 - 1, (
        "worst-case accumulator is %d, which now FITS int32 -- decimate() can drop to 32-bit "
        "arithmetic and should" % worst)


def test_the_contiguous_loop_is_the_same_filter_to_the_bit():
    """⚠️THE RISK OF REMOVING THE BRANCH. A faster loop computing something else is not faster, and
    a decimator bug is inaudible in a spectrum plot and wrong in every clip and every sketch. The
    node itself reported 0 differing outputs before this replaced the conditional form; this is the
    same check on the host, where it can run on every commit."""
    import random
    d, taps = _defines()
    H = [int(t) for t in taps]
    rng = random.Random(11)
    for _ in range(4):
        hist = [rng.randint(-32768, 32767) for _ in range(len(H) - 1)]
        x = [rng.randint(-32768, 32767) for _ in range(768)]
        ref = _run(H, x, hist, 3, d["DECIM_SHIFT"], fold=False)
        flat = _run(H, x, hist, 3, d["DECIM_SHIFT"], fold=True, flat=True)
        assert ref == flat, "contiguous loop differs on %d of %d outputs" % (
            sum(1 for p, q in zip(ref, flat) if p != q), len(ref))


def test_the_shipped_decimator_has_no_per_tap_conditional():
    """It cost 12 cycles per multiply, which is a third of the filter. Comments stripped first."""
    import re as _re
    src = _re.sub(r"//[^\n]*", "", (ROOT / "firmware" / "hear_node" / "hear_node.ino").read_text())
    i = src.index("static int decimate(const int16_t *in")
    body = src[i:src.index("\n}", i)]
    assert "dscratch" in body, "decimate() no longer uses the contiguous scratch span"
    assert "? in[" not in body and ">= 0 ?" not in body, (
        "a per-tap conditional is back in decimate(); that measured 36.0 cycles/MAC against 24.2")
