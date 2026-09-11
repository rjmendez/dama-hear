"""Clips are a rolling window: the oldest clip on the card makes room for the next, always.

The priority gate this replaced refused every clap of the 2026-09-10 walk on nyquist and rankine
with clip_why "budget", because the window was held by higher-ranked clips from earlier. Nothing
on the node may refuse a clip for budget again, and "oldest" has to hold across reboots, where the
sample counter and every RAM counter restart.

clip_order.h is compiled and run here, and hear/clips.py:eviction_key is checked against it on the
same names, because the drain fetches in that order and a drain that disagrees with the node about
which clip goes next fetches the wrong one. The rest scans the firmware SOURCE with comments
stripped, so a comment explaining a rule cannot satisfy or trip it.
"""
import ctypes
import itertools
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from hear import clips as CL

ROOT = Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
HDR = ROOT / "firmware" / "hear_node" / "clip_order.h"


def _code():
    src = INO.read_text()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(l.split("//")[0] for l in src.splitlines())


def _body(name):
    """The braces-balanced body of a C function, comments removed."""
    src = _code()
    i = src.index(name + "(")
    i = src.index("{", i)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[i:j]


@pytest.fixture(scope="module")
def order(tmp_path_factory):
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no cc on PATH: clip_order.h could not be compiled, so eviction order was NOT "
                    "executed by this run")
    d = tmp_path_factory.mktemp("clip_order")
    (d / "w.c").write_text(
        '#include "%s"\n' % HDR
        + "int w_parse(const char *n, uint32_t *o){clip_key_t k; int r = clip_parse(n, &k);"
          "o[0]=k.legacy; o[1]=k.seq; o[2]=k.sample; return r;}\n"
          "int w_cmp(const char *a, const char *b){clip_key_t x, y; clip_parse(a, &x);"
          "clip_parse(b, &y); return clip_cmp(a, &x, b, &y);}\n")
    so = d / "w.so"
    r = subprocess.run([cc, "-O2", "-fPIC", "-shared", "-Wall", "-Werror",
                        str(d / "w.c"), "-o", str(so)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lib = ctypes.CDLL(str(so))
    lib.w_parse.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32)]
    lib.w_parse.restype = ctypes.c_int
    lib.w_cmp.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    lib.w_cmp.restype = ctypes.c_int
    return lib


def _parse(lib, name):
    o = (ctypes.c_uint32 * 3)()
    ok = lib.w_parse(name.encode(), o)
    return ok, tuple(o)


NAMES = [
    # older firmware, in every shape a card can still hold
    "nyquist-db21acd5-1016781646.wav",
    "07-nyquist-4f0c9b12-0000149504.wav",
    "98-hear-5c4c94-6dbcdef7-0487893711.wav",
    "60-nyquist-fee615d8-0223973155.wav",
    # this firmware: boot sequence 0x00002a over two boots of the same card, then 0x00002b
    "nyquist-00002a9f13c0-0000000100.wav",
    "nyquist-00002a9f13c0-0240479148.wav",
    "nyquist-00002b000001-0000000050.wav",
    "nyquist-00002b000001-4294967295.wav",
    "hear-5c4c94-00002b7a7a7a-0000000051.wav",
]


def test_this_firmwares_names_parse_and_older_names_do_not(order):
    ok, (legacy, seq, sample) = _parse(order, "nyquist-00002a9f13c0-0240479148.wav")
    assert ok and not legacy and seq == 0x2a and sample == 240479148
    for n in NAMES[:4]:
        assert _parse(order, n)[1][0] == 1, "%s must be older-format" % n
    for bad in ("nyquist-00002a9f13c-0240479148.wav",       # 11 hex
                "nyquist-00002a9f13c0-240479148.wav",        # 9 digits
                "nyquist-00002a9f13c0-0240479148.WAV",
                "-00002a9f13c0-0240479148.wav",              # no node
                "nyquist-00002a9f13c0-9999999999.wav"):      # sample past uint32
        assert _parse(order, bad) == (0, (1, 0, 0)), bad


def test_every_older_name_goes_before_every_new_one_then_boot_then_sample(order):
    import functools
    got = sorted(NAMES, key=functools.cmp_to_key(lambda a, b: order.w_cmp(a.encode(), b.encode())))
    assert got[:4] == sorted(NAMES[:4])
    assert got[4:] == [
        "nyquist-00002a9f13c0-0000000100.wav",
        "nyquist-00002a9f13c0-0240479148.wav",
        "nyquist-00002b000001-0000000050.wav",
        "hear-5c4c94-00002b7a7a7a-0000000051.wav",
        "nyquist-00002b000001-4294967295.wav",
    ], "a later boot must never be evicted before an earlier one, whatever its sample counter says"


def test_the_drain_orders_clips_exactly_as_the_node_evicts_them(order):
    for a, b in itertools.product(NAMES, repeat=2):
        c = order.w_cmp(a.encode(), b.encode())
        ka, kb = CL.eviction_key(a), CL.eviction_key(b)
        py = (ka > kb) - (ka < kb)
        assert (c > 0) - (c < 0) == py, (a, b, c, ka, kb)


def test_the_parser_accepts_the_new_shape():
    p = CL.parse_clip_name("/clips/nyquist-00002a9f13c0-0240479148.wav")
    assert (p["node"], p["boot"], p["sample"], p["prio"]) == (
        "nyquist", "00002a9f13c0", 240479148, None)


def test_nothing_is_ranked_and_nothing_is_refused_for_budget():
    code = _code()
    for gone in ("clip_priority", "clip_evict_worse_than", "clip_prio_pending", "clip_skip_budget"):
        assert gone not in code, "%s is back" % gone
    assert not re.search(r"\bCLIP_BUDGET\b", code), (
        "a clip_st of CLIP_BUDGET means a clip refused because older clips held the window")
    assert '"budget"' not in _body("static const char *clip_why")


def test_room_is_made_by_evicting_the_oldest_before_the_file_is_opened():
    b = _body("static void clip_pump")
    assert "clip_make_room()" in b
    assert b.index("clip_make_room()") < b.index("SD.open(path, FILE_WRITE)")
    assert "sk_st" not in b, "with no ranking there is nothing to wait for the sketch for"
    m = _body("static bool clip_make_room")
    assert "clip_q[0]" in m, "eviction must take the head of the oldest-first queue"


def test_a_landed_clip_joins_the_back_of_the_queue():
    b = _body("static void clip_pump")
    ok = b.index("clip_written++")
    assert "clip_q_push(" in b[ok:ok + 200]


def test_oldest_holds_across_reboots():
    assert "clip_rescan();" in _body("void setup")
    r = _body("static void clip_rescan")
    assert "qsort(" in r and "clip_ent_cmp" in r
    assert "top + 1u" in r, "the next boot sequence is one past the highest on the card"


def test_the_sequence_moves_on_when_the_sample_counter_wraps():
    # g_samples is uint32 at 16 kHz and wraps every 74.6 h; without this a rescan after a long
    # boot would evict that boot's post-wrap clips before its pre-wrap ones.
    b = _body("static void clip_pump")
    bump = re.search(r"if \(clip_have_last && d\.sample < clip_last_sample\)\s*"
                     r"clip_seq = \(clip_seq \+ 1u\) & CLIP_SEQ_MASK;", b)
    assert bump, "a numerically smaller sample after a written clip must advance the sequence"
    assert bump.start() < b.index("clip_make_room()")


def test_a_row_names_its_clip_with_the_sequence_it_was_written_under():
    b = _body("static void clip_pump")
    assert "d.cseq = clip_seq;" in b and "clip_at_seq = clip_seq;" in b
    for fn in ("static void det_flush", "static void h_dets"):
        assert "clip_name(cp, sizeof cp, d.cseq, d.sample)" in _body(fn), fn


def test_the_name_carries_no_priority_field():
    b = _body("static void clip_name")
    m = re.search(r'CLIP_DIR\s+"([^"]+)"', b)
    assert m and m.group(1) == "/%s-%06lx%s-%010lu.wav", m and m.group(1)
