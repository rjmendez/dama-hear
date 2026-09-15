"""Canonical `hear.ingest.batch.v1` request frame and receipt for HTTPS batch ingest.

This is the *transport frame* that carries `hear.ingest.v1` envelopes (and, during the
mixed-version window, untranslated legacy telemetry bodies) over one
`POST /v1/ingest/batches` request. It is a contract, not an adapter: nothing here opens a
socket, names a host, touches Redis/SQLite/PostgreSQL, or translates a legacy body. The
design record is `docs/decisions/0002-phase4-https-batch-ingest-adapter.md` and the
operational specification is `docs/phase4-https-batch-ingest.md`.

`contracts/schemas/hear.ingest.batch.v1.schema.json` and
`contracts/fixtures/hear.ingest.batch.v1/` are GENERATED from the tables below by
`tools/gen_ingest_contracts.py`; `tests/test_ingest_batch_v1.py` fails if they drift.

Four rules carry the weight, and each has a test:

1. **The frame is separate from the item.** `batch_schema_version` versions the frame;
   each item carries its own `schema_version`. A frame a reader cannot parse is refused
   before any item is interpreted; an item a reader cannot parse refuses only that item.
   One poisoned row can therefore never refuse a whole node's backlog.
2. **Conservation closes per batch.** `accepted + duplicate + refused + deferred` equals
   the number of submitted items, always. There is no silent fifth state, and a refused
   item still leaves a durable refusal record.
3. **Acknowledgement is a contiguous durable prefix.** `ack_through_index` is the last
   index the server has made durable with no gap before it. A client may free spool space
   only up to that index; anything after it is `deferred` and must be retried.
4. **Identity, not arrival order, deduplicates.** Replay safety comes from
   `hear.ingest.v1`'s closed identity tuple, so a resent batch, a reordered batch, or a
   batch split differently after a reboot converges on the same durable events.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import envelope as EV

BATCH_SCHEMA_ID = "hear.ingest.batch.v1"
BATCH_SCHEMA_MAJOR = 1
"""The only `batch_schema_version` this reader supports. A larger major is refused whole,
because a v2 frame may reuse a v1 frame field with a different meaning."""

BATCH_SCHEMA_URI = "https://schemas.dama.example/hear/ingest/v1/batch.schema.json"
RECEIPT_SCHEMA_ID = "hear.ingest.batch.receipt.v1"
RECEIPT_SCHEMA_URI = "https://schemas.dama.example/hear/ingest/v1/batch-receipt.schema.json"

BATCH_CODEC_MEDIA_TYPES: Dict[str, str] = {
    "json": "application/vnd.dama.hear.ingest.batch.v1+json",
}
RECEIPT_MEDIA_TYPE = "application/vnd.dama.hear.ingest.batch-receipt.v1+json"

# --- limits ------------------------------------------------------------------------------
# Sized for what exists: six nodes on one machine, an ESP32-S3 with a single TLS session and
# a few hundred KiB of usable heap, and a drain/import adapter replaying an SD backlog. They
# are deliberately small enough that a full batch fits in one TLS record stream without the
# node fragmenting its spool, and they are server-enforced so a client bug cannot widen them.
MAX_ITEMS_PER_BATCH = 64
MAX_BATCH_BYTES = 262_144      # 256 KiB, decoded
MAX_ITEM_BYTES = 65_536        # 64 KiB, canonical form of one item
MAX_CLOCK_SKEW_S = 900         # frame `sent_at` sanity bound, not an item time source

ITEM_STATUSES: Tuple[str, ...] = ("accepted", "duplicate", "refused", "deferred")
"""Closed. `deferred` means *not durable*: the client must retry it, and it is the only
status that does not carry a durable server-side record."""

TRANSLATION_REQUIRED = "translation_required"
"""Classification, not a status: a recognizable legacy telemetry body that this contract
deliberately does not judge. The edge adapter translates it into `hear.ingest.v1` and the
translated envelope is what receives an item status."""

_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")

Field = Dict[str, Any]

# --- frame field table -------------------------------------------------------------------
BATCH_FIELDS: Tuple[Field, ...] = (
    {"name": "batch_schema_version", "kind": "int", "required": True, "minimum": 1,
     "doc": "Frame major version. Unsupported majors are refused before any item is read."},
    {"name": "batch_id", "kind": "str", "required": True,
     "doc": "Producer-chosen batch identity, stable across retries of the same batch. "
            "Used for receipt correlation and log joins, never for deduplication."},
    {"name": "device_id", "kind": "str", "required": True,
     "doc": "Submitting producer. Cross-checked against the credential; a mismatch is "
            "refused, never overwritten from the body."},
    {"name": "sent_at", "kind": "str_or_null", "required": True,
     "doc": "RFC3339 UTC instant the producer handed the batch to the transport, or null "
            "when it has no usable clock. Never used as an observation time."},
    {"name": "messages", "kind": "array", "required": True,
     "doc": "Items in producer order: hear.ingest.v1 envelopes, or legacy telemetry bodies "
            "during the mixed-version window. Order is preserved; it is not identity."},
    {"name": "producer", "kind": "object", "required": False,
     "doc": "Optional continuity evidence for the batch itself. Absent means the producer "
            "exposes no boot/sequence, not that continuity is fine.",
     "properties": (
         {"name": "boot_id", "kind": "str", "required": False,
          "doc": "Producer boot identity; a change means batch sequences are not comparable."},
         {"name": "boot_epoch_us", "kind": "int", "required": False,
          "doc": "Producer boot epoch in microseconds UTC."},
         {"name": "batch_sequence", "kind": "int", "required": False, "minimum": 0,
          "doc": "Monotonic per-boot batch counter. Gap detection evidence only."},
         {"name": "spool_backlog", "kind": "int", "required": False, "minimum": 0,
          "doc": "Items still spooled at the producer after this batch. Backpressure input."},
     )},
    {"name": "adapter", "kind": "object", "required": False,
     "doc": "Submitting adapter identity when a gateway, drain or import tool submits on a "
            "producer's behalf. Absent for a device submitting directly.",
     "properties": (
         {"name": "name", "kind": "str", "required": True, "doc": "Adapter name."},
         {"name": "version", "kind": "str", "required": True, "doc": "Adapter build."},
     )},
)

# --- receipt field table -----------------------------------------------------------------
RECEIPT_FIELDS: Tuple[Field, ...] = (
    {"name": "batch_schema_version", "kind": "int", "required": True, "minimum": 1,
     "doc": "Frame major version this receipt answers."},
    {"name": "batch_id", "kind": "str", "required": True,
     "doc": "Echoed submitted batch_id, so a receipt is attributable without the body."},
    {"name": "received_at", "kind": "str", "required": True,
     "doc": "RFC3339 UTC instant the server accepted the bytes. Never a producer claim."},
    {"name": "ack_through_index", "kind": "int", "required": True, "minimum": -1,
     "doc": "Last item index made durable with no gap before it; -1 acknowledges nothing. "
            "A producer may release spool space only up to this index."},
    {"name": "counts", "kind": "object", "required": True,
     "doc": "Conservation accounting. The four counts sum to the submitted item count.",
     "properties": (
         {"name": "submitted", "kind": "int", "required": True, "minimum": 0,
          "doc": "Items in the submitted frame."},
         {"name": "accepted", "kind": "int", "required": True, "minimum": 0,
          "doc": "Newly durable events."},
         {"name": "duplicate", "kind": "int", "required": True, "minimum": 0,
          "doc": "Already durable under the same event_id. Success, not an error."},
         {"name": "refused", "kind": "int", "required": True, "minimum": 0,
          "doc": "Durably refused with a machine reason and retained raw bytes."},
         {"name": "deferred", "kind": "int", "required": True, "minimum": 0,
          "doc": "Not made durable. The producer must retry these."},
     )},
    {"name": "results", "kind": "array", "required": True,
     "doc": "Per-item outcome in submitted order, one entry per submitted item."},
    {"name": "retry_after_s", "kind": "int_or_null", "required": True, "minimum": 0,
     "doc": "Server-directed minimum delay before the next batch. Null means no direction; "
            "it is advice about load, never a substitute for client backoff."},
    {"name": "server", "kind": "object", "required": True,
     "doc": "Answering build, so a bad translation or a bad rollout is attributable.",
     "properties": (
         {"name": "adapter", "kind": "str", "required": True, "doc": "Ingest adapter name."},
         {"name": "version", "kind": "str", "required": True, "doc": "Ingest adapter build."},
         {"name": "envelope_major", "kind": "int", "required": True, "minimum": 1,
          "doc": "Item envelope major this server supports."},
     )},
)

RESULT_FIELDS: Tuple[Field, ...] = (
    {"name": "index", "kind": "int", "required": True, "minimum": 0,
     "doc": "Zero-based index in the submitted `messages` array."},
    {"name": "status", "kind": "enum", "required": True, "enum": ITEM_STATUSES,
     "doc": "Outcome for this item. Unknown members are preserved, never guessed."},
    {"name": "event_id", "kind": "str_or_null", "required": True,
     "doc": "Derived event identity when one could be derived, else null."},
    {"name": "dispatchable", "kind": "bool", "required": True,
     "doc": "False for a stored record carrying an enum member this build does not know."},
    {"name": "reasons", "kind": "array", "required": True,
     "doc": "Machine reason codes. Empty for accepted and duplicate."},
    {"name": "raw_ref", "kind": "str_or_null", "required": True,
     "doc": "Object key of the retained original item bytes, or null when inlined."},
    {"name": "classification", "kind": "str_or_null", "required": True,
     "doc": "Item shape recognized by the frame reader: canonical envelope, or "
            "translation_required for a legacy telemetry body, or null."},
)


class BatchError(ValueError):
    """Input that cannot be represented as a batch frame at all."""


class ItemResult:
    """One item's outcome. Constructed by the reader, serialized into the receipt."""

    def __init__(self, index: int, status: str, *, event_id: Optional[str] = None,
                 dispatchable: bool = False, reasons: Sequence[str] = (),
                 raw_ref: Optional[str] = None,
                 classification: Optional[str] = None) -> None:
        if status not in ITEM_STATUSES:
            raise BatchError("unknown item status %r" % (status,))
        self.index = index
        self.status = status
        self.event_id = event_id
        self.dispatchable = dispatchable
        self.reasons = list(reasons)
        self.raw_ref = raw_ref
        self.classification = classification

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "status": self.status, "event_id": self.event_id,
                "dispatchable": self.dispatchable, "reasons": list(self.reasons),
                "raw_ref": self.raw_ref, "classification": self.classification}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ItemResult(%d, %s, %r)" % (self.index, self.status, self.reasons)


class BatchValidationResult:
    """Frame-level outcome plus per-item outcomes. No silent third state at either level."""

    def __init__(self, frame: Optional[Mapping[str, Any]], errors: Sequence[Dict[str, Any]],
                 warnings: Sequence[Dict[str, Any]],
                 items: Sequence[ItemResult] = ()) -> None:
        self.frame = dict(frame) if frame is not None else None
        self.errors: List[Dict[str, Any]] = [dict(e) for e in errors]
        self.warnings: List[Dict[str, Any]] = [dict(w) for w in warnings]
        self.items: List[ItemResult] = list(items)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def status(self) -> str:
        return "accepted" if self.ok else "refused"

    @property
    def reasons(self) -> List[str]:
        return [e["reason"] for e in self.errors]

    @property
    def unknown_fields(self) -> List[str]:
        return [w["path"] for w in self.warnings if w["reason"] == "unknown_field"]

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "errors": self.errors, "warnings": self.warnings,
                "items": [i.to_dict() for i in self.items]}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "BatchValidationResult(status=%s, errors=%r, items=%d)" % (
            self.status, self.reasons, len(self.items))


# --- item classification -----------------------------------------------------------------

def classify_item(item: Any) -> str:
    """Which shape an item is, without interpreting it.

    Classification is deliberately shallow. A legacy telemetry body is recognized so it is
    routed to the translating adapter instead of being refused as junk, but this contract
    never guesses at its meaning: translation is the adapter's attributable act.
    """
    if not isinstance(item, Mapping):
        return "unrecognized"
    if "schema_version" in item:
        return "canonical"
    if "telemetry_path" in item or "telemetry_schema_version" in item:
        return TRANSLATION_REQUIRED
    return "unrecognized"


def _item_bytes(item: Any) -> int:
    try:
        return len(EV.canonical_bytes(item))
    except EV.EnvelopeError:
        return MAX_ITEM_BYTES + 1


# --- frame validation --------------------------------------------------------------------

def _type_ok(kind: str, value: Any) -> bool:
    if kind == "str":
        return isinstance(value, str)
    if kind == "str_or_null":
        return value is None or isinstance(value, str)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "int_or_null":
        return value is None or (isinstance(value, int) and not isinstance(value, bool))
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "object":
        return isinstance(value, Mapping)
    if kind == "array":
        return isinstance(value, list)
    if kind == "enum":
        return isinstance(value, str)
    raise AssertionError("unhandled field kind %r" % (kind,))


def _check_fields(spec: Iterable[Field], obj: Mapping[str, Any], prefix: str,
                  errors: List[Dict[str, Any]], warnings: List[Dict[str, Any]]) -> None:
    spec = tuple(spec)
    known = {f["name"] for f in spec}
    for field in spec:
        path = prefix + field["name"]
        if field["name"] not in obj:
            if field["required"]:
                errors.append({"reason": "field_missing", "path": path})
            continue
        value = obj[field["name"]]
        if not _type_ok(field["kind"], value):
            errors.append({"reason": "type_invalid", "path": path,
                           "expected": field["kind"], "got": type(value).__name__})
            continue
        if field["kind"] == "enum" and value not in field["enum"]:
            warnings.append({"reason": "unknown_enum_value", "path": path, "value": value})
        if field["kind"] in ("int", "int_or_null") and "minimum" in field \
                and value is not None and value < field["minimum"]:
            errors.append({"reason": "value_out_of_range", "path": path,
                           "minimum": field["minimum"], "got": value})
        if field["kind"] == "object" and "properties" in field:
            _check_fields(field["properties"], value, path + ".", errors, warnings)
    for name in obj:
        if name not in known:
            warnings.append({"reason": "unknown_field", "path": prefix + name})


def validate_batch(frame: Any, *, credential_device_id: Optional[str] = None,
                   validate_items: bool = True) -> BatchValidationResult:
    """Validate one decoded batch frame and classify its items.

    Frame refusal and item refusal are separate on purpose. A frame error (unsupported
    frame major, oversized body, device/credential mismatch) refuses the whole request and
    the producer retries it unchanged. An item error refuses exactly one item, which is
    durably recorded and never resent, so a single poisoned row cannot wedge a backlog.

    `validate_items=False` is for a reader that only needs to admit the frame before
    streaming items to a translator; it never widens what is accepted.
    """
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    if not isinstance(frame, Mapping):
        return BatchValidationResult(None, [{"reason": "not_an_object",
                                             "got": type(frame).__name__}], [])

    version = frame.get("batch_schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        errors.append({"reason": "batch_schema_version_invalid",
                       "path": "batch_schema_version", "got": repr(version)})
    elif version != BATCH_SCHEMA_MAJOR:
        # Refused before any item is read: a v2 frame may reuse `messages` with different
        # framing, so interpreting its items would be a guess about someone else's contract.
        return BatchValidationResult(frame, [{"reason": "batch_schema_version_unsupported",
                                              "path": "batch_schema_version", "got": version,
                                              "supported": BATCH_SCHEMA_MAJOR}], [])

    _check_fields(BATCH_FIELDS, frame, "", errors, warnings)

    batch_id = frame.get("batch_id")
    if isinstance(batch_id, str) and not _BATCH_ID.match(batch_id):
        errors.append({"reason": "batch_id_invalid", "path": "batch_id"})

    sent_at = frame.get("sent_at")
    if isinstance(sent_at, str) and not _RFC3339_UTC.match(sent_at):
        errors.append({"reason": "timestamp_not_rfc3339_utc", "path": "sent_at",
                       "got": sent_at})

    device_id = frame.get("device_id")
    if credential_device_id is not None and isinstance(device_id, str) \
            and device_id != credential_device_id:
        # The credential is the identity. A body claim that disagrees is refused rather than
        # silently corrected, so a misrouted or replayed batch cannot be attributed to a node
        # that never sent it.
        errors.append({"reason": "device_identity_mismatch", "path": "device_id",
                       "credential": credential_device_id})

    messages = frame.get("messages")
    items: List[ItemResult] = []
    if isinstance(messages, list):
        if not messages:
            errors.append({"reason": "batch_empty", "path": "messages"})
        elif len(messages) > MAX_ITEMS_PER_BATCH:
            errors.append({"reason": "batch_too_many_items", "path": "messages",
                           "limit": MAX_ITEMS_PER_BATCH, "got": len(messages)})
        if validate_items and not errors:
            items = classify_and_validate_items(messages)

    return BatchValidationResult(frame, errors, warnings, items)


def classify_and_validate_items(messages: Sequence[Any]) -> List[ItemResult]:
    """Per-item outcomes for an admitted frame.

    Duplicate detection is not decidable here: it needs the durable store. This returns
    `accepted` for an item that is valid in isolation, and the persisting reader downgrades
    it to `duplicate` when `event_id` is already durable. That downgrade is the whole
    replay story, and it is identity-based, so it survives reordering and re-batching.
    """
    results: List[ItemResult] = []
    for index, item in enumerate(messages):
        classification = classify_item(item)
        if classification == "unrecognized":
            results.append(ItemResult(index, "refused", reasons=["item_unrecognized"],
                                      classification=None))
            continue
        size = _item_bytes(item)
        if size > MAX_ITEM_BYTES:
            results.append(ItemResult(index, "refused", reasons=["item_too_large"],
                                      classification=classification))
            continue
        if classification == TRANSLATION_REQUIRED:
            # Recognized, retained, and handed to the translating adapter. Not judged here,
            # and specifically not refused: refusing it would delete a legacy node's data
            # during the window where legacy is the only shape it can emit.
            results.append(ItemResult(index, "deferred", classification=TRANSLATION_REQUIRED))
            continue
        result = EV.validate(item)
        if not result.ok:
            results.append(ItemResult(index, "refused", reasons=sorted(set(result.reasons)),
                                      classification="canonical"))
            continue
        try:
            event_id = EV.derive_event_id(item)
        except EV.EnvelopeError:
            results.append(ItemResult(index, "refused", reasons=["item_not_canonicalizable"],
                                      classification="canonical"))
            continue
        results.append(ItemResult(index, "accepted", event_id=event_id,
                                  dispatchable=result.dispatchable,
                                  classification="canonical"))
    return results


def body_too_large(raw: bytes) -> bool:
    """Server-enforced body bound, checked before decoding so a hostile body cannot expand."""
    return len(raw) > MAX_BATCH_BYTES


# --- acknowledgement ---------------------------------------------------------------------

def ack_through_index(results: Sequence[ItemResult]) -> int:
    """Last index made durable with no gap before it; -1 when nothing is acknowledged.

    Contiguity is the point. A producer releases spool space by index, so acknowledging a
    later durable item while an earlier one is only `deferred` would let the producer free
    a record the server never stored.
    """
    ack = -1
    for result in results:
        if result.status == "deferred":
            break
        ack = result.index
    return ack


def counts(results: Sequence[ItemResult], submitted: Optional[int] = None) -> Dict[str, int]:
    """Conservation accounting. `input = accepted + duplicate + refused + deferred`."""
    tally = {status: 0 for status in ITEM_STATUSES}
    for result in results:
        tally[result.status] += 1
    total = len(results) if submitted is None else submitted
    out = {"submitted": total}
    out.update(tally)
    if sum(tally.values()) != total:
        raise BatchError("batch accounting does not close: %d submitted, %d accounted"
                         % (total, sum(tally.values())))
    return out


def build_receipt(frame: Mapping[str, Any], results: Sequence[ItemResult], *,
                  received_at: str, adapter: str, adapter_version: str,
                  retry_after_s: Optional[int] = None,
                  submitted: Optional[int] = None) -> Dict[str, Any]:
    """The response body a producer acknowledges against.

    The receipt is authoritative for spool release and for nothing else. It deliberately
    does not echo item bodies: a producer that needs the stored form re-reads it by
    `event_id`, and a receipt that quoted bodies back would double every batch's bytes on
    the exact link that is already the constraint.
    """
    if not isinstance(frame, Mapping):
        raise BatchError("receipt needs the submitted frame")
    return {
        "batch_schema_version": BATCH_SCHEMA_MAJOR,
        "batch_id": frame.get("batch_id"),
        "received_at": received_at,
        "ack_through_index": ack_through_index(results),
        "counts": counts(results, submitted),
        "results": [r.to_dict() for r in results],
        "retry_after_s": retry_after_s,
        "server": {"adapter": adapter, "version": adapter_version,
                   "envelope_major": EV.SCHEMA_MAJOR},
    }


def unacknowledged_indices(receipt: Mapping[str, Any]) -> List[int]:
    """Indices a producer must resend: everything after the acknowledged durable prefix.

    A refused item inside the acknowledged prefix is NOT resent. It is durable as a refusal
    record, and resending it would loop forever on a body the server will never accept.
    """
    ack = receipt.get("ack_through_index")
    results = receipt.get("results") or []
    if not isinstance(ack, int):
        raise BatchError("receipt has no usable ack_through_index")
    return [r["index"] for r in results
            if isinstance(r, Mapping) and isinstance(r.get("index"), int) and r["index"] > ack]


# --- idempotency -------------------------------------------------------------------------

def request_fingerprint(raw: bytes) -> str:
    """Digest of the exact submitted bytes, for `Idempotency-Key` reuse detection.

    Same key plus same fingerprint is a retry and replays the stored receipt. Same key plus
    a different fingerprint is a client bug and is a conflict, because returning either
    result would be a lie about which body was stored.
    """
    return hashlib.sha256(raw).hexdigest()


def idempotency_scope(*, site_id: str, principal: str, route: str, key: str) -> str:
    """Storage scope for an idempotency key: site + principal + route + key.

    Scoping by principal is what stops one device's key from colliding with another's, and
    scoping by route is what stops a key reused across endpoints from returning a receipt
    for an operation the caller never made.
    """
    blob = "\x1f".join((site_id, principal, route, key)).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def batch_refusal_record(raw: bytes, reasons: Sequence[str], *, source: str, adapter: str,
                         received_at: str, batch_id: Optional[str] = None,
                         raw_ref: Optional[str] = None) -> Dict[str, Any]:
    """Durable evidence for a refused frame. A whole-frame refusal is still not a drop."""
    record = EV.refusal_record(raw, reasons, source=source, adapter=adapter,
                               received_at=received_at, raw_ref=raw_ref)
    record["schema_id"] = BATCH_SCHEMA_ID
    record["batch_id"] = batch_id
    return record


def codec_for_media_type(media_type: str) -> str:
    """Resolve a batch Content-Type to a registered codec."""
    base = media_type.split(";", 1)[0].strip().lower()
    for codec, registered in BATCH_CODEC_MEDIA_TYPES.items():
        if base == registered:
            return codec
    if base == "application/json":
        return "json"
    raise BatchError("unsupported batch media type %r" % (media_type,))


def decode_batch(raw: bytes, codec: str = "json") -> Dict[str, Any]:
    """Bytes to a frame dict, after the size bound. The caller retains `raw` first."""
    if codec not in BATCH_CODEC_MEDIA_TYPES:
        raise BatchError("unregistered batch codec %r" % (codec,))
    if body_too_large(raw):
        raise BatchError("batch body exceeds %d bytes" % MAX_BATCH_BYTES)
    try:
        return EV.decode(raw, codec)
    except EV.EnvelopeError as exc:
        raise BatchError(str(exc)) from exc


# --- generated artifacts -----------------------------------------------------------------

def _json_schema_for(field: Field, item_schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    kind = field["kind"]
    if kind == "str":
        node: Dict[str, Any] = {"type": "string"}
    elif kind == "str_or_null":
        node = {"type": ["string", "null"]}
    elif kind == "int":
        node = {"type": "integer"}
        if "minimum" in field:
            node["minimum"] = field["minimum"]
    elif kind == "int_or_null":
        node = {"type": ["integer", "null"]}
        if "minimum" in field:
            node["minimum"] = field["minimum"]
    elif kind == "bool":
        node = {"type": "boolean"}
    elif kind == "enum":
        # Advertised, not constrained, for the same reason as the envelope: an unknown
        # member from a newer peer is preserved, never turned into a schema failure.
        node = {"type": "string", "x-known-values": list(field["enum"])}
    elif kind == "array":
        node = {"type": "array"}
        if item_schema is not None:
            node["items"] = item_schema
    elif kind == "object":
        node = {"type": "object"}
        if "properties" in field:
            props = {}
            required = []
            for sub in field["properties"]:
                props[sub["name"]] = _json_schema_for(sub)
                if sub["required"]:
                    required.append(sub["name"])
            node["properties"] = props
            if required:
                node["required"] = required
            node["additionalProperties"] = True
    else:  # pragma: no cover - guarded by _type_ok
        raise AssertionError(kind)
    node["description"] = field["doc"]
    return node


def _result_schema() -> Dict[str, Any]:
    props = {f["name"]: _json_schema_for(f) for f in RESULT_FIELDS}
    props["reasons"]["items"] = {"type": "string"}
    return {
        "type": "object",
        "title": "hear.ingest.batch.v1 item result",
        "additionalProperties": True,
        "required": [f["name"] for f in RESULT_FIELDS if f["required"]],
        "properties": props,
    }


def batch_schema_document() -> Dict[str, Any]:
    """Published batch frame schema, generated from BATCH_FIELDS so they cannot drift."""
    item_schema = {
        "description": (
            "One %s envelope, or a legacy telemetry body during the mixed-version window. "
            "Items are not constrained by this schema: the item contract is %s and a legacy "
            "body is translated by the edge adapter, not by the frame reader."
            % (EV.SCHEMA_ID, EV.SCHEMA_URI)),
        "type": "object",
    }
    properties = {f["name"]: _json_schema_for(f, item_schema) for f in BATCH_FIELDS}
    properties["batch_schema_version"]["const"] = BATCH_SCHEMA_MAJOR
    properties["messages"]["maxItems"] = MAX_ITEMS_PER_BATCH
    properties["messages"]["minItems"] = 1
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": BATCH_SCHEMA_URI,
        "title": BATCH_SCHEMA_ID,
        "description": (
            "DAMA Hear HTTPS batch ingest request frame, major version 1. Normative wire "
            "codec is UTF-8 JSON (%s). The frame major versions the framing only; each item "
            "carries its own schema_version. Readers MUST accept unknown properties within "
            "this major, MUST refuse an unsupported batch_schema_version before reading any "
            "item, and MUST refuse a single malformed item without refusing the batch."
            % BATCH_CODEC_MEDIA_TYPES["json"]),
        "type": "object",
        "additionalProperties": True,
        "required": [f["name"] for f in BATCH_FIELDS if f["required"]],
        "properties": properties,
        "x-schema-id": BATCH_SCHEMA_ID,
        "x-schema-major": BATCH_SCHEMA_MAJOR,
        "x-item-schema": EV.SCHEMA_URI,
        "x-codecs": dict(BATCH_CODEC_MEDIA_TYPES),
        "x-limits": {
            "max_items_per_batch": MAX_ITEMS_PER_BATCH,
            "max_batch_bytes": MAX_BATCH_BYTES,
            "max_item_bytes": MAX_ITEM_BYTES,
            "max_clock_skew_s": MAX_CLOCK_SKEW_S,
        },
    }


def receipt_schema_document() -> Dict[str, Any]:
    """Published receipt schema. The producer's spool-release rule is `ack_through_index`."""
    properties = {f["name"]: _json_schema_for(f, _result_schema()) for f in RECEIPT_FIELDS}
    properties["batch_schema_version"]["const"] = BATCH_SCHEMA_MAJOR
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": RECEIPT_SCHEMA_URI,
        "title": RECEIPT_SCHEMA_ID,
        "description": (
            "Receipt for one %s submission (%s). counts.submitted equals accepted + "
            "duplicate + refused + deferred. A producer may release spooled items only "
            "through ack_through_index; items after it are not durable."
            % (BATCH_SCHEMA_ID, RECEIPT_MEDIA_TYPE)),
        "type": "object",
        "additionalProperties": True,
        "required": [f["name"] for f in RECEIPT_FIELDS if f["required"]],
        "properties": properties,
        "x-schema-id": RECEIPT_SCHEMA_ID,
        "x-schema-major": BATCH_SCHEMA_MAJOR,
        "x-media-type": RECEIPT_MEDIA_TYPE,
        "x-item-statuses": list(ITEM_STATUSES),
    }
