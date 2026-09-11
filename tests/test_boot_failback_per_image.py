"""A new image is not proven by the one it replaced.

proven_ok lives in RTC memory, which survives esp_restart(). /update restarts into the new image
without touching it, so an OTA from a healthy image used to boot the new one already "proven":
the counter was zeroed and the revert switched off for exactly the image that had not earned it.
proven_ok now counts only on the partition that earned it, and an OTA always lands in the other one.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware" / "lib" / "hear_platform" / "src" / "hear_boot.cpp"


def _code():
    s = re.sub(r"/\*.*?\*/", "", SRC.read_text(), flags=re.S)
    return re.sub(r"//[^\n]*", "", s)


def _body(code, sig):
    i = code.index(sig)
    j = code.index("{", i)
    depth = 0
    for k in range(j, len(code)):
        depth += {"{": 1, "}": -1}.get(code[k], 0)
        if depth == 0:
            return code[j:k + 1]
    raise AssertionError("unbalanced braces after %s" % sig)


def test_the_partition_it_was_proven_on_survives_resets_like_the_flag():
    assert re.search(r"RTC_NOINIT_ATTR\s+static\s+uint32_t\s+proven_addr\s*;", _code())


def test_the_guard_drops_a_proof_earned_on_another_partition_before_honouring_it():
    g = _body(_code(), "void hear_boot_guard(")
    m = re.search(r"if\s*\(\s*proven_ok\s*&&\s*proven_addr\s*!=\s*running_addr\(\)\s*\)", g)
    assert m, "the guard never compares the proven partition with the running one"
    honour = re.search(r"if\s*\(\s*proven_ok\s*\)", g)
    assert honour and m.start() < honour.start(), (
        "a stale proof must be dropped BEFORE the early return that honours it")
    assert "proven_ok = 0" in g[m.start():honour.start()]


def test_marking_healthy_records_the_partition():
    t = _body(_code(), "void hear_boot_tick(")
    assert re.search(r"proven_addr\s*=\s*running_addr\(\)", t)
    assert t.index("proven_ok = 1") < len(t)


def test_running_addr_reads_the_running_partition_not_the_boot_one():
    assert "esp_ota_get_running_partition" in _body(_code(), "static uint32_t running_addr(")
