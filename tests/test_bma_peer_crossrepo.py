"""Compatibility check against a pinned, synthetic upstream pipeline capture."""
import copy
import json
from pathlib import Path

import pytest

from hear.ingest.bma_peer import BmaPeerStore, BmaValidationError
from tools.bma_aws_sender import Sender


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "testdata"
    / "bma-peer"
    / "upstream_generic_pipeline_synthetic.json"
)


def test_sender_to_pinned_generic_pipeline_capture_and_receiver(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["label"] == "SYNTHETIC"
    assert fixture["provenance"]["commit"] == "e55d4c5873cd8b382baba20f2582cfd5dbad8c10"
    expected = fixture["expected"]

    fleet_path = tmp_path / "synthetic-fleet.json"
    origin_path = tmp_path / "synthetic-origin.json"
    fleet_path.write_text(json.dumps(fixture["input"]["fleet"]), encoding="utf-8")
    origin_path.write_text(json.dumps(fixture["input"]["origin"]), encoding="utf-8")

    requests = []

    def transport(_host, _port, _path, _token, body, _timeout):
        requests.append(body)
        if len(requests) == 1:
            return 503, b""
        return 202, b'{"accepted":1,"quarantined":0,"enqueued":1}'

    sender = Sender("example.invalid", 443, "synthetic-token", 2, 5, transport)
    first = sender.poll(fleet_path, origin_path, 1794000000.5)
    second = sender.poll(fleet_path, origin_path, 1794000000.5)
    assert (first["observed"], first["refused"], first["unconfirmed"],
            first["pending"]) == (1, 0, 1, 1)
    assert (second["deduplicated"], second["enqueued"], second["pending"]) == (1, 1, 0)
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert json.loads(requests[0]) == expected["sender_batch"]

    normalized = expected["batch_ingest"]["valid_messages"][0]
    forwarded = expected["forwarder"]["payload"]
    assert expected["batch_ingest"]["invalid_messages"] == []
    assert expected["forwarder"]["quarantine_reason"] is None
    assert {key: value for key, value in forwarded.items() if key != "ts"} == normalized
    assert expected["oxalis"]["payload"] == forwarded
    assert expected["oxalis"]["topic"] == "dama/bma-bma-west/telemetry"
    assert forwarded["device_id"] == "bma-bma_west"
    assert forwarded["observation"]["unit_id"] == "bma_west"
    assert forwarded["observation"]["clock_precise"] is False
    assert forwarded["observation"]["evidence"] == "unvalidated"
    assert {
        key: forwarded[key]
        for key in ("idempotency_key", "batch_idempotency_key", "ingest_trace_id", "ts")
    } == {
        "idempotency_key": "c6648da7306cf2e4202ad30a15211a06fe1d19529b430ba771c7cb8e6a944de3",
        "batch_idempotency_key": fixture["batch_idempotency_key"],
        "ingest_trace_id": "12345678-1234-5678-1234-567812345678",
        "ts": 1794000000250,
    }

    store = BmaPeerStore(tmp_path / "peer.sqlite3", retention_s=5)
    routed_payload = json.dumps(
        forwarded, separators=(",", ":"), allow_nan=False).encode("utf-8")
    topic = expected["oxalis"]["topic"]
    identity = {
        "origin_id": "synthetic-yard",
        "device_id": "bma-bma_west",
        "det_id": "bma_west:305419896:7",
    }
    assert store.accept(routed_payload, topic, now_ms=1000) == {
        "status": "stored", **identity,
    }
    assert store.list_observations(now_ms=1000) == [
        {"received_at_ms": 1000, **forwarded},
    ]
    assert store.accept(routed_payload, topic, now_ms=1001) == {
        "status": "duplicate", **identity,
    }

    invalid_precise = copy.deepcopy(forwarded)
    invalid_precise["observation"]["clock_precise"] = True
    invalid_precise["observation"]["det_id"] = "bma_west:305419896:8"
    with pytest.raises(BmaValidationError, match="clock_precise must remain false"):
        store.accept(
            json.dumps(invalid_precise, separators=(",", ":"), allow_nan=False).encode("utf-8"),
            topic,
            now_ms=1002,
        )
    assert store.list_observations(now_ms=1002) == [
        {"received_at_ms": 1000, **forwarded},
    ]

    assert len(store.list_observations(now_ms=6000)) == 1
    assert store.list_observations(now_ms=6001) == []
    assert store.counts(now_ms=6001) == {"records": 0, "stored_bytes": 0}
