"""The hear_node MQTT bridge: routes AWS-relayed telemetry into dama:hear:* Redis state."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import hear_heartbeat_receiver as HR  # noqa: E402
from tools import hear_mqtt_bridge as B  # noqa: E402
from tests.test_hear_heartbeat_receiver import FakeRedis  # noqa: E402


def _heartbeat(**overrides):
    payload = {
        "telemetry_path": "hear/heartbeat",
        "telemetry_schema_version": 1,
        "device_id": "nyquist",
        "ts": "2026-09-12T23:43:00Z",
        "ts_ms": 1789256580000,
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


def _event(**overrides):
    payload = {
        "telemetry_path": "hear/event",
        "telemetry_schema_version": 1,
        "device_id": "nyquist",
        "ts": "2026-09-12T23:43:04Z",
        "ts_ms": 1789256584000,
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


def _batch_ingest_wrapped(payload):
    # What actually arrives over MQTT: batch_ingest's per-message normalization has added
    # device_id (already present here), idempotency_key, batch_idempotency_key and
    # ingest_trace_id, and replaced any "ts" with a numeric "ts_ms" it derived itself. The
    # bridge must accept this shape, not just the receiver's own bare test fixtures.
    body = dict(payload)
    body["idempotency_key"] = "deadbeef" * 8
    body["batch_idempotency_key"] = "cafebabe" * 8
    body["ingest_trace_id"] = "11111111-2222-3333-4444-555555555555"
    return body


@pytest.fixture
def store():
    fake = FakeRedis()
    return HR.HeartbeatReceiverStore(fake, heartbeat_ttl_s=30, redis_target="fake:6379"), fake


class TestDispatchMessage:
    def test_accepts_a_heartbeat_wrapped_the_way_batch_ingest_actually_sends_it(self, store):
        st, fake = store
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_heartbeat())).encode()
        record = B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats)
        assert record is not None
        assert record["device_id"] == "nyquist"
        assert stats.accepted == 1
        assert "dama:hear:nyquist" in fake.values
        assert "nyquist" in fake.sets["dama:hear:devices"]

    def test_accepts_an_event_and_writes_the_stream(self, store):
        st, fake = store
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_event())).encode()
        record = B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats)
        assert record is not None
        assert stats.accepted == 1
        assert fake.streams["dama:hear:events"][0]["device_id"] == "nyquist"

    def test_ignores_a_foreign_devices_telemetry_on_the_same_topic_pattern(self, store):
        # The Oxalis consumer republishes ANY device's telemetry to dama/<id>/telemetry --
        # a phone's payload has no telemetry_path field at all and must be skipped quietly.
        st, _fake = store
        stats = B.BridgeStats()
        raw = json.dumps({"device_id": "pixel_7_pro", "sensor": "gnss", "lat": 1.0}).encode()
        record = B.dispatch_message(st, raw, "dama/pixel_7_pro/telemetry", stats)
        assert record is None
        assert stats.ignored == 1
        assert stats.accepted == 0

    def test_drops_malformed_json_without_raising(self, store):
        st, _fake = store
        stats = B.BridgeStats()
        record = B.dispatch_message(st, b"{not json", "dama/nyquist/telemetry", stats)
        assert record is None
        assert stats.malformed == 1

    def test_drops_a_non_object_payload_without_raising(self, store):
        st, _fake = store
        stats = B.BridgeStats()
        record = B.dispatch_message(st, b"[1,2,3]", "dama/nyquist/telemetry", stats)
        assert record is None
        assert stats.malformed == 1

    def test_rejects_a_schema_violation_without_raising(self, store):
        st, _fake = store
        stats = B.BridgeStats()
        bad = _batch_ingest_wrapped(_heartbeat(counters={"scene_rows_written": 1}))
        record = B.dispatch_message(st, json.dumps(bad).encode(), "dama/nyquist/telemetry", stats)
        assert record is None
        assert stats.rejected == 1

    def test_a_numeric_millisecond_ts_is_converted_to_rfc3339_and_accepted(self, store):
        # What the forwarder actually sends: "ts" normalised to numeric milliseconds. The receiver
        # contract wants an RFC3339 string, and without the conversion every message was rejected.
        st, fake = store
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_heartbeat(ts=1789256580000))).encode()
        record = B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats)
        assert record is not None and stats.accepted == 1 and stats.rejected == 0
        assert json.loads(fake.values["dama:hear:nyquist"])["ts"] == "2026-09-12T23:43:00Z"

    def test_a_boolean_ts_is_not_taken_for_a_number(self, store):
        st, _fake = store
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_heartbeat(ts=True))).encode()
        assert B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats) is None
        assert stats.rejected == 1

    def test_a_bad_message_does_not_block_the_next_good_one(self, store):
        st, fake = store
        stats = B.BridgeStats()
        B.dispatch_message(st, b"{not json", "dama/nyquist/telemetry", stats)
        raw = json.dumps(_batch_ingest_wrapped(_heartbeat())).encode()
        record = B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats)
        assert record is not None
        assert stats.malformed == 1
        assert stats.accepted == 1


class TestMakeClient:
    def test_subscribes_to_the_configured_topic_on_connect(self, store, monkeypatch):
        st, _fake = store
        calls = []

        class FakeMqttClient:
            def __init__(self, *a, **k):
                pass

            def subscribe(self, topic, qos=0):
                calls.append((topic, qos))

        monkeypatch.setattr(B.mqtt, "Client", FakeMqttClient)
        client = B.make_client(st, topic="dama/+/telemetry")
        client.on_connect(client, None, {}, 0)
        assert calls == [("dama/+/telemetry", 1)]

    def test_a_nonzero_connect_rc_does_not_subscribe(self, store, monkeypatch):
        st, _fake = store
        calls = []

        class FakeMqttClient:
            def __init__(self, *a, **k):
                pass

            def subscribe(self, topic, qos=0):
                calls.append((topic, qos))

        monkeypatch.setattr(B.mqtt, "Client", FakeMqttClient)
        client = B.make_client(st, topic="dama/+/telemetry")
        client.on_connect(client, None, {}, 5)
        assert calls == []

    def test_on_message_dispatches_through_to_the_store(self, store, monkeypatch):
        st, fake = store

        class FakeMqttClient:
            def __init__(self, *a, **k):
                pass

            def subscribe(self, topic, qos=0):
                pass

        monkeypatch.setattr(B.mqtt, "Client", FakeMqttClient)
        client = B.make_client(st, topic="dama/+/telemetry")

        class FakeMsg:
            topic = "dama/nyquist/telemetry"
            payload = json.dumps(_batch_ingest_wrapped(_heartbeat())).encode()

        client.on_message(client, None, FakeMsg())
        assert "dama:hear:nyquist" in fake.values
        assert client.stats.accepted == 1
