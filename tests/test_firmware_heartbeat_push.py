"""Heartbeat/event push serialization and safety guards for hear_node firmware."""
import ctypes
import json
import re
import shutil
import subprocess
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


def test_transport_uses_a_single_short_connect_and_fire_and_forget_write():
    body = _fn("push_post_json")
    assert "HTTPClient" not in CODE
    assert "client.connect(HEAR_PUSH_HOST, HEAR_PUSH_PORT, HEAR_PUSH_CONNECT_TIMEOUT_MS)" in body
    assert '"X-Hear-Token: %s\\r\\n"' in body
    assert "client.write((const uint8_t *)req, (size_t)n)" in body
    assert "client.stop();" in body
    assert "boot_wdt_arm" not in body
    assert "while" not in body, "transport must not spin or retry inside one attempt"


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


def test_firmware_uses_the_host_port_the_receiver_actually_exposes():
    docs = list(yaml.safe_load_all(MANIFEST.read_text()))
    dep = docs[0]
    ports = dep["spec"]["template"]["spec"]["containers"][0]["ports"]
    host_port = ports[0]["hostPort"]
    assert '#define HEAR_PUSH_HOST              "mrpink"' in CODE
    assert '#define HEAR_PUSH_PORT              5051u' in CODE
    assert host_port == 5051
