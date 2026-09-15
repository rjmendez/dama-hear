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
