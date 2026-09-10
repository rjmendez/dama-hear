"""setup() must bring the recovery channel up before anything that can fail, and guard what can hang.

⚠️THIS IS A HEADSTONE. rankine was lost on 2026-09-10 to a firmware flash whose audio init did not
return. The node had a failback -- boot_guard() reverts the OTA partition after three boots that
never reach healthy -- and it saved nothing, because:

  * boot_guard() counts RESETS, and a hang is not a reset;
  * mark_healthy_once(), which forces a reboot when unreachable, runs in loop();
  * http.handleClient() also runs in loop(), so the server is begun but deaf.

A setup() that never returns therefore produces no reset, no counter, no revert and no HTTP -- a
node that is powered, awake and unreachable until someone walks to it. Both halves are asserted
here: the ORDER (recovery channel first) and the WATCHDOG (a hang becomes a reset the existing
failback already handles).

⚠️COMMENTS ARE STRIPPED BEFORE SCANNING. A guard that greps its own explanatory prose passes
against broken code; this repo has shipped that bug twice.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "night_node" / "night_node.ino"


def _setup_body():
    src = INO.read_text()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    i = src.index("\nvoid setup()")
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[j:k]
    raise AssertionError("setup() has no closing brace")


def _pos(body, needle):
    assert needle in body, "%s is not in setup() at all" % needle
    return body.index(needle)


@pytest.fixture(scope="module")
def body():
    return _setup_body()


def test_the_web_server_is_up_before_the_audio_hardware(body):
    """The recovery channel must not sit behind the thing most likely to fail."""
    assert _pos(body, "http.begin()") < _pos(body, "i2s.begin("), (
        "i2s.begin() runs before http.begin(): a node that fails there is unreachable")


def test_the_psram_ring_is_also_after_the_web_server(body):
    assert _pos(body, "http.begin()") < _pos(body, "ps_malloc("), (
        "the PSRAM ring is allocated before the server is up")


def test_the_audio_bringup_runs_under_a_watchdog(body):
    """⚠️ORDER ALONE DOES NOT SURVIVE A HANG -- handleClient() is in loop(). Only a reset does."""
    arm, dis = _pos(body, "boot_wdt_arm("), _pos(body, "boot_wdt_disarm()")
    i2s, psram = _pos(body, "i2s.begin("), _pos(body, "ps_malloc(")
    assert arm < i2s < dis, "i2s.begin() is not inside the watchdog window"
    assert arm < psram < dis, "the PSRAM allocation is not inside the watchdog window"


def test_the_watchdog_panics_rather_than_only_warning(body):
    """A watchdog that logs and continues leaves the hang in place. It must reset the chip, because
    the reset is what advances boot_try and eventually reverts the partition."""
    src = re.sub(r"//[^\n]*", "", INO.read_text())
    assert "trigger_panic = true" in src or ".trigger_panic = true" in src, (
        "the task watchdog is not configured to panic, so a hang would not become a reset")


def test_the_wifi_join_is_not_inside_the_watchdog_window(body):
    """The join deliberately spends up to 12 s retrying and must not be killed for it."""
    assert _pos(body, "WiFi.begin(") < _pos(body, "boot_wdt_arm("), (
        "the WiFi join is inside the watchdog window and will be shot for being slow")


def test_the_card_is_mounted_before_gps_bringup_reads_its_hint():
    """gps_pins_hint() reads /gps.cfg and returns -1 when !sd_ok. With SD.begin after
    gps_bringup() in setup(), the stored pin order was never seen at boot (Copilot, PR #26)."""
    body = _setup_body()
    assert _pos(body, "SD.begin(") < _pos(body, "gps_bringup()"), (
        "gps_bringup() runs before the card is mounted, so its pin-order hint is always -1")
