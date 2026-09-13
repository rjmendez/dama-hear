"""Heartbeat/event push serialization and safety guards for hear_node firmware."""
import ctypes
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
HDR = ROOT / "firmware" / "hear_node" / "hear_push_payload.h"
MANIFEST = ROOT / "deploy" / "k8s" / "hear-heartbeat.yaml"


def _strip_comments(src):
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//.*", "", src)


CODE = _strip_comments(INO.read_text())


def _block(src, i):
    i = src.index("{", i)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError("unbalanced block")


def _fn(name):
    m = re.search(r"^(?:static\s+)?[^\n;]*\b%s\(" % re.escape(name), CODE, flags=re.M)
    assert m, "%s() not found" % name
    return _block(CODE, m.end())


@pytest.fixture(scope="module")
def hb(tmp_path_factory):
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no cc on PATH: hear_push_payload.h could not be compiled")
    d = tmp_path_factory.mktemp("heartbeat_push")
    (d / "w.c").write_text(
        '#include <string.h>\n'
        '#include "%s"\n' % HDR
        + "int hb_json(char *out, size_t n){\n"
          "  hear_push_heartbeat_t hb = {\n"
          '    .device_id = "nyquist", .node_class = "xiao-s3-pps", .fw_version = "7f84d29",\n'
          "    .uptime_s = 12345, .gps_fix = 3, .time_valid = 1,\n"
          "    .utc_us = 1789256584000000LL, .wifi_has_rssi = 1, .wifi_rssi_dbm = -67,\n"
          "    .scene_rows_written = 456789, .dets_rows_written = 812,\n"
          "    .clips_written = 233, .clips_evicted = 41};\n"
          "  return hear_push_heartbeat_json(&hb, out, n);}\n"
          "int hb_null_json(char *out, size_t n){\n"
          "  hear_push_heartbeat_t hb = {\n"
          '    .device_id = "mach", .node_class = "xiao-s3-pps", .fw_version = "unset",\n'
          "    .uptime_s = 22, .gps_fix = 0, .time_valid = 0, .utc_us = 0,\n"
          "    .wifi_has_rssi = 0, .wifi_rssi_dbm = 0, .scene_rows_written = 1,\n"
          "    .dets_rows_written = 2, .clips_written = 3, .clips_evicted = 4};\n"
          "  return hear_push_heartbeat_json(&hb, out, n);}\n"
          "int clip_event_json(char *out, size_t n){\n"
          "  hear_push_event_t ev = {\n"
          '    .device_id = "nyquist", .node_class = "xiao-s3-pps", .fw_version = "7f84d29",\n'
          '    .uptime_s = 12349, .event_type = "clip_written", .event_seq = 234,\n'
          "    .time_valid = 1, .utc_us = 1789256584000000LL,\n"
          '    .clip_basename = "nyquist-db21acd5-1082530195.wav",\n'
          "    .clips_written = 234, .clips_evicted = 41};\n"
          "  return hear_push_event_json(&ev, out, n);}\n"
          "int det_event_json(char *out, size_t n){\n"
          "  hear_push_event_t ev = {\n"
          '    .device_id = "nyquist", .node_class = "xiao-s3-pps", .fw_version = "7f84d29",\n'
          '    .uptime_s = 12350, .event_type = "detection_batch_ready", .event_seq = 812,\n'
          "    .time_valid = 0, .utc_us = 0, .clip_basename = \"\",\n"
          "    .dets_rows_written = 812, .batch_rows = 16};\n"
          "  return hear_push_event_json(&ev, out, n);}\n"
          "int ts_json(char *out, size_t n){ return hear_push_rfc3339(1789256584000000LL, out, n); }\n"
    )
    so = d / "w.so"
    r = subprocess.run([cc, "-O2", "-fPIC", "-shared", "-Wall", "-Werror",
                        str(d / "w.c"), "-o", str(so)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lib = ctypes.CDLL(str(so))
    for name in ("hb_json", "hb_null_json", "clip_event_json", "det_event_json", "ts_json"):
        getattr(lib, name).argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        getattr(lib, name).restype = ctypes.c_int
    return lib


def _call(lib, name, size=512):
    buf = ctypes.create_string_buffer(size)
    n = getattr(lib, name)(buf, len(buf))
    assert n > 0
    return buf.value.decode()


def test_rfc3339_formatter(hb):
    got = _call(hb, "ts_json", 64)
    assert got == datetime.fromtimestamp(1789256584, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_heartbeat_payload_matches_the_design_shape(hb):
    got = json.loads(_call(hb, "hb_json"))
    assert got == {
        "telemetry_path": "hear/heartbeat",
        "telemetry_schema_version": 1,
        "device_id": "nyquist",
        "ts": "2026-09-12T23:43:04Z",
        "ts_ms": 1789256584000,
        "class": "xiao-s3-pps",
        "fw_version": "7f84d29",
        "uptime_s": 12345,
        "gps": {"fix": 3},
        "time": {"valid": True},
        "wifi": {"rssi_dbm": -67},
        "counters": {
            "scene_rows_written": 456789,
            "dets_rows_written": 812,
            "clips_written": 233,
            "clips_evicted": 41,
        },
    }


def test_heartbeat_omits_untrusted_time_as_null(hb):
    got = json.loads(_call(hb, "hb_null_json"))
    assert got["ts"] is None
    assert got["time"] == {"valid": False}
    assert got["wifi"] == {"rssi_dbm": None}
    # AWS's ingest Lambda quarantines any message without a positive ts_ms/ts/timestamp, so a
    # bare "ts": null would silently drop every heartbeat sent before first GPS fix. ts_ms must
    # still be a positive placeholder in that case -- callers must key off "time":{"valid":false}
    # rather than trust it as wall-clock time.
    assert got["ts_ms"] > 0
    assert got["ts_ms"] == 22 * 1000 + 1


def test_clip_event_payload_stays_small_and_names_only_the_clip(hb):
    raw = _call(hb, "clip_event_json")
    got = json.loads(raw)
    assert len(raw) < 384
    assert got["event_type"] == "clip_written"
    assert got["event_seq"] == 234
    assert got["event"] == {
        "clip_basename": "nyquist-db21acd5-1082530195.wav",
        "clips_written": 234,
        "clips_evicted": 41,
    }


def test_detection_batch_event_is_a_hint_not_the_csv_body(hb):
    got = json.loads(_call(hb, "det_event_json"))
    assert got["ts"] is None
    assert got["event_type"] == "detection_batch_ready"
    assert got["event"] == {"dets_rows_written": 812, "batch_rows": 16}
    assert "csv" not in json.dumps(got).lower()


def test_transport_makes_a_single_short_connect_and_reads_the_status_line():
    body = _fn("push_post_json")
    assert "HTTPClient" not in CODE
    assert "client.connect(HEAR_PUSH_HOST, HEAR_PUSH_PORT, HEAR_PUSH_CONNECT_TIMEOUT_MS)" in body
    assert '"X-Hear-Token: %s\\r\\n"' in body
    assert "client.write((const uint8_t *)req, (size_t)n)" in body
    assert "client.setTimeout(HEAR_PUSH_READ_TIMEOUT_MS);" in body
    assert "client.readStringUntil('\\n');" in body
    assert "client.stop();" in body
    assert "boot_wdt_arm" not in body
    assert "while" not in body, "transport must not spin or retry inside one attempt"


def test_transport_treats_a_written_request_as_pending_until_the_status_line_says_otherwise():
    body = _fn("push_post_json")
    # The old bug: *code_out = 204 was set unconditionally once client.write() returned, so a
    # 401 from an auth-enabled receiver was invisible to the firmware. Only the parsed status
    # line may decide the return value now.
    assert "*code_out = 204;\n  return true;" not in body
    assert "return code >= 200 && code < 300;" in body
    assert 'status_line.startsWith("HTTP/1.")' in body


def test_loop_only_attempts_one_pending_send_then_returns():
    body = _fn("push_pump")
    assert "push_clip_event.pending = false;" in body
    assert "push_send_event(&ev);" in body
    assert body.count("return;") >= 3
    assert "push_send_heartbeat();" in body


def test_loop_marks_real_events_where_they_happen():
    clip = _fn("clip_pump")
    dets = _fn("det_flush")
    assert "push_mark_clip_written(p);" in clip
    assert "if (wrote) push_mark_dets_ready(wrote);" in dets
    assert "push_pump();" in _fn("loop")


def test_heartbeat_backoff_is_tens_of_seconds_capped_at_one_minute():
    assert '#define HEAR_PUSH_HEARTBEAT_MS      10000UL' in CODE
    assert '#define HEAR_PUSH_HEARTBEAT_MAX_MS  60000UL' in CODE
    body = _fn("push_backoff_ms")
    assert "ms *= 2;" in body
    assert "return HEAR_PUSH_HEARTBEAT_MAX_MS;" in body


def test_firmware_defaults_to_the_public_ingest_api_over_tls():
    # The LAN receiver (172.21.171.198:5051) turned out unreachable inbound from the fleet's
    # subnet -- confirmed empirically (zero SYN packets arriving) after fixing the connect
    # timeout did not help. The default now targets dama-gotchi's existing public ingest API,
    # which the fleet already reaches like any other internet host.
    assert '#define HEAR_PUSH_HOST              "api.botnet.floppydicks.net"' in CODE
    assert '#define HEAR_PUSH_PORT              443u' in CODE
    assert '#define HEAR_PUSH_TLS               1' in CODE
    assert '"172.21.171.198"' not in CODE


def test_the_receiver_manifest_still_exposes_its_own_port_for_a_lan_only_build():
    # HEAR_PUSH_WRAP_BATCH=0 / HEAR_PUSH_HOST override still targets this receiver directly, so
    # its manifest and firmware's non-default LAN path must agree on the port even though the
    # compiled-in default no longer points here.
    docs = list(yaml.safe_load_all(MANIFEST.read_text()))
    dep = docs[0]
    ports = dep["spec"]["template"]["spec"]["containers"][0]["ports"]
    assert ports[0]["hostPort"] == 5051
    assert '"/api/hear/heartbeat"' in CODE
    assert '"/api/hear/event"' in CODE


def test_push_post_json_uses_tls_and_wraps_the_batch_envelope_for_the_ingest_api():
    body = _fn("push_post_json")
    assert "WiFiClientSecure client;" in body
    assert "client.setInsecure();" in body
    assert '"{\\"device_id\\":\\"%s\\",\\"messages\\":[%.*s]}"' in body
    assert "node_id," in body
    assert "Authorization: Bearer %s" in body


def test_the_connect_timeout_is_long_enough_to_complete_a_real_tcp_handshake():
    # HEAR_PUSH_CONNECT_TIMEOUT_MS was 15 -- 15 milliseconds, not 1.5 seconds -- which meant
    # every single push failed at connect() before a WiFi TCP handshake could ever complete,
    # regardless of whether the host was reachable or the token was valid. Caught by flashing a
    # real node and reading its /log: "push heartbeat failed (-4)" (client.connect() returned
    # false) on every attempt, even against a receiver later confirmed reachable and correctly
    # configured.
    m = re.search(r"#define HEAR_PUSH_CONNECT_TIMEOUT_MS\s+(\d+)u", CODE)
    assert m, "HEAR_PUSH_CONNECT_TIMEOUT_MS not found"
    assert int(m.group(1)) >= 1000
    # "mrpink" is a Tailscale MagicDNS name; WiFiClient on this firmware has no MagicDNS
    # resolver, so that default could never have delivered a single heartbeat.
    assert '"mrpink"' not in CODE


def test_gen_secrets_can_override_the_push_host_and_token_from_a_local_file(tmp_path, monkeypatch):
    sys.path.insert(0, str((ROOT / "firmware" / "hear_node")))
    import importlib
    import gen_secrets
    importlib.reload(gen_secrets)
    push_file = tmp_path / "hear_push"
    push_file.write_text("HEAR_PUSH_HOST=203.0.113.5\nHEAR_PUSH_TOKEN=deadbeefcafe\n")
    cfg = gen_secrets.read_push_config(str(push_file))
    assert cfg == {"HEAR_PUSH_HOST": "203.0.113.5", "HEAR_PUSH_TOKEN": "deadbeefcafe"}


def test_gen_secrets_push_config_is_empty_when_no_local_file_exists(tmp_path):
    sys.path.insert(0, str((ROOT / "firmware" / "hear_node")))
    import importlib
    import gen_secrets
    importlib.reload(gen_secrets)
    assert gen_secrets.read_push_config(str(tmp_path / "does-not-exist")) == {}
