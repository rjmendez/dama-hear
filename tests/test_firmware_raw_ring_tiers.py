"""The raw-PCM ring must fit a 2 MiB PSRAM part without shrinking anybody else's ring.

⚠️WHY THIS FILE EXISTS. `praw` used to step 80/60/45/30 s, and at FS_ACQ = 48 kHz the smallest of
those is 30 x 48000 x 2 = 2 880 000 B. gold is 8MB flash / **2 MiB quad** PSRAM
(docs/REDESIGN-LESSONS.md item 8, docs/release-v0.1.6-readiness.md row 5), so even after its quad
image is installed and `psramFound()` finally returns true, every tier fails the largest-free-block
test, `praw` stays NULL, `/audio` answers 503, and the node's microphone can never be cleared by
listening to PCM -- only by a summary statistic, which is exactly the evidence class that let a
saturated, rail-pinned mic pass as healthy for weeks (files/gold-audio-evidence-analysis/REPORT.md).

The fix adds tiers BELOW 30 s. The property that makes it safe is not that the new tiers are small,
it is that they are UNREACHABLE on a board that gets a ring today: the test applied to 80/60/45/30
is byte-identical, so ageev and kasami -- which measurably hold 80 s / 7.68 MB on the octal image
(docs/fleet-hardware-remediation-2026-09-15.md) -- cannot be moved down by this change. That is
proved here by sweeping every PSRAM free-block size and comparing the old table against the new.

These read the firmware SOURCE with comments and string literals stripped, so prose can neither
satisfy nor trip a check, and then MODEL the allocator over budgets the fleet actually has.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
BOARD = ROOT / "firmware" / "boards" / "esp32s3_i2s_gps.h"

MIB = 1024 * 1024

#: The tier table before this change. Kept as a literal because it is the thing being compared
#: against, and reading it from the same source as the new one could never fail.
OLD_TIERS_S = (80, 60, 45, 30)

#: Measured parts in the fleet. gold: docs/REDESIGN-LESSONS.md item 8. ageev/kasami: 8 MiB octal,
#: reporting ~8.34 MB free and a full 80 s ring on 2026-09-15.
GOLD_PSRAM_B = 2 * MIB
OCTAL_PSRAM_B = 8 * MIB
OCTAL_LARGEST_FREE_B = 8_340_000


def _strip(src, strings=False):
    """Comments out. With strings=False the literals are emptied too, so a check on code cannot be
    satisfied by a string; the checks that are ABOUT a literal read the strings=True view."""
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif src[i] in "\"'" and not strings:
            q, j = src[i], i + 1
            while j < n and src[j] != q:
                j += 2 if src[j] == "\\" else 1
            out.append(q + q)
            i = j + 1
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


MELIMP = ROOT / "firmware" / "hear_node" / "mel_impulse.h"

CODE = _strip(INO.read_text())
SRC = _strip(INO.read_text(), strings=True)     # comments gone, literals kept
RAW_CODE = INO.read_text()
DEFS = dict(re.findall(r"#define\s+([A-Za-z_]\w*)\s+(.+)",
                       "\n".join(_strip(p.read_text()) for p in (BOARD, MELIMP)) + "\n" + CODE))


def _val(name):
    e = re.sub(r"\((?:uint32_t|uint64_t|int|size_t)\)", "", DEFS[name])
    e = re.sub(r"\b(\d+)(?:ULL|UL|U|u)\b", r"\1", e).replace("/", "//")
    for ident in sorted(set(re.findall(r"\b[A-Za-z_]\w*\b", e)), key=len, reverse=True):
        e = re.sub(r"\b%s\b" % ident, "(%d)" % _val(ident), e)
    return eval(e, {"__builtins__": {}})


def _constexpr(name):
    m = re.search(r"static constexpr \w+ %s = (\d+);" % re.escape(name), CODE)
    assert m, "%s is not a constexpr constant in the sketch" % name
    return int(m.group(1))


def _tiers():
    m = re.search(r"static constexpr uint32_t PRAW_TIERS_S\[\]\s*=\s*\{([^}]*)\}", CODE)
    assert m, "PRAW_TIERS_S is not a literal array"
    return tuple(int(x.strip()) for x in m.group(1).split(",") if x.strip())


FS_ACQ = _val("FS_NOMINAL") * _val("DECIM")
KEEP_B = _val("PSRAM_KEEP_B")
TIERS_S = _tiers()
RESERVE_BELOW_S = _constexpr("PRAW_DET_RESERVE_BELOW_S")


def _det_bytes():
    """sizeof(Det) as the compiler lays it out, so the reserve modelled here is the real one."""
    frame = _val("MELIMP_FRAME_BYTES")
    fields = [(4, 4), (4, 4), (4, 4), (8, 8),            # sample, pps_n, us_since_pps, utc_us
              (8, 8), (8, 8), (8, 8),                    # sync_sigma_ns, anchor_age_us, boot_epoch
              (4, 4), (1, 1),                            # clock_discontinuity_flags, clock_state
              (2, 2), (2, 2), (4, 4), (8, 8),            # trigger, flags, uptime_s, fs_at
              (1, 1), (4, 4), (1, 1), (4, 4),            # clip_st, cseq, sk_st, acq_at
              (frame, 1)]
    off, align = 0, 1
    for size, a in fields:
        off = (off + a - 1) // a * a + size
        align = max(align, a)
    return (off + align - 1) // align * align


DET_RESERVE_B = _val("DET_RING_MIN") * _det_bytes()


def _keep_for(tier_s, reserve_b=DET_RESERVE_B):
    return KEEP_B + (reserve_b if tier_s < RESERVE_BELOW_S else 0)


def choose_tier(largest_free_b, tiers=TIERS_S, reserve_b=DET_RESERVE_B):
    """The tier setup() lands on, modelled exactly: largest-free must cover want + keep."""
    for tier in tiers:
        want = tier * FS_ACQ * 2
        if largest_free_b >= want + _keep_for(tier, reserve_b):
            return tier
    return None


def choose_tier_old(largest_free_b):
    """The allocator before this change: one flat PSRAM_KEEP_B stand-off, four tiers."""
    for tier in OLD_TIERS_S:
        if largest_free_b >= tier * FS_ACQ * 2 + KEEP_B:
            return tier
    return None


# --------------------------------------------------------------- the table itself

def test_the_first_tiers_and_their_order_are_exactly_what_shipped():
    assert TIERS_S[:len(OLD_TIERS_S)] == OLD_TIERS_S, TIERS_S
    assert list(TIERS_S) == sorted(TIERS_S, reverse=True), "tiers are tried largest first"
    assert len(set(TIERS_S)) == len(TIERS_S), "a repeated tier is a tier tried twice"
    assert TIERS_S[-1] < OLD_TIERS_S[-1], "nothing was added below the old floor"


def test_the_smallest_tier_still_holds_a_whole_clip():
    """A tier under the clip window would not corrupt a clip -- clip_pump refuses a lapped window
    -- it would silently turn every clip into a CLIP_RING skip. The sketch asserts this at compile
    time; this asserts the assert is still there, and the arithmetic behind it."""
    assert TIERS_S[-1] * FS_ACQ > _val("CLIP_SAMPLES")
    assert "the smallest raw-ring tier must still hold one whole clip window" in RAW_CODE


def test_the_longest_tier_still_fits_the_utc_marks():
    """praw_mark is one (UTC, sample) pair per GPS second and is a FIXED array. A tier longer than
    PRAW_MARKS would address samples no mark covers, and sample_to_utc would extrapolate."""
    assert TIERS_S[0] <= _val("PRAW_MARKS")
    assert "PRAW_MARKS must cover the longest ring that can allocate" in RAW_CODE


# --------------------------------------------------------------- no peer loses a byte

@pytest.mark.parametrize("free_b", [
    OCTAL_LARGEST_FREE_B,                    # ageev / kasami, measured 2026-09-15
    OCTAL_PSRAM_B,
    80 * FS_ACQ * 2 + KEEP_B,                # exactly enough for the top tier
    80 * FS_ACQ * 2 + KEEP_B - 1,            # one byte short of it
    30 * FS_ACQ * 2 + KEEP_B,                # exactly enough for the old floor
])
def test_a_board_that_gets_a_ring_today_gets_the_same_ring(free_b):
    assert choose_tier(free_b) == choose_tier_old(free_b)


def test_no_free_block_size_anywhere_loses_span_to_this_change():
    """The safety property, swept rather than sampled: wherever the OLD allocator returned a tier,
    the new one returns THE SAME tier. It may only add an answer where there was none."""
    edges = {0, 1, KEEP_B, OCTAL_PSRAM_B, OCTAL_LARGEST_FREE_B, GOLD_PSRAM_B, 16 * MIB}
    for tier in set(TIERS_S) | set(OLD_TIERS_S):
        for keep in (KEEP_B, KEEP_B + DET_RESERVE_B):
            base = tier * FS_ACQ * 2 + keep
            edges |= {base - 1, base, base + 1}
    for free_b in sorted(e for e in edges if e >= 0):
        old, new = choose_tier_old(free_b), choose_tier(free_b)
        if old is not None:
            assert new == old, (free_b, old, new)
        else:
            assert new is None or new < OLD_TIERS_S[-1], (free_b, new)
    for free_b in range(0, 9 * MIB, 65536):          # a coarse sweep of the whole PSRAM range
        old = choose_tier_old(free_b)
        if old is not None:
            assert choose_tier(free_b) == old, free_b


def test_the_new_tiers_are_unreachable_on_a_board_that_had_one():
    """A tier below the old floor can only be taken where the old floor itself failed."""
    for tier in TIERS_S:
        if tier >= OLD_TIERS_S[-1]:
            continue
        need = tier * FS_ACQ * 2 + _keep_for(tier)
        assert need < OLD_TIERS_S[-1] * FS_ACQ * 2 + KEEP_B, tier


# --------------------------------------------------------------- the part that forced it

def test_the_old_table_could_not_fit_gold_at_all():
    """The bug, stated as arithmetic: 2 MiB is smaller than the smallest old tier's requirement,
    so no amount of free PSRAM on that part could have produced a ring."""
    assert choose_tier_old(GOLD_PSRAM_B) is None
    assert OLD_TIERS_S[-1] * FS_ACQ * 2 + KEEP_B > GOLD_PSRAM_B


@pytest.mark.parametrize("used_b", [0, 128 * 1024, 256 * 1024, 384 * 1024, 512 * 1024])
def test_gold_gets_a_ring_once_its_quad_image_is_installed(used_b):
    """WiFi, LWIP and the WebServer allocate from PSRAM too (CONFIG_SPIRAM_USE_MALLOC=1,
    ALWAYSINTERNAL 4096 in the core's qio_qspi sdkconfig), and the ring is taken AFTER they are
    up, so the budget is the part minus whatever they hold. Across that range gold gets a ring."""
    tier = choose_tier(GOLD_PSRAM_B - used_b)
    assert tier is not None, used_b
    assert tier < OLD_TIERS_S[-1], tier


def test_gold_keeps_its_detection_ring_in_psram_at_every_tier_it_can_reach():
    """The reserve's whole purpose. Whatever tier a 2 MiB part lands on, the guard proved
    largest >= want + PSRAM_KEEP_B + DET_RING_MIN*sizeof(Det) BEFORE allocating, so after `want`
    is taken the detection ring's own guard (want_n + PSRAM_KEEP_B) still passes -- dets[] stays
    in PSRAM instead of falling back onto the internal heap that gold ran out of."""
    det_min_b = _val("DET_RING_MIN") * _det_bytes()
    for used_b in range(0, 900 * 1024, 32 * 1024):
        free_b = GOLD_PSRAM_B - used_b
        tier = choose_tier(free_b)
        if tier is None:
            continue
        assert tier < RESERVE_BELOW_S, "a 2 MiB part cannot reach a full tier"
        left = free_b - tier * FS_ACQ * 2
        assert left >= det_min_b + KEEP_B, (used_b, tier, left)


def test_a_part_too_small_for_even_the_last_tier_still_falls_back_to_no_ring():
    """A failed allocation is a MISSING FEATURE, not an error: praw stays NULL, /audio answers 503
    and capture, gating, detections and logging are untouched. The smaller tiers must not turn
    that into a partial ring or a crash."""
    assert choose_tier(TIERS_S[-1] * FS_ACQ * 2 + _keep_for(TIERS_S[-1]) - 1) is None
    assert choose_tier(0) is None
    setup = CODE[CODE.index("void setup()"):]
    alloc = setup[setup.index("for (unsigned k = 0; k < PRAW_TIER_N"):setup.index("heap_caps_calloc")]
    assert "&& !praw;" in alloc, "the loop must stop at the first tier that allocates"
    assert re.search(r"if \(praw\) \{ praw_cap = PRAW_TIERS_S\[k\] \* FS_ACQ;", alloc), \
        "praw_cap must be set only when ps_malloc actually returned memory"
    audio = SRC[SRC.index('http.on("/audio"'):]
    assert re.search(r"if \(!praw\) \{ http\.send\(503", audio)


# --------------------------------------------------------------- what /status must say

def test_status_says_which_tier_allocated_and_how_big_the_part_is():
    """A short ring on a healthy node and a broken ring on a sick one must not read the same.
    `raw.want_s` is the tier itself, `sys.psram_total` is the silicon that bounded it -- gold
    reported `psram: 0` and `raw.bytes: 0` for weeks with no field that said 2 MiB was the reason."""
    i = SRC.index('{\\"node\\":\\"%s\\"')
    block = SRC[i:SRC.index("i2c_found);", i)]
    assert '\\"want_s\\":%lu' in block
    assert '\\"psram_total\\":%lu' in block
    assert "(unsigned long)praw_want_s," in block
    assert "(unsigned long)ESP.getPsramSize()," in block


def test_the_boot_log_says_what_was_short_when_no_ring_allocates():
    """`praw  NO PSRAM ring` alone is what made gold's fault take weeks: it reads identically
    whether the part is absent, too small, or merely fragmented."""
    setup = CODE[CODE.index("void setup()"):]
    alloc = setup[setup.index("for (unsigned k = 0; k < PRAW_TIER_N"):setup.index("heap_caps_calloc")]
    assert "largest = heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM);" in alloc
    for arg in ("PRAW_TIERS_S[PRAW_TIER_N - 1]", "need / 1024UL", "largest / 1024UL",
                "ESP.getPsramSize() / 1024UL"):
        assert arg in alloc, arg
    assert "The smallest tier is %lu s and needs %lu kB contiguous" in RAW_CODE


# --------------------------------------------------------------- serving a short ring

def test_the_audio_overwrite_guard_scales_with_the_ring_instead_of_swallowing_it():
    """AUDIO_GUARD_S is 16 s. Flat, it would leave a 10 s ring with nothing addressable at all --
    negative, in fact. The handler already caps it at a third of the ring; these tiers are what
    make that cap load-bearing rather than theoretical."""
    audio = SRC[SRC.index('http.on("/audio"'):]
    assert "if (g > praw_cap_d() / 3) g = praw_cap_d() / 3;" in audio
    fs_nominal = _val("FS_NOMINAL")
    for tier in TIERS_S:
        cap_d = tier * FS_ACQ // _val("DECIM")
        guard = min(_val("AUDIO_GUARD_S") * fs_nominal, cap_d // 3)
        addressable_s = (cap_d - guard) / fs_nominal
        assert addressable_s >= tier * 2 / 3 - 1e-9, tier
        assert addressable_s > _val("CLIP_SAMPLES") / FS_ACQ, \
            "%d s ring addresses %.1f s, less than one clip" % (tier, addressable_s)


def test_a_short_ring_cannot_outrun_the_stream_before_the_guard_does():
    """The writer keeps running while /audio sends. The send stops at praw_floor() and the socket
    is pumped a chunk at a time, so a lap truncates the WAV rather than splicing later audio into
    it -- but on a small ring the margin is what makes that rare. One addressable window is
    two-thirds of the ring; sending it at the 335 kB/s measured on this node must take less."""
    measured_bytes_per_s = 335_000
    for tier in TIERS_S:
        addressable_s = tier * 2 / 3
        send_s = addressable_s * FS_ACQ * 2 / measured_bytes_per_s
        assert send_s < addressable_s, tier
        assert "if (s < praw_floor()) break;" in CODE
