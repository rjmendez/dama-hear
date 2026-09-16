"""Checked-in dashboard and alert assets for HTTPS batch ingest observability."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "deploy" / "observability" / "hear-ingest-batch-dashboard.json"
ALERTS = ROOT / "deploy" / "observability" / "hear-ingest-batch-alerts.yaml"


def test_dashboard_tracks_the_spool_and_ack_gap_metrics():
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    panels = {panel["title"]: panel for panel in dashboard["panels"]}
    assert dashboard["title"] == "Hear HTTPS batch ingest"
    assert {
        "Producer spool backlog",
        "Ack gap average (15m)",
        "Observed sequence gaps (6h)",
        "Batch admission rate by status (15m)",
    } <= set(panels)
    assert "ingest_producer_spool_backlog" in panels["Producer spool backlog"]["targets"][0]["expr"]
    assert "ingest_ack_gap_items_sum" in panels["Ack gap average (15m)"]["targets"][0]["expr"]
    assert "ingest_ack_gap_items_count" in panels["Ack gap average (15m)"]["targets"][0]["expr"]
    assert "ingest_sequence_gaps_total" in panels["Observed sequence gaps (6h)"]["targets"][0]["expr"]


def test_alert_rules_gate_on_real_batch_traffic_before_paging():
    docs = list(yaml.safe_load_all(ALERTS.read_text(encoding="utf-8")))
    assert [doc["kind"] for doc in docs] == ["PrometheusRule"]
    rules = docs[0]["spec"]["groups"][0]["rules"]
    by_name = {rule["alert"]: rule for rule in rules}
    assert set(by_name) == {
        "HearIngestAckGapSustained",
        "HearIngestProducerSpoolBacklogGrowing",
        "HearIngestSequenceGapObserved",
    }
    assert "ingest_ack_gap_items_sum" in by_name["HearIngestAckGapSustained"]["expr"]
    assert 'ingest_batches_total{status="accepted"}' in by_name["HearIngestAckGapSustained"]["expr"]
    assert "ingest_producer_spool_backlog" in by_name["HearIngestProducerSpoolBacklogGrowing"]["expr"]
    assert 'ingest_batches_total{status="accepted"}' in by_name["HearIngestProducerSpoolBacklogGrowing"]["expr"]
    assert "ingest_sequence_gaps_total" in by_name["HearIngestSequenceGapObserved"]["expr"]
