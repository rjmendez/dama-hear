"""The hear heartbeat receiver: schema guard, TTL write, and advisory event intake."""
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
from tools import hear_heartbeat_receiver as HR  # noqa: E402


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
                   durable_replay_interval_s=0.0, durable_replay_limit=HR.DURABLE_REPLAY_LIMIT):
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
        durable_replay_interval_s=durable_replay_interval_s,
        durable_replay_limit=durable_replay_limit,
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
        assert set(dedupe_zset) == {f"evt-{seq}" for seq in retained_seqs}

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
