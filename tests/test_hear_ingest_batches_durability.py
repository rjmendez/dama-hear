"""Adversarial durability tests for `POST /v1/ingest/batches`.

Existing receiver coverage already proves one crash window: if the process dies after event rows
commit but before the receipt itself is stored, a retry must re-walk the durable rows and report
duplicates instead of losing data. These tests go narrower and meaner:

* prove the HTTP receipt is not written until after the durable receipt transaction returns,
* crash after that commit but before the response reaches the caller and require replay of the
  original receipt,
* fail the event-store commit itself and require no success receipt plus no torn SQLite rows, and
* hard-kill a separate process mid-transaction and require previously committed batches to survive
  while the uncommitted write stays invisible after restart.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.ingest import batch as BA  # noqa: E402
from tests.test_hear_heartbeat_receiver import FakeRedis  # noqa: E402
from tools import gen_ingest_contracts as GEN  # noqa: E402
from tools import hear_heartbeat_receiver as HR  # noqa: E402


@contextmanager
def running_server(*, durable_store: HR.DurableRecordStore,
                   batch_adapter: HR.BatchIngestAdapter):
    fake = FakeRedis()
    store = HR.HeartbeatReceiverStore(
        fake,
        heartbeat_ttl_s=30,
        redis_target="fake:6379",
        durable_store=durable_store,
    )
    server = HR.create_server(
        "127.0.0.1",
        0,
        store,
        max_body_bytes=2048,
        socket_timeout_s=0.2,
        batch_adapter=batch_adapter,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _batch_credentials(tmp_path: Path) -> Path:
    path = tmp_path / "batch-creds.json"
    doc = {
        "credentials": [
            {
                "principal_id": "node:nyquist",
                "site_id": GEN.BASE["site_id"],
                "scope": "device",
                "device_id": GEN.DEVICE_ID,
                "permissions": ["ingest:write"],
                "key_id": "k-2026-09",
                "token_sha256": hashlib.sha256(b"node-secret").hexdigest(),
            },
        ],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _batch_adapter(tmp_path: Path, durable_store: HR.DurableRecordStore) -> HR.BatchIngestAdapter:
    return HR.BatchIngestAdapter.from_config(
        durable_store,
        str(_batch_credentials(tmp_path)),
        raw_root=str(tmp_path / "batch-raw"),
        adapter_version="test",
    )


def _post(url: str, frame: dict[str, Any], *, idempotency_key: str = "idem-1",
          token: str = "node-secret"):
    req = urllib.request.Request(
        url,
        data=json.dumps(frame).encode("utf-8"),
        headers={
            "Content-Type": BA.BATCH_CODEC_MEDIA_TYPES["json"],
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": idempotency_key,
            "Accept": BA.RECEIPT_MEDIA_TYPE,
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=2)


def _row_counts(db: Path) -> Dict[str, int]:
    with sqlite3.connect(db) as con:
        return {
            "events": con.execute("SELECT COUNT(*) FROM batch_events").fetchone()[0],
            "outbox": con.execute("SELECT COUNT(*) FROM batch_outbox").fetchone()[0],
            "receipts": con.execute("SELECT COUNT(*) FROM batch_receipts").fetchone()[0],
        }


class _ProxyConnection:
    def __init__(self, inner: sqlite3.Connection, phase: dict[str, str | None],
                 order: list[str], fail_on: dict[str, Any] | None = None):
        self._inner = inner
        self._phase = phase
        self._order = order
        self._fail_on = fail_on or {}

    def execute(self, sql: str, params: Any = ()) -> Any:
        normalized = sql.strip().upper()
        if normalized == "COMMIT" and self._phase.get("name") is not None:
            self._order.append(str(self._phase["name"]))
            if self._fail_on.get("phase") == self._phase["name"] and self._fail_on.get("armed"):
                self._fail_on["armed"] = False
                raise sqlite3.OperationalError(f"simulated {self._phase['name']} failure")
        return self._inner.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _receipt_scope(idempotency_key: str = "idem-1") -> str:
    return BA.idempotency_scope(
        site_id=GEN.BASE["site_id"],
        principal="node:nyquist",
        route="POST /v1/ingest/batches",
        key=idempotency_key,
    )


def test_batch_http_receipt_waits_for_durable_receipt_commit(monkeypatch, tmp_path):
    db = tmp_path / "heartbeats.sqlite3"
    store = HR.make_durable_store("sqlite", str(db))
    adapter = _batch_adapter(tmp_path, store)
    order: list[str] = []
    phase = {"name": None}

    original_connect = store._connect

    def wrapped_connect(*args, **kwargs):
        return _ProxyConnection(original_connect(*args, **kwargs), phase, order)

    original_batch_store_receipt = store.batch_store_receipt

    def wrapped_batch_store_receipt(**kwargs):
        phase["name"] = "receipt_commit"
        try:
            return original_batch_store_receipt(**kwargs)
        finally:
            phase["name"] = None

    original_fsync = HR.os.fsync

    def wrapped_fsync(fd: int) -> None:
        order.append("raw_fsync")
        original_fsync(fd)

    monkeypatch.setattr(store, "_connect", wrapped_connect)
    monkeypatch.setattr(store, "batch_store_receipt", wrapped_batch_store_receipt)
    monkeypatch.setattr(HR.os, "fsync", wrapped_fsync)

    with running_server(durable_store=store, batch_adapter=adapter) as (base_url, server):
        original_send_response = server.RequestHandlerClass.send_response

        def wrapped_send_response(handler, code, message=None):
            order.append("send_response")
            return original_send_response(handler, code, message)

        monkeypatch.setattr(server.RequestHandlerClass, "send_response", wrapped_send_response)
        with _post(base_url + "/v1/ingest/batches", GEN._batch_valid()) as resp:
            receipt = json.loads(resp.read().decode("utf-8"))

    assert receipt["counts"]["accepted"] == 3
    assert order.count("raw_fsync") >= 4
    assert "receipt_commit" in order
    assert order.index("receipt_commit") < order.index("send_response")
    assert max(i for i, marker in enumerate(order) if marker == "raw_fsync") < order.index("send_response")
    assert _row_counts(db) == {"events": 3, "outbox": 3, "receipts": 1}


def test_retry_replays_original_receipt_after_response_write_crash(monkeypatch, tmp_path):
    db = tmp_path / "heartbeats.sqlite3"
    store = HR.make_durable_store("sqlite", str(db))
    adapter = _batch_adapter(tmp_path, store)
    frame = GEN._batch_valid()

    with running_server(durable_store=store, batch_adapter=adapter) as (base_url, server):
        original_send_response = server.RequestHandlerClass.send_response
        crash_once = {"armed": True}

        def broken_send_response(handler, code, message=None):
            result = original_send_response(handler, code, message)
            if crash_once["armed"]:
                crash_once["armed"] = False
                raise ConnectionResetError("simulated crash after durable receipt commit")
            return result

        monkeypatch.setattr(server.RequestHandlerClass, "send_response", broken_send_response)

        with pytest.raises((http.client.RemoteDisconnected,
                            urllib.error.URLError,
                            ConnectionResetError,
                            ConnectionAbortedError)):
            with _post(base_url + "/v1/ingest/batches", frame):
                pass

        assert _row_counts(db) == {"events": 3, "outbox": 3, "receipts": 1}

        with _post(base_url + "/v1/ingest/batches", frame) as resp:
            assert resp.status == 200
            replayed = json.loads(resp.read().decode("utf-8"))

    assert replayed["counts"]["accepted"] == 3
    assert replayed["counts"]["duplicate"] == 0
    assert replayed["ack_through_index"] == 2
    assert _row_counts(db) == {"events": 3, "outbox": 3, "receipts": 1}


def test_failed_event_commit_returns_no_receipt_and_leaves_no_torn_rows(monkeypatch, tmp_path):
    db = tmp_path / "heartbeats.sqlite3"
    store = HR.make_durable_store("sqlite", str(db))
    adapter = _batch_adapter(tmp_path, store)
    phase = {"name": None}
    order: list[str] = []
    fail_on = {"phase": "event_commit", "armed": True}

    original_connect = store._connect

    def wrapped_connect(*args, **kwargs):
        return _ProxyConnection(original_connect(*args, **kwargs), phase, order, fail_on)

    original_batch_persist_event = store.batch_persist_event

    def wrapped_batch_persist_event(*args, **kwargs):
        phase["name"] = "event_commit"
        try:
            return original_batch_persist_event(*args, **kwargs)
        finally:
            phase["name"] = None

    monkeypatch.setattr(store, "_connect", wrapped_connect)
    monkeypatch.setattr(store, "batch_persist_event", wrapped_batch_persist_event)

    with running_server(durable_store=store, batch_adapter=adapter) as (base_url, _server):
        with pytest.raises(urllib.error.HTTPError) as ei:
            with _post(base_url + "/v1/ingest/batches", GEN._batch_valid()):
                pass
        assert ei.value.code == 503
        body = json.loads(ei.value.read().decode("utf-8"))
        assert body["code"] == "durable_store_unavailable"
        assert body["retryable"] is True
        assert _row_counts(db) == {"events": 0, "outbox": 0, "receipts": 0}

    healthy = _batch_adapter(tmp_path, HR.make_durable_store("sqlite", str(db)))
    with running_server(durable_store=healthy.durable_store, batch_adapter=healthy) as (base_url, _server):
        with _post(base_url + "/v1/ingest/batches", GEN._batch_valid()) as resp:
            recovered = json.loads(resp.read().decode("utf-8"))

    assert recovered["counts"]["accepted"] == 3
    assert recovered["counts"]["duplicate"] == 0
    assert recovered["ack_through_index"] == 2
    assert _row_counts(db) == {"events": 3, "outbox": 3, "receipts": 1}
    assert order.count("event_commit") == 1


def test_hard_kill_mid_transaction_keeps_committed_batches_and_hides_inflight_rows(tmp_path):
    db = tmp_path / "heartbeats.sqlite3"
    store = HR.make_durable_store("sqlite", str(db))
    adapter = _batch_adapter(tmp_path, store)
    frame = GEN._batch_valid()
    raw = json.dumps(frame).encode("utf-8")

    status, body, _headers = adapter.ingest(
        path="/v1/ingest/batches",
        raw=raw,
        content_type=BA.BATCH_CODEC_MEDIA_TYPES["json"],
        idempotency_key="idem-1",
        authorization="Bearer node-secret",
        content_encoding=None,
        request_id="req-committed",
    )
    assert status == 200
    assert json.loads(body)["counts"]["accepted"] == 3

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sqlite3, sys\n"
                "db = sys.argv[1]\n"
                "con = sqlite3.connect(db, isolation_level=None)\n"
                "con.execute('PRAGMA journal_mode=WAL')\n"
                "con.execute('PRAGMA synchronous=FULL')\n"
                "con.execute('BEGIN IMMEDIATE')\n"
                "con.execute(\"INSERT INTO batch_events "
                "(event_id, site_id, device_id, source, kind, observed_at, received_at, "
                "dispatchable, raw_ref, envelope_json, batch_id, item_index, principal_id, "
                "credential_scope, key_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\","
                " ('ghost-event', 'site-quarry-north', 'nyquist', 'batch-http', 'heartbeat', "
                "None, '2026-09-16T00:00:00Z', 1, None, '{}', 'ghost-batch', 0, 'node:nyquist', "
                "'device', 'k-2026-09'))\n"
                "con.execute(\"INSERT INTO batch_outbox (event_id, state, created_at) "
                "VALUES (?, ?, ?)\", ('ghost-event', 'pending', '2026-09-16T00:00:00Z'))\n"
                "os._exit(9)\n"
            ),
            str(db),
        ],
        check=False,
    )

    assert proc.returncode == 9

    recovered = HR.make_durable_store("sqlite", str(db))
    receipt = recovered.batch_lookup_receipt(_receipt_scope("idem-1"))
    assert receipt is not None
    with sqlite3.connect(db) as con:
        event_ids = [row[0] for row in con.execute(
            "SELECT event_id FROM batch_events ORDER BY item_index"
        )]
        outbox_ids = [row[0] for row in con.execute(
            "SELECT event_id FROM batch_outbox ORDER BY event_id"
        )]
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        ghost_event = con.execute(
            "SELECT COUNT(*) FROM batch_events WHERE event_id = 'ghost-event'"
        ).fetchone()[0]
        ghost_outbox = con.execute(
            "SELECT COUNT(*) FROM batch_outbox WHERE event_id = 'ghost-event'"
        ).fetchone()[0]

    assert integrity == "ok"
    assert event_ids == [row["event_id"] for row in frame["messages"]]
    assert sorted(outbox_ids) == sorted(event_ids)
    assert ghost_event == 0
    assert ghost_outbox == 0
