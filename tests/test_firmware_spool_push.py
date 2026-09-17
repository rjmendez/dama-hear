"""Host-compiled proof for hear_node's spool record and batch helpers."""
from __future__ import annotations

import ctypes
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HDR = ROOT / "firmware" / "hear_node" / "spool_push.h"
INO = ROOT / "firmware" / "hear_node" / "hear_node.ino"
BUILD = ROOT / ".otabuild" / "host_tests" / "spool_push"


@pytest.fixture(scope="module")
def spool_push():
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no cc on PATH: spool_push.h could not be compiled")
    BUILD.mkdir(parents=True, exist_ok=True)
    src = BUILD / "w.c"
    so = BUILD / "w.so"
    src.write_text(
        '#include "%s"\n' % HDR
        + "int w_detection_record(uint32_t seq, uint32_t event_seq, uint32_t batch_rows,"
          "                       uint8_t *record_out, size_t record_cap,"
          "                       char *json_out, size_t json_cap){"
          " hear_push_event_t ev = {"
          " .device_id=\"nyquist\", .node_class=\"xiao-s3-pps\", .fw_version=\"test-fw\","
          " .uptime_s=12345, .event_type=\"detection_batch_ready\", .event_seq=event_seq,"
          " .time_valid=1, .utc_us=1789256584000000LL, .clock_state=\"LOCKED\","
          " .sync_sigma_ns=41000, .anchor_age_us=250000, .boot_epoch_us=1789244239000000LL,"
          " .boot_id=\"0011223344556677\", .discontinuity_flags=0,"
          " .clip_basename=\"\", .dets_rows_written=event_seq, .batch_rows=batch_rows};"
          " return hear_spool_event_record_encode(&ev, seq, json_out, json_cap,"
          "                                       record_out, record_cap);}\n"
          "int w_parse(const uint8_t *p, size_t n, uint32_t *o){hear_spool_record_view_t r;"
          " memset(&r, 0, sizeof r); int rc = hear_spool_record_parse(p, n, &r);"
          " o[0]=r.fmt; o[1]=r.flags; o[2]=r.seq; o[3]=r.len; o[4]=r.crc32; return rc;}\n"
          "int w_batch(const uint8_t *records, size_t n, uint32_t batch_seq, uint32_t backlog,"
          "            char *body_out, size_t body_cap, uint32_t *seqs_out, size_t seqs_cap,"
          "            uint32_t *meta, char *batch_id, char *key){"
          " hear_spool_batch_plan_t plan; memset(&plan, 0, sizeof plan);"
          " int wrote = hear_spool_build_batch_from_records("
          "   records, n, \"nyquist\", \"0011223344556677\", 1789244239000000LL,"
          "   \"\\\"2026-09-16T12:00:00Z\\\"\", batch_seq, backlog, body_out, body_cap,"
          "   seqs_out, seqs_cap, &plan);"
          " if(wrote > 0){meta[0]=plan.first_seq; meta[1]=plan.last_seq; meta[2]=plan.item_count;"
          " meta[3]=plan.backlog_records; meta[4]=plan.body_crc32;"
          " memcpy(batch_id, plan.batch_id, sizeof plan.batch_id);"
          " memcpy(key, plan.idempotency_key, sizeof plan.idempotency_key);}"
          " return wrote;}\n"
          "int w_wm_encode(uint32_t gen, uint32_t acked, uint32_t oldest,"
          "                uint8_t *out, size_t cap){hear_spool_watermark_t w;"
          " memset(&w, 0, sizeof w); memcpy(w.boot_id, \"0011223344556677\", 17);"
          " w.gen=gen; w.acked_seq=acked; w.oldest_seq=oldest;"
          " return hear_spool_watermark_encode(&w, out, cap);}\n"
          "int w_plan_receipt(const uint8_t *a, size_t an, const uint8_t *b, size_t bn,"
          "                   const char *batch_id, const uint8_t *receipt, size_t rn,"
          "                   const uint32_t *seqs, size_t seq_count, uint8_t *planned,"
          "                   size_t planned_cap, uint32_t *meta){"
          " hear_spool_batch_plan_t plan; hear_spool_watermark_t next; int target=-1;"
          " memset(&plan, 0, sizeof plan); memset(&next, 0, sizeof next);"
          " int ok = hear_spool_plan_watermark_advance("
          "   a, an, b, bn, \"0011223344556677\", batch_id, receipt, rn, seqs, seq_count,"
          "   planned, planned_cap, &target, &plan);"
          " if(ok){hear_spool_watermark_decode(planned, planned_cap, &next);"
          " meta[0]=(uint32_t)plan.ack_through_index; meta[1]=plan.acked_seq;"
          " meta[2]=plan.oldest_seq; meta[3]=(uint32_t)plan.retry_after_s;"
          " meta[4]=(uint32_t)plan.retry_after_is_null; meta[5]=plan.refused_in_prefix;"
          " meta[6]=(uint32_t)target; meta[7]=next.gen; meta[8]=next.acked_seq;"
          " meta[9]=next.oldest_seq;} return ok;}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [cc, "-O2", "-fPIC", "-shared", "-Wall", "-Werror", str(src), "-o", str(so)],
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    lib = ctypes.CDLL(str(so))
    lib.w_detection_record.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    lib.w_detection_record.restype = ctypes.c_int
    lib.w_parse.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                            ctypes.POINTER(ctypes.c_uint32)]
    lib.w_parse.restype = ctypes.c_int
    lib.w_batch.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_uint32,
                            ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t,
                            ctypes.POINTER(ctypes.c_uint32), ctypes.c_size_t,
                            ctypes.POINTER(ctypes.c_uint32), ctypes.c_char_p, ctypes.c_char_p]
    lib.w_batch.restype = ctypes.c_int
    lib.w_wm_encode.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
    lib.w_wm_encode.restype = ctypes.c_int
    lib.w_plan_receipt.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                                   ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint8),
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint32),
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint8),
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint32)]
    lib.w_plan_receipt.restype = ctypes.c_int
    return lib


def _u8(buf: bytes):
    return (ctypes.c_uint8 * len(buf)).from_buffer_copy(buf)


def _detection_record(spool_push, seq: int, event_seq: int, batch_rows: int):
    record = (ctypes.c_uint8 * 2048)()
    payload = ctypes.create_string_buffer(1024)
    wrote = spool_push.w_detection_record(seq, event_seq, batch_rows, record, len(record),
                                          payload, len(payload))
    assert wrote > 16
    return bytes(record[:wrote]), payload.value.decode("utf-8")


def test_spool_scan_reopens_directory_entries_under_the_spool_dir():
    src = INO.read_text(encoding="utf-8")
    assert 'snprintf(seg.path, sizeof seg.path, HEAR_SPOOL_DIR "/%s", name);' in src
    assert 'snprintf(seg.path, sizeof seg.path, "%s", ent.name());' not in src


def _watermark(spool_push, gen: int, acked: int, oldest: int):
    out = (ctypes.c_uint8 * 33)()
    wrote = spool_push.w_wm_encode(gen, acked, oldest, out, len(out))
    assert wrote == len(out)
    return bytes(out)


def test_detection_capture_writes_a_spool_record(spool_push):
    record, payload = _detection_record(spool_push, seq=41, event_seq=812, batch_rows=16)
    meta = (ctypes.c_uint32 * 5)()
    assert spool_push.w_parse(_u8(record), len(record), meta) == 0
    assert tuple(meta[:4]) == (1, 1, 41, len(payload))
    got = json.loads(payload)
    assert got["telemetry_path"] == "hear/event"
    assert got["event_type"] == "detection_batch_ready"
    assert got["event"] == {"dets_rows_written": 812, "batch_rows": 16}


def test_batch_assembly_uses_the_unacked_range_in_order(spool_push):
    records = b"".join([
        _detection_record(spool_push, seq=11, event_seq=101, batch_rows=3)[0],
        _detection_record(spool_push, seq=12, event_seq=102, batch_rows=4)[0],
        _detection_record(spool_push, seq=13, event_seq=103, batch_rows=5)[0],
    ])
    body = ctypes.create_string_buffer(8192)
    seqs = (ctypes.c_uint32 * 8)()
    meta = (ctypes.c_uint32 * 5)()
    batch_id = ctypes.create_string_buffer(96)
    idem = ctypes.create_string_buffer(128)
    wrote = spool_push.w_batch(_u8(records), len(records), 7, 3, body, len(body), seqs, 8,
                               meta, batch_id, idem)
    assert wrote > 0
    assert tuple(meta[:4]) == (11, 13, 3, 3)
    assert tuple(seqs[:3]) == (11, 12, 13)
    assert batch_id.value == b"nyquist-0011223344556677-7"
    assert idem.value.startswith(b"nyquist.0011223344556677.11.3.")
    frame = json.loads(body.value.decode("utf-8"))
    assert frame["batch_schema_version"] == 1
    assert frame["batch_id"] == "nyquist-0011223344556677-7"
    assert frame["producer"]["spool_backlog"] == 3
    assert [msg["event"]["batch_rows"] for msg in frame["messages"]] == [3, 4, 5]


def test_watermark_advances_only_for_a_valid_matching_receipt(spool_push):
    slot_a = _watermark(spool_push, gen=7, acked=40, oldest=41)
    slot_b = bytes(33)
    seqs = (ctypes.c_uint32 * 3)(41, 42, 43)
    receipt = (
        b'{"batch_schema_version":1,"batch_id":"nyquist-0011223344556677-7",'
        b'"received_at":"2026-09-16T12:00:05Z","ack_through_index":1,'
        b'"counts":{"submitted":3,"accepted":1,"duplicate":1,"refused":0,"deferred":1},'
        b'"results":[{"index":0,"status":"accepted"},{"index":1,"status":"duplicate"},'
        b'{"index":2,"status":"deferred"}],"retry_after_s":null,'
        b'"server":{"adapter":"ingest-batch","version":"0.1.0","envelope_major":1}}'
    )
    planned = (ctypes.c_uint8 * 33)()
    meta = (ctypes.c_uint32 * 10)()
    ok = spool_push.w_plan_receipt(_u8(slot_a), len(slot_a), _u8(slot_b), len(slot_b),
                                   b"nyquist-0011223344556677-7", _u8(receipt), len(receipt),
                                   seqs, 3, planned, len(planned), meta)
    assert ok == 1
    assert tuple(meta) == (1, 42, 43, 0, 1, 0, 1, 8, 42, 43)


def test_malformed_or_mismatched_receipts_fail_closed(spool_push):
    slot_a = _watermark(spool_push, gen=7, acked=40, oldest=41)
    slot_b = bytes(33)
    seqs = (ctypes.c_uint32 * 3)(41, 42, 43)
    planned = (ctypes.c_uint8 * 33)()
    meta = (ctypes.c_uint32 * 10)()
    truncated = (
        b'{"batch_schema_version":1,"batch_id":"nyquist-0011223344556677-7",'
        b'"ack_through_index":1,"retry_after_s":0,"results":[{"index":0,"status":"accepted"}'
    )
    assert spool_push.w_plan_receipt(_u8(slot_a), len(slot_a), _u8(slot_b), len(slot_b),
                                     b"nyquist-0011223344556677-7", _u8(truncated),
                                     len(truncated), seqs, 3, planned, len(planned), meta) == 0
    wrong_batch = (
        b'{"batch_schema_version":1,"batch_id":"nyquist-0011223344556677-999",'
        b'"received_at":"2026-09-16T12:00:05Z","ack_through_index":2,'
        b'"counts":{"submitted":3,"accepted":3,"duplicate":0,"refused":0,"deferred":0},'
        b'"results":[{"index":0,"status":"accepted"},{"index":1,"status":"accepted"},'
        b'{"index":2,"status":"accepted"}],"retry_after_s":null,'
        b'"server":{"adapter":"ingest-batch","version":"0.1.0","envelope_major":1}}'
    )
    assert spool_push.w_plan_receipt(_u8(slot_a), len(slot_a), _u8(slot_b), len(slot_b),
                                     b"nyquist-0011223344556677-7", _u8(wrong_batch),
                                     len(wrong_batch), seqs, 3, planned, len(planned), meta) == 0
