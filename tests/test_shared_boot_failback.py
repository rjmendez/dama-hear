"""The OTA failback exists ONCE, and healthy always means reachable.

⚠️THIS GUARDS A DEFECT THAT WAS LIVE, not a style preference. boot_guard() and
mark_healthy_once() were copy-pasted into both sketches and the copies drifted:

    mark_healthy_once()   45.2% identical
    boot_guard()          80.9% identical

night_node's copy refused to mark an image healthy unless the node was reachable, and said why.
puc_node's copy marked healthy after 30 s unconditionally, set proven_ok, and thereby switched off
its own partition revert permanently -- on a node that tracks sta_ok in seven other places. The
warning did not travel with the copy, which is what copies do.

So: neither sketch may define these again, and the shared version must keep taking reachability as
an argument. Comments are stripped before scanning -- a guard that greps its own prose passes
against broken code, and this repo has shipped that bug twice.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKETCHES = [ROOT / "firmware" / "night_node" / "night_node.ino",
            ROOT / "firmware" / "puc_node" / "puc_node.ino"]
LIB = ROOT / "firmware" / "lib" / "hear_platform" / "src"


def _code(p):
    s = re.sub(r"/\*.*?\*/", "", p.read_text(), flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.name)
@pytest.mark.parametrize("fn", ["boot_guard", "mark_healthy_once"])
def test_no_sketch_redefines_the_failback(sketch, fn):
    code = _code(sketch)
    assert not re.search(r"\bstatic\s+\w+\s+%s\s*\(" % fn, code), (
        "%s defines its own %s() again; that is how the copies drifted the first time"
        % (sketch.name, fn))


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.name)
def test_every_sketch_calls_the_shared_one(sketch):
    code = _code(sketch)
    assert "hear_boot_guard()" in code, "%s never arms the failback" % sketch.name
    assert "hear_boot_tick(" in code, "%s never advances it from loop()" % sketch.name


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.name)
def test_the_tick_is_passed_real_reachability_not_a_literal(sketch):
    """⚠️THE WHOLE POINT. hear_boot_tick(true) would compile, restore the exact bug the parameter
    exists to prevent, and look like a call to the fixed function."""
    code = _code(sketch)
    m = re.search(r"hear_boot_tick\(\s*([^)]*)\)", code)
    assert m, "no hear_boot_tick call found"
    arg = m.group(1).strip()
    assert arg not in ("true", "1", ""), (
        "%s passes a constant (%r) as reachability, which disables the failback exactly as the "
        "duplicated copy did" % (sketch.name, arg))


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.name)
def test_the_include_is_not_nested_in_the_secrets_guard(sketch):
    """A sketch built without secrets.h would otherwise get no failback and no compile error --
    the same nesting mistake this repo already shipped once for the platform include."""
    src = sketch.read_text()
    i = src.find("#include <hear_boot.h>")
    assert i > 0, "%s does not include hear_boot.h" % sketch.name
    before = src[:i]
    depth = len(re.findall(r"^#if", before, re.M)) - len(re.findall(r"^#endif", before, re.M))
    assert depth == 0, (
        "%s includes hear_boot.h inside a conditional block (depth %d)" % (sketch.name, depth))


def test_healthy_still_requires_reachable_in_the_library():
    code = _code(LIB / "hear_boot.cpp")
    assert "if (!reachable) return;" in code, (
        "the library no longer refuses to mark an unreachable image healthy -- that is the defect")
    assert "esp_ota_mark_app_valid_cancel_rollback" in code


def test_the_library_is_the_only_definition():
    hits = [p.name for p in LIB.glob("*.cpp") if re.search(r"\bhear_boot_tick\s*\(", _code(p))]
    assert hits == ["hear_boot.cpp"], "hear_boot_tick defined in %s" % hits
