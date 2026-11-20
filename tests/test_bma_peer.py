"""Synthetic MQTT/SQLite contract tests; no broker or field validation is implied."""
from copy import deepcopy
import json
import logging
import sqlite3
import ssl
import sys
import threading
import time
import types

import pytest

from hear.ingest.bma_peer import (
    BmaIdentityConflict,
    BmaPeerError,
    BmaPeerStore,
    BmaStorageFull,
    BmaValidationError,
    decode_message,
)
from hear.ingest.bma_fleet import convert_fleet
from tools import bma_peer_receiver as receiver


@pytest.fixture
def record():
    observation = {
        "unit_id": "bma1",
        "det_id": "bma1:12:34",
        "source_time_wall_s": 1_795_000_000.125,
        "origin_id": "origin-a",
        "pose": {
            "origin_id": "origin-a",
            "position": {"e_m": 1.0, "n_m": 2.0, "u_m": 3.0, "sigma_m": 0.1},
            "orientation": {
                "heading_deg": 90.0, "pitch_deg": 0.0, "roll_deg": 0.0,
                "port_normal": "+z", "sigma_heading_deg": 2.0,
            },
            "source": "synthetic fixture",
            "surveyed_utc": "2026-01-01T00:00:00Z",
        },
        "bearing_deg_true": 90.0,
        "sigma_deg": 3.0,
        "clock_precise": False,
        "evidence": "unvalidated",
    }
    return {
        "device_id": "bma-bma1",
        "ts_ms": 1_795_000_000_125,
        "telemetry_path": "bma/bearing",
        "schema": "bma.bearing.v1",
        "observation": observation,
        "site_id": "yard-a",
    }


def wire(record):
    return json.dumps(record, separators=(",", ":"), allow_nan=False).encode("utf-8")


def forwarded(record, trace_id="11111111-2222-3333-4444-555555555555"):
    payload = deepcopy(record)
    payload["ts"] = payload["ts_ms"]
    payload["idempotency_key"] = "a" * 64
    payload["batch_idempotency_key"] = "b" * 64
    payload["ingest_trace_id"] = trace_id
    return payload


def test_stores_and_replays_exact_unvalidated_observation(record, tmp_path):
    store = BmaPeerStore(tmp_path / "bma.sqlite3")
    raw = wire(record)
    assert store.accept(raw, "dama/bma-bma1/telemetry", expected_site_id="yard-a",
                        now_ms=1000) == {
        "status": "stored", "origin_id": "origin-a",
        "device_id": "bma-bma1", "det_id": "bma1:12:34",
    }
    assert store.accept(raw, "dama/bma-bma1/telemetry", expected_site_id="yard-a",
                        now_ms=1001) == {
        "status": "duplicate", "origin_id": "origin-a",
        "device_id": "bma-bma1", "det_id": "bma1:12:34",
    }
    rows = store.list_observations(now_ms=1001)
    assert len(rows) == 1
    assert rows[0] == {"received_at_ms": 1000, **record}
    assert rows[0]["observation"]["clock_precise"] is False
    assert rows[0]["observation"]["evidence"] == "unvalidated"
    assert store.counts(now_ms=1001) == {"records": 1, "stored_bytes": len(wire(record)) + 64}


@pytest.mark.parametrize(("path", "value", "message"), [
    (("schema",), "bma.tdoa.v1", "unsupported BMA schema"),
    (("telemetry_path",), "hear/event", "unsupported BMA telemetry_path"),
    (("device_id",), "bma-bma2", "device_id conflicts"),
    (("ts_ms",), 1_795_000_000_126, "ts_ms conflicts"),
    (("observation", "unit_id"), "bma2", "det_id does not match"),
    (("observation", "clock_precise"), True, "clock_precise must remain false"),
    (("observation", "evidence"), "validated", "evidence must remain unvalidated"),
    (("observation", "pose", "origin_id"), "other-origin", "pose origin_id conflicts"),
])
def test_contract_and_claim_mismatches_are_rejected_without_storage(
        record, tmp_path, path, value, message):
    store = BmaPeerStore(tmp_path / "bma.sqlite3")
    bad = deepcopy(record)
    target = bad
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert len(store.list_observations(now_ms=1000)) == 0
    with pytest.raises(BmaValidationError, match=message):
        store.accept(wire(bad), "dama/bma-bma1/telemetry", now_ms=1000)
    assert store.counts(now_ms=1000) == {"records": 0, "stored_bytes": 0}
    assert store.accept(wire(record), "dama/bma-bma1/telemetry", now_ms=1000)["status"] == "stored"


@pytest.mark.parametrize(("topic", "expected_site", "message"), [
    ("dama/bma-bma2/telemetry", None, "topic device_id conflicts"),
    ("dama/BMA-BMA1/telemetry", None, "topic device_id is not normalized"),
    ("dama/bma-bma1/telemetry", "yard-b", "does not match configured site_id"),
    ("dama/bma-bma1/other", None, "topic must be dama"),
])
def test_topic_identity_and_site_are_checked(record, topic, expected_site, message):
    assert decode_message(wire(record), "dama/bma-bma1/telemetry") == record
    with pytest.raises(BmaValidationError, match=message):
        decode_message(wire(record), topic, expected_site_id=expected_site)


def test_oxalis_normalizes_underscore_device_id_like_canonical_consumer(record):
    assert decode_message(wire(record), "dama/bma-bma1/telemetry") == record
    underscored = deepcopy(record)
    underscored["device_id"] = "bma-bma_west"
    underscored["observation"]["unit_id"] = "bma_west"
    underscored["observation"]["det_id"] = "bma_west:12:34"
    assert decode_message(wire(underscored), "dama/bma-bma-west/telemetry") == underscored


def test_aws_forwarded_metadata_is_validated_preserved_and_excluded_from_dedup(record, tmp_path):
    first = forwarded(record)
    second = forwarded(record, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert decode_message(wire(first), "dama/bma-bma1/telemetry") == first
    store = BmaPeerStore(tmp_path / "bma.sqlite3")
    assert store.accept(wire(first), "dama/bma-bma1/telemetry", now_ms=1000)["status"] == "stored"
    assert store.accept(wire(second), "dama/bma-bma1/telemetry", now_ms=1001)["status"] == "duplicate"
    stored = store.list_observations(now_ms=1001)[0]
    assert stored["ingest_trace_id"] == first["ingest_trace_id"]
    assert stored["idempotency_key"] == first["idempotency_key"]
    assert stored["batch_idempotency_key"] == first["batch_idempotency_key"]
    assert stored["ts"] == first["ts"]
    assert stored["observation"] == record["observation"]
    assert store.counts(now_ms=1001)["records"] == 1


@pytest.mark.parametrize(("key", "value", "message"), [
    ("idempotency_key", "bad", "idempotency_key is invalid"),
    ("batch_idempotency_key", 12, "batch_idempotency_key is invalid"),
    ("ingest_trace_id", "not-a-uuid", "ingest_trace_id is invalid"),
    ("ts", 1_795_000_000_126, "AWS-normalized ts conflicts"),
])
def test_invalid_aws_forwarder_metadata_is_refused(record, key, value, message):
    payload = forwarded(record)
    assert decode_message(wire(payload), "dama/bma-bma1/telemetry") == payload
    payload[key] = value
    with pytest.raises(BmaValidationError, match=message):
        decode_message(wire(payload), "dama/bma-bma1/telemetry")


def test_duplicate_identity_with_changed_content_is_not_overwritten(record, tmp_path):
    store = BmaPeerStore(tmp_path / "bma.sqlite3")
    assert store.accept(wire(record), "dama/bma-bma1/telemetry", now_ms=1000)["status"] == "stored"
    changed = deepcopy(record)
    changed["observation"]["bearing_deg_true"] = 91.0
    with pytest.raises(BmaIdentityConflict, match="already exists with different content"):
        store.accept(wire(changed), "dama/bma-bma1/telemetry", now_ms=1001)
    assert store.list_observations(now_ms=1001)[0]["observation"]["bearing_deg_true"] == 90.0
    assert store.counts(now_ms=1001)["records"] == 1


def test_full_store_rejects_new_record_but_acknowledges_replay(record, tmp_path):
    store = BmaPeerStore(tmp_path / "bma.sqlite3", max_records=1, max_storage_bytes=8192,
                         max_message_bytes=4096)
    assert store.accept(wire(record), "dama/bma-bma1/telemetry", now_ms=1000)["status"] == "stored"
    assert store.accept(wire(record), "dama/bma-bma1/telemetry", now_ms=1001)["status"] == "duplicate"
    second = deepcopy(record)
    second["observation"]["det_id"] = "bma1:12:35"
    with pytest.raises(BmaStorageFull, match="capacity is full"):
        store.accept(wire(second), "dama/bma-bma1/telemetry", now_ms=1002)
    assert store.counts(now_ms=1002)["records"] == 1


def test_retention_reclaims_expired_rows_before_capacity_check(record, tmp_path):
    store = BmaPeerStore(
        tmp_path / "bma.sqlite3", max_records=1, max_storage_bytes=8192, retention_s=5,
        max_message_bytes=4096)
    assert store.accept(wire(record), "dama/bma-bma1/telemetry", now_ms=1000)["status"] == "stored"
    second = deepcopy(record)
    second["observation"]["det_id"] = "bma1:12:35"
    second["ts_ms"] = 1_795_000_000_250
    second["observation"]["source_time_wall_s"] = 1_795_000_000.25
    assert store.accept(wire(second), "dama/bma-bma1/telemetry", now_ms=6001)["status"] == "stored"
    assert [row["observation"]["det_id"] for row in
            store.list_observations(now_ms=6001)] == ["bma1:12:35"]


@pytest.mark.parametrize("raw", [
    b'{"a":1,"a":2}',
    b'{"schema":"bma.bearing.v1","x":NaN}',
    b"\xff",
    b"[]",
    b'{"large":' + b"9" * 5000 + b"}",
], ids=["duplicate-keys", "nonfinite", "invalid-utf8", "nonobject", "huge-integer"])
def test_malformed_json_is_refused(raw, record):
    with pytest.raises(BmaValidationError):
        decode_message(raw, "dama/bma-bma1/telemetry")
    assert decode_message(wire(record), "dama/bma-bma1/telemetry") == record


def test_pose_provenance_whitespace_matches_converter_without_rewriting(record, tmp_path):
    pose = record["observation"]["pose"]
    pose["source"] = " synthetic survey "
    pose["surveyed_utc"] = " 2026-01-01T00:00:00Z "
    observation = record["observation"]
    origin = {"origin_id": observation["origin_id"], "lat": 38.0, "lon": -77.0,
              "alt_m": 42.0, "datum": "WGS84", "north": "true"}
    fleet = {
        "v": 1, "k": "fleet", "updated_s": observation["source_time_wall_s"],
        "origin_id": observation["origin_id"],
        "units": [{"unit_id": observation["unit_id"], "pose_state": "surveyed",
                   "pose": pose, "ambiguous": False}],
        "rays": [{
            "unit_id": observation["unit_id"], "det_id": observation["det_id"],
            "t_wall_s": observation["source_time_wall_s"], "kind": "azel",
            "sigma_deg": observation["sigma_deg"], "az_deg_local": 0.0,
            "el_deg_local": 0.0, "bearing_deg_world": 90.0,
            "mirror_bearings_deg_world": None, "ambiguous": False,
        }],
    }
    converted = convert_fleet(fleet, origin)
    assert converted["refusals"] == []
    assert converted["observations"][0]["pose"]["source"] == " synthetic survey "
    assert converted["observations"][0]["pose"]["surveyed_utc"] == " 2026-01-01T00:00:00Z "
    assert decode_message(wire(record), "dama/bma-bma1/telemetry") == record
    store = BmaPeerStore(tmp_path / "bma.sqlite3")
    assert store.accept(wire(record), "dama/bma-bma1/telemetry", now_ms=1000)["status"] == "stored"
    stored_pose = store.list_observations(now_ms=1000)[0]["observation"]["pose"]
    assert stored_pose["source"] == " synthetic survey "
    assert stored_pose["surveyed_utc"] == " 2026-01-01T00:00:00Z "
    pose["source"] = "   "
    with pytest.raises(BmaValidationError, match="pose.source must be nonempty text"):
        decode_message(wire(record), "dama/bma-bma1/telemetry")


@pytest.mark.parametrize("field", ["source", "surveyed_utc"])
def test_pose_provenance_rejects_lone_surrogate_and_preserves_valid_unicode(
        record, tmp_path, field):
    store = BmaPeerStore(tmp_path / "bma.sqlite3")
    invalid = deepcopy(record)
    invalid["observation"]["pose"][field] = "fixture\ud800value"
    with pytest.raises(BmaValidationError, match=r"must be valid UTF-8 text"):
        store.accept(wire(invalid), "dama/bma-bma1/telemetry", now_ms=1000)
    assert store.counts(now_ms=1000) == {"records": 0, "stored_bytes": 0}

    valid = deepcopy(record)
    valid_value = "fixture café value"
    valid["observation"]["pose"][field] = valid_value
    assert store.accept(
        wire(valid), "dama/bma-bma1/telemetry", now_ms=1000)["status"] == "stored"
    assert store.list_observations(now_ms=1000)[0]["observation"]["pose"][field] == valid_value


def test_unrelated_message_probe_handles_huge_integer_and_valid_twin():
    unrelated = json.dumps(
        {"device_id": "phone-7", "schema": "phone.telemetry.v1"}).encode()
    oversized_integer = b'{"x":' + b"9" * 5000 + b"}"
    assert receiver._looks_like_bma_message(unrelated) is False
    assert receiver._looks_like_bma_message(oversized_integer) is False


def test_payload_and_query_limits_are_finite(record, tmp_path):
    store = BmaPeerStore(tmp_path / "bma.sqlite3", max_message_bytes=256,
                         max_storage_bytes=2048)
    with pytest.raises(BmaValidationError, match="byte limit"):
        store.accept(wire(record), "dama/bma-bma1/telemetry", now_ms=1)
    with pytest.raises(BmaPeerError, match="limit must be"):
        BmaPeerStore(tmp_path / "bma.sqlite3").list_observations(limit=1001)
    assert store.counts() == {"records": 0, "stored_bytes": 0}


def test_verified_tls_policy_and_loopback_exception(tmp_path, monkeypatch):
    class FakeContext:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True

    class FakeClient:
        def __init__(self):
            self.context = None
            self.insecure = None

        def tls_set_context(self, context):
            self.context = context

        def tls_insecure_set(self, value):
            self.insecure = value

    client = FakeClient()
    assert receiver.configure_tls(client, "127.0.0.1") is False
    with pytest.raises(receiver.TlsConfigError, match="require --ca-file"):
        receiver.configure_tls(client, "broker.example")
    invalid_ca = tmp_path / "bad-ca.pem"
    invalid_ca.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(receiver.TlsConfigError, match="cannot load a valid CA"):
        receiver.configure_tls(client, "broker.example", str(invalid_ca))
    ca = tmp_path / "ca.pem"
    ca.write_text("fixture CA path", encoding="utf-8")
    monkeypatch.setattr(receiver.ssl, "create_default_context",
                        lambda purpose, cafile: FakeContext())
    assert receiver.configure_tls(client, "broker.example", str(ca)) is True
    assert isinstance(client.context, FakeContext)
    assert client.insecure is False


def test_paho_is_optional_and_missing_dependency_is_clear(monkeypatch):
    monkeypatch.setitem(sys.modules, "paho", None)
    with pytest.raises(RuntimeError, match="requires optional dependency paho-mqtt"):
        receiver.load_paho_client("test")


def test_subscriber_filters_unrelated_topics_and_acks_only_after_commit(
        record, tmp_path, monkeypatch, caplog):
    class FakeClient:
        def __init__(self):
            self.acks = []
            self.records_at_ack = []
            self.disconnects = 0

        def ack(self, mid, qos):
            self.acks.append((mid, qos))
            self.records_at_ack.append(self.store.counts()["records"])
            return 0

        def disconnect(self):
            self.disconnects += 1
            return 0

    fake = FakeClient()
    monkeypatch.setattr(receiver, "load_paho_client", lambda client_id: fake)
    monkeypatch.setattr(receiver, "configure_tls", lambda client, host, ca_file: False)
    store = BmaPeerStore(tmp_path / "bma.sqlite3", max_records=1, max_storage_bytes=8192,
                         max_message_bytes=4096)
    fake.store = store
    client = receiver.make_client(store, host="127.0.0.1", port=1883,
                                  topic="dama/+/telemetry", client_id="test")
    unrelated = types.SimpleNamespace(
        payload=json.dumps({"device_id": "phone-7", "schema": "phone.telemetry.v1"}).encode(),
        topic="dama/phone-7/telemetry", mid=5, qos=1)
    client.on_message(client, None, unrelated)
    assert fake.acks == [(5, 1)]
    assert "refused BMA MQTT message" not in caplog.text
    misrouted = types.SimpleNamespace(
        payload=wire(record), topic="dama/phone-7/telemetry", mid=6, qos=1)
    client.on_message(client, None, misrouted)
    assert fake.acks == [(5, 1), (6, 1)]
    assert "topic device_id conflicts" in caplog.text
    assert store.counts()["records"] == 0
    message = types.SimpleNamespace(
        payload=wire(record), topic="dama/bma-bma1/telemetry", mid=7, qos=1)
    client.on_message(client, None, message)
    assert fake.acks == [(5, 1), (6, 1), (7, 1)]
    assert fake.records_at_ack == [0, 0, 1]
    assert store.counts()["records"] == 1
    message.mid = 8
    message.payload = wire({**record, "observation": {
        **record["observation"], "det_id": "bma1:12:35"}})
    client.on_message(client, None, message)
    assert fake.acks == [(5, 1), (6, 1), (7, 1)]
    assert fake.disconnects == 1
    assert client.retry_requested.is_set()
    assert store.counts()["records"] == 1
    client.retry_requested.clear()
    with sqlite3.connect(store.path) as connection:
        connection.execute("DELETE FROM bma_observations WHERE det_id='bma1:12:34'")
    client.on_message(client, None, message)
    assert fake.acks == [(5, 1), (6, 1), (7, 1), (8, 1)]
    assert fake.records_at_ack == [0, 0, 1, 1]
    assert store.counts()["records"] == 1
    message.mid = 9
    message.payload = wire({**record, "schema": "bma.tdoa.v1"})
    client.on_message(client, None, message)
    assert fake.acks == [(5, 1), (6, 1), (7, 1), (8, 1), (9, 1)]
    assert store.counts()["records"] == 1


@pytest.mark.parametrize("qos", [0, 1])
def test_subscriber_storage_failure_distinguishes_qos(
        record, tmp_path, monkeypatch, caplog, qos):
    acks = []
    disconnects = []
    store = BmaPeerStore(tmp_path / "bma.sqlite3", max_records=1)

    def ack(mid, qos):
        acks.append((mid, qos, store.counts()["records"]))
        return 0

    fake = types.SimpleNamespace(ack=ack, disconnect=lambda: disconnects.append(True))
    monkeypatch.setattr(receiver, "load_paho_client", lambda client_id: fake)
    topic = "dama/bma-bma1/telemetry"
    client = receiver.make_client(store, host="127.0.0.1", port=1883,
                                  topic="dama/+/telemetry", client_id="test")
    caplog.set_level(logging.INFO, logger=receiver.LOG.name)
    client.on_message(client, None, types.SimpleNamespace(
        payload=wire(record), topic=topic, mid=7, qos=qos))
    assert store.list_observations()[0]["observation"] == record["observation"]
    expected_acks = [(7, 1, 1)] if qos == 1 else []
    assert acks == expected_acks
    assert ("(best-effort QoS 0 delivery)" in caplog.text) is (qos == 0)
    caplog.clear()

    record["observation"]["det_id"] = "bma1:12:35"
    client.on_message(client, None, types.SimpleNamespace(
        payload=wire(record), topic=topic, mid=8, qos=qos))
    assert acks == expected_acks
    assert [row["observation"]["det_id"] for row in store.list_observations()] == ["bma1:12:34"]
    assert disconnects == ([True] if qos == 1 else [])
    assert client.retry_requested.is_set() is (qos == 1)
    capacity_error = "configured BMA observation capacity is full"
    expected_error = (
        f"BMA MQTT store unavailable; leaving delivery unacknowledged: {capacity_error}"
        if qos == 1 else
        f"lost QoS 0 BMA MQTT delivery on {topic}: storage failed; no broker redelivery: "
        f"{capacity_error}"
    )
    assert [entry.getMessage() for entry in caplog.records] == [expected_error]


@pytest.mark.parametrize(("grant", "failed"), [(1, False), (0, True), (128, True)])
def test_subscriber_requires_qos_one_suback_and_logs_only_granted_subscription(
        record, tmp_path, monkeypatch, caplog, grant, failed):
    class FakeClient:
        def __init__(self):
            self.disconnects = 0
            self.subscriptions = []

        def subscribe(self, topic, qos):
            self.subscriptions.append((topic, qos))
            return 0, 12

        def disconnect(self):
            self.disconnects += 1
            return 0

    fake = FakeClient()
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(receiver, "load_paho_client", lambda client_id: fake)
    monkeypatch.setattr(receiver, "configure_tls", lambda client, host, ca_file: False)
    store = BmaPeerStore(tmp_path / "bma.sqlite3")
    client = receiver.make_client(
        store, host="127.0.0.1", port=1883, topic="dama/+/telemetry",
        client_id="test")

    client.on_connect(client, None, {}, 0)
    assert fake.subscriptions == [("dama/+/telemetry", 1)]
    assert "subscribed to" not in caplog.text
    client.on_subscribe(client, None, 12, [grant])
    assert client.subscription_failed.is_set() is failed
    assert fake.disconnects == (1 if failed else 0)
    assert ("subscribed to dama/+/telemetry with QoS 1" in caplog.text) is not failed


@pytest.mark.parametrize(("grant", "failed"), [(1, False), (0, True), (128, True)])
def test_actual_paho_v2_dispatches_suback_from_fake_socket(
        tmp_path, monkeypatch, grant, failed):
    mqtt = pytest.importorskip("paho.mqtt.client")
    if not hasattr(mqtt, "CallbackAPIVersion"):
        pytest.skip("installed Paho has no version-2 callback API")

    monkeypatch.setattr(receiver, "configure_tls", lambda client, host, ca_file: False)
    client = receiver.make_client(
        BmaPeerStore(tmp_path / "bma.sqlite3"), host="127.0.0.1", port=1883,
        topic="dama/+/telemetry", client_id="actual-paho-test")

    class FakeSocket:
        def __init__(self, packet):
            self.packet = packet

        def recv(self, size):
            data, self.packet = self.packet[:size], self.packet[size:]
            return data

        def close(self):
            pass

    client._sock = FakeSocket(bytes((0x90, 0x03, 0x00, 0x01, grant)))
    assert client.loop_read() == mqtt.MQTTErrorCode.MQTT_ERR_SUCCESS
    assert client.subscription_failed.is_set() is failed


def test_paho_uses_a_stable_persistent_manual_ack_session(monkeypatch):
    client_module = types.ModuleType("paho.mqtt.client")
    created = {}

    class FakePahoClient:
        def __init__(self, **kwargs):
            created.update(kwargs)

    client_module.Client = FakePahoClient
    client_module.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    mqtt_module = types.ModuleType("paho.mqtt")
    mqtt_module.__path__ = []
    mqtt_module.client = client_module
    paho_module = types.ModuleType("paho")
    paho_module.__path__ = []
    paho_module.mqtt = mqtt_module
    monkeypatch.setitem(sys.modules, "paho", paho_module)
    monkeypatch.setitem(sys.modules, "paho.mqtt", mqtt_module)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", client_module)
    receiver.load_paho_client("stable-bma-peer")
    assert created == {
        "client_id": "stable-bma-peer", "clean_session": False, "manual_ack": True,
        "callback_api_version": 2,
    }


def test_receiver_rejects_an_empty_persistent_session_client_id(tmp_path, monkeypatch):
    class FakeClient:
        pass

    client = FakeClient()
    monkeypatch.setattr(receiver, "load_paho_client", lambda client_id: client)
    monkeypatch.setattr(receiver, "configure_tls", lambda mqtt_client, host, ca_file: False)
    store = BmaPeerStore(tmp_path / "bma.sqlite3")
    with pytest.raises(ValueError, match="client ID must be stable"):
        receiver.make_client(store, host="127.0.0.1", port=1883,
                             topic="dama/+/telemetry", client_id="")
    assert receiver.make_client(
        store, host="127.0.0.1", port=1883, topic="dama/+/telemetry",
        client_id="stable-bma-peer") is client


def test_run_reconnects_with_capped_backoff_after_storage_failure(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.retry_requested = threading.Event()
            self.subscription_failed = threading.Event()
            self.connects = 0
            self.loop_count = 0

        def connect(self, host, port, keepalive):
            self.connects += 1

        def loop_forever(self):
            self.loop_count += 1
            if self.loop_count <= 3:
                self.retry_requested.set()
            return 0

    client = FakeClient()
    sleeps = []
    monkeypatch.setattr(receiver, "BmaPeerStore", lambda *args, **kwargs: object())
    monkeypatch.setattr(receiver, "make_client", lambda *args, **kwargs: client)
    monkeypatch.setattr(receiver.time, "sleep", sleeps.append)
    monkeypatch.setattr(receiver, "RECONNECT_INITIAL_DELAY_S", 2)
    monkeypatch.setattr(receiver, "RECONNECT_MAX_DELAY_S", 3)
    args = types.SimpleNamespace(
        db="synthetic.sqlite3", max_records=10, max_storage_bytes=4096,
        max_message_bytes=1024, retention_s=60, mqtt_host="127.0.0.1",
        mqtt_port=1883, mqtt_topic="dama/+/telemetry", mqtt_client_id="test",
        ca_file=None, site_id=None, keepalive_s=30)
    assert receiver._run(args) == 0
    assert client.connects == 4
    assert sleeps == [2, 3, 3]


def test_run_stops_on_subscription_failure_without_retrying(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.retry_requested = threading.Event()
            self.subscription_failed = threading.Event()
            self.connects = 0

        def connect(self, host, port, keepalive):
            self.connects += 1

        def loop_forever(self):
            self.subscription_failed.set()
            self.retry_requested.set()
            return 0

    client = FakeClient()
    sleeps = []
    monkeypatch.setattr(receiver, "BmaPeerStore", lambda *args, **kwargs: object())
    monkeypatch.setattr(receiver, "make_client", lambda *args, **kwargs: client)
    monkeypatch.setattr(receiver.time, "sleep", sleeps.append)
    args = types.SimpleNamespace(
        db="synthetic.sqlite3", max_records=10, max_storage_bytes=4096,
        max_message_bytes=1024, retention_s=60, mqtt_host="127.0.0.1",
        mqtt_port=1883, mqtt_topic="dama/+/telemetry", mqtt_client_id="test",
        ca_file=None, site_id=None, keepalive_s=30)
    assert receiver._run(args) == 2
    assert client.connects == 1
    assert sleeps == []


def test_inspect_command_reports_usage_and_display_only_warning(record, tmp_path, capsys):
    db = tmp_path / "bma.sqlite3"
    store = BmaPeerStore(db)
    now_ms = time.time_ns() // 1_000_000
    assert store.accept(
        wire(record), "dama/bma-bma1/telemetry", now_ms=now_ms)["status"] == "stored"
    assert receiver.main(["inspect", "--db", str(db)]) == 0
    rendered = json.loads(capsys.readouterr().out)
    assert rendered["usage"] == {"records": 1, "stored_bytes": len(wire(record)) + 64}
    assert rendered["observations"][0]["observation"] == record["observation"]
    assert "display-only" in rendered["interpretation"]


def test_run_requires_explicit_opt_in():
    with pytest.raises(SystemExit) as exc:
        receiver.main(["run"])
    assert exc.value.code == 2


def test_database_survives_reopen(record, tmp_path):
    db = tmp_path / "bma.sqlite3"
    now_ms = time.time_ns() // 1_000_000
    assert BmaPeerStore(db).accept(
        wire(record), "dama/bma-bma1/telemetry", now_ms=now_ms)["status"] == "stored"
    reopened = BmaPeerStore(db)
    assert reopened.list_observations(now_ms=now_ms)[0]["observation"] == record["observation"]
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
