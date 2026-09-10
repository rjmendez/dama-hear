"""A build with no credentials must fail, not ship a node nobody can reach.

⚠️THIS IS A HEADSTONE TOO. On 2026-09-10 rankine was flashed with an image built without
secrets.h. `#ifndef WIFI_N` supplied WIFI_N 0 and two empty string arrays, arduino-cli reported
success, and the node came up in AP mode on 192.168.4.1 -- invisible from the LAN. It sat awake for
hours with a GPS fix, writing scene rows to its card, while it was diagnosed as bricked and
physically retrieved from the field. The only thing that said what had happened was the serial
console, which is precisely what a deployed node does not have.

The AP-only build is still legitimate -- bench work, compile checks -- so it survives behind a flag
someone has to type. What does not survive is getting it BY DEFAULT and BY ACCIDENT.

Comments are stripped before scanning; a guard that greps its own prose passes against broken code.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKETCHES = [ROOT / "firmware" / "night_node" / "night_node.ino",
            ROOT / "firmware" / "puc_node" / "puc_node.ino"]
GUARD = ROOT / "firmware" / "lib" / "hear_platform" / "src" / "hear_wifi_guard.h"


def _code(p):
    s = re.sub(r"/\*.*?\*/", "", p.read_text(), flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.name)
def test_no_sketch_defines_a_silent_wifi_fallback(sketch):
    """`#ifndef WIFI_N / #define WIFI_N 0` is the exact construct that produced the AP-only node."""
    code = _code(sketch)
    m = re.search(r"#\s*ifndef\s+WIFI_N(.{0,400}?)#\s*endif", code, re.S)
    assert m is None, (
        "%s still defines its own WIFI_N fallback; that is how a credential-less build silently "
        "becomes a node on its own AP" % sketch.name)


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.name)
def test_every_sketch_includes_the_guard(sketch):
    assert "#include <hear_wifi_guard.h>" in _code(sketch), (
        "%s does not include the guard, so a build with no secrets.h would succeed" % sketch.name)


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.name)
def test_the_guard_comes_after_the_secrets_include(sketch):
    """It decides on WIFI_N. Included first, it would fire on every build, credentials or not."""
    code = _code(sketch)
    assert code.index('#include "secrets.h"') < code.index("#include <hear_wifi_guard.h>"), (
        "%s includes the guard before secrets.h, so it can never see WIFI_N" % sketch.name)


def test_the_guard_errors_by_default_and_only_yields_to_an_explicit_flag():
    code = _code(GUARD)
    assert "#    error" in code or "#error" in code, "the guard does not actually stop the build"
    assert "HEAR_ALLOW_NO_WIFI" in code, "there is no deliberate opt-out, so bench builds are blocked"
    # The error must be the DEFAULT branch: opt-out inside the #if, error in the #else.
    i_flag, i_err = code.index("HEAR_ALLOW_NO_WIFI"), code.index("error")
    assert i_flag < i_err, "the opt-out must be the exception and the error the default"


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.name)
def test_an_ap_only_image_admits_it_at_runtime(sketch):
    """⚠️REACHABLE AND LYING ABOUT WHY is the second-worst outcome. A node answering on its own AP
    must say that it was built without credentials, not merely that sta is false."""
    code = _code(sketch)
    assert "HEAR_WIFI_CONFIGURED" in code, (
        "%s never reports whether it was built with credentials at all" % sketch.name)
    assert re.search(r'wifi_configured|\\"configured\\"', code), (
        "%s does not expose it in /status" % sketch.name)
