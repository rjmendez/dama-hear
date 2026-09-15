import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
HEAR = ROOT / "firmware" / "hear_node" / "hear_node.ino"
PUC = ROOT / "firmware" / "puc_node" / "puc_node.ino"


def _strip(src):
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//.*", " ", src)


def _block(src, name):
    text = _strip(src)
    m = re.search(rf"\b{name}\s*\([^)]*\)\s*\{{", text)
    assert m, f"{name}() not found"
    i = text.find("{", m.start())
    depth = 0
    for j in range(i, len(text)):
        if text[j] == '{':
            depth += 1
        elif text[j] == '}':
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    raise AssertionError(f"unbalanced block for {name}")


def _braced(text, i):
    """The braces-balanced block starting at the first '{' at or after i."""
    i = text.index("{", i)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == '{':
            depth += 1
        elif text[j] == '}':
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    raise AssertionError("unbalanced block")


def _handler(text, path):
    i = text.index('http.on("%s"' % path)
    return _braced(text, text.index("[]()", i))


def test_hear_node_keeps_watchdog_and_rtc_postmortem_state_live():
    src = _strip(HEAR.read_text())
    assert "esp_task_wdt.h" in src
    assert "loop_wdt_arm(5000);" in src
    assert "boot_wdt_service();" in _block(HEAR.read_text(), "loop")
    assert "RTC_NOINIT_ATTR static uint32_t rtc_last_reset_reason" in src
    assert "RTC_NOINIT_ATTR static uint32_t rtc_last_panic_code" in src
    rtc = _block(HEAR.read_text(), "setup")
    assert "rtc_prev_reset_reason = rtc_last_reset_reason;" in rtc
    assert "rtc_last_reset_reason = reset_reason;" in rtc
    assert '\\"postmortem\\":%s' in src
    assert "boot_wdt_service();" in _block(HEAR.read_text(), "stream_ready")
    assert "Update.write(u.buf, u.currentSize)" in src and src.count("boot_wdt_service();") >= 2


def test_the_response_stream_path_services_the_runtime_watchdog():
    """loop_wdt_arm(5000) counts against handlers too: http.handleClient() runs them from inside
    loop(), so a whole-file /sd fetch of an 11.6 MB rolled CSV sits between two of loop()'s
    resets for tens of seconds. v0.1.5 panicked with reset=task_wdt in every */15 drain window
    on rankine because stream_ready() -- the one function every long handler gates each chunk on
    -- never reset the watchdog."""
    body = _block(HEAR.read_text(), "stream_ready")
    assert "boot_wdt_service();" in body, (
        "stream_ready() must service the loop watchdog; without it any response larger than "
        "5 s of link time panics the node")
    # Before the pump and the writability probe, so a chunk is serviced on every iteration
    # including the one that returns true straight away.
    assert body.index("boot_wdt_service();") < body.index("stream_pump(due);") < body.index("select(")
    # Servicing belongs to the wait, not to the blocking calls around it: a card read or a socket
    # write that hangs must still reach the watchdog.
    src = _strip(HEAR.read_text())
    for name in ("/sd", "/ls", "/audio", "/perf"):
        assert "boot_wdt_service" not in _handler(src, name), (
            "%s services the watchdog outside stream_ready(); that hides blocked I/O" % name)


def test_puc_node_has_runtime_watchdog_and_reset_logging():
    src = _strip(PUC.read_text())
    assert "esp_task_wdt.h" in src
    assert "task_wdt_arm(5000);" in src
    assert "task_wdt_service();" in _block(PUC.read_text(), "loop")
    assert "RTC_NOINIT_ATTR static uint32_t rtc_last_reset_reason" in src
    assert "RTC_NOINIT_ATTR static uint32_t rtc_last_panic_code" in src
    assert "rtc_prev_reset_reason = rtc_last_reset_reason;" in src
    assert '\\"postmortem\\":%s' in src
    assert "task_wdt_wait_ms(3000);" in src
    assert "task_wdt_wait_ms(1500);" in src
    assert "task_wdt_kick();" in _block(PUC.read_text(), "ntp_query")
    assert src.count("Update.write(u.buf, u.currentSize)") == 1 and src.count("task_wdt_service();") >= 3
