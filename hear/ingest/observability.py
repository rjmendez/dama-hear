"""Metrics extraction and exposition for future HTTPS batch ingest.

This module stays at the contract seam on purpose: it reads one validated
``hear.ingest.batch.v1`` frame and emits the exact metric families named in
``docs/phase4-https-batch-ingest.md`` §12 without opening sockets, touching a
store or depending on ``prometheus_client``. The eventual batch endpoint can
therefore wire these metrics in immediately, while the current tree gains a
tested, scrapeable definition surface now.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from . import batch as BA

BASE_LABELS: Tuple[str, ...] = ("site", "device_id", "source", "adapter")
NO_REASON = "none"


def _parse_utc(value: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


def _seconds_between(later: Optional[str], earlier: Optional[str]) -> Optional[float]:
    late = _parse_utc(later)
    early = _parse_utc(earlier)
    if late is None or early is None:
        return None
    return max((late - early).total_seconds(), 0.0)


def _site_from_frame(frame: Mapping[str, Any]) -> str:
    messages = frame.get("messages")
    if isinstance(messages, Sequence):
        for item in messages:
            if isinstance(item, Mapping):
                site = item.get("site_id")
                if isinstance(site, str) and site.strip():
                    return site.strip()
    return "unknown"


def _adapter_from_frame(frame: Mapping[str, Any]) -> str:
    adapter = frame.get("adapter")
    if isinstance(adapter, Mapping):
        name = adapter.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return "direct"


def _base_labels(frame: Optional[Mapping[str, Any]], *, site: Optional[str],
                 source: str, adapter: Optional[str]) -> Dict[str, str]:
    frame = frame if isinstance(frame, Mapping) else {}
    device_id = frame.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        device_id = "unknown"
    return {
        "site": site.strip() if isinstance(site, str) and site.strip() else _site_from_frame(frame),
        "device_id": device_id.strip(),
        "source": source.strip() if isinstance(source, str) and source.strip() else "unknown",
        "adapter": adapter.strip() if isinstance(adapter, str) and adapter.strip()
        else _adapter_from_frame(frame),
    }


def _reason_label(reasons: Sequence[str]) -> str:
    kept = [r.strip() for r in reasons if isinstance(r, str) and r.strip()]
    return "+".join(kept) if kept else NO_REASON


def _sample_key(label_names: Sequence[str], labels: Mapping[str, str]) -> Tuple[str, ...]:
    return tuple(str(labels.get(name, "")) for name in label_names)


def _render_labels(label_names: Sequence[str], key: Tuple[str, ...]) -> str:
    if not label_names:
        return ""

    def esc(text: str) -> str:
        return text.replace("\\", r"\\").replace("\n", r"\n").replace('"', r"\"")

    pairs = [f'{name}="{esc(value)}"' for name, value in zip(label_names, key)]
    return "{" + ",".join(pairs) + "}"


def _render_help(text: str) -> str:
    return text.replace("\\", r"\\").replace("\n", r"\n")


@dataclass(frozen=True)
class MetricDefinition:
    name: str
    kind: str
    help: str
    label_names: Tuple[str, ...] = ()
    buckets: Tuple[float, ...] = ()


class _Counter:
    def __init__(self, definition: MetricDefinition) -> None:
        self.definition = definition
        self.samples: Dict[Tuple[str, ...], float] = {}

    def inc(self, labels: Mapping[str, str], value: float = 1.0) -> None:
        key = _sample_key(self.definition.label_names, labels)
        self.samples[key] = self.samples.get(key, 0.0) + float(value)

    def render(self) -> list[str]:
        lines = [
            f"# HELP {self.definition.name} {_render_help(self.definition.help)}",
            f"# TYPE {self.definition.name} counter",
        ]
        for key in sorted(self.samples):
            lines.append(
                f"{self.definition.name}{_render_labels(self.definition.label_names, key)} "
                f"{self.samples[key]:g}"
            )
        return lines


class _Gauge:
    def __init__(self, definition: MetricDefinition) -> None:
        self.definition = definition
        self.samples: Dict[Tuple[str, ...], float] = {}

    def set(self, labels: Mapping[str, str], value: float) -> None:
        self.samples[_sample_key(self.definition.label_names, labels)] = float(value)

    def render(self) -> list[str]:
        lines = [
            f"# HELP {self.definition.name} {_render_help(self.definition.help)}",
            f"# TYPE {self.definition.name} gauge",
        ]
        for key in sorted(self.samples):
            lines.append(
                f"{self.definition.name}{_render_labels(self.definition.label_names, key)} "
                f"{self.samples[key]:g}"
            )
        return lines


class _Histogram:
    def __init__(self, definition: MetricDefinition) -> None:
        self.definition = definition
        self.samples: Dict[Tuple[str, ...], Dict[str, Any]] = {}

    def observe(self, labels: Mapping[str, str], value: float) -> None:
        key = _sample_key(self.definition.label_names, labels)
        state = self.samples.setdefault(key, {
            "buckets": [0 for _ in self.definition.buckets],
            "count": 0,
            "sum": 0.0,
        })
        numeric = float(value)
        for idx, upper in enumerate(self.definition.buckets):
            if numeric <= upper:
                state["buckets"][idx] += 1
        state["count"] += 1
        state["sum"] += numeric

    def render(self) -> list[str]:
        lines = [
            f"# HELP {self.definition.name} {_render_help(self.definition.help)}",
            f"# TYPE {self.definition.name} histogram",
        ]
        for key in sorted(self.samples):
            state = self.samples[key]
            for idx, upper in enumerate(self.definition.buckets):
                bucket_labels = dict(zip(self.definition.label_names, key))
                bucket_labels["le"] = "+Inf" if upper == float("inf") else ("%g" % upper)
                lines.append(
                    f"{self.definition.name}_bucket"
                    f"{_render_labels(self.definition.label_names + ('le',), _sample_key(self.definition.label_names + ('le',), bucket_labels))} "
                    f"{state['buckets'][idx]}"
                )
            lines.append(
                f"{self.definition.name}_count{_render_labels(self.definition.label_names, key)} "
                f"{state['count']}"
            )
            lines.append(
                f"{self.definition.name}_sum{_render_labels(self.definition.label_names, key)} "
                f"{state['sum']:g}"
            )
        return lines


DEFAULT_BUCKETS = {
    "ingest_batch_items": (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, float("inf")),
    "ingest_batch_bytes": (512.0, 1024.0, 4096.0, 16384.0, 65536.0,
                            131072.0, 262144.0, float("inf")),
    "ingest_ack_gap_items": (0.0, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, float("inf")),
    "ingest_lag_seconds": (1.0, 5.0, 15.0, 30.0, 60.0, 300.0, 900.0,
                            3600.0, 21600.0, float("inf")),
    "ingest_clock_skew_seconds": (1.0, 5.0, 15.0, 30.0, 60.0, 300.0, 900.0,
                                   3600.0, float("inf")),
}


METRIC_DEFINITIONS: Tuple[MetricDefinition, ...] = (
    MetricDefinition("ingest_batches_total", "counter",
                     "Admitted vs refused HTTPS batch frames.",
                     BASE_LABELS + ("status",)),
    MetricDefinition("ingest_items_total", "counter",
                     "Per-item outcomes; the accepted+duplicate+refused+deferred conservation equation.",
                     BASE_LABELS + ("status", "reason")),
    MetricDefinition("ingest_items_nondispatchable_total", "counter",
                     "Stored but non-dispatchable items from a newer writer or unknown enum member.",
                     BASE_LABELS),
    MetricDefinition("ingest_batch_items", "histogram",
                     "Observed batch occupancy versus the published 64-item limit.",
                     BASE_LABELS, DEFAULT_BUCKETS["ingest_batch_items"]),
    MetricDefinition("ingest_batch_bytes", "histogram",
                     "Observed request size versus the published 256 KiB bound.",
                     BASE_LABELS, DEFAULT_BUCKETS["ingest_batch_bytes"]),
    MetricDefinition("ingest_ack_gap_items", "histogram",
                     "submitted - (ack_through_index + 1); non-zero means durability lag inside the batch.",
                     BASE_LABELS, DEFAULT_BUCKETS["ingest_ack_gap_items"]),
    MetricDefinition("ingest_lag_seconds", "histogram",
                     "Server received_at minus item observed_at for durable item outcomes.",
                     BASE_LABELS, DEFAULT_BUCKETS["ingest_lag_seconds"]),
    MetricDefinition("ingest_refusals_total", "counter",
                     "Durable refusal reasons for whole-frame and per-item refusals.",
                     BASE_LABELS + ("reason",)),
    MetricDefinition("ingest_idempotent_replays_total", "counter",
                     "Duplicate items that converged on an already-durable event_id.",
                     BASE_LABELS),
    MetricDefinition("ingest_idempotency_conflicts_total", "counter",
                     "Idempotency-Key reuses with a different body fingerprint.",
                     BASE_LABELS),
    MetricDefinition("ingest_producer_spool_backlog", "gauge",
                     "Producer-reported count of items still spooled after this batch.",
                     BASE_LABELS),
    MetricDefinition("ingest_sequence_gaps_total", "counter",
                     "Missing producer.batch_sequence steps observed within one boot_id.",
                     BASE_LABELS),
    MetricDefinition("ingest_clock_skew_seconds", "histogram",
                     "Absolute skew between frame sent_at and the server receive time.",
                     BASE_LABELS, DEFAULT_BUCKETS["ingest_clock_skew_seconds"]),
    MetricDefinition("ingest_dual_write_mismatch_total", "counter",
                     "Dual-write mismatch classifications emitted by the reconciliation path.",
                     BASE_LABELS + ("class",)),
)


class BatchSequenceTracker:
    """Detect positive `producer.batch_sequence` gaps within one producer boot."""

    def __init__(self) -> None:
        self._last_seen: Dict[Tuple[str, str, str, str, str], int] = {}

    def observe(self, *, site: str, device_id: str, source: str, adapter: str,
                boot_id: str, batch_sequence: int) -> int:
        key = (site, device_id, source, adapter, boot_id)
        previous = self._last_seen.get(key)
        if previous is None or batch_sequence > previous:
            self._last_seen[key] = batch_sequence
        if previous is None or batch_sequence <= previous + 1:
            return 0
        return batch_sequence - previous - 1


class IngestMetrics:
    """Mutable registry for the ingest metric families named in the Phase 4 docs."""

    def __init__(self, *, sequence_tracker: Optional[BatchSequenceTracker] = None) -> None:
        self.sequence_tracker = sequence_tracker or BatchSequenceTracker()
        self._definitions = {definition.name: definition for definition in METRIC_DEFINITIONS}
        self._metrics: Dict[str, Any] = {}
        for definition in METRIC_DEFINITIONS:
            if definition.kind == "counter":
                self._metrics[definition.name] = _Counter(definition)
            elif definition.kind == "gauge":
                self._metrics[definition.name] = _Gauge(definition)
            elif definition.kind == "histogram":
                self._metrics[definition.name] = _Histogram(definition)
            else:  # pragma: no cover - constant table
                raise AssertionError(definition.kind)

    def metric_names(self) -> Tuple[str, ...]:
        return tuple(definition.name for definition in METRIC_DEFINITIONS)

    def observe_validation(self, validation: BA.BatchValidationResult, *,
                           raw_body_bytes: Optional[int] = None,
                           received_at: Optional[str] = None,
                           site: Optional[str] = None,
                           source: str = "unknown",
                           adapter: Optional[str] = None) -> None:
        if validation.ok:
            self.observe_batch_results(
                validation.frame if isinstance(validation.frame, Mapping) else {},
                validation.items,
                raw_body_bytes=raw_body_bytes,
                received_at=received_at,
                site=site,
                source=source,
                adapter=adapter,
            )
            return
        self.observe_frame_refusal(
            validation.reasons or [NO_REASON],
            frame=validation.frame if isinstance(validation.frame, Mapping) else None,
            raw_body_bytes=raw_body_bytes,
            received_at=received_at,
            site=site,
            source=source,
            adapter=adapter,
        )

    def observe_frame_refusal(self, reasons: Sequence[str], *, frame: Optional[Mapping[str, Any]] = None,
                              raw_body_bytes: Optional[int] = None,
                              received_at: Optional[str] = None,
                              site: Optional[str] = None,
                              source: str = "unknown",
                              adapter: Optional[str] = None) -> None:
        labels = _base_labels(frame, site=site, source=source, adapter=adapter)
        self._metrics["ingest_batches_total"].inc({**labels, "status": "refused"})
        self._observe_frame_shape(frame, labels, raw_body_bytes=raw_body_bytes, admitted=False)
        for reason in reasons or [NO_REASON]:
            self._metrics["ingest_refusals_total"].inc({**labels, "reason": reason})

    def observe_batch_results(self, frame: Mapping[str, Any], results: Sequence[BA.ItemResult], *,
                              raw_body_bytes: Optional[int] = None,
                              received_at: Optional[str] = None,
                              site: Optional[str] = None,
                              source: str = "unknown",
                              adapter: Optional[str] = None,
                              frame_status: str = "accepted") -> None:
        labels = _base_labels(frame, site=site, source=source, adapter=adapter)
        self._metrics["ingest_batches_total"].inc({**labels, "status": frame_status})
        self._observe_frame_shape(frame, labels, raw_body_bytes=raw_body_bytes, admitted=True)

        items = list(results)
        self._metrics["ingest_ack_gap_items"].observe(
            labels,
            len(items) - (BA.ack_through_index(items) + 1),
        )
        if received_at is not None:
            skew = _seconds_between(received_at, frame.get("sent_at"))
            if skew is not None:
                self._metrics["ingest_clock_skew_seconds"].observe(labels, skew)

        messages = frame.get("messages")
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes, bytearray)):
            for result in items:
                self._metrics["ingest_items_total"].inc({
                    **labels,
                    "status": result.status,
                    "reason": _reason_label(result.reasons),
                })
                if not result.dispatchable:
                    self._metrics["ingest_items_nondispatchable_total"].inc(labels)
                if result.status == "duplicate":
                    self._metrics["ingest_idempotent_replays_total"].inc(labels)
                if result.status == "refused":
                    for reason in result.reasons or [NO_REASON]:
                        self._metrics["ingest_refusals_total"].inc({**labels, "reason": reason})
                if received_at is None or result.status == "deferred":
                    continue
                if result.index >= len(messages):
                    continue
                item = messages[result.index]
                if not isinstance(item, Mapping):
                    continue
                lag = _seconds_between(received_at, item.get("observed_at"))
                if lag is not None:
                    self._metrics["ingest_lag_seconds"].observe(labels, lag)

    def _observe_frame_shape(self, frame: Optional[Mapping[str, Any]], labels: Mapping[str, str], *,
                             raw_body_bytes: Optional[int], admitted: bool) -> None:
        frame = frame if isinstance(frame, Mapping) else {}
        if raw_body_bytes is not None:
            self._metrics["ingest_batch_bytes"].observe(labels, raw_body_bytes)
        messages = frame.get("messages")
        if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes, bytearray)):
            self._metrics["ingest_batch_items"].observe(labels, len(messages))

        producer = frame.get("producer")
        if isinstance(producer, Mapping):
            backlog = producer.get("spool_backlog")
            if isinstance(backlog, int):
                self._metrics["ingest_producer_spool_backlog"].set(labels, backlog)
            boot_id = producer.get("boot_id")
            batch_sequence = producer.get("batch_sequence")
            if admitted and isinstance(boot_id, str) and isinstance(batch_sequence, int):
                gap = self.sequence_tracker.observe(
                    site=labels["site"],
                    device_id=labels["device_id"],
                    source=labels["source"],
                    adapter=labels["adapter"],
                    boot_id=boot_id,
                    batch_sequence=batch_sequence,
                )
                if gap > 0:
                    self._metrics["ingest_sequence_gaps_total"].inc(labels, gap)

    def observe_idempotency_conflict(self, *, site: str, device_id: str,
                                     source: str, adapter: str = "direct") -> None:
        labels = _base_labels({"device_id": device_id}, site=site, source=source, adapter=adapter)
        self._metrics["ingest_idempotency_conflicts_total"].inc(labels)

    def observe_dual_write_mismatch(self, *, mismatch_class: str, site: str, device_id: str,
                                    source: str, adapter: str = "direct",
                                    count: int = 1) -> None:
        labels = _base_labels({"device_id": device_id}, site=site, source=source, adapter=adapter)
        self._metrics["ingest_dual_write_mismatch_total"].inc(
            {**labels, "class": mismatch_class},
            count,
        )

    def render_prometheus(self) -> bytes:
        lines: list[str] = []
        for definition in METRIC_DEFINITIONS:
            lines.extend(self._metrics[definition.name].render())
        return ("\n".join(lines) + "\n").encode("utf-8")
