"""Host-compiled proof for spool framing, recovery and bounded receipt scanning."""
import ctypes
import shutil
import subprocess
from pathlib import Path

import pytest
import zlib

ROOT = Path(__file__).resolve().parents[1]
HDR = ROOT / "firmware" / "hear_node" / "spool_protocol.h"
BUILD = ROOT / ".otabuild" / "host_tests" / "spool_protocol"


@pytest.fixture(scope="module")
def spool():
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no cc on PATH: spool_protocol.h could not be compiled")
    BUILD.mkdir(parents=True, exist_ok=True)
    src = BUILD / "w.c"
    so = BUILD / "w.so"
    src.write_text(
        '#include "%s"\n' % HDR
        + "uint32_t w_crc(const uint8_t *p, size_t n){return hear_spool_crc32(p, n);}\n"
          "int w_encode(uint32_t seq, uint8_t flags, const uint8_t *p, uint32_t n,"
          "             uint8_t *out, size_t cap){return hear_spool_record_encode(seq, flags, p,"
          "             n, out, cap);}\n"
          "int w_parse(const uint8_t *p, size_t n, uint32_t *o){hear_spool_record_view_t r;"
          " memset(&r, 0, sizeof r); int rc = hear_spool_record_parse(p, n, &r);"
          " o[0]=r.fmt; o[1]=r.flags; o[2]=r.seq; o[3]=r.len; o[4]=r.crc32; return rc;}\n"
          "int w_scan(const uint8_t *p, size_t n, uint64_t *o){hear_spool_scan_result_t r;"
          " if(!hear_spool_scan_segment(p, n, &r)) return 0;"
          " o[0]=r.records; o[1]=r.crc_drops; o[2]=r.torn_tail; o[3]=r.last_good_seq;"
          " o[4]=(uint64_t)r.last_good_end; o[5]=(uint64_t)r.truncate_offset; o[6]=r.status;"
          " return 1;}\n"
          "int w_wm_encode(const char *boot, uint32_t gen, uint32_t acked, uint32_t oldest,"
          "                uint8_t *out, size_t cap){hear_spool_watermark_t w;"
          " memset(&w, 0, sizeof w); memcpy(w.boot_id, boot, 17); w.gen=gen; w.acked_seq=acked;"
          " w.oldest_seq=oldest; return hear_spool_watermark_encode(&w, out, cap);}\n"
          "int w_wm_decode(const uint8_t *p, size_t n, uint32_t *o, char *boot){"
          " hear_spool_watermark_t w; memset(&w, 0, sizeof w);"
          " int ok = hear_spool_watermark_decode(p, n, &w);"
          " if(ok){memcpy(boot, w.boot_id, 17); o[0]=w.gen; o[1]=w.acked_seq; o[2]=w.oldest_seq;}"
          " return ok;}\n"
          "int w_wm_choose(const uint8_t *a, size_t an, const uint8_t *b, size_t bn,"
          "                uint32_t *o, char *boot){hear_spool_watermark_t w; int slot=-1;"
          " memset(&w, 0, sizeof w);"
          " int ok = hear_spool_watermark_choose(a, an, b, bn, &w, &slot);"
          " if(ok){memcpy(boot, w.boot_id, 17); o[0]=w.gen; o[1]=w.acked_seq; o[2]=w.oldest_seq;"
          " o[3]=(uint32_t)slot;} return ok;}\n"
          "int w_wm_plan(const uint8_t *a, size_t an, const uint8_t *b, size_t bn,"
          "              const char *boot, uint32_t acked, uint32_t oldest, uint8_t *out,"
          "              size_t cap, uint32_t *target){int slot=-1;"
          " int ok = hear_spool_watermark_plan_write(a, an, b, bn, boot, acked, oldest, out, cap,"
          " &slot); if(ok) target[0]=(uint32_t)slot; return ok;}\n"
          "int w_receipt(const uint8_t *p, size_t n, int *o){hear_spool_receipt_scan_t r;"
          " int ok = hear_spool_scan_receipt(p, n, &r);"
          " if(ok){o[0]=r.valid; o[1]=r.ack_through_index; o[2]=r.retry_after_s;"
          " o[3]=r.retry_after_is_null; o[4]=(int)r.refused_in_prefix; o[5]=(int)r.results_seen;}"
          " return ok;}\n",
        encoding="utf-8",
    )
    r = subprocess.run([cc, "-O2", "-fPIC", "-shared", "-Wall", "-Werror", str(src), "-o", str(so)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lib = ctypes.CDLL(str(so))
    lib.w_crc.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
    lib.w_crc.restype = ctypes.c_uint32
    lib.w_encode.argtypes = [ctypes.c_uint32, ctypes.c_uint8, ctypes.POINTER(ctypes.c_uint8),
                             ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
    lib.w_encode.restype = ctypes.c_int
    lib.w_parse.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                            ctypes.POINTER(ctypes.c_uint32)]
    lib.w_parse.restype = ctypes.c_int
    lib.w_scan.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                           ctypes.POINTER(ctypes.c_uint64)]
    lib.w_scan.restype = ctypes.c_int
    lib.w_wm_encode.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
    lib.w_wm_encode.restype = ctypes.c_int
    lib.w_wm_decode.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                                ctypes.POINTER(ctypes.c_uint32), ctypes.c_char_p]
    lib.w_wm_decode.restype = ctypes.c_int
    lib.w_wm_choose.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                                ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                                ctypes.POINTER(ctypes.c_uint32), ctypes.c_char_p]
    lib.w_wm_choose.restype = ctypes.c_int
    lib.w_wm_plan.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                              ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_char_p,
                              ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8),
                              ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint32)]
    lib.w_wm_plan.restype = ctypes.c_int
    lib.w_receipt.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                              ctypes.POINTER(ctypes.c_int)]
    lib.w_receipt.restype = ctypes.c_int
    return lib


def _u8(buf: bytes):
    return (ctypes.c_uint8 * len(buf)).from_buffer_copy(buf)


def _record(spool, seq: int, payload: bytes, flags: int = 0) -> bytes:
    out = (ctypes.c_uint8 * (16 + len(payload)))()
    wrote = spool.w_encode(seq, flags, _u8(payload), len(payload), out, len(out))
    assert wrote == len(out)
    return bytes(out)


def _scan(spool, data: bytes):
    out = (ctypes.c_uint64 * 7)()
    assert spool.w_scan(_u8(data), len(data), out) == 1
    return tuple(out)


def _watermark(spool, gen: int, acked: int, oldest: int,
               boot: bytes = b"0011223344556677") -> bytes:
    out = (ctypes.c_uint8 * 33)()
    wrote = spool.w_wm_encode(boot, gen, acked, oldest, out, len(out))
    assert wrote == len(out)
    return bytes(out)


def _receipt(spool, body: bytes):
    out = (ctypes.c_int * 6)()
    ok = spool.w_receipt(_u8(body), len(body), out)
    return ok, tuple(out)


def test_record_frame_round_trips_with_crc(spool):
    payload = (b'{"schema_version":1,"device_id":"nyquist","kind":"detection","producer":'
               b'{"boot_id":"0011223344556677","sequence":19},"payload":{"x":1}}')
    assert spool.w_crc(_u8(payload), len(payload)) == zlib.crc32(payload) & 0xffffffff
    frame = _record(spool, 19, payload, flags=1)
    meta = (ctypes.c_uint32 * 5)()
    assert spool.w_parse(_u8(frame), len(frame), meta) == 0
    assert tuple(meta) == (1, 1, 19, len(payload), zlib.crc32(payload) & 0xffffffff)
    assert frame[:2] == b"HS"
    assert frame[16:] == payload


def test_record_frame_rejects_corrupt_crc(spool):
    frame = bytearray(_record(spool, 7, b'{"payload":"ok"}'))
    frame[-1] ^= 0x01
    meta = (ctypes.c_uint32 * 5)()
    assert spool.w_parse(_u8(bytes(frame)), len(frame), meta) == 5


def test_torn_tail_scanner_truncates_partial_append(spool):
    first = _record(spool, 100, b'{"n":1}')
    second = _record(spool, 101, b'{"n":2}')
    torn = _record(spool, 102, b'{"n":3,"tail":"partial"}')[:11]
    got = _scan(spool, first + second + torn)
    assert got == (
        2,  # records
        0,  # crc drops
        1,  # torn tail
        101,  # last good seq
        len(first) + len(second),  # last good end
        len(first) + len(second),  # truncate offset
        1,  # torn tail status
    )


def test_middle_crc_failure_is_skipped_when_a_later_frame_is_valid(spool):
    first = _record(spool, 10, b'{"n":1}')
    middle = bytearray(_record(spool, 11, b'{"n":2,"bad":true}'))
    last = _record(spool, 12, b'{"n":3}')
    middle[-2] ^= 0x80
    got = _scan(spool, first + bytes(middle) + last)
    assert got == (
        2,
        1,
        0,
        12,
        len(first) + len(middle) + len(last),
        len(first) + len(middle) + len(last),
        0,
    )


def test_watermark_round_trips_and_prefers_highest_valid_generation(spool):
    older = _watermark(spool, gen=3, acked=40, oldest=20)
    newer = _watermark(spool, gen=4, acked=55, oldest=21)
    vals = (ctypes.c_uint32 * 4)()
    boot = ctypes.create_string_buffer(17)
    assert spool.w_wm_choose(_u8(older), len(older), _u8(newer), len(newer), vals, boot) == 1
    assert boot.value == b"0011223344556677"
    assert tuple(vals) == (4, 55, 21, 1)


def test_watermark_write_survives_kill_mid_write(spool):
    slot_a = _watermark(spool, gen=7, acked=100, oldest=90)
    empty = bytes(33)
    planned = (ctypes.c_uint8 * 33)()
    target = (ctypes.c_uint32 * 1)()
    assert spool.w_wm_plan(_u8(slot_a), len(slot_a), _u8(empty), len(empty),
                           b"0011223344556677", 111, 91, planned, len(planned), target) == 1
    assert target[0] == 1
    torn_b = bytearray(empty)
    torn_b[:9] = bytes(planned)[:9]
    vals = (ctypes.c_uint32 * 4)()
    boot = ctypes.create_string_buffer(17)
    assert spool.w_wm_choose(_u8(slot_a), len(slot_a), _u8(bytes(torn_b)), len(torn_b), vals,
                             boot) == 1
    assert tuple(vals) == (7, 100, 90, 0)
    assert spool.w_wm_choose(_u8(slot_a), len(slot_a), planned, len(planned), vals, boot) == 1
    assert tuple(vals) == (8, 111, 91, 1)


def test_watermark_read_reports_loss_when_both_slots_are_invalid(spool):
    vals = (ctypes.c_uint32 * 4)()
    boot = ctypes.create_string_buffer(17)
    assert spool.w_wm_choose(_u8(bytes(33)), 33, _u8(bytes(33)), 33, vals, boot) == 0


def test_receipt_scanner_extracts_ack_retry_and_refused_prefix_count(spool):
    body = b"""{
      "batch_schema_version": 1,
      "ack_through_index": 2,
      "retry_after_s": null,
      "results": [
        {"index": 2, "status": "accepted", "raw_ref": "raw/2"},
        {"index": 0, "status": "duplicate", "raw_ref": "raw/0"},
        {"index": 1, "status": "refused", "reasons": ["field_missing"], "raw_ref": "raw/1"}
      ]
    }"""
    ok, vals = _receipt(spool, body)
    assert ok == 1
    assert vals == (1, 2, 0, 1, 1, 3)


def test_receipt_scanner_accepts_integer_retry_after(spool):
    ok, vals = _receipt(
        spool,
        b'{"ack_through_index":-1,"retry_after_s":17,"results":[]}',
    )
    assert ok == 1
    assert vals == (1, -1, 17, 0, 0, 0)


def test_receipt_scanner_fails_closed_on_truncation_and_bad_ack(spool):
    truncated = (b'{"ack_through_index":1,"retry_after_s":0,"results":[{"index":0,'
                 b'"status":"accepted"}')
    assert _receipt(spool, truncated)[0] == 0
    bad_ack = (b'{"ack_through_index":2,"retry_after_s":1,"results":['
               b'{"index":0,"status":"accepted"},{"index":1,"status":"accepted"}]}')
    assert _receipt(spool, bad_ack)[0] == 0
