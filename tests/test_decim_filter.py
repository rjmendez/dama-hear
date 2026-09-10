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
HDR = ROOT / "firmware" / "night_node" / "decim.h"
GEN = ROOT / "firmware" / "gen_decim.py"

#: The band the scene mel bank actually uses.
TOP_HZ = 7812.5
FS_ACQ = 48000.0
FS_DEC = 16000.0


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
        "    python3 firmware/gen_decim.py > firmware/night_node/decim.h")


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
    which is the exact failure the mel-bank static_asserts exist to catch one layer down."""
    ino = (ROOT / "firmware" / "night_node" / "night_node.ino").read_text()
    assert "#define FS_ACQ     (FS_NOMINAL * DECIM)" in ino
    for guard in ("static_assert((int)MEL16_FS == FS_NOMINAL",
                  "static_assert((int)MELS_FS  == FS_NOMINAL"):
        assert guard in ino, "the mel-bank rate guard is gone: %s" % guard
