"""The sketch path's domain arithmetic, EXECUTED -- plus the source-shape guards that the
arithmetic alone cannot cover.

firmware/night_node/sketch_domain.h is host-compilable on purpose: this repo has no ESP32 test
harness, so the choice was between running the real expressions and pattern-matching them. The
functions run here are the same translation units night_node.ino includes; nothing is re-derived
in Python except the WRONG variants, which are written out explicitly so each test says what it
is refusing rather than merely what it wants.

⚠️Each _wrong_* mirror below is a version that COMPILES and ships bad data silently.
"""
import ctypes
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hear import sketch as SK              # noqa: E402
from hear.node import detect as DET        # noqa: E402

INO = ROOT / "firmware" / "night_node" / "night_node.ino"
HDR = ROOT / "firmware" / "night_node" / "sketch_domain.h"
BANK = ROOT / "firmware" / "night_node" / "mel_impulse.h"
DECIM_H = ROOT / "firmware" / "night_node" / "decim.h"
# The rate is the BOARD's, not the sketch's -- night_node.ino:99 includes it rather than restating
# it, and this test must read it from the same place or it stops guarding a board swap.
BOARD = ROOT / "firmware" / "boards" / "xiao_s3_sense.h"


# ---------------------------------------------------------------- source, with the prose removed
def _strip_comments(src):
    """⚠️A source-scanning guard that does not strip comments matches its own prose. night_node.ino
    names MELIMP_HOP, dcblk and aring_w in the comments that explain why they are NOT used."""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//.*", "", src)


CODE = _strip_comments(INO.read_text())


def _define(src, name, cast=int):
    m = re.search(r"^#define\s+%s\s+([^\s/]+)" % re.escape(name), src, re.M)
    assert m, "%s is not defined" % name
    return cast(m.group(1).rstrip("fu"))


assert '#include "../boards/xiao_s3_sense.h"' in CODE, "night_node changed boards"
FS_NOMINAL = _define(_strip_comments(BOARD.read_text()), "FS_NOMINAL")
DECIM = _define(CODE, "DECIM")
FS_ACQ = FS_NOMINAL * DECIM
ARING = _define(CODE, "ARING")
DECIM_DELAY = _define(_strip_comments(DECIM_H.read_text()), "DECIM_DELAY")
MELIMP_FS = _define(BANK.read_text(), "MELIMP_FS", float)
MELIMP_HOP = _define(BANK.read_text(), "MELIMP_HOP")
MELIMP_NFFT = _define(BANK.read_text(), "MELIMP_NFFT")
SKETCH_SPAN = MELIMP_NFFT + (SK.FRAMES - 1) * MELIMP_HOP


# ---------------------------------------------------------------- the header, actually compiled
@pytest.fixture(scope="module")
def dom(tmp_path_factory):
    cc = shutil.which("cc")
    if cc is None:
        # A silent skip is a blind guard. Say why, so a CI image without a compiler is visible.
        pytest.skip("no cc on PATH: sketch_domain.h could not be compiled, so its arithmetic "
                    "was NOT executed by this run")
    d = tmp_path_factory.mktemp("sketch_domain")
    (d / "w.c").write_text(
        '#include "%s"\n' % HDR
        + "uint32_t w_back(double s, double fs){return sk_back_acq_len(s,fs);}\n"
          "uint32_t w_onset(uint32_t b,uint32_t i,uint32_t d,uint32_t g)"
          "{return sk_onset_acq_at(b,i,d,g);}\n"
          "uint32_t w_start(uint32_t o,uint32_t b){return sk_window_start_acq_at(o,b);}\n"
          "int w_landed(uint32_t h,uint32_t s,uint32_t n){return sk_window_landed(h,s,n);}\n"
          "int w_lapped(uint32_t h,uint32_t s,uint32_t n,uint32_t r)"
          "{return sk_window_lapped(h,s,n,r);}\n")
    so = d / "w.so"
    r = subprocess.run([cc, "-O2", "-fPIC", "-shared", "-Wall", "-Werror",
                        str(d / "w.c"), "-o", str(so)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lib = ctypes.CDLL(str(so))
    lib.w_back.argtypes = [ctypes.c_double, ctypes.c_double]
    lib.w_back.restype = ctypes.c_uint32
    for nm, na in (("w_onset", 4), ("w_start", 2)):
        getattr(lib, nm).argtypes = [ctypes.c_uint32] * na
        getattr(lib, nm).restype = ctypes.c_uint32
    for nm, na in (("w_landed", 3), ("w_lapped", 4)):
        getattr(lib, nm).argtypes = [ctypes.c_uint32] * na
        getattr(lib, nm).restype = ctypes.c_int
    return lib


# ---------------------------------------------------------------- the wrong versions that compile
def _wrong_ring_fed_from_the_decimated_loop(n_acq):
    """Variant 1: the aring write left inside the `for i < nd` loop, now copying acblk[i]. Each
    768-sample block contributes acblk[0..255] -- 5.33 ms of audio then a 10.67 ms hole."""
    return [i for i in range(n_acq) if (i % (ARING and 768)) < 256]


def _wrong_start_from_the_decimated_index(d_sample):
    """Variant 2: start = d.sample * DECIM - SKETCH_BACK. Never subtracts the group delay, so the
    window is DECIM_DELAY acquisition samples late and the pre-onset context is gone."""
    return d_sample * DECIM - _sketch_back()


def _wrong_start_from_the_hop(d_sample):
    """Variant 3: start = acq_of(d.sample - MELIMP_HOP). Arithmetically identical to the right
    answer while MELIMP_HOP was 64; at 192 the same unchanged line looks back 3x too far."""
    a = (d_sample - MELIMP_HOP) * DECIM + (DECIM - 1)
    return a - DECIM_DELAY if a > DECIM_DELAY else 0


def _wrong_head_is_delay_corrected(head):
    """Variant 4: acq_of() applied to the write head -- the shipped ring-floor bug with the sign
    flipped. The head reads DECIM_DELAY early, so good windows are declared lapped."""
    return head - DECIM_DELAY if head > DECIM_DELAY else 0


def _sketch_back():
    return max(1, int(DET.SKETCH_BACK_S * FS_ACQ))


def _acq_of(d_samp):
    a = d_samp * DECIM + (DECIM - 1)
    return a - DECIM_DELAY if a > DECIM_DELAY else 0


# ---------------------------------------------------------------- T1: the arithmetic
def test_the_onset_instant_is_the_delay_corrected_one(dom):
    """Fails against variant 2 (`d.sample * DECIM`), which is the pre-change sketch_pump's whole
    idea of where a detection is: an index, never an instant."""
    for base, i in ((0, 0), (768, 5), (48000, 255), (3 * 10 ** 6, 17)):
        got = dom.w_onset(base, i, DECIM, DECIM_DELAY)
        d_sample = base // DECIM + i
        assert got == _acq_of(d_sample), (base, i)
        newest = base + i * DECIM + (DECIM - 1)
        assert got == (newest - DECIM_DELAY if newest > DECIM_DELAY else 0)
    # and the variant it refuses, priced in milliseconds
    late = _wrong_start_from_the_decimated_index(48000 // DECIM + 5) - dom.w_start(
        dom.w_onset(48000, 5, DECIM, DECIM_DELAY), _sketch_back())
    # DECIM_DELAY, less the (DECIM-1) offset of the output's newest input that variant 2 also drops
    assert late == DECIM_DELAY - (DECIM - 1)
    assert late / FS_ACQ == pytest.approx(0.004625, abs=1e-6), "variant 2 is %d samples late" % late


def test_the_window_starts_one_back_off_before_the_onset(dom):
    """Fails against variant 3 (`acq_of(d.sample - MELIMP_HOP)`), which was RIGHT at 16 kHz and is
    a 12 ms look-back at 48 kHz off the same unchanged line."""
    back = _sketch_back()
    onset = dom.w_onset(48000, 5, DECIM, DECIM_DELAY)
    assert dom.w_start(onset, back) == onset - back
    d_sample = 48000 // DECIM + 5
    assert _wrong_start_from_the_hop(d_sample) == onset - MELIMP_HOP * DECIM
    assert (onset - _wrong_start_from_the_hop(d_sample)) / FS_ACQ == pytest.approx(0.012)


def test_the_first_window_of_a_boot_says_it_is_short(dom):
    """Fails against unsigned wrap AND against the pre-change code, which flagged bit 1 only for a
    lapped ring: a boot-time window read zeros in front of the onset and said nothing."""
    for onset in range(0, _sketch_back() + 2):
        s = dom.w_start(onset, _sketch_back())
        assert s < 2 ** 31, "start wrapped at onset=%d" % onset
        assert s == max(0, onset - _sketch_back()) or (onset <= _sketch_back() and s == 0)
    assert dom.w_onset(0, 0, DECIM, DECIM_DELAY) == 0        # saturates, does not wrap to ~4e9
    short = [d for d in (0, 100, _sketch_back() - 1) if d < _sketch_back()]
    assert len(short) == 3
    ino = CODE
    assert re.search(r"if\s*\(\s*d\.acq_at\s*<\s*SKETCH_BACK\s*\)\s*d\.flags\s*\|=\s*0x0002", ino), \
        "a saturated window is not flagged as short"


def test_readiness_is_not_scaled_by_decim(dom):
    """Fails against the pre-change test `g_samples - (d.sample + (SKETCH_SPAN - SKETCH_BACK))`,
    which compares a DECIMATED head against an ACQUISITION span: with SKETCH_SPAN 1600 it would
    wait 1408 decimated samples = 88 ms and sketch a window that is only one third written."""
    start = 100000
    assert not dom.w_landed(start + SKETCH_SPAN - 1, start, SKETCH_SPAN)
    assert dom.w_landed(start + SKETCH_SPAN, start, SKETCH_SPAN)
    # the mixed-domain form declares it ready this many acquisition samples early:
    dec_head_equiv = (start + SKETCH_SPAN - _sketch_back()) * DECIM
    assert dec_head_equiv != start + SKETCH_SPAN


def test_a_lapped_or_unwritten_window_is_flagged(dom):
    """Fails against the pre-change one-ended test (`aring_total - start > ARING`), which passed a
    window whose TAIL was gone, and against variant 4."""
    start = 10 ** 6
    ok_head = start + SKETCH_SPAN
    assert not dom.w_lapped(ok_head, start, SKETCH_SPAN, ARING)
    assert dom.w_lapped(start + ARING + 1, start, SKETCH_SPAN, ARING)      # front lapped
    assert dom.w_lapped(start + SKETCH_SPAN - 1, start, SKETCH_SPAN, ARING)  # tail not written
    # variant 4: a head that has had the group delay taken off it condemns a good window
    tight = start + SKETCH_SPAN + 10
    assert not dom.w_lapped(tight, start, SKETCH_SPAN, ARING)
    assert dom.w_lapped(_wrong_head_is_delay_corrected(tight), start, SKETCH_SPAN, ARING), \
        "acq_of() on the write head must be detectable as yield loss"


def test_a_write_head_is_not_an_instant(dom):
    """Row 1/2 of the unit table. Fails against any build that converts g_acq the way a detection
    index is converted -- silent yield loss, which is why it needs a test and not a comment."""
    lost = 0
    for k in range(2000):
        head = 10 ** 6 + k
        start = head - SKETCH_SPAN
        if dom.w_lapped(_wrong_head_is_delay_corrected(head), start, SKETCH_SPAN, ARING) and \
           not dom.w_lapped(head, start, SKETCH_SPAN, ARING):
            lost += 1
    assert lost > 0
    assert "acq_of(g_acq" not in CODE and "acq_of(g_samples" not in CODE


def test_short_reads_do_not_slip_the_ring():
    """decimate() consumes floor(n/DECIM)*DECIM inputs for OUTPUT but pushes the last DECIM_TAPS-1
    of ALL n into dhist, so g_samples * DECIM falls behind the FIR's true input count by n % DECIM
    every short read -- cumulatively. Fails against deriving the ring position that way: the
    derived head would place two consecutive blocks OVERLAPPING in the ring by the slip, which is
    spliced content, not a shifted index."""
    g_acq = g_samples = 0
    for k in range(200):
        n = 768 if k % 3 else 767
        g_acq += n
        g_samples += n // DECIM
    slip = g_acq - g_samples * DECIM
    assert slip > 0, "the replay did not produce a short read"
    assert slip == sum(1 for k in range(200) if k % 3 == 0) * (767 % DECIM)
    assert slip / FS_ACQ > 0.001, "slip is %d samples = %.1f ms" % (slip, 1e3 * slip / FS_ACQ)


# ---------------------------------------------------------------- T3: the back-off's derivation
def test_the_back_off_is_derived_from_time_not_from_the_hop(dom):
    """Fails against `#define SKETCH_BACK ((uint32_t)MELIMP_HOP)` at ANY rate -- it aims at the
    derivation, not at the number 192. The two reference constants are independent and equal by
    coincidence: hear/sketch.HOP_S and hear/node/detect.SKETCH_BACK_S are both 0.004 s."""
    for fs in (8000.0, 16000.0, 48000.0, 100.0):
        assert dom.w_back(DET.SKETCH_BACK_S, fs) == max(1, int(DET.SKETCH_BACK_S * fs))
    assert dom.w_back(DET.SKETCH_BACK_S, FS_ACQ) == 192
    m = re.search(r"#define\s+SKETCH_BACK\s+(.+)", CODE)
    assert m, "SKETCH_BACK is gone"
    expr = m.group(1)
    assert "SKETCH_BACK_S" in expr and "FS_ACQ" in expr, expr
    assert "HOP" not in expr, "SKETCH_BACK is transplanted from the hop again: %s" % expr
    assert _define(CODE, "SKETCH_BACK_S", float) == DET.SKETCH_BACK_S
    # and the coupling is deliberately NOT asserted in C -- see the comment at the define
    assert "SKETCH_BACK == MELIMP_HOP" not in CODE


# ---------------------------------------------------------------- T5: the shape of the .ino
def _fn(name):
    """One function body from the comment-stripped .ino, braces balanced."""
    i = CODE.index("static void %s()" % name)
    depth, j = 0, CODE.index("{", i)
    for k in range(j, len(CODE)):
        depth += (CODE[k] == "{") - (CODE[k] == "}")
        if not depth:
            return CODE[i:k + 1]
    raise AssertionError("%s() is unbalanced" % name)


def _audio_pump():
    return _fn("audio_pump")


#: Which function body each sketch_domain.h helper must be CALLED from. None = file scope (a
#: #define). ⚠️Proving a helper correct through ctypes proves nothing about whether the firmware
#: reaches it: the rejected D3 form -- `dets[idx].acq_at = acq_of(dets[idx].sample)` -- passed the
#: whole suite unchanged, because every domain test drove sketch_domain.h directly and no test
#: looked at the one production call site.
SK_CALL_SITES = {
    "sk_back_acq_len": None,
    "sk_onset_acq_at": "audio_pump",
    "sk_window_start_acq_at": "sketch_pump",
    "sk_window_landed": "sketch_pump",
    "sk_window_lapped": "sketch_pump",
}


def test_every_domain_helper_is_called_from_the_function_that_owns_it():
    """sketch_domain.h's own header says night_node.ino must not open-code any of this. Fails
    against dropping ANY of these call sites -- including the D3 revert, which removes the only
    sk_onset_acq_at() call. The map is checked against the header both ways, so a helper added
    there and never wired up is a failure rather than dead code nobody notices."""
    declared = set(re.findall(r"static inline \w+ (sk_\w+)\(",
                              _strip_comments(HDR.read_text())))
    assert declared == set(SK_CALL_SITES), \
        "sketch_domain.h declares %r; the call-site map names %r" % (
            sorted(declared), sorted(SK_CALL_SITES))
    for fn, owner in sorted(SK_CALL_SITES.items()):
        where = CODE if owner is None else _fn(owner)
        assert fn + "(" in where, \
            "%s() is never called from %s" % (fn, owner or "night_node.ino")


def test_the_onset_instant_is_recorded_where_the_block_offset_is_known():
    """D3's decision, at its ONE production call site. `acq_of(d.sample)` converts a DECIMATED
    index by multiplying by DECIM, which is n%DECIM low for good after every short I2S read
    (test_short_reads_do_not_slip_the_ring measures the slip); the instant has to be taken in
    audio_pump where acq_base and i are both in hand.

    Fails against the exact rejected form `dets[idx].acq_at = acq_of(dets[idx].sample);`, which
    compiles, ships flags-clear frames cut around the wrong instant, and passed the entire suite
    before this test existed."""
    body = _audio_pump()
    got = re.findall(r"dets\[[^\]]+\]\.acq_at\s*=\s*([^;]+);", body)
    assert got, "audio_pump() no longer records the onset instant at all"
    for expr in got:
        expr = " ".join(expr.split())
        assert "sk_onset_acq_at(" in expr, \
            "the onset instant is re-derived instead of taken here: %s" % expr
        assert "acq_of(" not in expr and "g_samples" not in expr, \
            "the onset instant is built from the decimated counter: %s" % expr
    # and nowhere else, so a second writer cannot reintroduce it out of sight of this check
    assert len(re.findall(r"\.acq_at\s*=\s*[^;=]", CODE)) == len(got), \
        "acq_at is assigned outside audio_pump()"


def test_the_sketch_ring_is_fed_from_the_acquisition_stream():
    """Fails against the pre-change feed (`aring[aring_w] = sac` inside the `for i < nd` loop) and
    against variant 1, which keeps the write in that loop and merely swaps dcblk for acblk: the
    ring then holds 5.33 ms of every 16 ms block and every FFT frame is a splice across the gap."""
    body = _audio_pump()
    lines = [(k, ln) for k, ln in enumerate(body.splitlines()) if "aring" in ln]
    assert lines, "audio_pump no longer feeds aring at all"
    for k, ln in lines:
        assert "dcblk" not in ln and "sac" not in ln, "the sketch ring is still on the gated stream: %s" % ln.strip()
        assert "acblk" in ln or "ARING" in ln or "g_acq" in ln, ln.strip()
    dec = [k for k, ln in enumerate(body.splitlines()) if "decimate(" in ln]
    assert dec, "decimate() call not found in audio_pump"
    assert max(k for k, _ in lines) < dec[0], \
        "the aring feed is downstream of decimate(), so it runs once per DECIM inputs"
    # the fragment count that variant 1 would produce, stated so the number is on the record
    assert len(_wrong_ring_fed_from_the_decimated_loop(768)) == 256


def test_no_free_running_ring_counter_survives():
    """Rows 5-6. aring_w/aring_total were a second counter, free to drift from what was written;
    a derived position cannot. Fails against keeping either."""
    assert "aring_w" not in CODE and "aring_total" not in CODE
    assert re.search(r"g_acq\s*%\s*\(?\s*(uint32_t\s*\)?\s*)?ARING", CODE) or \
        re.search(r"acq_base\s*%\s*\(?\s*(uint32_t\s*\)?\s*)?ARING", CODE), \
        "the ring position is not derived from the acquisition counter"
    for m in re.finditer(r"^[^\n]*g_acq\s*(\+=|=)[^\n;]*;", CODE, re.M):
        ln = m.group(0)
        assert "DECIM" not in ln and "g_samples" not in ln, \
            "g_acq is derived from the decimated counter, so a short read splices the ring: %s" % ln.strip()


def test_the_ring_holds_the_time_its_comment_claims():
    """Row 7. Fails against ARING 4096 with the bank at 48 kHz (85 ms, a third of the window's
    pre-roll) and against any future rate change that keeps the COUNT instead of the TIME."""
    assert ARING / MELIMP_FS >= 0.256 - 1e-9, \
        "ARING is %d = %.1f ms at %.0f Hz" % (ARING, 1e3 * ARING / MELIMP_FS, MELIMP_FS)
    assert ARING > SKETCH_SPAN * 2, "the ring cannot hold the window plus its pre-roll"
    assert MELIMP_FS == FS_ACQ, "the sketch bank is not at the rate the ring is fed at"


def test_the_ring_index_survives_the_acquisition_counters_own_wrap():
    """⚠️THE RING INDEX IS DERIVED, so `pos % ARING` must agree either side of g_acq's wrap at
    2^32 -- otherwise one window per 24 h 51 min of uptime reads audio from ARING samples earlier
    with flags bit 1 CLEAR, which is a measurement of a different moment wearing this one's
    clothes. sk_window_lapped's `head - start` is modular and stays small across the wrap, so
    nothing else catches it.

    Fails against ARING 12288 (2^32 %% 12288 == 4096) -- 256 ms exactly, and the reason the
    property is asserted rather than the number. The pre-change 4096 had it for free. Aimed at
    the property so the NEXT re-dimensioning cannot drop it again."""
    assert (2 ** 32) % ARING == 0, \
        "ARING %d leaves %d: a window straddling the wrap splices" % (ARING, (2 ** 32) % ARING)
    # and the .ino says so itself, so the constraint cannot be lost by editing only this file
    assert re.search(r"static_assert\(\s*\(0x100000000ULL\s*%\s*\(unsigned long long\)ARING\)\s*==\s*0",
                     CODE), "the divisibility constraint is not asserted in the firmware"
    # the C counter's own wrap, walked one sample at a time through it
    for pos in (2 ** 32 - 3, 2 ** 32 - 2, 2 ** 32 - 1):
        nxt = (pos + 1) % (2 ** 32)                     # uint32_t g_acq += 1
        assert nxt % ARING == (pos % ARING + 1) % ARING, \
            "position %d -> %d jumps from ring index %d to %d" % (
                pos, nxt, pos % ARING, nxt % ARING)


def test_the_two_banks_bind_to_two_different_rates():
    """The static_asserts. Fails against the pre-change pair (both bound to FS_NOMINAL) and against
    repointing the SCENE assert by mistake -- it names each bank's own stream, not one rate."""
    assert "static_assert((int)MELIMP_FS == FS_ACQ" in CODE, "the sketch bank is not tied to FS_ACQ"
    assert "static_assert((int)MELS_FS  == FS_NOMINAL" in CODE, "the scene bank left FS_NOMINAL"
    assert "MELIMP_FS == FS_NOMINAL" not in CODE and "MELS_FS  == FS_ACQ" not in CODE
    assert "static_assert(MELS_NFFT == MELIMP_NFFT" in CODE, "the shared window is unguarded"


def test_peak_still_comes_from_the_gated_stream():
    """Row 17 / D7. `peak` is not a model input (classify.score_sketch reads q and ref_db only); it
    buckets against DET_PEAK_QUARTILES derived from stored 16 kHz peaks. Moving it to acblk would
    silently redefine a stored column. Fails against `d.trigger = acblk[...]`."""
    assert re.search(r"dets\[idx\]\.trigger\s*=\s*sac\s*;", CODE)
    assert re.search(r"int16_t\s+sac\s*=\s*dcblk\[i\]\s*;", CODE)


def test_the_retrigger_window_stays_in_the_decimated_domain():
    """Row 16. RETRIGGER_SAMPLES is differenced against d.sample, which stays decimated. Fails
    against a well-meaning sweep that re-based it on FS_ACQ along with everything else."""
    m = re.search(r"#define\s+RETRIGGER_SAMPLES\s+(.+)", CODE)
    assert m and "FS_NOMINAL" in m.group(1) and "FS_ACQ" not in m.group(1), m.group(1)
    assert re.search(r"dets\[idx\]\.sample\s*-\s*prev\.sample\s*\)\s*<\s*RETRIGGER_SAMPLES", CODE)


def test_status_reports_the_slip_between_the_two_acquisition_numberings():
    """Row 3. Two acquisition numberings now exist in this build (g_acq, and praw's
    g_samples*DECIM rule); the difference is MEASURED rather than assumed to be zero. Fails
    against landing g_acq with no way to see whether the praw rule needs its own fix."""
    assert '\\"acq_slip\\":' in CODE
    assert re.search(r"g_acq\s*-\s*\(?uint32_t\)?\s*\)?\s*g_samples\s*\*\s*DECIM", CODE), \
        "acq_slip is not derived from the two counters"
