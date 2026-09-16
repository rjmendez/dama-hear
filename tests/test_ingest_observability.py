"""Observability seams for future HTTPS batch ingest."""
from __future__ import annotations

import json
from pathlib import Path

from hear.ingest import batch as BA
from hear.ingest import envelope as EV
from hear.ingest import observability as IO

FIXTURE_DIR = (Path(__file__).resolve().parents[1] / "contracts" / "fixtures"
               / "hear.ingest.batch.v1")


def _frame(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


DEV = {"credential_device_id": "nyquist"}


def _line(text: str, prefix: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(prefix))


def test_metric_registry_publishes_the_documented_families():
    metrics = IO.IngestMetrics()
    assert set(metrics.metric_names()) == {
        "ingest_batches_total",
        "ingest_items_total",
        "ingest_items_nondispatchable_total",
        "ingest_batch_items",
        "ingest_batch_bytes",
        "ingest_ack_gap_items",
        "ingest_lag_seconds",
        "ingest_refusals_total",
        "ingest_idempotent_replays_total",
        "ingest_idempotency_conflicts_total",
        "ingest_producer_spool_backlog",
        "ingest_sequence_gaps_total",
        "ingest_clock_skew_seconds",
        "ingest_dual_write_mismatch_total",
    }


def test_valid_batch_emits_spool_backlog_lag_and_clock_skew_metrics():
    frame = _frame("valid-node-batch")
    validation = BA.validate_batch(frame, **DEV)
    metrics = IO.IngestMetrics()
    metrics.observe_validation(
        validation,
        raw_body_bytes=len(EV.encode(frame)),
        received_at="2026-09-14T18:03:12.100000Z",
        source="lan_http_batch",
    )
    text = metrics.render_prometheus().decode("utf-8")

    assert 'ingest_batches_total{site="site-quarry-north",device_id="nyquist",' in text
    assert 'source="lan_http_batch",adapter="direct",status="accepted"} 1' in text
    assert _line(
        text,
        'ingest_producer_spool_backlog{site="site-quarry-north",device_id="nyquist",'
        'source="lan_http_batch",adapter="direct"} ',
    ).endswith(" 118")
    assert _line(
        text,
        'ingest_ack_gap_items_sum{site="site-quarry-north",device_id="nyquist",'
        'source="lan_http_batch",adapter="direct"} ',
    ).endswith(" 0")
    assert _line(
        text,
        'ingest_lag_seconds_count{site="site-quarry-north",device_id="nyquist",'
        'source="lan_http_batch",adapter="direct"} ',
    ).endswith(" 3")
    assert _line(
        text,
        'ingest_lag_seconds_sum{site="site-quarry-north",device_id="nyquist",'
        'source="lan_http_batch",adapter="direct"} ',
    ).endswith(" 2.55")
    assert _line(
        text,
        'ingest_clock_skew_seconds_sum{site="site-quarry-north",device_id="nyquist",'
        'source="lan_http_batch",adapter="direct"} ',
    ).endswith(" 0.11")


def test_ack_gap_and_sequence_gap_observations_follow_the_contract():
    frame = _frame("valid-node-batch")
    metrics = IO.IngestMetrics()
    first = BA.BatchValidationResult(
        frame,
        [],
        [],
        [
            BA.ItemResult(0, "accepted", dispatchable=True),
            BA.ItemResult(1, "deferred"),
            BA.ItemResult(2, "accepted", dispatchable=True),
        ],
    )
    metrics.observe_validation(first, received_at="2026-09-14T18:03:12.100000Z",
                               raw_body_bytes=900, source="lan_http_batch")

    later = dict(frame, producer=dict(frame["producer"], batch_sequence=10))
    second = BA.BatchValidationResult(
        later,
        [],
        [],
        [BA.ItemResult(0, "accepted", dispatchable=True)],
    )
    metrics.observe_validation(second, received_at="2026-09-14T18:03:13.100000Z",
                               raw_body_bytes=400, source="lan_http_batch")

    text = metrics.render_prometheus().decode("utf-8")
    assert _line(
        text,
        'ingest_ack_gap_items_sum{site="site-quarry-north",device_id="nyquist",'
        'source="lan_http_batch",adapter="direct"} ',
    ).endswith(" 2")
    assert _line(
        text,
        'ingest_sequence_gaps_total{site="site-quarry-north",device_id="nyquist",'
        'source="lan_http_batch",adapter="direct"} ',
    ).endswith(" 2")


def test_frame_refusals_and_manual_counters_are_accounted_separately():
    frame = _frame("empty-batch")
    validation = BA.validate_batch(frame, **DEV)
    metrics = IO.IngestMetrics()
    metrics.observe_validation(validation, raw_body_bytes=len(EV.encode(frame)),
                               received_at="2026-09-14T18:03:12.100000Z",
                               site="site-quarry-north", source="lan_http_batch")
    metrics.observe_idempotency_conflict(site="site-quarry-north", device_id="nyquist",
                                         source="lan_http_batch")
    metrics.observe_dual_write_mismatch(mismatch_class="missing_canonical",
                                        site="site-quarry-north", device_id="nyquist",
                                        source="lan_http_batch")
    text = metrics.render_prometheus().decode("utf-8")

    assert _line(
        text,
        'ingest_batches_total{site="site-quarry-north",device_id="nyquist",'
        'source="lan_http_batch",adapter="direct",status="refused"} ',
    ).endswith(" 1")
    assert _line(
        text,
        'ingest_refusals_total{site="site-quarry-north",device_id="nyquist",'
        'source="lan_http_batch",adapter="direct",reason="batch_empty"} ',
    ).endswith(" 1")
    assert _line(
        text,
        'ingest_idempotency_conflicts_total{site="site-quarry-north",device_id="nyquist",'
        'source="lan_http_batch",adapter="direct"} ',
    ).endswith(" 1")
    assert _line(
        text,
        'ingest_dual_write_mismatch_total{site="site-quarry-north",device_id="nyquist",'
        'source="lan_http_batch",adapter="direct",class="missing_canonical"} ',
    ).endswith(" 1")
