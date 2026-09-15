"""The hear_node MQTT bridge: routes AWS-relayed telemetry into dama:hear:* Redis state."""
import json
import os
import sqlite3
import sys
import time

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
        st, fake = store
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_heartbeat(ts=1789256580000))).encode()
        record = B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats)
        assert record is not None and stats.accepted == 1 and stats.rejected == 0
        assert json.loads(fake.values["dama:hear:nyquist"])["ts"] == "2026-09-12T23:43:00Z"

    def test_a_fractional_millisecond_ts_is_stored_to_the_second(self, store):
        st, fake = store
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_heartbeat(ts=1789256580123.4))).encode()
        assert B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats) is not None
        assert json.loads(fake.values["dama:hear:nyquist"])["ts"] == "2026-09-12T23:43:00Z"

    @pytest.mark.parametrize("bad_ts", [1e30, -1e30])
    def test_an_unrepresentable_numeric_ts_is_rejected_without_raising(self, store, bad_ts):
        st, _fake = store
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_heartbeat(ts=bad_ts))).encode()
        assert B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats) is None
        assert stats.rejected == 1 and stats.accepted == 0
        raw_ok = json.dumps(_batch_ingest_wrapped(_heartbeat())).encode()
        assert B.dispatch_message(st, raw_ok, "dama/nyquist/telemetry", stats) is not None

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

    def test_duplicate_event_retry_is_idempotent_with_the_sqlite_outbox(self, tmp_path):
        db = tmp_path / "mqtt-bridge.sqlite3"
        fake = FakeRedis()
        st = HR.HeartbeatReceiverStore(
            fake,
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_event())).encode()
        assert B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats) is not None
        assert B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats) is not None
        with sqlite3.connect(db) as con:
            assert con.execute("SELECT COUNT(*) FROM durable_records").fetchone()[0] == 1
            assert con.execute(
                "SELECT COUNT(*) FROM cache_attempts WHERE outcome='succeeded'"
            ).fetchone()[0] == 1
        assert len(fake.streams["dama:hear:events"]) == 1


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

    def test_parse_args_accepts_durable_store_flags(self):
        args = B.parse_args([
            "--durable-store", "sqlite",
            "--durable-db", "/state/mqtt-bridge.sqlite3",
            "--durable-replay-limit", "17",
            "--durable-replay-interval-s", "2.5",
        ])
        assert args.durable_store == "sqlite"
        assert args.durable_db == "/state/mqtt-bridge.sqlite3"
        assert args.durable_replay_limit == 17
        assert args.durable_replay_interval_s == 2.5


class TestBackgroundReplayWorker:
    def test_a_failed_live_mqtt_write_is_replayed_after_redis_recovers_without_a_restart(
        self, tmp_path,
    ):
        # Blocker: hear_mqtt_bridge previously only replayed pending durable records once, at
        # process startup. A Redis outage that starts *after* that startup replay (as this test
        # simulates) must still self-heal while the bridge process stays up.
        db = tmp_path / "mqtt-bridge.sqlite3"
        fake = FakeRedis(failures_before_success=1)
        store = HR.HeartbeatReceiverStore(
            fake,
            heartbeat_ttl_s=30,
            redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_event())).encode()
        # This live dispatch hits the simulated outage. The write already reached the durable
        # ledger before the Redis attempt (see HeartbeatReceiverStore._write), so the record is
        # left pending regardless of how the caller handles this exception -- in the real bridge,
        # paho-mqtt's message-callback dispatch logs and swallows it, keeping the network loop
        # (and this worker) alive rather than crashing the process.
        with pytest.raises(RuntimeError, match="simulated redis outage"):
            B.dispatch_message(store, raw, "dama/nyquist/telemetry", stats)
        assert stats.rejected == 0 and stats.accepted == 0
        with sqlite3.connect(db) as con:
            assert con.execute(
                "SELECT COUNT(*) FROM cache_attempts WHERE outcome='failed'"
            ).fetchone()[0] == 1
            assert con.execute(
                "SELECT COUNT(*) FROM cache_attempts WHERE outcome='succeeded'"
            ).fetchone()[0] == 0

        worker = HR.DurableReplayWorker(store, interval_s=0.05, limit=8,
                                        name="test-mqtt-bridge-replay")
        try:
            assert worker.thread is not None
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
        finally:
            worker.stop()
        assert len(fake.streams[HR.EVENT_STREAM_KEY]) == 1
        assert not worker.is_alive

    def test_worker_is_disabled_for_the_default_none_durable_backend(self):
        store = HR.HeartbeatReceiverStore(FakeRedis(), heartbeat_ttl_s=30, redis_target="fake:6379")
        worker = HR.DurableReplayWorker(store, interval_s=0.05)
        assert worker.thread is None
        worker.stop()  # no-op, must not raise

    def test_worker_stop_leaves_no_thread_behind(self, tmp_path):
        db = tmp_path / "mqtt-bridge.sqlite3"
        store = HR.HeartbeatReceiverStore(
            FakeRedis(), heartbeat_ttl_s=30, redis_target="fake:6379",
            durable_store=HR.make_durable_store("sqlite", str(db)),
        )
        worker = HR.DurableReplayWorker(store, interval_s=0.02, limit=8)
        thread = worker.thread
        assert thread is not None and thread.is_alive()
        worker.stop()
        assert not thread.is_alive()
        assert worker.thread is None


    def test_parse_args_accepts_tls_and_mtls_flags(self):
        args = B.parse_args([
            "--ca-certs", "/tls/ca.crt",
            "--certfile", "/tls/tls.crt",
            "--keyfile", "/tls/tls.key",
            "--tls-insecure",
            "--tls-version", "tlsv1_3",
        ])
        assert args.ca_certs == "/tls/ca.crt"
        assert args.certfile == "/tls/tls.crt"
        assert args.keyfile == "/tls/tls.key"
        assert args.tls_insecure is True
        assert args.tls_version == "tlsv1_3"

    def test_parse_args_tls_defaults_are_disabled_plaintext(self):
        args = B.parse_args([])
        assert args.ca_certs is None
        assert args.certfile is None
        assert args.keyfile is None
        assert args.tls_insecure is False

    def test_parse_args_rejects_an_unknown_tls_version(self):
        with pytest.raises(SystemExit):
            B.parse_args(["--tls-version", "sslv3"])


class FakeTlsClient:
    """Stand-in for mqtt.Client that only records the TLS-relevant calls."""

    def __init__(self, *a, **k):
        self.tls_set_calls = []
        self.tls_insecure_calls = []

    def tls_set(self, **kwargs):
        self.tls_set_calls.append(kwargs)

    def tls_insecure_set(self, value):
        self.tls_insecure_calls.append(value)

    def subscribe(self, topic, qos=0):
        pass


class TestConfigureTls:
    def test_no_ca_certs_leaves_the_client_in_plaintext_mode(self):
        client = FakeTlsClient()
        enabled = B.configure_tls(client)
        assert enabled is False
        assert client.tls_set_calls == []
        assert client.tls_insecure_calls == []

    def test_ca_certs_alone_enables_server_authenticated_tls(self):
        client = FakeTlsClient()
        enabled = B.configure_tls(client, ca_certs="/tls/ca.crt")
        assert enabled is True
        assert client.tls_set_calls == [{
            "ca_certs": "/tls/ca.crt",
            "certfile": None,
            "keyfile": None,
            "tls_version": B.resolve_tls_version("tlsv1_2"),
        }]
        # Verification must stay ON unless explicitly disabled.
        assert client.tls_insecure_calls == [False]

    def test_ca_certs_plus_certfile_and_keyfile_enables_mtls(self):
        client = FakeTlsClient()
        enabled = B.configure_tls(
            client, ca_certs="/tls/ca.crt", certfile="/tls/tls.crt", keyfile="/tls/tls.key",
        )
        assert enabled is True
        assert client.tls_set_calls == [{
            "ca_certs": "/tls/ca.crt",
            "certfile": "/tls/tls.crt",
            "keyfile": "/tls/tls.key",
            "tls_version": B.resolve_tls_version("tlsv1_2"),
        }]

    def test_certfile_without_keyfile_is_rejected(self):
        client = FakeTlsClient()
        with pytest.raises(B.TlsConfigError):
            B.configure_tls(client, ca_certs="/tls/ca.crt", certfile="/tls/tls.crt")
        assert client.tls_set_calls == []

    def test_keyfile_without_certfile_is_rejected(self):
        client = FakeTlsClient()
        with pytest.raises(B.TlsConfigError):
            B.configure_tls(client, ca_certs="/tls/ca.crt", keyfile="/tls/tls.key")
        assert client.tls_set_calls == []

    def test_certfile_and_keyfile_without_ca_certs_is_rejected(self):
        client = FakeTlsClient()
        with pytest.raises(B.TlsConfigError):
            B.configure_tls(client, certfile="/tls/tls.crt", keyfile="/tls/tls.key")
        assert client.tls_set_calls == []

    def test_tls_insecure_true_is_honored_but_still_explicit(self):
        client = FakeTlsClient()
        enabled = B.configure_tls(client, ca_certs="/tls/ca.crt", tls_insecure=True)
        assert enabled is True
        assert client.tls_insecure_calls == [True]

    def test_tls_insecure_defaults_to_false(self):
        client = FakeTlsClient()
        B.configure_tls(client, ca_certs="/tls/ca.crt")
        assert client.tls_insecure_calls == [False]

    def test_an_unsupported_tls_version_is_rejected(self):
        client = FakeTlsClient()
        with pytest.raises(B.TlsConfigError):
            B.configure_tls(client, ca_certs="/tls/ca.crt", tls_version="sslv3")

    @pytest.mark.parametrize("name", ["tlsv1_2", "tlsv1.2", "TLSv1_2"])
    def test_tls_version_names_are_case_and_separator_insensitive(self, name):
        # Should not raise.
        assert B.resolve_tls_version(name) is not None


class TestMakeClientTlsWiring:
    def test_make_client_enables_tls_when_ca_certs_is_supplied(self, store, monkeypatch):
        st, _fake = store
        monkeypatch.setattr(B.mqtt, "Client", FakeTlsClient)
        client = B.make_client(st, topic="dama/+/telemetry", ca_certs="/tls/ca.crt")
        assert client.tls_enabled is True
        assert client.tls_set_calls

    def test_make_client_stays_plaintext_with_no_tls_flags(self, store, monkeypatch):
        st, _fake = store
        monkeypatch.setattr(B.mqtt, "Client", FakeTlsClient)
        client = B.make_client(st, topic="dama/+/telemetry")
        assert client.tls_enabled is False
        assert client.tls_set_calls == []

    def test_make_client_propagates_a_bad_mtls_configuration(self, store, monkeypatch):
        st, _fake = store
        monkeypatch.setattr(B.mqtt, "Client", FakeTlsClient)
        with pytest.raises(B.TlsConfigError):
            B.make_client(st, topic="dama/+/telemetry", certfile="/tls/tls.crt")

    def test_main_reports_a_bad_tls_config_as_a_clean_exit_code_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(B, "make_redis_client", lambda *a, **k: FakeRedis())
        exit_code = B.main([
            "--certfile", "/tls/tls.crt",
            "--durable-store", "none",
        ])
        assert exit_code == 2


class TestDeviceIdFromTopic:
    @pytest.mark.parametrize("topic,expected", [
        ("dama/nyquist/telemetry", "nyquist"),
        ("dama/pixel_7_pro/telemetry", "pixel_7_pro"),
    ])
    def test_extracts_the_device_id_segment(self, topic, expected):
        assert B.device_id_from_topic(topic) == expected

    @pytest.mark.parametrize("topic", [
        "dama/telemetry",
        "dama//telemetry",
        "dama/nyquist/telemetry/extra",
        "hear/nyquist/telemetry",
        "dama/nyquist/status",
        "",
    ])
    def test_returns_none_for_anything_that_is_not_the_expected_shape(self, topic):
        assert B.device_id_from_topic(topic) is None


class TestTopicPayloadDeviceIdValidation:
    def test_rejects_a_heartbeat_whose_payload_device_id_does_not_match_the_topic(self, store):
        st, fake = store
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_heartbeat(device_id="impostor"))).encode()
        record = B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats)
        assert record is None
        assert stats.impersonation == 1
        assert stats.accepted == 0
        assert "dama:hear:nyquist" not in fake.values
        assert "dama:hear:impostor" not in fake.values

    def test_rejects_an_event_whose_payload_device_id_does_not_match_the_topic(self, store):
        st, fake = store
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_event(device_id="impostor"))).encode()
        record = B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats)
        assert record is None
        assert stats.impersonation == 1
        assert not fake.streams.get("dama:hear:events")

    def test_accepts_when_topic_and_payload_device_id_agree(self, store):
        st, _fake = store
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_heartbeat(device_id="nyquist"))).encode()
        record = B.dispatch_message(st, raw, "dama/nyquist/telemetry", stats)
        assert record is not None
        assert stats.impersonation == 0
        assert stats.accepted == 1

    def test_a_topic_shape_this_bridge_cannot_parse_does_not_block_processing(self, store):
        # If the topic doesn't match dama/<device_id>/telemetry we can't cross-check it;
        # fall back to trusting the schema-validated payload rather than dropping everything.
        st, _fake = store
        stats = B.BridgeStats()
        raw = json.dumps(_batch_ingest_wrapped(_heartbeat())).encode()
        record = B.dispatch_message(st, raw, "unexpected/topic/shape", stats)
        assert record is not None
        assert stats.impersonation == 0

    def test_a_mismatch_does_not_block_the_next_legitimate_message(self, store):
        st, fake = store
        stats = B.BridgeStats()
        bad = json.dumps(_batch_ingest_wrapped(_heartbeat(device_id="impostor"))).encode()
        B.dispatch_message(st, bad, "dama/nyquist/telemetry", stats)
        good = json.dumps(_batch_ingest_wrapped(_heartbeat())).encode()
        record = B.dispatch_message(st, good, "dama/nyquist/telemetry", stats)
        assert record is not None
        assert stats.impersonation == 1
        assert stats.accepted == 1
        assert "dama:hear:nyquist" in fake.values
