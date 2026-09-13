"""The hear heartbeat receiver: schema guard, TTL write, and advisory event intake."""
import json
import os
import sys
import threading
import urllib.error
import urllib.request

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


@pytest.fixture()
def running_server():
    fake = FakeRedis()
    store = HR.HeartbeatReceiverStore(fake, heartbeat_ttl_s=30, redis_target="fake:6379")
    server = HR.create_server("127.0.0.1", 0, store, max_body_bytes=2048)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", fake
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


class TestHttpIntegration(TestValidation):
    @staticmethod
    def _post(url, payload):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=2)

    def test_posting_a_heartbeat_sets_the_ttl_key(self, running_server):
        base_url, fake = running_server
        with self._post(base_url + "/api/hear/heartbeat", self._heartbeat()) as resp:
            assert resp.status == 204
            assert resp.read() == b""
        body = json.loads(fake.values["dama:hear:nyquist"])
        assert fake.ttls["dama:hear:nyquist"] == 30
        assert body["device_id"] == "nyquist"
        assert body["received_at"].endswith("Z")
        assert fake.sets["dama:hear:devices"] == {"nyquist"}
        assert json.loads(fake.values["dama:hear:latest"])["device_id"] == "nyquist"

    def test_posting_an_event_updates_the_advisory_stream(self, running_server):
        base_url, fake = running_server
        with self._post(base_url + "/api/hear/event", self._event()) as resp:
            assert resp.status == 204
        latest = json.loads(fake.values["dama:hear:event:nyquist"])
        assert latest["event_type"] == "clip_written"
        stream = fake.streams[HR.EVENT_STREAM_KEY]
        assert len(stream) == 1
        assert stream[0]["device_id"] == "nyquist"
        assert json.loads(stream[0]["payload"])["event_seq"] == 234

    def test_a_bad_payload_is_rejected_and_does_not_touch_redis(self, running_server):
        base_url, fake = running_server
        with pytest.raises(urllib.error.HTTPError) as ei:
            self._post(base_url + "/api/hear/heartbeat", self._heartbeat(device_id=""))
        assert ei.value.code == 400
        assert fake.values == {}
        assert fake.ttls == {}
        assert fake.sets == {}

    def test_healthz_reports_the_service_without_hitting_redis(self, running_server):
        base_url, _fake = running_server
        with urllib.request.urlopen(base_url + "/healthz", timeout=2) as resp:
            assert resp.status == 200
            body = json.loads(resp.read().decode("utf-8"))
        assert body["service"] == "hear-heartbeat-receiver"
        assert body["redis_target"] == "fake:6379"
