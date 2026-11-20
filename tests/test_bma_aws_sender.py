"""Synthetic metadata only; HTTP is always stubbed."""
import json
from pathlib import Path
import ssl

import pytest

from hear.ingest.bma_peer import BmaValidationError, decode_message
from tools import bma_aws_sender as sender


@pytest.fixture
def files(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fleet = json.loads((root / "testdata" / "bma-peer" / "fleet_feed.json").read_text())
    origin = {"origin_id": "example-origin", "lat": 0, "lon": 0, "alt_m": 0,
              "datum": "WGS84", "north": "true"}
    fleet_path, origin_path = tmp_path / "fleet.json", tmp_path / "origin.json"
    fleet_path.write_text(json.dumps(fleet), encoding="utf-8")
    origin_path.write_text(json.dumps(origin), encoding="utf-8")
    return fleet_path, origin_path, fleet, origin


def receipt(accepted=1, quarantined=0, enqueued=1):
    return 202, json.dumps({"accepted": accepted, "quarantined": quarantined,
                            "enqueued": enqueued}).encode()


def test_wire_contract_dedup_and_device_isolation(files):
    fleet_path, origin_path, fleet, origin = files
    requests = []
    def transport(host, port, path, token, body, timeout):
        requests.append(json.loads(body))
        return receipt()
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport)
    report = s.poll(fleet_path, origin_path, 1794000000.5)
    observations = sender.convert_fleet(fleet, origin, now_s=1794000000.5, max_age_s=5)
    assert report == {"observed": 2, "refused": 1, "deduplicated": 0,
                      "capacity_dropped": 0, "expired": 0, "attempted": 2,
                      "accepted": 2, "quarantined": 0, "enqueued": 2,
                      "unconfirmed": 0, "not_enqueued": 0, "input_error": 0,
                      "pending": 0, "deferred": 0, "clock_rollback": 0}
    assert requests == [
        {"device_id": "bma-" + obs["unit_id"], "messages": [{
            "device_id": "bma-" + obs["unit_id"], "ts_ms": 1794000000250,
            "telemetry_path": "bma/bearing", "schema": "bma.bearing.v1",
            "observation": obs}]} for obs in observations["observations"]]
    assert [r["messages"][0]["observation"]["clock_precise"] for r in requests] == [False, False]
    assert [r["messages"][0]["observation"]["evidence"] for r in requests] == ["unvalidated"] * 2
    again = s.poll(fleet_path, origin_path, 1794000000.5)
    assert again["deduplicated"] == 2
    assert again["attempted"] == 0
    assert len(requests) == 2


def test_retry_exact_bytes_expiry_and_quarantine(files):
    fleet_path, origin_path, _, _ = files
    calls = []
    def transport(host, port, path, token, body, timeout):
        calls.append(body)
        if len(calls) <= 2:
            return 503, b'{"enqueued":1}'
        return receipt(0, 1, 0)
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport)
    first = s.poll(fleet_path, origin_path, 1794000000.5)
    assert (first["unconfirmed"], first["pending"]) == (2, 2)
    second = s.poll(fleet_path, origin_path, 1794000001)
    assert calls[:2] == calls[2:]
    assert (second["quarantined"], second["pending"], second["deduplicated"]) == (2, 0, 2)
    s2 = sender.Sender("example.invalid", 443, "secret", 2, 5,
                       lambda *args: (503, b""))
    assert s2.poll(fleet_path, origin_path, 1794000000.5)["pending"] == 2
    late = s2.poll(fleet_path, origin_path, 1794000006)
    assert (late["expired"], late["attempted"], late["pending"]) == (2, 0, 0)


def test_seen_retires_only_after_freshness_boundary_and_rollback_fails_closed(files, monkeypatch):
    fleet_path, origin_path, fleet, _ = files
    monkeypatch.setattr(sender, "MAX_SEEN", 2)
    calls = []
    def transport(*args):
        calls.append(json.loads(args[4]))
        return receipt()
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport)
    assert s.poll(fleet_path, origin_path, 1794000000.5)["enqueued"] == 2
    fleet["updated_s"] = 1794000005.25
    fleet["rays"][0]["det_id"] = "bma1:305419896:9"
    fleet["rays"][0]["t_wall_s"] = 1794000005.25
    fleet["rays"] = fleet["rays"][:1]
    fleet_path.write_text(json.dumps(fleet), encoding="utf-8")
    boundary = s.poll(fleet_path, origin_path, 1794000005.25)
    assert (boundary["capacity_dropped"], boundary["attempted"]) == (1, 0)
    assert len(calls) == 2
    retired = s.poll(fleet_path, origin_path, 1794000005.26)
    assert (retired["capacity_dropped"], retired["enqueued"]) == (0, 1)
    fleet["rays"][0]["det_id"] = "bma1:305419896:7"
    fleet["rays"][0]["t_wall_s"] = 1794000000.25
    fleet["updated_s"] = 1794000000.5
    fleet_path.write_text(json.dumps(fleet), encoding="utf-8")
    rollback = s.poll(fleet_path, origin_path, 1794000000.5)
    assert (rollback["clock_rollback"], rollback["attempted"], rollback["pending"]) == (1, 0, 0)
    assert len(calls) == 3
    assert s.poll(fleet_path, origin_path, 1794000005.26)["refused"] == 1
    assert len(calls) == 3


def test_attempt_limit_rotates_failed_items_without_starving_pending(files):
    fleet_path, origin_path, _, _ = files
    calls = []
    def transport(*args):
        calls.append(json.loads(args[4])["device_id"])
        return (503, b"") if calls[-1] == "bma-bma1" else receipt()
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport, max_attempts=1)
    first = s.poll(fleet_path, origin_path, 1794000000.5)
    assert (first["attempted"], first["unconfirmed"], first["deferred"], first["pending"]) == (1, 1, 1, 2)
    second = s.poll(fleet_path, origin_path, 1794000001)
    assert (second["attempted"], second["enqueued"], second["deferred"], second["pending"]) == (1, 1, 1, 1)
    third = s.poll(fleet_path, origin_path, 1794000002)
    assert (third["attempted"], third["unconfirmed"], third["deferred"], third["pending"]) == (1, 1, 0, 1)
    assert calls == ["bma-bma1", "bma-bma2", "bma-bma1"]


def test_rollback_does_not_expire_or_attempt_pending(files):
    fleet_path, origin_path, _, _ = files
    calls = []
    def transport(*args):
        calls.append(args[4])
        return (503, b"") if len(calls) <= 2 else receipt()
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport)
    assert s.poll(fleet_path, origin_path, 1794000001)["pending"] == 2
    rollback = s.poll(fleet_path, origin_path, 1794000000.5)
    assert (rollback["clock_rollback"], rollback["attempted"], rollback["expired"], rollback["pending"]) == (1, 0, 0, 2)
    assert len(calls) == 2
    resumed = s.poll(fleet_path, origin_path, 1794000001)
    assert (resumed["clock_rollback"], resumed["enqueued"], resumed["pending"]) == (0, 2, 0)
    assert calls[:2] == calls[2:]


def test_stale_malformed_and_nonfinite_never_send(files):
    fleet_path, origin_path, fleet, _ = files
    calls = []
    def transport(*args):
        calls.append(args)
        return receipt()
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport)
    assert s.poll(fleet_path, origin_path, 1794000010)["refused"] == 1
    assert calls == []
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport)
    fleet_path.write_text('{"v":1,"v":1}', encoding="utf-8")
    assert s.poll(fleet_path, origin_path, 1794000000.5)["input_error"] == 1
    assert calls == []
    fleet_path.write_text(json.dumps(fleet).replace('"v": 1', '"v": 1e999', 1), encoding="utf-8")
    assert s.poll(fleet_path, origin_path, 1794000000.5)["input_error"] == 1
    assert calls == []
    fleet_path.write_text(json.dumps(fleet), encoding="utf-8")
    assert s.poll(fleet_path, origin_path, 1794000000.5)["enqueued"] == 2
    assert len(calls) == 2


def test_capacity_and_partial_receipt(files, monkeypatch):
    fleet_path, origin_path, _, _ = files
    monkeypatch.setattr(sender, "MAX_PENDING", 1)
    calls = []
    def transport(*args):
        calls.append(args)
        return receipt(1, 0, 0)
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport)
    report = s.poll(fleet_path, origin_path, 1794000000.5)
    assert (report["capacity_dropped"], report["not_enqueued"], report["pending"]) == (1, 1, 0)
    assert len(calls) == 1


def test_oversized_message_is_counted_not_sent(files, monkeypatch):
    fleet_path, origin_path, _, _ = files
    calls = []
    def transport(*args):
        calls.append(args)
        return receipt()
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport)
    monkeypatch.setattr(sender, "MAX_MESSAGE", 10)
    assert s.poll(fleet_path, origin_path, 1794000000.5)["capacity_dropped"] == 2
    assert calls == []
    monkeypatch.setattr(sender, "MAX_MESSAGE", 65536)
    assert s.poll(fleet_path, origin_path, 1794000000.5)["enqueued"] == 2
    assert len(calls) == 2


def test_invalid_receipt_stays_unconfirmed(files):
    fleet_path, origin_path, _, _ = files
    calls = []
    def transport(*args):
        calls.append(args)
        return 202, b'{"accepted":1,"quarantined":0,"enqueued":true}'
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport)
    report = s.poll(fleet_path, origin_path, 1794000000.5)
    assert (report["unconfirmed"], report["pending"], report["accepted"]) == (2, 2, 0)
    assert len(calls) == 2
    assert sender._receipt(*receipt()) == [1, 0, 1]


@pytest.mark.parametrize("url", [
    "http://example.invalid/ingest/batch", "https://u:p@example.invalid/ingest/batch",
    "https://example.invalid/ingest/batch?token=x", "https://example.invalid/elsewhere",
    "https://example.invalid/ingest/batch#frag", "https://example.invalid/a/b/ingest/batch",
    "https://example.invalid/../ingest/batch", "https://example.invalid/%2e%2e/ingest/batch",
    "https://example.invalid//ingest/batch",
])
def test_endpoint_rejects_unsafe_urls(url):
    assert sender.endpoint("https://example.invalid/ingest/batch", "secret") == (
        "example.invalid", 443, "/ingest/batch")
    assert sender.endpoint("https://example.invalid/prod/ingest/batch", "secret") == (
        "example.invalid", 443, "/prod/ingest/batch")
    with pytest.raises(sender.InputError, match="invalid endpoint or token"):
        sender.endpoint(url, "secret")


def test_https_request_boundary_does_not_follow_redirect(monkeypatch):
    calls = []
    class Response:
        status = 302
        def read(self, size):
            calls.append(("read", size))
            return b"redirect"
    class Connection:
        def __init__(self, host, port, timeout, context):
            calls.append(("connect", host, port, timeout, context.verify_mode, context.check_hostname))
        def request(self, method, path, body, headers):
            calls.append(("request", method, path, body, headers))
        def getresponse(self):
            return Response()
        def close(self):
            calls.append(("close",))
    monkeypatch.setattr(sender.http.client, "HTTPSConnection", Connection)
    assert sender.post("example.invalid", 443, "/prod/ingest/batch", "secret", b"{}", 2) == (302, b"redirect")
    assert calls == [
        ("connect", "example.invalid", 443, 2, ssl.CERT_REQUIRED, True),
        ("request", "POST", "/prod/ingest/batch", b"{}", {
            "Authorization": "Bearer secret", "Content-Type": "application/json",
            "Accept": "application/json"}),
        ("read", sender.MAX_RESPONSE + 1), ("close",)]
    assert sender._receipt(302, b"redirect") is None


def test_configured_stage_reaches_transport(files):
    fleet_path, origin_path, _, _ = files
    host, port, path = sender.endpoint("https://example.invalid/prod/ingest/batch", "secret")
    calls = []
    def transport(*args):
        calls.append(args[2])
        return receipt()
    s = sender.Sender(host, port, "secret", 2, 5, transport, path=path)
    assert s.poll(fleet_path, origin_path, 1794000000.5)["enqueued"] == 2
    assert calls == ["/prod/ingest/batch", "/prod/ingest/batch"]


@pytest.mark.parametrize("site", ["A", "A" * 64, "yard_1.2:west-3"])
def test_site_id_messages_match_receiver_and_retries(files, site):
    fleet_path, origin_path, fleet, origin = files
    requests = []
    def transport(*args):
        requests.append(args[4])
        return (503, b"") if len(requests) <= 2 else receipt()
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport, site_id=site)
    assert s.poll(fleet_path, origin_path, 1794000000.5)["pending"] == 2
    assert s.poll(fleet_path, origin_path, 1794000001)["enqueued"] == 2
    assert requests[:2] == requests[2:]
    observations = sender.convert_fleet(fleet, origin, now_s=1794000000.5, max_age_s=5)["observations"]
    for raw, obs in zip(requests[:2], observations):
        batch = json.loads(raw)
        device = "bma-" + obs["unit_id"]
        expected = {"device_id": device, "ts_ms": 1794000000250,
                    "telemetry_path": "bma/bearing", "schema": "bma.bearing.v1",
                    "observation": obs, "site_id": site}
        assert batch == {"device_id": device, "messages": [expected]}
        message = json.dumps(batch["messages"][0]).encode()
        assert decode_message(message, "dama/" + device + "/telemetry",
                              expected_site_id=site) == expected
        with pytest.raises(BmaValidationError, match="message site_id does not match configured site_id"):
            decode_message(message, "dama/" + device + "/telemetry",
                           expected_site_id="other-site" if site != "other-site" else "another")


def test_missing_site_id_remains_optional_but_site_filtered_receiver_refuses(files):
    fleet_path, origin_path, _, _ = files
    messages = []
    def transport(*args):
        messages.append(json.loads(args[4])["messages"][0])
        return receipt()
    s = sender.Sender("example.invalid", 443, "secret", 2, 5, transport)
    assert s.poll(fleet_path, origin_path, 1794000000.5)["enqueued"] == 2
    for message in messages:
        assert "site_id" not in message
        raw = json.dumps(message).encode()
        topic = "dama/" + message["device_id"] + "/telemetry"
        assert decode_message(raw, topic) == message
        with pytest.raises(BmaValidationError, match="message site_id does not match configured site_id"):
            decode_message(raw, topic, expected_site_id="yard-a")


@pytest.mark.parametrize("site", ["", "_yard", ".yard", "yard space", "yard\n",
                                   "é", "A" * 65, "a/b", None, 12])
def test_site_id_validation_before_any_requests(site):
    calls = []
    def transport(*args):
        calls.append(args)
        return receipt()
    assert sender.Sender("example.invalid", 443, "secret", 2, 5, transport, site_id="yard-a").site_id == "yard-a"
    if site is None:
        assert sender.Sender("example.invalid", 443, "secret", 2, 5, transport, site_id=site).site_id is None
    else:
        with pytest.raises(sender.InputError, match="invalid site_id"):
            sender.Sender("example.invalid", 443, "secret", 2, 5, transport, site_id=site)
    assert calls == []


def test_cli_site_id_reaches_receiver_via_mocked_https(files, monkeypatch):
    fleet_path, origin_path, _, _ = files
    requests = []
    class Response:
        status = 202
        def read(self, size):
            return receipt()[1]
    class Connection:
        def __init__(self, *args, **kwargs):
            pass
        def request(self, method, path, body, headers):
            requests.append((method, path, json.loads(body)))
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(sender.http.client, "HTTPSConnection", Connection)
    monkeypatch.setattr(sender.time, "time", lambda: 1794000000.5)
    monkeypatch.setenv("BMA_AWS_URL", "https://example.invalid/ingest/batch")
    monkeypatch.setenv("BMA_AWS_TOKEN", "secret")
    assert sender.main(["--fleet", str(fleet_path), "--origin", str(origin_path),
                        "--polls", "1", "--site-id", "yard-a"]) == 1
    assert len(requests) == 2
    for method, path, batch in requests:
        assert (method, path) == ("POST", "/ingest/batch")
        message = batch["messages"][0]
        assert message["site_id"] == "yard-a"
        assert decode_message(json.dumps(message).encode(),
                              "dama/" + message["device_id"] + "/telemetry",
                              expected_site_id="yard-a") == message


def test_cli_rejects_bad_site_before_network(files, monkeypatch, capsys):
    fleet_path, origin_path, _, _ = files
    calls = []
    monkeypatch.setattr(sender.http.client, "HTTPSConnection", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(SystemExit, match="2"):
        sender.main(["--fleet", str(fleet_path), "--origin", str(origin_path),
                     "--polls", "1", "--site-id", "bad space"])
    assert calls == []
    assert "--site-id" in capsys.readouterr().err


def test_missing_environment_never_starts(files, monkeypatch, capsys):
    fleet_path, origin_path, _, _ = files
    monkeypatch.delenv("BMA_AWS_URL", raising=False)
    monkeypatch.delenv("BMA_AWS_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr(sender, "post", lambda *args: calls.append(args))
    assert sender.main(["--fleet", str(fleet_path), "--origin", str(origin_path), "--polls", "1"]) == 2
    assert calls == []
    assert "BMA_AWS_URL" in capsys.readouterr().err
