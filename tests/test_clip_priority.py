"""The clip budget must not go back to first-come, or to ranking by loudness.

Measured 2026-09-08/09 over 19.9 h on three nodes: 437 primary detections, 96 of them heard by a
second node within 250 ms. Ranking by `peak` selects those at 8% against a 22% base rate -- worse
than random, because the loudest things here are close and local. Low-frequency dominance selects
them at 3.1x the base rate and survives dropping the strongest class (2.78x).

These scan the firmware SOURCE, so they read only each function's body with comments stripped --
a grep over the whole file would match the very comment that explains the rule.
"""
import re
from pathlib import Path

INO = Path(__file__).resolve().parents[1] / "firmware" / "night_node" / "night_node.ino"


def _body(name):
    """The braces-balanced body of a C function, comments removed."""
    src = INO.read_text()
    i = src.index(name + "(")
    i = src.index("{", i)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{": depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0: break
        j += 1
    body = src[i:j]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return "\n".join(l.split("//")[0] for l in body.splitlines())


def test_priority_is_computed_from_the_sketch_bands():
    b = _body("static uint8_t clip_priority")
    assert "frame[12" in b, "priority must read the mel bands out of the packed frame"
    assert "frame[8]" in b and "frame[9]" in b, "geometry comes from the frame, not a constant"


def test_priority_does_not_rank_by_amplitude():
    # The measured trap: top-25 by peak is 8% coincident against a 22% base rate.
    b = _body("static uint8_t clip_priority")
    for banned in ("peak", "ref_db", "r4", "trigger"):
        assert banned not in b, (
            "clip_priority references %r. Amplitude ranking measured WORSE than random for "
            "selecting cross-node events; see the note above the function." % banned)


def test_the_priority_leads_the_clip_name():
    # Eviction is a lexicographic directory scan, so priority must be the first field or the scan
    # sorts by node and boot instead and priority never binds.
    b = _body("static void clip_name")
    m = re.search(r'CLIP_DIR\s+"([^"]+)"', b)
    assert m, "clip_name must build its path from CLIP_DIR"
    assert m.group(1).lstrip("/").startswith("%02u"), (
        "the priority field must come first in the filename, got %r" % m.group(1))


def test_eviction_refuses_to_delete_something_better():
    b = _body("static bool clip_evict_worse_than")
    assert "weakest >= prio" in b, (
        "eviction must refuse when the weakest clip on the card is at least as good as the "
        "incoming one; unconditional eviction is first-come wearing a different hat")


def test_an_unranked_clip_is_not_written():
    src = INO.read_text()
    assert "if (d.sk_st == 0) return;" in src, (
        "a clip whose sketch has not been built has no priority, and writing it would put an "
        "unranked clip ahead of a measured one")
