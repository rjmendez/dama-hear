"""Canonical `hear.ingest.v1` envelope: field table, validator, identity and codec.

This module is the single source of truth for the envelope. `contracts/schemas/` and
`contracts/fixtures/` are GENERATED from the table below by `tools/gen_ingest_contracts.py`;
`tests/test_ingest_envelope_v1.py` fails if the checked-in artifacts drift from it.

Three rules carry the compatibility weight, and each has a test:

1. **The envelope is codec-agnostic; v1 pins exactly one wire codec (JSON).** Decoding is a
   registry lookup, so a second codec can be added later without a schema major bump. See
   `docs/decisions/0001-hear-ingest-v1-envelope-and-codec.md`.
2. **`event_id` is derived from a CLOSED identity tuple, never from the whole envelope.**
   A future additive field therefore cannot change an event's identity, so a mixed-version
   fleet cannot produce two durable events for one observation.
3. **Unknown is not invalid.** Unknown fields and unknown enum members inside a supported
   major are accepted and preserved verbatim, but an envelope carrying an unknown enum
   member is not dispatchable to workers that switch on that member. An unsupported major
   is refused with a machine reason and a durable refusal record -- never dropped, never
   guessed.

Nothing here may name a transport, a Redis key, a PVC path, an AWS resource or a
gotchi-specific field: adapters translate into this shape, the core never reaches back out.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SCHEMA_ID = "hear.ingest.v1"
SCHEMA_MAJOR = 1
"""The only `schema_version` this reader supports. A larger major is refused, not guessed."""

SCHEMA_URI = "https://schemas.dama.example/hear/ingest/v1/envelope.schema.json"

DEFAULT_CODEC = "json"
CODEC_MEDIA_TYPES: Dict[str, str] = {
    "json": "application/vnd.dama.hear.ingest.v1+json",
}
"""Registered wire codecs for v1. Exactly one is normative; the mapping is the seam that
lets a measured bandwidth constraint add a second codec without touching field meaning."""

IDENTITY_NAMESPACE = uuid.UUID("6f1f6f6a-9a3f-5f7a-9c2b-0f4a2d1e7b55")
"""Fixed namespace for content-derived event IDs. Changing it re-identifies the world, so it
is frozen for the life of the v1 major."""

# --- enums -------------------------------------------------------------------------------
SOURCES: Tuple[str, ...] = ("node-http", "node-mqtt", "gotchi", "import")
KINDS: Tuple[str, ...] = ("heartbeat", "detection", "scene", "clip", "sketch", "health")
CLOCK_TIERS: Tuple[str, ...] = ("gps_pps", "wall", "monotonic")

_RFC3339_UTC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$"
)

# --- field table -------------------------------------------------------------------------
# kind: str, str_or_null, int, bool, object, enum, enum_or_null
Field = Dict[str, Any]

FIELDS: Tuple[Field, ...] = (
    {"name": "event_id", "kind": "str", "required": True,
     "doc": "Stable content-derived UUID. See derive_event_id(); identity inputs are closed."},
    {"name": "source", "kind": "enum", "required": True, "enum": SOURCES,
     "doc": "Ingress family that produced this envelope, not the transport endpoint."},
    {"name": "site_id", "kind": "str", "required": True,
     "doc": "Site token. Derived from the credential; never trusted from an untrusted body."},
    {"name": "device_id", "kind": "str", "required": True,
     "doc": "Node or producer identity as enrolled."},
    {"name": "device_class", "kind": "str", "required": True,
     "doc": "Board or producer class, e.g. esp32s3-i2s-gps."},
    {"name": "firmware_version", "kind": "str", "required": True,
     "doc": "Producer build identifier."},
    {"name": "observed_at", "kind": "str_or_null", "required": True,
     "doc": "RFC3339 UTC instant of observation, or null when the producer had no usable clock."},
    {"name": "received_at", "kind": "str", "required": True,
     "doc": "RFC3339 UTC instant the adapter accepted the bytes. Never a producer claim."},
    {"name": "clock", "kind": "object", "required": True,
     "doc": "Clock evidence for observed_at.",
     "properties": (
         {"name": "valid", "kind": "bool", "required": True,
          "doc": "False means observed_at is not usable for precise localization."},
         {"name": "tier", "kind": "enum", "required": True, "enum": CLOCK_TIERS,
          "doc": "Timebase class. wall/monotonic are never treated as PPS time."},
         {"name": "sigma_ns", "kind": "int", "required": True, "minimum": 0,
          "doc": "Declared 1-sigma UTC uncertainty in nanoseconds."},
     )},
    {"name": "kind", "kind": "enum", "required": True, "enum": KINDS,
     "doc": "Record family. An unknown member is preserved but not dispatchable."},
    {"name": "schema_version", "kind": "int", "required": True, "minimum": 1,
     "doc": "Envelope major version. Unsupported majors are refused, not coerced."},
    {"name": "payload", "kind": "object", "required": True,
     "doc": "Kind-specific body. Binary frames travel here base64-encoded with their own "
            "version/profile fields; the envelope never reinterprets them."},
    {"name": "raw_ref", "kind": "str_or_null", "required": True,
     "doc": "Object-store key of the retained original bytes, or null when inlined."},
    {"name": "adapter", "kind": "object", "required": True,
     "doc": "Translating adapter identity, so a bad translation is attributable.",
     "properties": (
         {"name": "name", "kind": "str", "required": True, "doc": "Adapter name."},
         {"name": "version", "kind": "str", "required": True, "doc": "Adapter build."},
     )},
    {"name": "producer", "kind": "object", "required": False,
     "doc": "Optional producer continuity evidence. Additive in v1: absent means the "
            "producer exposes no boot/sequence/cursor, not that continuity is fine.",
     "properties": (
         {"name": "boot_id", "kind": "str", "required": False,
          "doc": "Producer boot identity; a change means cursors are not comparable."},
         {"name": "boot_epoch_us", "kind": "int", "required": False,
          "doc": "Producer boot epoch in microseconds UTC."},
         {"name": "sequence", "kind": "int", "required": False, "minimum": 0,
          "doc": "Monotonic per-boot record counter."},
         {"name": "cursor", "kind": "str_or_null", "required": False,
          "doc": "Opaque source partition/cursor token the record was read at."},
     )},
)

_FIELDS_BY_NAME: Dict[str, Field] = {f["name"]: f for f in FIELDS}

IDENTITY_INPUTS: Tuple[str, ...] = (
    "source", "device_id", "kind", "observed_at", "producer.boot_id",
    "producer.sequence", "producer.cursor", "payload",
)
"""CLOSED set. Adding to it is a major version change; nothing else may enter the digest."""


class EnvelopeError(ValueError):
    """Malformed input that cannot be represented as an envelope at all."""


class ValidationResult:
    """Explicit accepted/refused outcome. There is no silent third state."""

    def __init__(self, envelope: Optional[Mapping[str, Any]], errors: Sequence[Dict[str, Any]],
                 warnings: Sequence[Dict[str, Any]]) -> None:
        self.envelope = dict(envelope) if envelope is not None else None
        self.errors: List[Dict[str, Any]] = [dict(e) for e in errors]
        self.warnings: List[Dict[str, Any]] = [dict(w) for w in warnings]

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

    @property
    def dispatchable(self) -> bool:
        """An accepted envelope whose enum members are all understood by this build.

        Forward traffic from a newer writer stays storable and replayable; it simply is not
        handed to a worker that would have to guess what the new member means.
        """
        return self.ok and not any(w["reason"] == "unknown_enum_value" for w in self.warnings)

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "errors": self.errors, "warnings": self.warnings,
                "dispatchable": self.dispatchable}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "ValidationResult(status=%s, errors=%r, warnings=%r)" % (
            self.status, self.reasons, [w["reason"] for w in self.warnings])


# --- canonical form and identity ---------------------------------------------------------

def canonical_bytes(value: Any) -> bytes:
    """Deterministic UTF-8 serialization used for digests, never for the wire.

    Keys sorted, no insignificant whitespace, non-finite floats refused. This is what makes
    `event_id` independent of the transport codec and of key ordering, so the same
    observation retried over a different codec is still one durable event.
    """
    _reject_non_finite(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _reject_json_constant(token: str) -> None:
    """Refuse the `NaN`/`Infinity` JSON literals rather than decoding them into floats."""
    raise ValueError("non-finite literal %r is not representable in the envelope" % (token,))


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise EnvelopeError("non-finite number is not representable in the envelope")
    elif isinstance(value, Mapping):
        for v in value.values():
            _reject_non_finite(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _reject_non_finite(v)


def identity_tuple(envelope: Mapping[str, Any]) -> Dict[str, Any]:
    """The closed identity inputs pulled out of an envelope, missing ones as null."""
    producer = envelope.get("producer") or {}
    if not isinstance(producer, Mapping):
        raise EnvelopeError("producer must be an object when present")
    return {
        "source": envelope.get("source"),
        "device_id": envelope.get("device_id"),
        "kind": envelope.get("kind"),
        "observed_at": envelope.get("observed_at"),
        "boot_id": producer.get("boot_id"),
        "sequence": producer.get("sequence"),
        "cursor": producer.get("cursor"),
        "payload": envelope.get("payload"),
    }


def derive_event_id(envelope: Mapping[str, Any]) -> str:
    """Content-derived UUID over the closed identity tuple.

    A UUIDv8 whose bytes are a SHA-256 prefix: collision-resistant, and unlike uuid5 it does
    not depend on SHA-1. Retries, duplicate transports and adapter restarts converge on one
    ID; an additive v1 field never moves it.
    """
    blob = SCHEMA_ID.encode("ascii") + b"\x00" + canonical_bytes(identity_tuple(envelope))
    digest = hashlib.sha256(IDENTITY_NAMESPACE.bytes + blob).digest()[:16]
    b = bytearray(digest)
    b[6] = (b[6] & 0x0F) | 0x80  # version 8 (custom)
    b[8] = (b[8] & 0x3F) | 0x80  # RFC 4122 variant
    return str(uuid.UUID(bytes=bytes(b)))


def verify_event_id(envelope: Mapping[str, Any]) -> bool:
    try:
        return envelope.get("event_id") == derive_event_id(envelope)
    except EnvelopeError:
        return False


# --- validation --------------------------------------------------------------------------

def _type_ok(kind: str, value: Any) -> bool:
    if kind == "str":
        return isinstance(value, str)
    if kind == "str_or_null":
        return value is None or isinstance(value, str)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "object":
        return isinstance(value, Mapping)
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
        if field["kind"] == "int" and "minimum" in field and value < field["minimum"]:
            errors.append({"reason": "value_out_of_range", "path": path,
                           "minimum": field["minimum"], "got": value})
        if field["kind"] == "object" and "properties" in field:
            _check_fields(field["properties"], value, path + ".", errors, warnings)
    for name in obj:
        if name not in known:
            warnings.append({"reason": "unknown_field", "path": prefix + name})


def validate(envelope: Any, *, strict_unknown: bool = False,
             require_event_id_match: bool = False) -> ValidationResult:
    """Validate one decoded envelope.

    `strict_unknown` exists for producer-side CI only. A reader must never enable it: that
    would turn a newer peer's additive field into a durable refusal and make rollout
    order load-bearing.
    """
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    if not isinstance(envelope, Mapping):
        return ValidationResult(None, [{"reason": "not_an_object",
                                        "got": type(envelope).__name__}], [])

    version = envelope.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        errors.append({"reason": "schema_version_invalid", "path": "schema_version",
                       "got": repr(version)})
    elif version != SCHEMA_MAJOR:
        # Refused before any other interpretation: a v2 body may reuse a v1 field name with
        # a different meaning, so field-level checks would be an educated guess.
        return ValidationResult(envelope, [{"reason": "schema_version_unsupported",
                                            "path": "schema_version", "got": version,
                                            "supported": SCHEMA_MAJOR}], [])

    _check_fields(FIELDS, envelope, "", errors, warnings)

    for key in ("observed_at", "received_at"):
        value = envelope.get(key)
        if isinstance(value, str) and not _RFC3339_UTC.match(value):
            errors.append({"reason": "timestamp_not_rfc3339_utc", "path": key, "got": value})

    clock = envelope.get("clock")
    if isinstance(clock, Mapping) and clock.get("valid") is True \
            and envelope.get("observed_at") is None:
        errors.append({"reason": "clock_valid_without_observed_at", "path": "clock.valid"})

    if require_event_id_match and not errors and not verify_event_id(envelope):
        errors.append({"reason": "event_id_not_derived", "path": "event_id"})

    if strict_unknown:
        for warning in list(warnings):
            if warning["reason"] == "unknown_field":
                errors.append({"reason": "unknown_field_strict", "path": warning["path"]})

    return ValidationResult(envelope, errors, warnings)


# --- codec -------------------------------------------------------------------------------

def encode(envelope: Mapping[str, Any], codec: str = DEFAULT_CODEC) -> bytes:
    if codec not in CODEC_MEDIA_TYPES:
        raise EnvelopeError("unregistered codec %r" % (codec,))
    _reject_non_finite(envelope)
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def decode(raw: bytes, codec: str = DEFAULT_CODEC) -> Dict[str, Any]:
    """Bytes to a dict. Raises EnvelopeError; the caller records a durable refusal.

    The raw bytes must already be retained by the adapter before this is called.
    """
    if codec not in CODEC_MEDIA_TYPES:
        raise EnvelopeError("unregistered codec %r" % (codec,))
    try:
        # `NaN`/`Infinity` are JSON literals Python accepts by default but the canonical
        # form cannot represent, so a body carrying one could never be given an event_id.
        # Refusing it here makes that a clean, attributable refusal instead of a surprise
        # much later. RecursionError is not a ValueError, so it needs naming explicitly:
        # a deeply nested body is a refusal, never an unhandled crash with no accounting.
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise EnvelopeError("undecodable %s body: %s" % (codec, exc)) from exc
    if not isinstance(value, dict):
        raise EnvelopeError("envelope body is %s, not an object" % type(value).__name__)
    return value


def codec_for_media_type(media_type: str) -> str:
    """Resolve a Content-Type to a registered codec, so adding one is a registry edit."""
    base = media_type.split(";", 1)[0].strip().lower()
    for codec, registered in CODEC_MEDIA_TYPES.items():
        if base == registered:
            return codec
    if base == "application/json":
        return "json"
    raise EnvelopeError("unsupported media type %r" % (media_type,))


def refusal_record(raw: bytes, reasons: Sequence[str], *, source: str, adapter: str,
                   received_at: str, raw_ref: Optional[str] = None) -> Dict[str, Any]:
    """Durable evidence for a refused body. No adapter silently drops a row."""
    return {
        "schema_id": SCHEMA_ID,
        "refusal_version": 1,
        "source": source,
        "adapter": adapter,
        "received_at": received_at,
        "raw_ref": raw_ref,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_bytes": len(raw),
        "reasons": list(reasons),
    }


# --- generated artifacts -----------------------------------------------------------------

def _json_schema_for(field: Field) -> Dict[str, Any]:
    kind = field["kind"]
    if kind == "str":
        node: Dict[str, Any] = {"type": "string"}
    elif kind == "str_or_null":
        node = {"type": ["string", "null"]}
    elif kind == "int":
        node = {"type": "integer"}
        if "minimum" in field:
            node["minimum"] = field["minimum"]
    elif kind == "bool":
        node = {"type": "boolean"}
    elif kind == "enum":
        # Advertised, deliberately NOT constrained: an unknown member from a newer writer is
        # preserved, and the reader marks it non-dispatchable instead of refusing it.
        node = {"type": "string", "x-known-values": list(field["enum"])}
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


def schema_document() -> Dict[str, Any]:
    """The published JSON Schema, generated from FIELDS so the two cannot drift."""
    properties = {f["name"]: _json_schema_for(f) for f in FIELDS}
    properties["schema_version"]["const"] = SCHEMA_MAJOR
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_URI,
        "title": SCHEMA_ID,
        "description": (
            "Canonical DAMA Hear ingest envelope, major version 1. Normative wire codec is "
            "UTF-8 JSON (%s). Readers MUST accept unknown properties and unknown enum "
            "members within this major and MUST refuse an unsupported schema_version with a "
            "durable refusal record. event_id is derived only from %s."
            % (CODEC_MEDIA_TYPES["json"], ", ".join(IDENTITY_INPUTS))
        ),
        "type": "object",
        "additionalProperties": True,
        "required": [f["name"] for f in FIELDS if f["required"]],
        "properties": properties,
        "x-schema-id": SCHEMA_ID,
        "x-schema-major": SCHEMA_MAJOR,
        "x-codecs": dict(CODEC_MEDIA_TYPES),
        "x-identity-inputs": list(IDENTITY_INPUTS),
    }
