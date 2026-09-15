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
          "    .utc_us = 1789256584000000LL, .clock_state = \"LOCKED\", .sync_sigma_ns = 41000,\n"
          "    .anchor_age_us = 250000, .boot_epoch_us = 1789244239000000LL,\n"
          "    .boot_id = \"0011223344556677\", .discontinuity_flags = 0,\n"
          "    .wifi_has_rssi = 1, .wifi_rssi_dbm = -67,\n"
          "    .scene_rows_written = 456789, .dets_rows_written = 812,\n"
          "    .clips_written = 233, .clips_evicted = 41};\n"
          "  return hear_push_heartbeat_json(&hb, out, n);}\n"
          "int hb_null_json(char *out, size_t n){\n"
          "  hear_push_heartbeat_t hb = {\n"
          '    .device_id = "mach", .node_class = "xiao-s3-pps", .fw_version = "unset",\n'
          "    .uptime_s = 22, .gps_fix = 0, .time_valid = 0, .utc_us = 0,\n"
          "    .clock_state = \"FAULT\", .sync_sigma_ns = 0, .anchor_age_us = 0,\n"
          "    .boot_epoch_us = 0, .boot_id = \"8899aabbccddeeff\", .discontinuity_flags = 1,\n"
          "    .wifi_has_rssi = 0, .wifi_rssi_dbm = 0, .scene_rows_written = 1,\n"
          "    .dets_rows_written = 2, .clips_written = 3, .clips_evicted = 4};\n"
          "  return hear_push_heartbeat_json(&hb, out, n);}\n"
          "int clip_event_json(char *out, size_t n){\n"
          "  hear_push_event_t ev = {\n"
          '    .device_id = "nyquist", .node_class = "xiao-s3-pps", .fw_version = "7f84d29",\n'
          '    .uptime_s = 12349, .event_type = "clip_written", .event_seq = 234,\n'
          "    .time_valid = 1, .utc_us = 1789256584000000LL, .clock_state = \"HOLDOVER\",\n"
          "    .sync_sigma_ns = 625000, .anchor_age_us = 30000000,\n"
          "    .boot_epoch_us = 1789244235000000LL, .boot_id = \"0011223344556677\",\n"
          "    .discontinuity_flags = 16,\n"
          '    .clip_basename = "nyquist-db21acd5-1082530195.wav",\n'
          "    .clips_written = 234, .clips_evicted = 41};\n"
          "  return hear_push_event_json(&ev, out, n);}\n"
          "int det_event_json(char *out, size_t n){\n"
          "  hear_push_event_t ev = {\n"
          '    .device_id = "nyquist", .node_class = "xiao-s3-pps", .fw_version = "7f84d29",\n'
          '    .uptime_s = 12350, .event_type = "detection_batch_ready", .event_seq = 812,\n'
          "    .time_valid = 0, .utc_us = 0, .clock_state = \"FAULT\", .sync_sigma_ns = 0,\n"
          "    .anchor_age_us = 0, .boot_epoch_us = 0, .boot_id = \"0011223344556677\",\n"
          "    .discontinuity_flags = 1, .clip_basename = \"\",\n"
          "    .dets_rows_written = 812, .batch_rows = 16};\n"
          "  return hear_push_event_json(&ev, out, n);}\n"
          "int ts_json(char *out, size_t n){ return hear_push_rfc3339(1789256584000000LL, out, n); }\n"
          # The heartbeat a real node emits at its WIDEST: a 23-char node id and class (the
          # sizeof node_id/node_class the sketch declares, minus the NUL), a full commit fw
          # string, the longest clock state name, a saturated sigma/anchor age/boot epoch,
          # a 16-hex boot_id, every discontinuity flag set and counters at their type maxima.
          # Phase 0's "time" object is what pushed this past the old envelope buffer.
          "int hb_max_json(char *out, size_t n){\n"
          "  hear_push_heartbeat_t hb = {\n"
          '    .device_id = "nyquist-bravo-00000007", .node_class = "esp32s3-i2s-gps-lora",\n'
          '    .fw_version = "7f84d2946bf1c0a3e5d7", .uptime_s = 4294967295UL,\n'
          "    .gps_fix = 3, .time_valid = 1, .utc_us = 9223372036854775807LL,\n"
          '    .clock_state = "HOLDOVER", .sync_sigma_ns = 18446744073709551615ULL,\n'
          "    .anchor_age_us = 18446744073709551615ULL,\n"
          "    .boot_epoch_us = 9223372036854775807LL,\n"
          '    .boot_id = "ffffffffffffffff", .discontinuity_flags = 4294967295U,\n'
          "    .wifi_has_rssi = 1, .wifi_rssi_dbm = -2147483647,\n"
          "    .scene_rows_written = 4294967295UL, .dets_rows_written = 4294967295UL,\n"
          "    .clips_written = 4294967295UL, .clips_evicted = 4294967295UL};\n"
          "  return hear_push_heartbeat_json(&hb, out, n);}\n"
          # The firmware's own envelope wrap, byte for byte -- the assertion below checks this
          # format literal is still the one in hear_node.ino.
          "int wrap_body(const char *device_id, const char *body, size_t body_len,\n"
          "              char *out, size_t n){\n"
          '  int wn = snprintf(out, n, "{\\"device_id\\":\\"%s\\",\\"messages\\":[%.*s]}",\n'
          "                    device_id, (int)body_len, body);\n"
          "  if (wn <= 0 || wn >= (int)n) return -8;\n"
          "  return wn;}\n"
    )
    so = d / "w.so"
    r = subprocess.run([cc, "-O2", "-fPIC", "-shared", "-Wall", "-Werror",
                        str(d / "w.c"), "-o", str(so)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lib = ctypes.CDLL(str(so))
    for name in ("hb_json", "hb_null_json", "clip_event_json", "det_event_json", "ts_json",
                 "hb_max_json"):
        getattr(lib, name).argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        getattr(lib, name).restype = ctypes.c_int
    lib.wrap_body.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t,
                              ctypes.c_char_p, ctypes.c_size_t]
    lib.wrap_body.restype = ctypes.c_int
    return lib


def _call(lib, name, size=1024):
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
        "time": {
            "valid": True,
            "state": "LOCKED",
            "sync_sigma_ns": 41000,
            "anchor_age_us": 250000,
            "boot_epoch_us": 1789244239000000,
            "boot_id": "0011223344556677",
            "discontinuity_flags": 0,
        },
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
    assert got["time"] == {
        "valid": False,
        "state": "FAULT",
        "sync_sigma_ns": None,
        "anchor_age_us": None,
        "boot_epoch_us": None,
        "boot_id": "8899aabbccddeeff",
        "discontinuity_flags": 1,
    }
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
    assert len(raw) < 640
    assert got["time"]["state"] == "HOLDOVER"
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
    assert got["time"]["state"] == "FAULT"
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


def test_heartbeat_backoff_uses_exponential_full_jitter_capped_at_one_minute():
    assert '#define HEAR_PUSH_RETRY_BASE_MS     1000UL' in CODE
    assert '#define HEAR_PUSH_HEARTBEAT_MAX_MS  60000UL' in CODE
    body = _fn("push_backoff_ms")
    assert "cap * 2" in body
    assert "random((long)cap + 1L)" in body
    assert "return delay_ms ? delay_ms : 1;" in body


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
    dep = next(d for d in docs if d["kind"] == "Deployment")
    ports = dep["spec"]["template"]["spec"]["containers"][0]["ports"]
    assert ports[0]["hostPort"] == 5051
    assert '"/api/hear/heartbeat"' in CODE
    assert '"/api/hear/event"' in CODE


def test_push_post_json_uses_tls_and_wraps_the_batch_envelope_for_the_ingest_api():
    body = _fn("push_post_json")
    assert "WiFiClientSecure client;" in body
    # Alert 3 remediation: the default path verifies the chain against a pinned root CA
    # rather than calling setInsecure(). setInsecure() only appears at all behind the
    # explicit, off-by-default HEAR_PUSH_TLS_INSECURE opt-in (see hear_push_ca.h).
    assert "client.setCACert(HEAR_PUSH_CA_CERT);" in body
    assert "#if HEAR_PUSH_TLS_INSECURE" in body
    assert "client.setInsecure();" in body
    assert '"{\\"device_id\\":\\"%s\\",\\"messages\\":[%.*s]}"' in body
    assert "node_id," in body
    assert "static char wrapped[HEAR_PUSH_WRAPPED_MAX];" in body
    assert "Authorization: Bearer %s" in body


# ------------------------------------------------------- envelope buffer sizing (nyquist, -8)
# Phase 0's expanded "time" object pushed a real heartbeat body to ~520 bytes; wrapping it in
# {"device_id":...,"messages":[...]} needs ~560, and the wrapper buffer was an independently
# chosen 512. snprintf caught it -- push_post_json returns -8 rather than sending a truncated
# body -- so node "nyquist" logged `push heartbeat failed (-8)` every cycle through a whole OTA
# rollout and went heartbeat-blind. The buffers are derived from each other now; these tests are
# what keeps them derived.

def _push_sizes():
    """The declared sizes of the push chain, read from the sketch rather than restated here."""
    body_max = int(re.search(r"#define HEAR_PUSH_BODY_MAX\s+(\d+)u", CODE).group(1))
    node_id = int(re.search(r"char\s+node_id\[(\d+)\]", CODE).group(1))
    envelope = len('{"device_id":"","messages":[]}') + 1 + node_id
    return body_max, node_id, body_max + envelope


def test_the_push_buffers_are_derived_from_what_they_hold_not_from_magic_numbers():
    assert ("#define HEAR_PUSH_WRAPPED_MAX       "
            "(HEAR_PUSH_BODY_MAX + HEAR_PUSH_ENVELOPE_OVERHEAD)") in CODE
    assert ("#define HEAR_PUSH_REQ_MAX           "
            "(HEAR_PUSH_WRAPPED_MAX + HEAR_PUSH_REQ_HEADER_MAX)") in CODE
    # The body buffers the encoders fill, the envelope that carries them and the request that
    # carries that are ONE number, spelled once. A literal here is the defect this stops.
    assert CODE.count("static char body[HEAR_PUSH_BODY_MAX];") == 2
    assert not re.search(r"char (body|wrapped|req)\[\d+\];", CODE)
    assert "static char req[HEAR_PUSH_REQ_MAX];" in _fn("push_post_json")


def test_the_firmware_static_asserts_the_envelope_can_hold_a_full_size_body():
    body = _fn("push_post_json")
    assert "static_assert(sizeof wrapped >= HEAR_PUSH_BODY_MAX +" in body
    assert "static_assert(sizeof req > HEAR_PUSH_WRAPPED_MAX," in body


def test_a_maximally_populated_phase0_heartbeat_wraps_without_truncation(hb):
    body_max, node_id_size, wrapped_max = _push_sizes()
    body = _call(hb, "hb_max_json", body_max).encode()
    got = json.loads(body)
    assert got["time"]["state"] == "HOLDOVER"
    assert got["time"]["discontinuity_flags"] == 4294967295
    assert got["time"]["boot_id"] == "ffffffffffffffff"
    assert len(body) < body_max, "the encoder itself must fit the body buffer"

    device_id = b"n" * (node_id_size - 1)
    out = ctypes.create_string_buffer(wrapped_max)
    wn = hb.wrap_body(device_id, body, len(body), out, len(out))
    assert wn > 0, "the derived envelope buffer must not truncate a worst-case heartbeat"
    assert json.loads(out.value)["messages"][0] == got
    # the buffer the fleet was actually running is what this heartbeat could not fit into
    assert hb.wrap_body(device_id, body, len(body), out, 512) == -8


def test_the_envelope_buffer_bounds_the_worst_case_the_encoders_can_emit_with_margin():
    body_max, node_id_size, wrapped_max = _push_sizes()
    worst = body_max - 1 + len('{"device_id":"","messages":[]}') + (node_id_size - 1)
    assert wrapped_max > worst, (
        "a full %d-byte body wraps to %d bytes into a %d-byte buffer"
        % (body_max, worst, wrapped_max))
    # The 512 the fleet shipped could not hold even the body it was handed, let alone the wrapper.
    assert 512 < worst


def test_truncation_still_fails_the_push_instead_of_sending_half_a_message():
    body = _fn("push_post_json")
    assert "if (wn <= 0 || wn >= (int)sizeof wrapped) {" in body
    assert "*code_out = -8;" in body
    assert "if (n <= 0 || n >= (int)sizeof req) {" in body
    assert "*code_out = -3;" in body

# ------------------------------------------------- push buffers off the stack (nyquist, panic)
# Sizing the envelope chain correctly put body(768) + wrapped(823) + req(1103) live at the same
# time in the push call chain -- ~2.7 kB -- in the same frame where push_post_json() stands up a
# WiFiClientSecure whose mbedtls handshake wants several kB of the 8 kB Arduino loop task stack.
# nyquist panicked ~20-25 s after boot on real hardware, twice; the boot guard saw reset=panic
# before HEAR_BOOT_HEALTHY_MS and reverted the slot both times. `static` keeps the sizes above and
# takes the bytes out of the frame. A stack-depth test is not possible on the host, so what is
# checkable is the storage class itself, plus the single-threadedness that makes it safe.

PUSH_BUFFERS = (
    ("push_post_json", "wrapped", "HEAR_PUSH_WRAPPED_MAX"),
    ("push_post_json", "req", "HEAR_PUSH_REQ_MAX"),
    ("push_send_heartbeat", "body", "HEAR_PUSH_BODY_MAX"),
    ("push_send_event", "body", "HEAR_PUSH_BODY_MAX"),
)


@pytest.mark.parametrize("fn,buf,size", PUSH_BUFFERS)
def test_the_push_buffers_have_static_storage_not_a_stack_frame(fn, buf, size):
    body = _fn(fn)
    assert re.search(r"\bstatic\s+char\s+%s\[%s\]\s*;" % (buf, size), body), (
        "%s() must declare %s[] static: on the loop task stack it is what panicked nyquist" % (fn, buf))
    assert not re.search(r"(?<!static )\bchar\s+%s\[" % buf, body), (
        "%s[] must not also exist as a stack local in %s()" % (buf, fn))


def test_the_push_frame_no_longer_carries_the_overflowing_buffer_total():
    # The math the panic came from: the three buffers are live simultaneously, because
    # push_send_heartbeat()/push_send_event() hold body while push_post_json() builds wrapped
    # and req from it. None of those bytes may be frame bytes any more.
    body_max, _node_id, wrapped_max = _push_sizes()
    # body -> wrapped -> req, so the request buffer is strictly the largest of the three and the
    # live total is well past the point where it matters on an 8 kB loop task that also runs a
    # TLS handshake in the same frame.
    live_total = body_max + wrapped_max + (wrapped_max + 160)
    assert live_total > 2048, "the simultaneous total is what overflowed the frame"
    for fn, buf, size in PUSH_BUFFERS:
        assert "static char %s[%s];" % (buf, size) in _fn(fn)


def test_the_push_path_is_single_threaded_so_static_buffers_cannot_be_reentered():
    # `static` is only safe because nothing else can be inside this call chain at the same time.
    # push_pump() is called from loop() and nowhere else; no xTaskCreate anywhere in the sketch
    # puts a second task on it; and the one ISR in the sketch (pps_isr) touches no push state.
    assert CODE.count("push_pump();") == 1
    assert "xTaskCreate" not in CODE
    for caller in ("pps_isr",):
        isr = _fn(caller)
        assert "push_" not in isr
    # push_pump() dispatches at most one of the three senders per call, so body[] is never held
    # by two frames at once either.
    pump = _fn("push_pump")
    assert pump.count("return;") >= 2
    assert "push_send_heartbeat();" in pump


def test_the_loop_task_gets_headroom_on_top_of_the_static_buffers_not_instead_of_them():
    # A bigger stack alone would only move the cliff: the buffers must be off the frame FIRST,
    # and this is the margin for whatever grows next (mbedtls, another header, a longer body).
    m = re.search(r"SET_LOOP_TASK_STACK_SIZE\((\d+)\s*\*\s*1024\)", CODE)
    assert m, "the loop task stack size is left at the core's 8 kB default"
    assert int(m.group(1)) >= 12
    for fn, buf, size in PUSH_BUFFERS:
        assert "static char %s[%s];" % (buf, size) in _fn(fn)


def test_the_sketch_records_why_the_push_buffers_are_static():
    assert "static`, NOT A STACK LOCAL" in INO.read_text()


def test_the_ca_header_pins_a_root_not_a_leaf_and_defaults_to_verified_tls():
    hdr = (ROOT / "firmware" / "hear_node" / "hear_push_ca.h").read_text()
    assert "BEGIN CERTIFICATE" in hdr and "END CERTIFICATE" in hdr
    assert "#ifndef HEAR_PUSH_CA_CERT" in hdr, (
        "a build must be able to override the pinned CA in secrets.h for a non-default "
        "HEAR_PUSH_HOST")
    assert "#ifndef HEAR_PUSH_TLS_INSECURE" in hdr
    assert "#define HEAR_PUSH_TLS_INSECURE 0" in hdr, (
        "the insecure escape hatch must default OFF; only secrets.h may turn it on")
    assert '#include "hear_push_ca.h"' in CODE
    assert CODE.index('#include "hear_push_ca.h"') > CODE.index('#include "secrets.h"'), (
        "hear_push_ca.h must be included after secrets.h so a build can override "
        "HEAR_PUSH_CA_CERT / HEAR_PUSH_TLS_INSECURE there")

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
