"""The hear heartbeat receiver: schema guard, TTL write, and advisory event intake."""
import hashlib
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Dict

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear.ingest import batch as BA  # noqa: E402
from tools import hear_heartbeat_receiver as HR  # noqa: E402
from tools import gen_ingest_contracts as GEN  # noqa: E402


def redis_cluster_key_slot(key: str) -> int:
    """Mirrors Redis Cluster's real key-slot hashing (CRC16/XMODEM over the {tag} substring if
    present, else the whole key) so tests can assert that a script's declared KEYS are actually
    cluster-safe (all map to one slot) rather than merely "happen to work" against a single node.
    """
    start = key.find("{")
    if start != -1:
        end = key.find("}", start + 1)
        if end != -1 and end != start + 1:
            key = key[start + 1:end]
    crc = 0
    for byte in key.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc % 16384


class _FakeScript:
    """Stands in for the redis-py Script object returned by ``client.register_script``."""

    def __init__(self, client: "FakeRedis"):
        self._client = client

    def __call__(self, keys=None, args=None):
        return self._client._eval_event_dedupe_write(keys or [], args or [])


class _FakePipeline:
    """Stands in for ``client.pipeline(transaction=False)``: queues single-key commands and
    applies them all on ``execute()``. Used only by the heartbeat path, which never needs
    cross-key atomicity (a retried overwrite is a no-op in effect)."""

    def __init__(self, client: "FakeRedis"):
        self._client = client
        self._ops: list = []

    def setex(self, key, ttl, value):
        self._ops.append(("setex", key, ttl, value))
        return self

    def sadd(self, key, *members):
        self._ops.append(("sadd", key, members))
        return self

    def set(self, key, value):
        self._ops.append(("set", key, value))
        return self

    def execute(self):
        self._client.execute_calls += 1
        ops, self._ops = self._ops, []
        if self._client.failures_before_success > 0:
            self._client.failures_before_success -= 1
            raise RuntimeError("simulated redis outage")
        for kind, *rest in ops:
            if kind == "setex":
                key, ttl, value = rest
                self._client.values[key] = value
                self._client.ttls[key] = ttl
            elif kind == "sadd":
                key, members = rest
                self._client.sets.setdefault(key, set()).update(members)
            elif kind == "set":
                key, value = rest
                self._client.values[key] = value
        return []


class FakeRedis:
    """A minimal in-memory double for the two Redis surfaces RedisHeartbeatCache uses:

    * a plain non-transactional pipeline for heartbeats (``pipeline`` / ``sadd``), and
    * a single atomic event-dedupe-and-write Lua script (``register_script``), keyed only by the
      declared KEYS the real script takes -- no key is ever built by string concatenation here,
      mirroring the production script's cluster-safety contract.

    ``failures_before_success`` simulates a clean outage: the write never reaches Redis (or Redis
    rejects it outright), so nothing is mutated before the exception is raised.
    ``ambiguous_failures_before_success`` simulates the crash/ambiguous-ACK window this receiver
    must tolerate: Redis fully applies the script (including the dedupe marker) but the caller
    never learns of the success (e.g. the connection drops before the reply arrives), so the
    caller retries. Because the dedupe marker is already recorded, the retry must no-op instead
    of duplicating the stream/event write. This mode only applies to the event path -- heartbeat
    writes have no dedupe marker to lose track of.
    """

    def __init__(self, failures_before_success: int = 0,
                ambiguous_failures_before_success: int = 0):
        self.values = {}
        self.ttls = {}
        self.sets = {}
        self.streams = {}
        self.failures_before_success = failures_before_success
        self.ambiguous_failures_before_success = ambiguous_failures_before_success
        self.execute_calls = 0
        # dedupe_key -> {record_uid: seq}; seq_key -> int. Keyed by the *declared* KEYS values
        # themselves (not a single flat structure) so a test can assert isolation between an
        # event stream's dedupe metadata and anything else.
        self.dedupe: Dict[str, Dict[str, int]] = {}
        self._seq_counters: Dict[str, int] = {}

    def register_script(self, script: str) -> _FakeScript:
        return _FakeScript(self)

    def pipeline(self, transaction=False):
        assert transaction is False
        return _FakePipeline(self)

    def sadd(self, key, *members):
        # Direct (non-pipelined) call used by the event write path; idempotent, so no queuing
        # or atomicity is needed.
        self.sets.setdefault(key, set()).update(members)

    def set(self, key, value):
        # Direct (non-pipelined) call used by the event write path's per-device "last event"
        # cache; idempotent, so it stays outside the atomic dedupe script.
        self.values[key] = value

    def _eval_event_dedupe_write(self, keys, args):
        self.execute_calls += 1
        if self.failures_before_success > 0:
            self.failures_before_success -= 1
            raise RuntimeError("simulated redis outage")

        dedupe_key, seq_key, stream_key = keys
        record_uid, node_id, body_json, maxlen = args
        maxlen = int(maxlen)

        dedupe_zset = self.dedupe.setdefault(dedupe_key, {})
        if record_uid in dedupe_zset:
            return 0

        stream = self.streams.setdefault(stream_key, [])
        if any(entry.get("device_id") == node_id and entry.get("payload") == body_json
               for entry in stream):
            seq = self._seq_counters.get(seq_key, 0) + 1
            self._seq_counters[seq_key] = seq
            dedupe_zset[record_uid] = seq
            if len(dedupe_zset) > maxlen:
                stale = sorted(dedupe_zset.items(), key=lambda kv: kv[1])[
                    :len(dedupe_zset) - maxlen]
                for stale_uid, _ in stale:
                    del dedupe_zset[stale_uid]
            return 0

        stream.append({"device_id": node_id, "payload": body_json})
        if len(stream) > maxlen:  # exact MAXLEN trim, no '~' slack.
            del stream[:-maxlen]

        seq = self._seq_counters.get(seq_key, 0) + 1
        self._seq_counters[seq_key] = seq
        dedupe_zset[record_uid] = seq
        if len(dedupe_zset) > maxlen:
            stale = sorted(dedupe_zset.items(), key=lambda kv: kv[1])[:len(dedupe_zset) - maxlen]
            for stale_uid, _ in stale:
                del dedupe_zset[stale_uid]

        if self.ambiguous_failures_before_success > 0:
            self.ambiguous_failures_before_success -= 1
            raise RuntimeError("simulated ambiguous redis ack loss")
        return 1


class TestValidation:
    @staticmethod
    def _heartbeat(**overrides):
        payload = {
            "telemetry_path": "hear/heartbeat",
            "telemetry_schema_version": 1,
            "device_id": "nyquist",
            "ts": "2026-09-12T23:43:00Z",
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
        payload.update(overrides)
        return payload

    @staticmethod
    def _event(**overrides):
        payload = {
            "telemetry_path": "hear/event",
            "telemetry_schema_version": 1,
            "device_id": "nyquist",
            "ts": "2026-09-12T23:43:04Z",
            "class": "xiao-s3-pps",
            "fw_version": "7f84d29",
            "uptime_s": 12349,
            "time": {"valid": True},
            "event_type": "clip_written",
            "event_seq": 234,
            "event": {
                "clip_basename": "nyquist-db21acd5-1082530195.wav",
                "clips_written": 234,
                "clips_evicted": 41,
            },
        }
        payload.update(overrides)
        return payload

    def test_valid_heartbeat_accepts_the_design_doc_shape(self):
        got = HR.validate_heartbeat_payload(self._heartbeat())
        assert got["device_id"] == "nyquist"
        assert got["counters"]["clips_written"] == 233

    def test_missing_node_utc_is_allowed_when_time_is_invalid(self):
        got = HR.validate_heartbeat_payload(self._heartbeat(ts=None, time={"valid": False}))
        assert got["ts"] is None
        assert got["time"]["valid"] is False

    def test_explicit_clock_state_metadata_is_accepted(self):
        got = HR.validate_heartbeat_payload(self._heartbeat(time={
            "valid": True,
            "state": "LOCKED",
            "sync_sigma_ns": 41000,
            "anchor_age_us": 250000,
            "boot_epoch_us": 1789244239000000,
            "boot_id": "0011223344556677",
            "discontinuity_flags": 0,
        }))
        assert got["time"]["state"] == "LOCKED"
        assert got["time"]["boot_id"] == "0011223344556677"

    def test_true_time_requires_ts(self):
        with pytest.raises(HR.RequestError, match="ts is required"):
            HR.validate_heartbeat_payload(self._heartbeat(ts=None))

    def test_false_time_rejects_a_non_null_ts(self):
        with pytest.raises(HR.RequestError, match="ts must be null"):
            HR.validate_heartbeat_payload(self._heartbeat(time={"valid": False}))

    def test_wrong_schema_version_is_rejected(self):
        with pytest.raises(HR.RequestError, match="telemetry_schema_version"):
            HR.validate_heartbeat_payload(self._heartbeat(telemetry_schema_version=2))

    def test_heartbeat_requires_all_counters(self):
        bad = self._heartbeat(counters={"scene_rows_written": 1})
        with pytest.raises(HR.RequestError, match="dets_rows_written"):
            HR.validate_heartbeat_payload(bad)

    def test_event_requires_a_nested_event_object(self):
        with pytest.raises(HR.RequestError, match="event must be an object"):
            HR.validate_event_payload(self._event(event="clip_written"))

    def test_event_payload_does_not_require_a_gps_block(self):
        got = HR.validate_event_payload(self._event())
        assert got["event_type"] == "clip_written"

    def test_unknown_event_type_is_rejected(self):
        with pytest.raises(HR.RequestError, match="event_type"):
            HR.validate_event_payload(self._event(event_type="surprise"))

    def test_fault_state_requires_time_invalid(self):
        with pytest.raises(HR.RequestError, match="FAULT"):
            HR.validate_heartbeat_payload(self._heartbeat(time={
                "valid": True,
                "state": "FAULT",
                "sync_sigma_ns": 1,
                "anchor_age_us": 2,
                "boot_epoch_us": 3,
                "boot_id": "0011223344556677",
            }))

    def test_invalid_time_requires_fault_when_state_is_stated(self):
        with pytest.raises(HR.RequestError, match="time.valid false requires time.state FAULT"):
            HR.validate_event_payload(self._event(ts=None, time={
                "valid": False,
                "state": "HOLDOVER",
                "boot_id": "0011223344556677",
            }))


@contextmanager
def running_server(auth_token=None, socket_timeout_s=0.2, durable_store=None, fake=None,
                   batch_adapter=None,
                   durable_replay_interval_s=0.0, durable_replay_limit=HR.DURABLE_REPLAY_LIMIT,
                   durable_prune_interval_s=0.0, durable_retention_days=HR.DURABLE_RETENTION_DAYS):
    fake = fake if fake is not None else FakeRedis()
    store = HR.HeartbeatReceiverStore(
        fake,
        heartbeat_ttl_s=30,
        redis_target="fake:6379",
        durable_store=durable_store,
    )
    server = HR.create_server(
        "127.0.0.1", 0, store, max_body_bytes=2048,
        auth_token=auth_token, socket_timeout_s=socket_timeout_s,
        batch_adapter=batch_adapter,
        durable_replay_interval_s=durable_replay_interval_s,
        durable_replay_limit=durable_replay_limit,
        durable_prune_interval_s=durable_prune_interval_s,
        durable_retention_days=durable_retention_days,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", fake, (host, port), server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class TestHttpIntegration(TestValidation):
    @staticmethod
    def _post(url, payload, headers=None):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=2)

    def test_posting_a_heartbeat_sets_the_ttl_key(self):
        with running_server() as (base_url, fake, _addr, _server):
            with self._post(base_url + "/api/hear/heartbeat", self._heartbeat()) as resp:
                assert resp.status == 204
                assert resp.read() == b""
        body = json.loads(fake.values["dama:hear:nyquist"])
        assert fake.ttls["dama:hear:nyquist"] == 30
        assert body["device_id"] == "nyquist"
        assert body["received_at"].endswith("Z")
        assert fake.sets["dama:hear:devices"] == {"nyquist"}
        assert json.loads(fake.values["dama:hear:latest"])["device_id"] == "nyquist"

    def test_posting_an_event_updates_the_advisory_stream(self):
        with running_server() as (base_url, fake, _addr, _server):
            with self._post(base_url + "/api/hear/event", self._event()) as resp:
                assert resp.status == 204
        latest = json.loads(fake.values["dama:hear:event:nyquist"])
        assert latest["event_type"] == "clip_written"
        stream = fake.streams[HR.EVENT_STREAM_KEY]
        assert len(stream) == 1
        assert stream[0]["device_id"] == "nyquist"
        assert json.loads(stream[0]["payload"])["event_seq"] == 234

    def test_auth_token_is_required_when_configured(self):
        with running_server(auth_token="secret") as (base_url, fake, _addr, _server):
            with pytest.raises(urllib.error.HTTPError) as ei:
                self._post(base_url + "/api/hear/heartbeat", self._heartbeat())
            assert ei.value.code == 401
            assert fake.values == {}
            with self._post(base_url + "/api/hear/heartbeat", self._heartbeat(),
                            headers={"X-Hear-Token": "secret"}) as resp:
                assert resp.status == 204

    def test_a_bad_payload_is_rejected_and_does_not_touch_redis(self):
        with running_server() as (base_url, fake, _addr, _server):
            with pytest.raises(urllib.error.HTTPError) as ei:
                self._post(base_url + "/api/hear/heartbeat", self._heartbeat(device_id="bad/id"))
            assert ei.value.code == 400
            assert fake.values == {}
            assert fake.ttls == {}
            assert fake.sets == {}

    def test_healthz_reports_the_service_without_hitting_redis(self):
        with running_server() as (base_url, _fake, _addr, _server):
            with urllib.request.urlopen(base_url + "/healthz", timeout=2) as resp:
                assert resp.status == 200
                body = json.loads(resp.read().decode("utf-8"))
        assert body["service"] == "hear-heartbeat-receiver"
        assert body["redis_target"] == "fake:6379"
        assert body["durable_store"]["backend"] == "none"

    def test_metrics_exposes_the_checked_in_ingest_metric_families(self):
        with running_server() as (base_url, _fake, _addr, _server):
            with urllib.request.urlopen(base_url + "/metrics", timeout=2) as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"].startswith("text/plain; version=0.0.4")
                body = resp.read().decode("utf-8")
        assert "# HELP ingest_producer_spool_backlog " in body
        assert "# TYPE ingest_ack_gap_items histogram" in body
        assert "# TYPE ingest_idempotency_conflicts_total counter" in body

    def test_slow_client_times_out_instead_of_holding_a_thread_forever(self):
        with running_server(socket_timeout_s=0.2) as (_base_url, _fake, addr, server):
            assert server.daemon_threads is True
            with socket.create_connection(addr, timeout=1) as sock:
                sock.sendall(
                    b"POST /api/hear/heartbeat HTTP/1.1\r\n"
                    b"Host: test\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 2\r\n\r\n"
                )
                sock.settimeout(1)
                chunks = []
                while True:
                    try:
                        part = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not part:
                        break
                    chunks.append(part)
                got = b"".join(chunks)
        assert b"408" in got
        assert b"request body read timed out" in got

    def test_parse_port_handles_integers_strings_and_k8s_service_urls(self):
        from tools.hear_heartbeat_receiver import _parse_port
        assert _parse_port(None, 5051) == 5051
        assert _parse_port("", 5051) == 5051
        assert _parse_port("5051") == 5051
        assert _parse_port("8080") == 8080
        assert _parse_port("tcp://10.43.154.155:5051") == 5051
        assert _parse_port("invalid", 5051) == 5051


def _batch_credentials(tmp_path):
    path = tmp_path / "batch-credentials.json"
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
            {
                "principal_id": "svc:hear-drain-shadow",
                "site_id": GEN.BASE["site_id"],
                "scope": "site",
                "permissions": ["ingest:write"],
                "key_id": "k-2026-09",
                "token_sha256": hashlib.sha256(b"site-secret").hexdigest(),
            },
        ]
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _batch_adapter(tmp_path, durable_store, before_receipt_store=None):
    return HR.BatchIngestAdapter.from_config(
        durable_store,
        str(_batch_credentials(tmp_path)),
        raw_root=str(tmp_path / "batch-raw"),
        adapter_version="test",
    ) if before_receipt_store is None else HR.BatchIngestAdapter(
        durable_store,
        HR.BatchCredentialStore.from_file(str(_batch_credentials(tmp_path))),
        raw_root=str(tmp_path / "batch-raw"),
        adapter_version="test",
        before_receipt_store=before_receipt_store,
    )


class TestBatchIngestRoute:
    @staticmethod
    def _post(url, frame, *, token="node-secret", headers=None):
        req = urllib.request.Request(
            url,
            data=json.dumps(frame).encode("utf-8"),
            headers={
                "Content-Type": BA.BATCH_CODEC_MEDIA_TYPES["json"],
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "idem-1",
                "Accept": BA.RECEIPT_MEDIA_TYPE,
                **(headers or {}),
            },
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=2)

    def test_batch_route_requires_bearer_auth(self, tmp_path):
        durable = HR.make_durable_store("sqlite", str(tmp_path / "heartbeats.sqlite3"))
        adapter = _batch_adapter(tmp_path, durable)
        with running_server(durable_store=durable, batch_adapter=adapter) as (
            base_url, _fake, _addr, _server,
        ):
            req = urllib.request.Request(
                base_url + "/v1/ingest/batches",
                data=json.dumps(GEN._batch_valid()).encode("utf-8"),
                headers={
                    "Content-Type": BA.BATCH_CODEC_MEDIA_TYPES["json"],
                    "Idempotency-Key": "idem-1",
                },
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(req, timeout=2)
            assert ei.value.code == 401
            body = json.loads(ei.value.read().decode("utf-8"))
            assert body["code"] == "credential_missing"

    def test_batch_route_enforces_limits_and_media_type(self, tmp_path):
        durable = HR.make_durable_store("sqlite", str(tmp_path / "heartbeats.sqlite3"))
        adapter = _batch_adapter(tmp_path, durable)
        with running_server(durable_store=durable, batch_adapter=adapter) as (
            base_url, _fake, _addr, _server,
        ):
            with pytest.raises(urllib.error.HTTPError) as ei:
                self._post(base_url + "/v1/ingest/batches", GEN._batch_valid(),
                           headers={"Content-Type": "application/cbor"})
            assert ei.value.code == 415
            too_many = GEN._batch_too_many_items()
            with pytest.raises(urllib.error.HTTPError) as ei2:
                self._post(base_url + "/v1/ingest/batches", too_many,
                           headers={"Content-Type": "application/json"})
            assert ei2.value.code == 422
            body = json.loads(ei2.value.read().decode("utf-8"))
            assert body["code"] == "batch_too_many_items"

    def test_batch_route_replays_a_stored_receipt_for_the_same_key_and_body(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        durable = HR.make_durable_store("sqlite", str(db))
        adapter = _batch_adapter(tmp_path, durable)
        with running_server(durable_store=durable, batch_adapter=adapter) as (
            base_url, _fake, _addr, _server,
        ):
            with self._post(base_url + "/v1/ingest/batches", GEN._batch_valid()) as resp:
                first = resp.read()
            with self._post(base_url + "/v1/ingest/batches", GEN._batch_valid()) as resp:
                second = resp.read()
            assert first == second
            receipt = json.loads(first.decode("utf-8"))
            assert receipt["counts"]["accepted"] == 3
            assert receipt["counts"]["duplicate"] == 0
            changed = GEN._batch_valid()
            changed["batch_id"] = "018f2c1a-batch-9999"
            req = urllib.request.Request(
                base_url + "/v1/ingest/batches",
                data=json.dumps(changed).encode("utf-8"),
                headers={
                    "Content-Type": BA.BATCH_CODEC_MEDIA_TYPES["json"],
                    "Authorization": "Bearer node-secret",
                    "Idempotency-Key": "idem-1",
                },
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(req, timeout=2)
            assert ei.value.code == 409

    def test_batch_route_updates_spool_metrics_for_future_producers(self, tmp_path):
        durable = HR.make_durable_store("sqlite", str(tmp_path / "heartbeats.sqlite3"))
        adapter = _batch_adapter(tmp_path, durable)
        with running_server(durable_store=durable, batch_adapter=adapter) as (
            base_url, _fake, _addr, _server,
        ):
            with self._post(base_url + "/v1/ingest/batches", GEN._batch_valid()) as resp:
                assert resp.status == 200
            gapped = GEN._batch_valid()
            gapped["batch_id"] = "018f2c1a-batch-0010"
            gapped["producer"]["batch_sequence"] = 10
            with self._post(base_url + "/v1/ingest/batches", gapped,
                            headers={"Idempotency-Key": "idem-2"}) as resp:
                assert resp.status == 200
            with urllib.request.urlopen(base_url + "/metrics", timeout=2) as resp:
                body = resp.read().decode("utf-8")
        assert 'ingest_producer_spool_backlog{site="site-quarry-north",device_id="nyquist",' \
               'source="batch-http",adapter="ingest-batch"} 118' in body
        assert 'ingest_ack_gap_items_sum{site="site-quarry-north",device_id="nyquist",' \
               'source="batch-http",adapter="ingest-batch"} 0' in body
        assert 'ingest_sequence_gaps_total{site="site-quarry-north",device_id="nyquist",' \
               'source="batch-http",adapter="ingest-batch"} 2' in body

    def test_batch_route_translates_legacy_items_before_receipting(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        durable = HR.make_durable_store("sqlite", str(db))
        adapter = _batch_adapter(tmp_path, durable)
        with running_server(durable_store=durable, batch_adapter=adapter) as (
            base_url, _fake, _addr, _server,
        ):
            with self._post(base_url + "/v1/ingest/batches", GEN._batch_legacy_messages()) as resp:
                receipt = json.loads(resp.read().decode("utf-8"))
        assert receipt["ack_through_index"] == 1
        assert [row["classification"] for row in receipt["results"]] == [BA.TRANSLATED, BA.TRANSLATED]
        assert receipt["counts"]["accepted"] == 2
        with sqlite3.connect(db) as con:
            kinds = [row[0] for row in con.execute("SELECT kind FROM batch_events ORDER BY item_index")]
        assert kinds == ["heartbeat", "detection"]

    def test_batch_route_survives_a_kill_after_durable_write_before_receipt(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        durable = HR.make_durable_store("sqlite", str(db))
        crash_once = {"armed": True}

        def crash():
            if crash_once["armed"]:
                crash_once["armed"] = False
                raise RuntimeError("simulated kill between durable write and receipt")

        adapter = _batch_adapter(tmp_path, durable, before_receipt_store=crash)
        frame = GEN._batch_valid()
        raw = json.dumps(frame).encode("utf-8")
        with pytest.raises(RuntimeError, match="simulated kill"):
            adapter.ingest(path="/v1/ingest/batches", raw=raw,
                           content_type=BA.BATCH_CODEC_MEDIA_TYPES["json"],
                           idempotency_key="idem-1", authorization="Bearer node-secret",
                           content_encoding=None, request_id="req_crash")

        rows = sqlite3.connect(db).execute("SELECT COUNT(*) FROM batch_events").fetchone()[0]
        assert rows == 3
        recovered = _batch_adapter(tmp_path, HR.make_durable_store("sqlite", str(db)))
        status, body, _headers = recovered.ingest(
            path="/v1/ingest/batches", raw=raw,
            content_type=BA.BATCH_CODEC_MEDIA_TYPES["json"],
            idempotency_key="idem-1", authorization="Bearer node-secret",
            content_encoding=None, request_id="req_retry",
        )
        receipt = json.loads(body)
        assert status == 200
        assert receipt["counts"]["accepted"] == 0
        assert receipt["counts"]["duplicate"] == 3
        assert receipt["ack_through_index"] == 2


class TestDurableSqlite(TestValidation):
    @staticmethod
    def _counts(db_path):
        with sqlite3.connect(db_path) as con:
            records = con.execute("SELECT COUNT(*) FROM durable_records").fetchone()[0]
            return {
                "records": records,
                "successes": con.execute(
                    "SELECT COUNT(*) FROM cache_attempts WHERE outcome='succeeded'"
                ).fetchone()[0],
                "failures": con.execute(
                    "SELECT COUNT(*) FROM cache_attempts WHERE outcome='failed'"
                ).fetchone()[0],
                "payload": con.execute(
                    "SELECT payload_json FROM durable_records ORDER BY id LIMIT 1"
                ).fetchone()[0] if records else None,
            }

    def test_sqlite_durable_store_commits_before_cache_and_tracks_success(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        store = HR.HeartbeatReceiverStore(
            FakeRedis(),
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        record = store.write_heartbeat(self._heartbeat())
        counts = self._counts(db)
        assert record["device_id"] == "nyquist"
        assert counts["records"] == 1
        assert counts["successes"] == 1
        assert json.loads(counts["payload"])["received_at"] == record["received_at"]
        assert store.health_snapshot()["durable_store"]["pending_records"] == 0

    def test_duplicate_retry_is_idempotent_and_does_not_duplicate_stream_entries(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis()
        store = HR.HeartbeatReceiverStore(
            fake,
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        store.write_event(self._event())
        store.write_event(self._event())
        counts = self._counts(db)
        assert counts["records"] == 1
        assert counts["successes"] == 1
        assert fake.execute_calls == 1
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1

    def test_cache_failure_leaves_a_pending_record_and_retry_repairs_it(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis(failures_before_success=1)
        store = HR.HeartbeatReceiverStore(
            fake,
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        with pytest.raises(RuntimeError, match="simulated redis outage"):
            store.write_event(self._event())
        counts = self._counts(db)
        assert counts["records"] == 1
        assert counts["successes"] == 0
        assert counts["failures"] == 1
        assert fake.streams == {}

        store.write_event(self._event())
        counts = self._counts(db)
        assert counts["records"] == 1
        assert counts["successes"] == 1
        assert counts["failures"] == 1
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1

    def test_replay_pending_repairs_a_record_after_a_crash_window(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        with pytest.raises(RuntimeError, match="simulated redis outage"):
            HR.HeartbeatReceiverStore(
                FakeRedis(failures_before_success=1),
                heartbeat_ttl_s=30,
                redis_target="fake:6379",
                durable_store=HR.make_durable_store("sqlite", str(db)),
            ).write_heartbeat(self._heartbeat())

        recovered_fake = FakeRedis()
        recovered = HR.HeartbeatReceiverStore(
            recovered_fake,
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        summary = recovered.replay_pending(limit=10)
        counts = self._counts(db)
        assert summary == {
            "backend": "sqlite",
            "attempted": 1,
            "synced": 1,
            "failed": 0,
            "remaining_pending": 0,
        }
        assert counts["records"] == 1
        assert counts["successes"] == 1
        assert json.loads(recovered_fake.values["dama:hear:nyquist"])["device_id"] == "nyquist"

    def test_ambiguous_redis_ack_loss_does_not_duplicate_the_stream_on_retry(self, tmp_path):
        # Simulates the crash/ambiguous-ACK window: Redis fully applies the write (including the
        # atomic dedupe marker) but the caller never learns of the success, so the durable store
        # marks the attempt "failed" and a retry is expected. The retry must not duplicate the
        # event stream entry because Redis-side dedupe recognizes the record_uid was already
        # applied.
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis(ambiguous_failures_before_success=1)
        store = HR.HeartbeatReceiverStore(
            fake,
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        with pytest.raises(RuntimeError, match="simulated ambiguous redis ack loss"):
            store.write_event(self._event())
        counts = self._counts(db)
        assert counts["records"] == 1
        assert counts["successes"] == 0
        assert counts["failures"] == 1
        # Redis actually applied the write despite the caller seeing an exception.
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1

        store.write_event(self._event())
        counts = self._counts(db)
        assert counts["records"] == 1
        assert counts["successes"] == 1
        assert counts["failures"] == 1
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1
        assert fake.execute_calls == 2

    def test_dedupe_metadata_is_bounded_by_event_stream_maxlen(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis()
        store = HR.HeartbeatReceiverStore(
            fake,
            heartbeat_ttl_s=30,
            event_stream_maxlen=3,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        for seq in range(10):
            store.write_event(self._event(event_seq=seq, idempotency_key=f"evt-{seq}"))
        dedupe_zset = fake.dedupe[store.cache.event_dedupe_key]
        stream = fake.streams[HR.EVENT_STREAM_KEY]
        assert len(dedupe_zset) == 3
        assert len(stream) == 3
        # Exact retention alignment: the record_uids still tracked for dedupe are exactly the
        # ones whose payloads are still physically present in the (exactly, not approximately)
        # trimmed stream -- nothing lingers past what XADD actually retained, and nothing that
        # is still retained has silently lost its dedupe marker.
        retained_seqs = {json.loads(entry["payload"])["event_seq"] for entry in stream}
        assert retained_seqs == {7, 8, 9}
        assert set(dedupe_zset) == {
            HR._record_uid(self._event(event_seq=seq, idempotency_key=f"evt-{seq}"))
            for seq in retained_seqs
        }

    def test_event_dedupe_script_keys_are_fully_declared_and_share_one_cluster_slot(self):
        # Regression guard for the original bug: a key built by string concatenation inside the
        # Lua script (e.g. ``dedupe_key .. ':seq'``) is an *undeclared* key access that Redis
        # Cluster cannot route correctly, and any KEYS that don't share a hash tag will land on
        # different slots and make the script fail outright against a real cluster. Assert both
        # structurally (no concatenation operator building a key name in the script body) and
        # by replaying Redis's own CRC16 hash-slot algorithm over the actual keys the cache uses.
        script = HR._EVENT_DEDUPE_SCRIPT
        assert "..'" not in script.replace(" ", "") and '.."' not in script.replace(" ", ""), (
            "script must not build key names by string concatenation")
        assert script.count("KEYS[") == 3, "script must declare exactly its three used keys"

        cache = HR.RedisHeartbeatCache(FakeRedis(), event_stream_key="dama:hear:events")
        slots = {
            "stream": redis_cluster_key_slot(cache.event_stream_key),
            "dedupe": redis_cluster_key_slot(cache.event_dedupe_key),
            "dedupe_seq": redis_cluster_key_slot(cache.event_dedupe_seq_key),
        }
        assert len(set(slots.values())) == 1, f"all script keys must share one slot: {slots}"
        tagged = HR.RedisHeartbeatCache(FakeRedis(), event_stream_key="{tenant}:events")
        tagged_slots = {
            redis_cluster_key_slot(tagged.event_stream_key),
            redis_cluster_key_slot(tagged.event_dedupe_key),
            redis_cluster_key_slot(tagged.event_dedupe_seq_key),
        }
        assert len(tagged_slots) == 1
        assert tagged.event_dedupe_key == "{tenant}:dedupe"
        # The per-device "last event" cache key is intentionally *not* part of the atomic
        # script (see write()'s comment) precisely because it can never share that fixed slot:
        # its natural tag depends on device_id, so it must stay a plain single-key command.
        for device_id in ("nyquist", "totally-different-node"):
            per_device_key = f"dama:hear:event:{device_id}"
            assert per_device_key not in (cache.event_dedupe_key, cache.event_dedupe_seq_key)

    def test_heavy_heartbeat_traffic_does_not_touch_or_evict_event_dedupe_metadata(self, tmp_path):
        # Blocker: heartbeat writes must never share (and thus can never crowd out) the event
        # stream's dedupe ZSET/sequence counter, however much heartbeat volume is interleaved.
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis()
        store = HR.HeartbeatReceiverStore(
            fake,
            heartbeat_ttl_s=30,
            event_stream_maxlen=3,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        store.write_event(self._event(event_seq=0, idempotency_key="evt-0"))
        dedupe_key = store.cache.event_dedupe_key
        before = dict(fake.dedupe[dedupe_key])

        for i in range(500):
            # uptime_s varies per call so each payload gets a distinct record_uid -- otherwise
            # the durable store's own SQLite-level idempotency (not the thing under test here)
            # would skip most of these as already-cached duplicates before they ever reach Redis.
            store.write_heartbeat(self._heartbeat(device_id=f"node-{i % 5}", uptime_s=12345 + i))

        assert fake.dedupe[dedupe_key] == before, "heartbeat traffic must not mutate event dedupe"
        assert dedupe_key not in fake.sets and dedupe_key not in fake.ttls
        assert fake.execute_calls == 501  # 1 event script call + 500 heartbeat pipeline.execute()

        # The original marker is still recognized: replaying the same event is still a no-op.
        store.write_event(self._event(event_seq=0, idempotency_key="evt-0"))
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1

    def test_pending_event_from_before_dedupe_rollout_is_not_appended_twice(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        payload = self._event(event_seq=77, idempotency_key="legacy-event")
        fake = FakeRedis(failures_before_success=1)
        store = HR.HeartbeatReceiverStore(
            fake,
            heartbeat_ttl_s=30,
            event_stream_maxlen=8,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        with pytest.raises(RuntimeError, match="simulated redis outage"):
            store.write_event(payload)

        pending = store.durable_store.pending_records(1)[0]
        fake.streams[HR.EVENT_STREAM_KEY] = [{
            "device_id": pending.record["device_id"],
            "payload": pending.body_json,
        }]
        summary = store.replay_pending(limit=8)

        assert summary["synced"] == 1
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1
        assert pending.record_uid in fake.dedupe[store.cache.event_dedupe_key]

    def test_bad_payload_does_not_append_any_durable_rows(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        durable = HR.make_durable_store("sqlite", str(db))
        with running_server(durable_store=durable) as (base_url, _fake, _addr, _server):
            with pytest.raises(urllib.error.HTTPError) as ei:
                TestHttpIntegration._post(
                    base_url + "/api/hear/heartbeat",
                    self._heartbeat(device_id="bad/id"),
                )
            assert ei.value.code == 400
        counts = self._counts(db)
        assert counts["records"] == 0
        assert counts["successes"] == 0
        assert counts["failures"] == 0

    def test_healthz_reports_sqlite_outbox_status(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        durable = HR.make_durable_store("sqlite", str(db))
        with running_server(durable_store=durable) as (base_url, _fake, _addr, _server):
            with urllib.request.urlopen(base_url + "/healthz", timeout=2) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        assert body["durable_store"]["backend"] == "sqlite"
        assert body["durable_store"]["path"].endswith("heartbeats.sqlite3")
        assert body["durable_store"]["pending_records"] == 0


class TestBackgroundReplayWorker:
    @staticmethod
    def _event(**overrides):
        return TestValidation._event(**overrides)

    def test_a_failed_live_write_is_replayed_after_redis_recovers_without_a_restart(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        durable = HR.make_durable_store("sqlite", str(db))
        # The first live write hits a clean outage (no restart needed to recover: Redis just
        # comes back on its own, as it would after a network blip or pod restart).
        fake = FakeRedis(failures_before_success=1)
        with running_server(durable_store=durable, fake=fake,
                            durable_replay_interval_s=0.05) as (base_url, _fake, _addr, server):
            assert server._replay_thread is not None
            with pytest.raises(urllib.error.HTTPError) as ei:
                TestHttpIntegration._post(base_url + "/api/hear/event", self._event())
            assert ei.value.code == 503

            synced = False
            for _ in range(50):
                time.sleep(0.05)
                with sqlite3.connect(db) as con:
                    successes = con.execute(
                        "SELECT COUNT(*) FROM cache_attempts WHERE outcome='succeeded'"
                    ).fetchone()[0]
                if successes:
                    synced = True
                    break
            assert synced, "background replay worker did not repair the pending record in time"
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1

    def test_background_worker_is_disabled_when_durability_is_off(self):
        with running_server(durable_store=None, durable_replay_interval_s=0.05) as (
            _base_url, _fake, _addr, server,
        ):
            assert server._replay_thread is None

    def test_server_close_stops_the_replay_worker_and_leaves_no_thread_behind(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        durable = HR.make_durable_store("sqlite", str(db))
        store = HR.HeartbeatReceiverStore(
            FakeRedis(), heartbeat_ttl_s=30, redis_target="fake:6379", durable_store=durable,
        )
        server = HR.create_server(
            "127.0.0.1", 0, store, durable_replay_interval_s=0.02, durable_replay_limit=8,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            replay_thread = server._replay_thread
            assert replay_thread is not None
            assert replay_thread.is_alive()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        assert not replay_thread.is_alive()
        assert server._replay_thread is None

    def test_stop_surfaces_a_worker_that_does_not_terminate(self):
        worker = object.__new__(HR.DurableReplayWorker)
        worker._stop = threading.Event()

        class StuckThread:
            name = "stuck-replay"

            def join(self, timeout=None):
                return None

            def is_alive(self):
                return True

        worker.thread = StuckThread()
        with pytest.raises(RuntimeError, match="did not stop"):
            worker.stop(timeout=0)
        assert worker.thread is not None


class TestDurableRetentionPruning(TestValidation):
    @staticmethod
    def _counts(db_path):
        with sqlite3.connect(db_path) as con:
            return {
                "records": con.execute("SELECT COUNT(*) FROM durable_records").fetchone()[0],
                "attempts": con.execute("SELECT COUNT(*) FROM cache_attempts").fetchone()[0],
            }

    def test_prune_acknowledged_reclaims_only_old_cache_synced_records(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        store = HR.SqliteDurableRecordStore(str(db))
        old_uid, new_uid, pending_uid = "old-uid", "new-uid", "pending-uid"
        record = dict(self._heartbeat())
        record["received_at"] = HR.utc_now()
        record["receiver_schema_version"] = 1
        for uid in (old_uid, new_uid, pending_uid):
            store.persist(uid, record, json.dumps(record))
        store.note_cache_success(old_uid, "fake:6379")
        store.note_cache_success(new_uid, "fake:6379")
        # pending_uid is left with no successful cache_attempts -- it must never be pruned.

        old_cutoff = (HR.datetime.now(HR.timezone.utc) - HR.timedelta(days=45)).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        with sqlite3.connect(db) as con:
            con.execute("UPDATE durable_records SET created_at = ? WHERE record_uid = ?",
                       (old_cutoff, old_uid))

        pruned = store.prune_acknowledged(retention_days=30)

        assert pruned == 1
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            remaining = {row["record_uid"] for row in con.execute(
                "SELECT record_uid FROM durable_records")}
        assert remaining == {new_uid, pending_uid}

    def test_prune_acknowledged_is_a_no_op_when_retention_days_is_not_positive(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        store = HR.SqliteDurableRecordStore(str(db))
        record = dict(self._heartbeat())
        record["received_at"] = HR.utc_now()
        record["receiver_schema_version"] = 1
        store.persist("uid-1", record, json.dumps(record))
        store.note_cache_success("uid-1", "fake:6379")
        old_cutoff = (HR.datetime.now(HR.timezone.utc) - HR.timedelta(days=365)).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        with sqlite3.connect(db) as con:
            con.execute("UPDATE durable_records SET created_at = ?", (old_cutoff,))

        assert store.prune_acknowledged(retention_days=0) == 0
        assert store.prune_acknowledged(retention_days=-1) == 0
        assert self._counts(db)["records"] == 1

    def test_none_backend_prune_acknowledged_is_a_no_op(self):
        assert HR.DurableRecordStore().prune_acknowledged(retention_days=30) == 0

    def test_background_prune_worker_reclaims_old_records_while_the_server_is_up(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        durable = HR.make_durable_store("sqlite", str(db))
        with running_server(durable_store=durable,
                            durable_prune_interval_s=0.05,
                            durable_retention_days=30) as (base_url, _fake, _addr, server):
            assert server._prune_worker.thread is not None
            TestHttpIntegration._post(base_url + "/api/hear/event", self._event())
            old_cutoff = (HR.datetime.now(HR.timezone.utc) - HR.timedelta(days=45)).isoformat(
                timespec="seconds").replace("+00:00", "Z")
            with sqlite3.connect(db) as con:
                con.execute("UPDATE durable_records SET created_at = ?", (old_cutoff,))

            pruned = False
            for _ in range(50):
                time.sleep(0.05)
                if self._counts(db)["records"] == 0:
                    pruned = True
                    break
            assert pruned, "background prune worker did not reclaim the old record in time"

    def test_prune_worker_is_disabled_when_retention_days_is_not_positive(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        durable = HR.make_durable_store("sqlite", str(db))
        with running_server(durable_store=durable, durable_prune_interval_s=0.05,
                            durable_retention_days=0) as (_base_url, _fake, _addr, server):
            assert server._prune_worker.thread is None

    def test_server_close_stops_the_prune_worker_and_leaves_no_thread_behind(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        durable = HR.make_durable_store("sqlite", str(db))
        store = HR.HeartbeatReceiverStore(
            FakeRedis(), heartbeat_ttl_s=30, redis_target="fake:6379", durable_store=durable,
        )
        server = HR.create_server(
            "127.0.0.1", 0, store, durable_prune_interval_s=0.02, durable_retention_days=30,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            prune_thread = server._prune_worker.thread
            assert prune_thread is not None
            assert prune_thread.is_alive()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        assert not prune_thread.is_alive()
        assert server._prune_worker.thread is None

    def test_stop_surfaces_a_prune_worker_that_does_not_terminate(self):
        worker = object.__new__(HR.DurablePruneWorker)
        worker._stop = threading.Event()

        class StuckThread:
            name = "stuck-prune"

            def join(self, timeout=None):
                return None

            def is_alive(self):
                return True

        worker.thread = StuckThread()
        with pytest.raises(RuntimeError, match="did not stop"):
            worker.stop(timeout=0)
        assert worker.thread is not None


class TestStartupConfig:
    @pytest.mark.parametrize("token", [None, "", "   "])
    def test_configured_auth_token_rejects_missing_or_blank_values(self, token):
        with pytest.raises(ValueError, match="HEAR_HEARTBEAT_TOKEN / --auth-token"):
            HR._configured_auth_token(token)

    def test_configured_auth_token_trims_whitespace(self):
        assert HR._configured_auth_token("  secret  ") == "secret"

    def test_make_durable_store_rejects_postgres_until_it_is_implemented(self):
        with pytest.raises(ValueError, match="postgres"):
            HR.make_durable_store("postgres", "postgresql://db.example/dama_hear")


class TestDurableOutboxCorrectness(TestValidation):
    """Regression guards for the three reproduced durable-outbox defects (D1/D2/D3) plus the
    replay-ordering hazard they exposed. Every one of these failed before the fix."""

    @staticmethod
    def _store(db_path, fake, **kwargs):
        return HR.HeartbeatReceiverStore(
            fake,
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db_path)),
            **kwargs,
        )

    @staticmethod
    def _rows(db_path, sql, params=()):
        con = sqlite3.connect(db_path)
        try:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    # --- D1: record_uid must be device-scoped -------------------------------------------------
    def test_two_devices_reusing_one_idempotency_key_are_both_stored_and_published(self, tmp_path):
        # D1: node-local idempotency keys are only unique within a device. With the bare key as
        # the durable identity, the second device's heartbeat was dropped as a duplicate and the
        # node never appeared in Redis at all, while its firmware saw a 204 "accepted".
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis()
        store = self._store(db, fake)
        store.write_heartbeat(self._heartbeat(device_id="nyquist", idempotency_key="boot-1:7"))
        store.write_heartbeat(self._heartbeat(device_id="shannon", idempotency_key="boot-1:7"))

        rows = self._rows(db, "SELECT device_id, record_uid FROM durable_records ORDER BY id")
        assert [row["device_id"] for row in rows] == ["nyquist", "shannon"]
        assert len({row["record_uid"] for row in rows}) == 2
        assert json.loads(fake.values["dama:hear:nyquist"])["device_id"] == "nyquist"
        assert json.loads(fake.values["dama:hear:shannon"])["device_id"] == "shannon"
        assert fake.sets["dama:hear:devices"] == {"nyquist", "shannon"}

    def test_the_same_device_repeating_its_key_is_still_one_durable_record(self, tmp_path):
        # The other half of D1: scoping must not weaken same-device idempotency.
        db = tmp_path / "heartbeats.sqlite3"
        store = self._store(db, FakeRedis())
        store.write_event(self._event(idempotency_key="boot-1:7"))
        store.write_event(self._event(idempotency_key="boot-1:7"))
        assert len(self._rows(db, "SELECT id FROM durable_records")) == 1

    def test_one_telemetry_path_key_does_not_shadow_the_other_path(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        store = self._store(db, FakeRedis())
        store.write_heartbeat(self._heartbeat(idempotency_key="boot-1:7"))
        store.write_event(self._event(idempotency_key="boot-1:7"))
        paths = [row["telemetry_path"]
                 for row in self._rows(db, "SELECT telemetry_path FROM durable_records ORDER BY id")]
        assert paths == ["hear/heartbeat", "hear/event"]

    def test_a_v1_ledger_is_migrated_in_place_without_losing_records_or_replaying_them(self, tmp_path):
        # Forward-compatibility: an existing PVC holds schema-v1 rows keyed by the bare
        # idempotency_key. Opening it with this version must rewrite the identity (records and
        # their cache_attempts together), keep every row, and still treat an in-flight retry of
        # the same payload as a duplicate rather than re-publishing it.
        db = tmp_path / "heartbeats.sqlite3"
        payload = self._event(idempotency_key="legacy-1")
        legacy = HR.encode_json({**payload, "received_at": "2026-09-01T00:00:00Z",
                                 "receiver_schema_version": 1})
        con = sqlite3.connect(db)
        with con:
            con.execute(
                "CREATE TABLE durable_records (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "record_uid TEXT NOT NULL UNIQUE, telemetry_path TEXT NOT NULL, "
                "device_id TEXT NOT NULL, idempotency_key TEXT, payload_json TEXT NOT NULL, "
                "received_at TEXT NOT NULL, receiver_schema_version INTEGER NOT NULL, "
                "created_at TEXT NOT NULL, durable_schema_version INTEGER NOT NULL)")
            con.execute(
                "CREATE TABLE cache_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "record_uid TEXT NOT NULL, cache_target TEXT NOT NULL, outcome TEXT NOT NULL "
                "CHECK (outcome IN ('succeeded', 'failed')), error_text TEXT, "
                "created_at TEXT NOT NULL, "
                "FOREIGN KEY(record_uid) REFERENCES durable_records(record_uid))")
            con.execute(
                "INSERT INTO durable_records (record_uid, telemetry_path, device_id, "
                "idempotency_key, payload_json, received_at, receiver_schema_version, "
                "created_at, durable_schema_version) VALUES (?,?,?,?,?,?,?,?,1)",
                ("legacy-1", "hear/event", "nyquist", "legacy-1", legacy,
                 "2026-09-01T00:00:00Z", 1, "2026-09-01T00:00:00Z"))
            con.execute(
                "INSERT INTO cache_attempts (record_uid, cache_target, outcome, error_text, "
                "created_at) VALUES (?,?,?,?,?)",
                ("legacy-1", "fake:6379", "succeeded", None, "2026-09-01T00:00:00Z"))
        con.close()

        fake = FakeRedis()
        store = self._store(db, fake)
        migrated_uid = HR._record_uid(payload)
        records = self._rows(db, "SELECT record_uid, durable_schema_version, payload_json "
                                 "FROM durable_records")
        attempts = self._rows(db, "SELECT record_uid, outcome FROM cache_attempts")
        assert [row["record_uid"] for row in records] == [migrated_uid]
        assert records[0]["durable_schema_version"] == HR.DURABLE_SCHEMA_VERSION == 1, (
            "the SQLite generation stamp is not what changed; identity revision is tracked "
            "in durable_meta so a row stamped 2 keeps meaning 'written by the Postgres store'")
        assert records[0]["payload_json"] == legacy, "record content must never be rewritten"
        assert attempts == [{"record_uid": migrated_uid, "outcome": "succeeded"}]
        assert self._rows(db, "SELECT value FROM durable_meta WHERE key = ?",
                          (HR.DURABLE_IDENTITY_META_KEY,)) == [
            {"value": str(HR.DURABLE_IDENTITY_VERSION)}]
        assert store.health_snapshot()["durable_store"]["pending_records"] == 0

        store.write_event(payload)
        assert len(self._rows(db, "SELECT id FROM durable_records")) == 1
        assert fake.execute_calls == 0, "an already-synced legacy record must not be republished"

    def test_a_bare_key_row_written_by_a_rolled_back_binary_is_scoped_on_the_next_start(
            self, tmp_path):
        # Rollback/forward safety: an older receiver rolled back onto a migrated file writes rows
        # keyed by the bare idempotency_key again. The next start-up must still scope them rather
        # than skip the sweep because the file was already stamped.
        db = tmp_path / "heartbeats.sqlite3"
        payload = self._event(idempotency_key="rolled-back")
        store = self._store(db, FakeRedis())
        body = HR.encode_json({**payload, "received_at": "2026-09-01T00:00:00Z",
                               "receiver_schema_version": 1})
        con = sqlite3.connect(db)
        with con:
            con.execute(
                "INSERT INTO durable_records (record_uid, telemetry_path, device_id, "
                "idempotency_key, payload_json, received_at, receiver_schema_version, "
                "created_at, durable_schema_version) VALUES (?,?,?,?,?,?,?,?,1)",
                ("rolled-back", "hear/event", "nyquist", "rolled-back", body,
                 "2026-09-01T00:00:00Z", 1, "2026-09-01T00:00:00Z"))
        con.close()
        assert self._rows(db, "SELECT value FROM durable_meta WHERE key = ?",
                          (HR.DURABLE_IDENTITY_META_KEY,)) == [
            {"value": str(HR.DURABLE_IDENTITY_VERSION)}]

        self._store(db, FakeRedis())

        assert [row["record_uid"] for row in
                self._rows(db, "SELECT record_uid FROM durable_records")] == [
            HR._record_uid(payload)]
        del store

    def test_migration_is_idempotent_across_repeated_opens(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        self._store(db, FakeRedis()).write_event(self._event(idempotency_key="k-1"))
        before = self._rows(db, "SELECT record_uid, durable_schema_version FROM durable_records")
        self._store(db, FakeRedis())
        self._store(db, FakeRedis())
        assert self._rows(db, "SELECT record_uid, durable_schema_version "
                              "FROM durable_records") == before

    # --- D2: one durable row must produce exactly one cache publish ---------------------------
    def test_concurrent_identical_events_publish_to_redis_exactly_once(self, tmp_path):
        # D2: eight simultaneous retries of one event produced a single durable row but four
        # Redis script invocations, because every caller read "not cached yet" before any of
        # them recorded success. The publish right is now claimed inside the same transaction.
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis()
        store = self._store(db, fake)
        payload = self._event(idempotency_key="concurrent-1")
        start = threading.Barrier(8)
        errors = []

        def _writer():
            start.wait(timeout=5)
            try:
                store.write_event(payload)
            except Exception as exc:  # pragma: no cover - a failure here fails the assert below
                errors.append(exc)

        threads = [threading.Thread(target=_writer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert errors == []
        assert len(self._rows(db, "SELECT id FROM durable_records")) == 1
        assert fake.execute_calls == 1
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1
        assert len(self._rows(
            db, "SELECT id FROM cache_attempts WHERE outcome='succeeded'")) == 1
        assert store.health_snapshot()["durable_store"]["pending_records"] == 0

    def test_concurrent_identical_heartbeats_publish_to_redis_exactly_once(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis()
        store = self._store(db, fake)
        payload = self._heartbeat(idempotency_key="concurrent-hb")
        start = threading.Barrier(8)

        def _writer():
            start.wait(timeout=5)
            store.write_heartbeat(payload)

        threads = [threading.Thread(target=_writer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert len(self._rows(db, "SELECT id FROM durable_records")) == 1
        # Exactly one caller won the claim and published: a second recorded success would mean a
        # single durable row fanned out into several Redis writes, which is D2 itself.
        assert len(self._rows(
            db, "SELECT id FROM cache_attempts WHERE outcome='succeeded'")) == 1
        assert fake.ttls["dama:hear:nyquist"] == 30
        assert store.health_snapshot()["durable_store"]["pending_records"] == 0

    def test_a_claim_is_released_when_the_publish_fails_so_replay_can_take_it(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        store = self._store(db, FakeRedis(failures_before_success=1))
        with pytest.raises(RuntimeError, match="simulated redis outage"):
            store.write_event(self._event(idempotency_key="claim-release"))
        assert self._rows(db, "SELECT record_uid FROM cache_claims") == []
        assert store.durable_store.claim(HR._record_uid(
            self._event(idempotency_key="claim-release"))) is True

    def test_an_expired_claim_can_be_taken_over_so_a_dead_publisher_cannot_strand_a_record(
            self, tmp_path):
        # A process that dies between claiming and publishing must not pin the record forever.
        db = tmp_path / "heartbeats.sqlite3"
        durable = HR.make_durable_store("sqlite", str(db))
        assert durable.claim("orphan-uid") is True
        assert durable.claim("orphan-uid") is False
        con = sqlite3.connect(db)
        with con:
            con.execute("UPDATE cache_claims SET expires_at = '2000-01-01T00:00:00Z'")
        con.close()
        assert durable.claim("orphan-uid") is True

    # --- D3: a deduped heartbeat must still re-arm the cache TTL ------------------------------
    def test_a_duplicate_heartbeat_re_arms_the_ttl_instead_of_skipping_redis(self, tmp_path):
        # D3: during a reboot/retry loop the node resends the same heartbeat. The durable store
        # recognized it and returned before Redis, so dama:hear:<id> quietly expired and the node
        # read as offline while it was in fact reporting.
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis()
        store = self._store(db, fake)
        payload = self._heartbeat(idempotency_key="reboot-loop-1")
        store.write_heartbeat(payload)
        # Simulate the TTL running out between the original and the retry.
        del fake.values["dama:hear:nyquist"]
        del fake.ttls["dama:hear:nyquist"]

        store.write_heartbeat(payload)

        assert json.loads(fake.values["dama:hear:nyquist"])["device_id"] == "nyquist"
        assert fake.ttls["dama:hear:nyquist"] == 30
        # Still exactly one durable record and one recorded success: the refresh is a pure cache
        # overwrite, not a second ledger entry.
        assert len(self._rows(db, "SELECT id FROM durable_records")) == 1
        assert len(self._rows(
            db, "SELECT id FROM cache_attempts WHERE outcome='succeeded'")) == 1

    def test_a_redelivered_older_heartbeat_does_not_roll_the_cache_backwards(self, tmp_path):
        # The D3 refresh must not become a stale-overwrite of its own: an older heartbeat can be
        # redelivered (MQTT QoS-1, a node draining its local outbox, an out-of-order retry) after
        # a newer one was cached. Re-arming the TTL with that older body would walk the fleet
        # view backwards, so a superseded duplicate refreshes nothing.
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis()
        store = self._store(db, fake)
        older = self._heartbeat(uptime_s=100, idempotency_key="hb-old")
        store.write_heartbeat(older)
        store.write_heartbeat(self._heartbeat(uptime_s=200, idempotency_key="hb-new"))
        assert json.loads(fake.values["dama:hear:nyquist"])["uptime_s"] == 200

        store.write_heartbeat(older)

        assert json.loads(fake.values["dama:hear:nyquist"])["uptime_s"] == 200
        assert json.loads(fake.values["dama:hear:latest"])["uptime_s"] == 200

    def test_a_duplicate_heartbeat_surfaces_a_cache_outage_instead_of_a_false_ack(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis()
        store = self._store(db, fake)
        payload = self._heartbeat(idempotency_key="reboot-loop-2")
        store.write_heartbeat(payload)
        fake.failures_before_success = 1
        with pytest.raises(RuntimeError, match="simulated redis outage"):
            store.write_heartbeat(payload)
        assert len(self._rows(db, "SELECT id FROM cache_attempts WHERE outcome='failed'")) == 1

    def test_a_duplicate_event_is_still_a_no_op_and_never_re_touches_the_stream(self, tmp_path):
        # The D3 refresh is heartbeat-only on purpose: the event stream is append-only.
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis()
        store = self._store(db, fake)
        payload = self._event(idempotency_key="evt-dupe")
        store.write_event(payload)
        store.write_event(payload)
        assert fake.execute_calls == 1
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1

    # --- replay ordering ----------------------------------------------------------------------
    def test_replay_does_not_overwrite_a_newer_heartbeat_with_a_stale_pending_one(self, tmp_path):
        # A heartbeat stranded by a cache outage is strictly older than whatever the node has
        # reported since. Replaying it would SETEX the stale body back over the fresh snapshot,
        # so the fleet view would jump backwards because of a retry. It is marked synced (the
        # cache already holds newer state) instead of being published or left pending forever.
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis(failures_before_success=1)
        store = self._store(db, fake)
        with pytest.raises(RuntimeError, match="simulated redis outage"):
            store.write_heartbeat(self._heartbeat(uptime_s=100, idempotency_key="hb-old"))
        store.write_heartbeat(self._heartbeat(uptime_s=200, idempotency_key="hb-new"))
        assert json.loads(fake.values["dama:hear:nyquist"])["uptime_s"] == 200

        summary = store.replay_pending(limit=10)

        assert summary["attempted"] == 1
        assert summary["synced"] == 1
        assert summary["failed"] == 0
        assert summary["remaining_pending"] == 0
        assert json.loads(fake.values["dama:hear:nyquist"])["uptime_s"] == 200
        assert json.loads(fake.values["dama:hear:latest"])["uptime_s"] == 200

    def test_a_stale_heartbeat_for_another_device_is_still_replayed(self, tmp_path):
        # Supersession is per device: a newer heartbeat from node A must not suppress node B's
        # pending record.
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis(failures_before_success=1)
        store = self._store(db, fake)
        with pytest.raises(RuntimeError, match="simulated redis outage"):
            store.write_heartbeat(self._heartbeat(device_id="shannon", idempotency_key="hb-b"))
        store.write_heartbeat(self._heartbeat(device_id="nyquist", idempotency_key="hb-a"))

        summary = store.replay_pending(limit=10)

        assert summary == {"backend": "sqlite", "attempted": 1, "synced": 1, "failed": 0,
                           "remaining_pending": 0}
        assert json.loads(fake.values["dama:hear:shannon"])["device_id"] == "shannon"

    def test_replay_is_bounded_by_its_limit_and_drains_oldest_first(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis(failures_before_success=4)
        store = self._store(db, fake)
        for seq in range(4):
            with pytest.raises(RuntimeError, match="simulated redis outage"):
                store.write_event(self._event(event_seq=seq, idempotency_key=f"evt-{seq}"))

        first = store.replay_pending(limit=2)
        assert first["attempted"] == 2 and first["synced"] == 2
        assert first["remaining_pending"] == 2
        assert [json.loads(entry["payload"])["event_seq"]
                for entry in fake.streams[HR.EVENT_STREAM_KEY]] == [0, 1]

        second = store.replay_pending(limit=2)
        assert second["synced"] == 2 and second["remaining_pending"] == 0
        assert [json.loads(entry["payload"])["event_seq"]
                for entry in fake.streams[HR.EVENT_STREAM_KEY]] == [0, 1, 2, 3]

    def test_pruning_reclaims_claim_rows_too(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        store = self._store(db, FakeRedis())
        store.write_event(self._event(idempotency_key="pruned"))
        con = sqlite3.connect(db)
        with con:
            con.execute("UPDATE durable_records SET created_at = '2000-01-01T00:00:00Z'")
            con.execute("INSERT INTO cache_claims (record_uid, claimed_at, expires_at) "
                        "SELECT record_uid, created_at, created_at FROM durable_records")
        con.close()
        assert store.durable_store.prune_acknowledged(1) == 1
        assert self._rows(db, "SELECT record_uid FROM cache_claims") == []

    def test_health_counts_are_index_backed_rather_than_full_table_scans(self, tmp_path):
        # /healthz is probed every ~10s; its three counts must not degrade into full scans of an
        # append-only ledger as it grows.
        db = tmp_path / "heartbeats.sqlite3"
        store = self._store(db, FakeRedis())
        store.write_event(self._event(idempotency_key="health-1"))
        con = sqlite3.connect(db)
        try:
            plans = [
                " ".join(str(part) for part in row)
                for row in con.execute(
                    "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM cache_attempts "
                    "WHERE outcome = 'succeeded'").fetchall()
            ]
        finally:
            con.close()
        assert any("cache_attempts_outcome_id" in plan for plan in plans), plans


class TestLegacyEventTimeBlock(TestValidation):
    """R4: a pre-#156 firmware sends no time block on hear/event at all.

    mach is still on v0.1.4-5-g2355270, whose hear_push_event_json() omits the key that
    hear_push_heartbeat_json() writes, so every one of its heartbeats is accepted and every one
    of its events is refused with "time must be an object" before anything durable is written.
    """

    @staticmethod
    def _legacy_event(**overrides):
        payload = TestValidation._event(**overrides)
        payload.pop("time", None)
        return payload

    def test_a_legacy_event_with_a_timestamp_is_accepted_as_clock_valid(self):
        got = HR.validate_event_payload(self._legacy_event())
        assert got["time"] == {"valid": True, "source": HR.LEGACY_TIME_SOURCE}
        assert got["ts"] == "2026-09-12T23:43:04Z"

    def test_a_legacy_event_without_a_timestamp_is_accepted_as_clock_invalid(self):
        got = HR.validate_event_payload(self._legacy_event(ts=None))
        assert got["time"] == {"valid": False, "source": HR.LEGACY_TIME_SOURCE}

    def test_the_inferred_block_is_marked_so_a_reader_can_tell_it_apart(self):
        reported = HR.validate_event_payload(TestValidation._event())
        assert "source" not in reported["time"]

    def test_the_canonicalisation_does_not_mutate_the_callers_body(self):
        payload = self._legacy_event()
        HR.validate_event_payload(payload)
        assert "time" not in payload

    def test_a_heartbeat_without_a_time_block_is_still_refused(self):
        """Only events lost the block; a heartbeat missing it is a genuinely broken body."""
        payload = TestValidation._heartbeat()
        payload.pop("time")
        with pytest.raises(HR.RequestError, match="time must be an object"):
            HR.validate_heartbeat_payload(payload)

    @pytest.mark.parametrize("ts", [1757721784000, "", "   ", True, [], {}])
    def test_a_malformed_ts_is_still_refused_rather_than_guessed(self, ts):
        with pytest.raises(HR.RequestError):
            HR.validate_event_payload(self._legacy_event(ts=ts))

    def test_an_explicit_time_block_is_never_overridden(self):
        with pytest.raises(HR.RequestError, match="ts must be null"):
            HR.validate_event_payload(TestValidation._event(time={"valid": False}))

    def test_a_current_firmware_event_is_unaffected(self):
        got = HR.validate_event_payload(TestValidation._event(
            time={"valid": True, "state": "LOCKED", "boot_id": "a1b2c3d4",
                  "sync_sigma_ns": 900, "anchor_age_us": 12, "boot_epoch_us": 17},
        ))
        assert got["time"]["state"] == "LOCKED"
        assert "source" not in got["time"]


class TestDurableRefusals(TestValidation):
    """R4: a refused message leaves a durable row instead of only a log line."""

    @staticmethod
    def _store(db, fake=None, **kwargs):
        durable = HR.SqliteDurableRecordStore(str(db), **kwargs)
        return HR.HeartbeatReceiverStore(fake or FakeRedis(), redis_target="fake:6379",
                                         durable_store=durable)

    def test_a_refusal_is_recorded_with_the_body_as_received(self, tmp_path):
        store = self._store(tmp_path / "hb.sqlite3")
        entry = store.record_refusal("hear/event", "mach", "mqtt_bridge",
                                     "time must be an object", b'{"telemetry_path":"hear/event"}')
        assert entry is not None
        assert entry.reason == "time must be an object"
        assert entry.body_text == '{"telemetry_path":"hear/event"}'
        stored = store.durable_store.refusals()
        assert [r.refusal_uid for r in stored] == [entry.refusal_uid]

    def test_a_refusal_never_becomes_replay_material(self, tmp_path):
        store = self._store(tmp_path / "hb.sqlite3")
        store.record_refusal("hear/event", "mach", "mqtt_bridge", "nope", b"{}")
        assert store.durable_store.pending_records(10) == []
        assert store.durable_store.health()["pending_records"] == 0

    def test_a_repeat_of_the_same_refusal_does_not_add_a_row(self, tmp_path):
        store = self._store(tmp_path / "hb.sqlite3")
        for _ in range(5):
            store.record_refusal("hear/event", "mach", "mqtt_bridge", "nope", b"{}")
        assert len(store.durable_store.refusals()) == 1
        assert store.durable_store.refusal_health()["refused_messages"] == 1

    def test_an_oversized_body_is_truncated_rather_than_stored_whole(self, tmp_path):
        store = self._store(tmp_path / "hb.sqlite3")
        entry = store.record_refusal("hear/event", "mach", "mqtt_bridge", "nope",
                                     b"x" * (HR.REFUSAL_MAX_BODY_CHARS * 3))
        assert entry.truncated is True
        assert len(entry.body_text) == HR.REFUSAL_MAX_BODY_CHARS

    def test_refusals_are_not_in_the_cross_store_health_contract(self, tmp_path):
        """The PG generation must implement health() key for key; refusals are their own surface."""
        store = self._store(tmp_path / "hb.sqlite3")
        store.record_refusal("hear/event", "mach", "mqtt_bridge", "nope", b"{}")
        assert set(store.durable_store.health()) == set(HR.DurableRecordStore().health())
        snapshot = store.health_snapshot()
        assert "refused_messages" not in snapshot["durable_store"]
        assert snapshot["refusals"]["refused_messages"] == 1
        assert snapshot["refusals"]["last_refusal_at"].endswith("Z")

    def test_the_disabled_store_accepts_the_seam_without_storing_anything(self):
        store = HR.HeartbeatReceiverStore(FakeRedis(), redis_target="fake:6379")
        assert store.record_refusal("hear/event", "mach", "mqtt_bridge", "nope", b"{}") is None
        assert store.durable_store.refusals() == []
        assert store.durable_store.refusal_health()["enabled"] is False

    def test_a_storage_failure_never_propagates_to_the_caller(self, tmp_path):
        store = self._store(tmp_path / "hb.sqlite3")

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        store.durable_store.record_refusal = boom
        assert store.record_refusal("hear/event", "mach", "mqtt_bridge", "nope", b"{}") is None

    # -- transactions and connections ---------------------------------------------------------

    def test_every_refusal_path_closes_its_connection(self, tmp_path):
        """A leaked connection holds a WAL read snapshot open and keeps the file from checkpointing."""
        store = self._store(tmp_path / "hb.sqlite3")
        opened = []
        real_connect = sqlite3.connect

        def tracking_connect(*args, **kwargs):
            con = real_connect(*args, **kwargs)
            opened.append(con)
            return con

        sqlite3.connect = tracking_connect
        try:
            store.record_refusal("hear/event", "mach", "mqtt_bridge", "nope", b"{}")
            store.durable_store.refusals()
            store.durable_store.prune_refusals(1)
            store.durable_store.refusal_health()
        finally:
            sqlite3.connect = real_connect
        assert opened
        for con in opened:
            with pytest.raises(sqlite3.ProgrammingError):
                con.execute("SELECT 1")

    def test_the_insert_and_its_confirmation_are_one_transaction(self, tmp_path):
        """A caller handed a RefusalEntry must be holding a committed row, not a hopeful one."""
        db = tmp_path / "hb.sqlite3"
        store = self._store(db)
        entry = store.record_refusal("hear/event", "mach", "mqtt_bridge", "nope", b"{}")
        con = sqlite3.connect(db)
        try:
            rows = con.execute("SELECT refusal_uid FROM refused_messages").fetchall()
        finally:
            con.close()
        assert [r[0] for r in rows] == [entry.refusal_uid]

    def test_a_failed_refusal_write_leaves_no_partial_row(self, tmp_path):
        db = tmp_path / "hb.sqlite3"
        durable = HR.SqliteDurableRecordStore(str(db))
        real_enforce = durable._enforce_refusal_caps

        def failing(con):
            real_enforce(con)
            raise sqlite3.OperationalError("database is locked")

        durable._enforce_refusal_caps = failing
        with pytest.raises(sqlite3.OperationalError):
            durable.record_refusal("hear/event", "mach", "mqtt_bridge", "nope", b"{}")
        assert durable.refusals() == []

    # -- bounded capacity ---------------------------------------------------------------------

    def test_a_flood_of_distinct_bodies_is_bounded_by_row_count_not_age(self, tmp_path):
        """Any publisher on the shared topic can mint unique rejected bodies; age alone is not a bound."""
        durable = HR.SqliteDurableRecordStore(str(tmp_path / "hb.sqlite3"), refusal_max_rows=10)
        for i in range(200):
            durable.record_refusal("hear/event", "flooder", "mqtt_bridge", "malformed JSON",
                                   ('{"n":%d}' % i).encode())
        health = durable.refusal_health()
        assert health["refused_messages"] == 10
        assert health["evicted_refusals"] == 190

    def test_the_byte_cap_bounds_the_volume_independently_of_the_row_cap(self, tmp_path):
        durable = HR.SqliteDurableRecordStore(str(tmp_path / "hb.sqlite3"),
                                              refusal_max_rows=10_000, refusal_max_bytes=4096)
        for i in range(50):
            durable.record_refusal("hear/event", "flooder", "mqtt_bridge", "malformed JSON",
                                   (('%04d' % i) + "x" * 1000).encode())
        assert durable.refusal_health()["refused_bytes"] <= 4096

    def test_a_flood_evicts_its_own_history_before_another_devices(self, tmp_path):
        durable = HR.SqliteDurableRecordStore(str(tmp_path / "hb.sqlite3"), refusal_max_rows=5)
        durable.record_refusal("hear/event", "mach", "mqtt_bridge", "time must be an object",
                               b'{"device_id":"mach"}')
        for i in range(100):
            durable.record_refusal("hear/event", "flooder", "mqtt_bridge", "malformed JSON",
                                   ('{"n":%d}' % i).encode())
        devices = {r.device_id for r in durable.refusals(limit=50)}
        assert "mach" in devices, "a flooding publisher must not evict another node's evidence"

    def test_bounding_a_refusal_flood_never_touches_accepted_records(self, tmp_path):
        db = tmp_path / "hb.sqlite3"
        durable = HR.SqliteDurableRecordStore(str(db), refusal_max_rows=2)
        store = HR.HeartbeatReceiverStore(FakeRedis(), redis_target="fake:6379",
                                          durable_store=durable, refusal_rate_limit=0)
        store.write_event(self._event(idempotency_key="keep-me"))
        for i in range(50):
            durable.record_refusal("hear/event", "flooder", "mqtt_bridge", "malformed JSON",
                                   ('{"n":%d}' % i).encode())
        con = sqlite3.connect(db)
        try:
            assert con.execute("SELECT COUNT(*) FROM durable_records").fetchone()[0] == 1
            assert con.execute("SELECT COUNT(*) FROM cache_attempts").fetchone()[0] == 1
            assert con.execute("SELECT COUNT(*) FROM refused_messages").fetchone()[0] == 2
        finally:
            con.close()

    def test_a_flood_is_rate_limited_in_memory_before_it_reaches_the_disk(self, tmp_path):
        """Without this, every hostile message costs one synchronous fsync on the shared volume."""
        store = self._store(tmp_path / "hb.sqlite3")
        store.refusal_rate_limit = 5
        for i in range(100):
            store.record_refusal("hear/event", "flooder", "mqtt_bridge", "malformed JSON",
                                 ('{"n":%d}' % i).encode())
        assert store.durable_store.refusal_health()["refused_messages"] == 5
        assert store.suppressed_refusals == 95

    def test_one_loud_source_does_not_consume_another_sources_rate_budget(self, tmp_path):
        store = self._store(tmp_path / "hb.sqlite3")
        store.refusal_rate_limit = 2
        for i in range(20):
            store.record_refusal("hear/event", "flooder", "mqtt_bridge", "malformed JSON",
                                 ('{"n":%d}' % i).encode())
        assert store.record_refusal("hear/event", "mach", "mqtt_bridge",
                                    "time must be an object", b'{"device_id":"mach"}') is not None

    def test_the_rate_window_reopens(self, tmp_path):
        store = self._store(tmp_path / "hb.sqlite3")
        store.refusal_rate_limit = 1
        store.refusal_rate_window_s = 0.5
        assert store.record_refusal("hear/event", "mach", "mqtt_bridge", "a", b"{}") is not None
        assert store.record_refusal("hear/event", "mach", "mqtt_bridge", "b", b"{}") is None
        time.sleep(0.6)
        assert store.record_refusal("hear/event", "mach", "mqtt_bridge", "c", b"{}") is not None

    # -- retention ----------------------------------------------------------------------------

    def test_pruning_drops_refusals_older_than_the_retention_window(self, tmp_path):
        db = tmp_path / "hb.sqlite3"
        durable = HR.SqliteDurableRecordStore(str(db))
        durable.record_refusal("hear/event", "mach", "mqtt_bridge", "old", b'{"a":1}')
        durable.record_refusal("hear/event", "mach", "mqtt_bridge", "new", b'{"a":2}')
        con = sqlite3.connect(db)
        try:
            con.execute("UPDATE refused_messages SET received_at='2000-01-01T00:00:00Z' "
                        "WHERE reason='old'")
            con.commit()
        finally:
            con.close()
        assert durable.prune_refusals(30) == 1
        assert [r.reason for r in durable.refusals()] == ["new"]

    def test_pruning_is_disabled_by_a_non_positive_retention(self, tmp_path):
        durable = HR.SqliteDurableRecordStore(str(tmp_path / "hb.sqlite3"))
        durable.record_refusal("hear/event", "mach", "mqtt_bridge", "old", b"{}")
        assert durable.prune_refusals(0) == 0
        assert len(durable.refusals()) == 1

    def test_the_background_prune_worker_sweeps_refusals_too(self, tmp_path):
        db = tmp_path / "hb.sqlite3"
        durable = HR.SqliteDurableRecordStore(str(db))
        durable.record_refusal("hear/event", "mach", "mqtt_bridge", "old", b"{}")
        con = sqlite3.connect(db)
        try:
            con.execute("UPDATE refused_messages SET received_at='2000-01-01T00:00:00Z'")
            con.commit()
        finally:
            con.close()
        store = HR.HeartbeatReceiverStore(FakeRedis(), redis_target="fake:6379",
                                          durable_store=durable)
        worker = HR.DurablePruneWorker(store, interval_s=0.05, retention_days=30)
        try:
            for _ in range(100):
                time.sleep(0.05)
                if not durable.refusals():
                    break
        finally:
            worker.stop()
        assert durable.refusals() == []

    # -- HTTP ---------------------------------------------------------------------------------

    def test_a_refused_post_leaves_a_durable_row(self, tmp_path):
        durable = HR.SqliteDurableRecordStore(str(tmp_path / "hb.sqlite3"))
        with running_server(durable_store=durable) as (base_url, _fake, _addr, _server):
            with pytest.raises(urllib.error.HTTPError) as ei:
                TestHttpIntegration._post(base_url + "/api/hear/event",
                                          self._event(event_seq="not-an-int"))
            assert ei.value.code == 400
        stored = durable.refusals()
        assert len(stored) == 1
        assert stored[0].telemetry_path == "hear/event"
        assert stored[0].device_id == "nyquist"
        assert stored[0].source == "lan_http"

    def test_an_unauthenticated_post_is_not_allowed_to_write_to_the_volume(self, tmp_path):
        durable = HR.SqliteDurableRecordStore(str(tmp_path / "hb.sqlite3"))
        with running_server(auth_token="secret", durable_store=durable) as (base_url, *_):
            with pytest.raises(urllib.error.HTTPError) as ei:
                TestHttpIntegration._post(base_url + "/api/hear/event", self._event())
            assert ei.value.code == 401
        assert durable.refusals() == []

    def test_an_unparseable_body_is_quarantined_as_received(self, tmp_path):
        durable = HR.SqliteDurableRecordStore(str(tmp_path / "hb.sqlite3"))
        with running_server(durable_store=durable) as (base_url, _fake, _addr, _server):
            req = urllib.request.Request(base_url + "/api/hear/event", data=b"{not json",
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(req, timeout=2)
        stored = durable.refusals()
        assert [r.body_text for r in stored] == ["{not json"]
        assert stored[0].device_id == "unknown"

    def test_healthz_reports_the_quarantine(self, tmp_path):
        durable = HR.SqliteDurableRecordStore(str(tmp_path / "hb.sqlite3"))
        with running_server(durable_store=durable) as (base_url, _fake, _addr, _server):
            with pytest.raises(urllib.error.HTTPError):
                TestHttpIntegration._post(base_url + "/api/hear/event",
                                          self._event(event_seq="not-an-int"))
            with urllib.request.urlopen(base_url + "/healthz", timeout=2) as resp:
                body = json.loads(resp.read())
        assert body["refusals"]["refused_messages"] == 1
        assert "refused_messages" not in body["durable_store"]

    def test_a_legacy_event_posted_over_http_is_accepted_and_not_quarantined(self, tmp_path):
        durable = HR.SqliteDurableRecordStore(str(tmp_path / "hb.sqlite3"))
        legacy = TestLegacyEventTimeBlock._legacy_event()
        with running_server(durable_store=durable) as (base_url, fake, _addr, _server):
            with TestHttpIntegration._post(base_url + "/api/hear/event", legacy) as resp:
                assert resp.status == 204
        assert durable.refusals() == []
        assert json.loads(fake.values["dama:hear:event:nyquist"])["event_type"] == "clip_written"
