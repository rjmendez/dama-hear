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

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import hear_heartbeat_receiver as HR  # noqa: E402


class FakeRedis:
    """Minimal stand-in. ``eval`` emulates tools/hear_heartbeat_receiver's publish-once script."""

    def __init__(self, failures_before_success: int = 0):
        self.values = {}
        self.ttls = {}
        self.sets = {}
        self.streams = {}
        self.failures_before_success = failures_before_success
        self.execute_calls = 0
        self.eval_calls = 0
        self._ops = []
        self._lock = threading.Lock()

    def pipeline(self, transaction=False):
        assert transaction is False
        self._ops = []
        return self

    def setex(self, key, ttl, value):
        self._ops.append(("setex", key, ttl, value))
        return self

    def sadd(self, key, *members):
        self._ops.append(("sadd", key, members))
        return self

    def set(self, key, value, nx=False, ex=None):
        if nx:
            with self._lock:
                if key in self.values:
                    return None
                self.values[key] = value
                if ex is not None:
                    self.ttls[key] = ex
                return True
        self._ops.append(("set", key, value))
        return self

    def delete(self, key):
        with self._lock:
            self.values.pop(key, None)
            self.ttls.pop(key, None)
        return 1

    def eval(self, script, numkeys, *args):
        assert numkeys == 2
        self.eval_calls += 1
        guard_key, stream_key = args[0], args[1]
        guard_value, guard_ttl, maxlen, device_id, payload = args[2:7]
        with self._lock:
            if guard_key in self.values:
                return 0
            self.values[guard_key] = guard_value
            self.ttls[guard_key] = int(guard_ttl)
            self._append_stream(stream_key, {"device_id": device_id, "payload": payload},
                                int(maxlen))
        return 1

    def xadd(self, key, fields, maxlen=None, approximate=True):
        self._ops.append(("xadd", key, dict(fields), maxlen, approximate))
        return str(len(self.streams.get(key, [])) + 1)

    def _append_stream(self, key, fields, maxlen):
        stream = self.streams.setdefault(key, [])
        stream.append(fields)
        if maxlen is not None and len(stream) > maxlen:
            del stream[:-maxlen]

    def execute(self):
        self.execute_calls += 1
        ops, self._ops = self._ops, []
        if self.failures_before_success > 0:
            self.failures_before_success -= 1
            raise RuntimeError("simulated redis outage")
        for op in ops:
            kind = op[0]
            if kind == "setex":
                _, key, ttl, value = op
                self.values[key] = value
                self.ttls[key] = ttl
            elif kind == "sadd":
                _, key, members = op
                self.sets.setdefault(key, set()).update(members)
            elif kind == "set":
                _, key, value = op
                self.values[key] = value
            elif kind == "xadd":
                _, key, fields, maxlen, approximate = op
                self._append_stream(key, fields, maxlen)
                assert approximate is True
        return []


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


@contextmanager
def running_server(auth_token=None, socket_timeout_s=0.2, durable_store=None):
    fake = FakeRedis()
    store = HR.HeartbeatReceiverStore(
        fake,
        heartbeat_ttl_s=30,
        redis_target="fake:6379",
        durable_store=durable_store,
    )
    server = HR.create_server(
        "127.0.0.1", 0, store, max_body_bytes=2048,
        auth_token=auth_token, socket_timeout_s=socket_timeout_s,
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


class TestPublishExactlyOnce(TestValidation):
    """Regressions for the double-publish window between XADD and the durable success marker."""

    @staticmethod
    def _counts(db_path):
        return TestDurableSqlite._counts(db_path)

    def _store(self, db, fake):
        return HR.HeartbeatReceiverStore(
            fake,
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )

    def test_crash_before_mark_success_does_not_republish_the_event_on_replay(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis()
        store = self._store(db, fake)
        # Simulate the crash window: the cache publish lands, the process dies before
        # note_cache_success() is appended, so the record is still pending at the next start.
        store.durable_store.note_cache_success = lambda *a, **k: None
        store.write_event(self._event())
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1
        assert store.health_snapshot()["durable_store"]["pending_records"] == 1

        recovered = self._store(db, fake)
        summary = recovered.replay_pending(limit=10)
        assert summary["attempted"] == 1
        assert summary["synced"] == 1
        assert summary["failed"] == 0
        assert summary["remaining_pending"] == 0
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1
        assert self._counts(db)["successes"] == 1

    def test_concurrent_duplicate_requests_publish_the_event_exactly_once(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        fake = FakeRedis()
        store = self._store(db, fake)
        payload = self._event(idempotency_key="nyquist-clip-234")
        start = threading.Barrier(8)
        errors = []

        def _post():
            try:
                start.wait(timeout=5)
                store.write_event(dict(payload))
            except Exception as exc:  # pragma: no cover - surfaced by the assert below.
                errors.append(exc)

        threads = [threading.Thread(target=_post) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        counts = self._counts(db)
        assert counts["records"] == 1
        assert counts["successes"] == 1
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1

    def test_publish_guard_falls_back_when_the_client_cannot_run_scripts(self, tmp_path):
        class NoScriptRedis(FakeRedis):
            def eval(self, *a, **k):
                raise HR.redis.exceptions.ResponseError("unknown command 'EVAL'")

        db = tmp_path / "heartbeats.sqlite3"
        fake = NoScriptRedis()
        store = self._store(db, fake)
        store.durable_store.note_cache_success = lambda *a, **k: None
        store.write_event(self._event())
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1

        recovered = self._store(db, fake)
        recovered.cache.client = fake
        recovered.replay_pending(limit=10)
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1

    def test_a_failed_publish_releases_the_guard_so_replay_can_retry(self, tmp_path):
        class FlakyPublishRedis(FakeRedis):
            def __init__(self):
                super().__init__()
                self.publish_failures = 1

            def eval(self, *a, **k):
                if self.publish_failures > 0:
                    self.publish_failures -= 1
                    raise RuntimeError("simulated redis outage")
                return super().eval(*a, **k)

        db = tmp_path / "heartbeats.sqlite3"
        fake = FlakyPublishRedis()
        store = self._store(db, fake)
        with pytest.raises(RuntimeError, match="simulated redis outage"):
            store.write_event(self._event())
        assert fake.streams == {}
        counts = self._counts(db)
        assert counts["successes"] == 0
        assert counts["failures"] == 1

        summary = store.replay_pending(limit=10)
        assert summary["synced"] == 1
        assert summary["remaining_pending"] == 0
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1


class TestReplayDrain(TestValidation):
    """A backlog larger than one bounded replay batch must drain without a restart."""

    @staticmethod
    def _seed_pending(db_path, count):
        durable = HR.make_durable_store("sqlite", str(db_path))
        for i in range(count):
            payload = TestValidation._heartbeat(idempotency_key=f"backlog-{i:04d}")
            payload["received_at"] = HR.utc_now()
            payload["receiver_schema_version"] = HR.RECEIVER_SCHEMA_VERSION
            durable.persist(f"backlog-{i:04d}", payload, HR.encode_json(payload))
        return durable

    def test_backlog_larger_than_the_replay_limit_drains_in_batches(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        self._seed_pending(db, 300)
        fake = FakeRedis()
        store = HR.HeartbeatReceiverStore(
            fake,
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        assert store.health_snapshot()["durable_store"]["pending_records"] == 300

        # One bounded pass strands the remainder -- this is the defect.
        single = store.replay_pending(limit=256)
        assert single["synced"] == 256
        assert single["remaining_pending"] == 44

        # The draining loop finishes the job in the same process, no restart involved.
        drained = store.drain_pending(limit=256)
        assert drained["remaining_pending"] == 0
        assert drained["drained"] is True
        assert drained["batches"] >= 1
        assert store.health_snapshot()["durable_store"]["pending_records"] == 0
        assert self._counts(db)["successes"] == 300

    def test_drain_from_scratch_clears_a_300_row_backlog_with_bounded_batches(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        self._seed_pending(db, 300)
        store = HR.HeartbeatReceiverStore(
            FakeRedis(),
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        summary = store.drain_pending(limit=64)
        assert summary["attempted"] == 300
        assert summary["synced"] == 300
        assert summary["failed"] == 0
        assert summary["batches"] == 5
        assert summary["remaining_pending"] == 0

    def test_drain_stops_instead_of_spinning_while_the_cache_is_down(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        self._seed_pending(db, 10)
        fake = FakeRedis(failures_before_success=1000)
        store = HR.HeartbeatReceiverStore(
            fake,
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        summary = store.drain_pending(limit=4)
        assert summary["batches"] == 1
        assert summary["failed"] == 4
        assert summary["synced"] == 0
        assert summary["remaining_pending"] == 10
        assert summary["drained"] is False

    def test_the_background_worker_resumes_a_backlog_left_by_an_outage(self, tmp_path):
        db = tmp_path / "heartbeats.sqlite3"
        self._seed_pending(db, 300)
        store = HR.HeartbeatReceiverStore(
            FakeRedis(),
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        worker = HR.start_replay_worker(store, interval_s=0.01, limit=128)
        assert worker is not None
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                if store.health_snapshot()["durable_store"]["pending_records"] == 0:
                    break
                time.sleep(0.05)
        finally:
            worker.stop(timeout=5)
        assert store.health_snapshot()["durable_store"]["pending_records"] == 0

    def test_start_replay_worker_is_disabled_without_durability_or_interval(self, tmp_path):
        store = HR.HeartbeatReceiverStore(FakeRedis(), redis_target="fake:6379")
        assert HR.start_replay_worker(store, interval_s=5) is None
        durable_store = HR.HeartbeatReceiverStore(
            FakeRedis(),
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(tmp_path / "hb.sqlite3")),
        )
        assert HR.start_replay_worker(durable_store, interval_s=0) is None

    @staticmethod
    def _counts(db_path):
        return TestDurableSqlite._counts(db_path)


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
