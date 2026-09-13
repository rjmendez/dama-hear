"""The hear heartbeat receiver: schema guard, TTL write, and advisory event intake."""
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import hear_heartbeat_receiver as HR  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}
        self.sets = {}
        self.streams = {}

    def pipeline(self, transaction=False):
        assert transaction is False
        return self

    def setex(self, key, ttl, value):
        self.values[key] = value
        self.ttls[key] = ttl
        return self

    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)
        return self

    def set(self, key, value):
        self.values[key] = value
        return self

    def xadd(self, key, fields, maxlen=None, approximate=True):
        stream = self.streams.setdefault(key, [])
        stream.append(dict(fields))
        if maxlen is not None and len(stream) > maxlen:
            del stream[:-maxlen]
        assert approximate is True
        return str(len(stream))

    def execute(self):
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
            "gps": {"fix": 3},
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

    def test_unknown_event_type_is_rejected(self):
        with pytest.raises(HR.RequestError, match="event_type"):
            HR.validate_event_payload(self._event(event_type="surprise"))


@contextmanager
def running_server(auth_token=None, socket_timeout_s=0.2):
    fake = FakeRedis()
    store = HR.HeartbeatReceiverStore(fake, heartbeat_ttl_s=30, redis_target="fake:6379")
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

