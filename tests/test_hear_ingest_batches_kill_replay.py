"""End-to-end replay safety for batch ingest when the receipt is lost after durable write.

This complements the direct adapter crash-window coverage in
`tests/test_hear_heartbeat_receiver.py` by exercising the HTTP route the device actually
talks to: the batch is durably written, the receipt is durably stored, the server dies
before the client receives the reply, and the restarted server must replay the original
success receipt without duplicating storage.

For the device-side half, this test reuses the same host-compiled `spool_protocol.h`
receipt/watermark helpers that PR #280 introduced and `tests/test_firmware_spool_protocol.py`
already covers more exhaustively.
"""
from __future__ import annotations

import ctypes
from http.client import RemoteDisconnected
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hear.ingest import batch as BA  # noqa: E402
from tools import gen_ingest_contracts as GEN  # noqa: E402
from tools import hear_heartbeat_receiver as HR  # noqa: E402

HDR = ROOT / "firmware" / "hear_node" / "spool_protocol.h"
BUILD = ROOT / ".otabuild" / "host_tests" / "ingest_batches_kill_replay"
BOOT_ID = b"0011223344556677"


class _NoopScript:
    def __call__(self, *args, **kwargs):
        return 0


class _NoopPipeline:
    def setex(self, *args, **kwargs):
        return self

    def sadd(self, *args, **kwargs):
        return self

    def set(self, *args, **kwargs):
        return self

    def execute(self):
        return []


class _NoopRedis:
    def register_script(self, script: str) -> _NoopScript:
        return _NoopScript()

    def pipeline(self, transaction: bool = False) -> _NoopPipeline:
        assert transaction is False
        return _NoopPipeline()

    def sadd(self, *args, **kwargs):
        return 0

    def set(self, *args, **kwargs):
        return True


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
        + "int w_receipt(const uint8_t *p, size_t n, int *o){hear_spool_receipt_scan_t r;"
          " int ok = hear_spool_scan_receipt(p, n, &r);"
          " if(ok){o[0]=r.valid; o[1]=r.ack_through_index; o[2]=r.retry_after_s;"
          " o[3]=r.retry_after_is_null; o[4]=(int)r.refused_in_prefix; o[5]=(int)r.results_seen;}"
          " return ok;}\n"
          "int w_wm_encode(const char *boot, uint32_t gen, uint32_t acked, uint32_t oldest,"
          "                uint8_t *out, size_t cap){hear_spool_watermark_t w;"
          " memset(&w, 0, sizeof w); memcpy(w.boot_id, boot, 17); w.gen=gen; w.acked_seq=acked;"
          " w.oldest_seq=oldest; return hear_spool_watermark_encode(&w, out, cap);}\n"
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
          " &slot); if(ok) target[0]=(uint32_t)slot; return ok;}\n",
        encoding="utf-8",
    )
    built = subprocess.run(
        [cc, "-O2", "-fPIC", "-shared", "-Wall", "-Werror", str(src), "-o", str(so)],
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    lib = ctypes.CDLL(str(so))
    lib.w_receipt.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                              ctypes.POINTER(ctypes.c_int)]
    lib.w_receipt.restype = ctypes.c_int
    lib.w_wm_encode.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8),
                                ctypes.c_size_t]
    lib.w_wm_encode.restype = ctypes.c_int
    lib.w_wm_choose.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                                ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                                ctypes.POINTER(ctypes.c_uint32), ctypes.c_char_p]
    lib.w_wm_choose.restype = ctypes.c_int
    lib.w_wm_plan.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
                              ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_char_p,
                              ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8),
                              ctypes.c_size_t, ctypes.POINTER(ctypes.c_uint32)]
    lib.w_wm_plan.restype = ctypes.c_int
    return lib


def _u8(buf: bytes):
    return (ctypes.c_uint8 * len(buf)).from_buffer_copy(buf)


def _watermark(spool, gen: int, acked: int, oldest: int, boot: bytes = BOOT_ID) -> bytes:
    out = (ctypes.c_uint8 * 33)()
    wrote = spool.w_wm_encode(boot, gen, acked, oldest, out, len(out))
    assert wrote == len(out)
    return bytes(out)


def _choose_watermark(spool, slot_a: bytes, slot_b: bytes):
    out = (ctypes.c_uint32 * 4)()
    boot = ctypes.create_string_buffer(17)
    ok = spool.w_wm_choose(_u8(slot_a), len(slot_a), _u8(slot_b), len(slot_b), out, boot)
    return ok, boot.value, tuple(out)


def _scan_receipt(spool, body: bytes):
    out = (ctypes.c_int * 6)()
    ok = spool.w_receipt(_u8(body), len(body), out)
    return ok, tuple(out)


def _acked_and_oldest_after_receipt(current_acked: int, seqs: list[int], receipt_scan: tuple[int, ...]):
    ack_index = receipt_scan[1]
    if ack_index < 0:
        return current_acked, seqs[0]
    acked_seq = seqs[ack_index]
    oldest_seq = seqs[ack_index + 1] if ack_index + 1 < len(seqs) else seqs[-1] + 1
    return acked_seq, oldest_seq


def _apply_receipt_if_valid(spool, slot_a: bytes, slot_b: bytes, receipt_body: bytes,
                            seqs: list[int]):
    ok, _boot, current = _choose_watermark(spool, slot_a, slot_b)
    assert ok == 1
    current_acked = int(current[1])
    scan_ok, receipt = _scan_receipt(spool, receipt_body)
    if scan_ok != 1 or receipt[0] != 1:
        return slot_a, slot_b, False
    acked_seq, oldest_seq = _acked_and_oldest_after_receipt(current_acked, seqs, receipt)
    if acked_seq == current_acked:
        return slot_a, slot_b, False
    planned = (ctypes.c_uint8 * 33)()
    target = (ctypes.c_uint32 * 1)()
    assert spool.w_wm_plan(_u8(slot_a), len(slot_a), _u8(slot_b), len(slot_b), BOOT_ID,
                           acked_seq, oldest_seq, planned, len(planned), target) == 1
    if target[0] == 0:
        return bytes(planned), slot_b, True
    return slot_a, bytes(planned), True


def _remaining_spooled(seqs: list[int], acked_seq: int) -> list[int]:
    return [seq for seq in seqs if seq > acked_seq]


def _batch_credentials(tmp_path: Path) -> Path:
    path = tmp_path / "batch-credentials.json"
    path.write_text(json.dumps({
        "credentials": [{
            "principal_id": "node:nyquist",
            "site_id": GEN.BASE["site_id"],
            "scope": "device",
            "device_id": GEN.DEVICE_ID,
            "permissions": ["ingest:write"],
            "key_id": "k-2026-09",
            "token_sha256": HR.hashlib.sha256(b"node-secret").hexdigest(),
        }]
    }), encoding="utf-8")
    return path


def _batch_adapter(tmp_path: Path, durable_store: HR.DurableRecordStore) -> HR.BatchIngestAdapter:
    return HR.BatchIngestAdapter.from_config(
        durable_store,
        str(_batch_credentials(tmp_path)),
        raw_root=str(tmp_path / "batch-raw"),
        adapter_version="test",
    )


@contextmanager
def _running_batch_server(tmp_path: Path, *, durable_store: HR.DurableRecordStore,
                          before_batch_response=None):
    store = HR.HeartbeatReceiverStore(
        _NoopRedis(),
        heartbeat_ttl_s=30,
        redis_target="fake:6379",
        durable_store=durable_store,
    )
    server = HR.create_server(
        "127.0.0.1",
        0,
        store,
        max_body_bytes=IB_MAX_BATCH_BYTES,
        socket_timeout_s=0.2,
        batch_adapter=_batch_adapter(tmp_path, durable_store),
        before_batch_response=before_batch_response,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


IB_MAX_BATCH_BYTES = BA.MAX_BATCH_BYTES


def _post_batch(base_url: str, frame: dict, *, idempotency_key: str = "idem-kill-replay",
                request_id: str = "req-kill-replay"):
    req = urllib.request.Request(
        base_url + "/v1/ingest/batches",
        data=json.dumps(frame).encode("utf-8"),
        headers={
            "Content-Type": BA.BATCH_CODEC_MEDIA_TYPES["json"],
            "Authorization": "Bearer node-secret",
            "Idempotency-Key": idempotency_key,
            "Accept": BA.RECEIPT_MEDIA_TYPE,
            "X-Request-ID": request_id,
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=2)


def _stored_receipt_row(db: Path):
    with sqlite3.connect(db) as con:
        return con.execute(
            "SELECT request_fingerprint, response_status, response_body, request_id "
            "FROM batch_receipts"
        ).fetchone()


def test_kill_between_durable_write_and_receipt_replays_original_success_without_duplication(
    tmp_path, spool
):
    db = tmp_path / "heartbeats.sqlite3"
    frame = GEN._batch_valid()
    durable = HR.make_durable_store("sqlite", str(db))
    fault_once = {"armed": True}

    def kill_before_delivery(status: int, body: str, headers: dict[str, str], request_id: str):
        assert status == 200
        assert headers["Content-Type"] == BA.RECEIPT_MEDIA_TYPE
        assert request_id == "req-kill-replay"
        if fault_once["armed"]:
            fault_once["armed"] = False
            raise RuntimeError("simulated kill after receipt persistence before delivery")

    with _running_batch_server(tmp_path, durable_store=durable,
                               before_batch_response=kill_before_delivery) as base_url:
        with pytest.raises((RemoteDisconnected, ConnectionResetError,
                            socket.timeout, urllib.error.URLError)):
            with _post_batch(base_url, frame):
                pass

    stored = _stored_receipt_row(db)
    assert stored is not None
    assert stored[1] == 200
    stored_receipt = json.loads(stored[2])
    assert stored_receipt["counts"] == {
        "submitted": 3,
        "accepted": 3,
        "duplicate": 0,
        "refused": 0,
        "deferred": 0,
    }
    assert stored_receipt["ack_through_index"] == 2

    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM batch_events").fetchone()[0] == 3
        assert con.execute("SELECT COUNT(*) FROM batch_receipts").fetchone()[0] == 1

    with _running_batch_server(
        tmp_path,
        durable_store=HR.make_durable_store("sqlite", str(db)),
    ) as restarted:
        with _post_batch(restarted, frame) as resp:
            replay_body = resp.read().decode("utf-8")
            replay_status = resp.status

    assert replay_status == 200
    assert json.loads(replay_body) == stored_receipt
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM batch_events").fetchone()[0] == 3

    scan_ok, receipt = _scan_receipt(spool, replay_body.encode("utf-8"))
    assert scan_ok == 1
    assert receipt == (1, 2, 0, 1, 0, 3)

    batch_seqs = [41, 42, 43]
    slot_a = _watermark(spool, gen=7, acked=40, oldest=41)
    slot_b = bytes(33)
    slot_a, slot_b, advanced = _apply_receipt_if_valid(
        spool, slot_a, slot_b, replay_body.encode("utf-8"), batch_seqs
    )
    assert advanced is True
    ok, boot, chosen = _choose_watermark(spool, slot_a, slot_b)
    assert ok == 1
    assert boot == BOOT_ID
    assert chosen == (8, 43, 44, 1)
    assert _remaining_spooled(batch_seqs, acked_seq=43) == []


def test_truncated_receipt_fails_closed_and_does_not_advance_watermark(tmp_path, spool):
    db = tmp_path / "heartbeats.sqlite3"
    frame = GEN._batch_valid()
    with _running_batch_server(
        tmp_path,
        durable_store=HR.make_durable_store("sqlite", str(db)),
    ) as base_url:
        with _post_batch(base_url, frame, idempotency_key="idem-good",
                         request_id="req-good") as resp:
            receipt_body = resp.read()

    truncated = receipt_body[:receipt_body.index(b'],\"retry_after_s\"')]
    scan_ok, _receipt = _scan_receipt(spool, truncated)
    assert scan_ok == 0

    batch_seqs = [41, 42, 43]
    slot_a = _watermark(spool, gen=7, acked=40, oldest=41)
    slot_b = bytes(33)
    next_a, next_b, advanced = _apply_receipt_if_valid(spool, slot_a, slot_b, truncated,
                                                       batch_seqs)
    assert advanced is False
    ok, boot, chosen = _choose_watermark(spool, next_a, next_b)
    assert ok == 1
    assert boot == BOOT_ID
    assert chosen == (7, 40, 41, 0)
    assert _remaining_spooled(batch_seqs, acked_seq=40) == [41, 42, 43]
