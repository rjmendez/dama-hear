"""Regression checks for firmware Wi-Fi and HTTP reconnect backoff."""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKETCHES = [
    ROOT / "firmware" / "hear_node" / "hear_node.ino",
    ROOT / "firmware" / "puc_node" / "puc_node.ino",
]


def _code(path):
    text = path.read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


@pytest.mark.parametrize("sketch", SKETCHES, ids=lambda p: p.parent.name)
def test_wifi_reconnect_uses_full_jitter_exponential_backoff(sketch):
    code = _code(sketch)
    assert "WiFi.reconnect();" in code
    assert "1000UL" in code and "60000UL" in code
    assert "random((long)cap + 1L)" in code
    assert "cap = cap > 30000UL ? 60000UL : cap * 2;" in code
    assert "wifi_failures++" in code
    assert "wifi_failures = 0" in code


def test_hear_http_push_retries_are_jittered_and_keep_normal_heartbeat_cadence():
    code = _code(SKETCHES[0])
    assert "#define HEAR_PUSH_HEARTBEAT_MS      10000UL" in code
    assert "#define HEAR_PUSH_RETRY_BASE_MS     1000UL" in code
    assert "#define HEAR_PUSH_HEARTBEAT_MAX_MS  60000UL" in code
    start = code.index("static uint32_t push_backoff_ms()")
    end = code.index("static void push_schedule_heartbeat", start)
    body = code[start:end]
    assert "random((long)cap + 1L)" in body
    assert "cap = cap > HEAR_PUSH_HEARTBEAT_MAX_MS / 2 ? HEAR_PUSH_HEARTBEAT_MAX_MS : cap * 2;" in body


def test_sketches_do_not_claim_to_implement_mqtt_reconnection():
    for sketch in SKETCHES:
        code = _code(sketch).lower()
        assert "pubsubclient" not in code
        assert "mqttclient" not in code
