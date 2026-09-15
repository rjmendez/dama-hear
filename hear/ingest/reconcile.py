"""Dual-write reconciliation semantics: correlation, classification and mismatch receipts.

This module is the source of truth for how a *future* legacy + HTTPS dual-write is observed
and reconciled. It decides nothing about transport and performs no I/O: no sockets, no
store, no scheduler, no credential material. A comparator process is expected to read both
sides, hand pairs to `classify()`, and persist whatever `build_receipt()` returns.

The legacy path stays authoritative for the whole of Phase 4. Everything here is therefore
written from the position that **legacy is the reference and canonical is the claimant**:

1. **Nothing is re-derived.** The comparator never recomputes a legacy `record_uid`, never
   recomputes an `event_id`, and never re-times or re-profiles either side. Both identities
   are recorded by the dual-writer at write time and joined here. A join that cannot be made
   is an *orphan classification*, never a silent drop and never a guess.
2. **Every compared pair reaches exactly one terminal classification, or stays `pending`.**
   `CLASSIFICATIONS` is closed and `conservation()` asserts the accounting closes, the same
   discipline `hear/ingest/batch.py` applies to `submitted = accepted + duplicate + refused
   + deferred`. There is no "other" bucket, because an "other" bucket is where a real loss
   goes to be ignored.
3. **Absence before the grace window is not a mismatch.** A canonical write that has not
   landed yet is `pending`. Without this a reconciler pages on its own scheduling jitter,
   the operator mutes it, and the one real loss arrives muted.
4. **A receipt carries hashes, paths and reason codes -- not payloads.** `redact_value()`
   is deny-list-first: a field is quoted literally only if it is on the allow list AND not
   on the deny list. Locations, credentials and clip bytes can never be quoted, whatever a
   future field table calls them.
5. **Unknown is not invalid.** An unknown classification member from a newer comparator is
   preserved and marked non-actionable rather than coerced to `match`, mirroring rule 3 of
   `docs/decisions/0001-hear-ingest-v1-envelope-and-codec.md`.

Deliberately **not** named `*SCHEMA*`: `tools/freeze_contracts.py` harvests published
contract ids from `[A-Z_]*SCHEMA[A-Z_]*` assignments, and this receipt is not published
under `contracts/` yet. `docs/decisions/0005-phase4-dual-write-observability.md` records why
and states the exact promotion procedure.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

RECEIPT_CONTRACT_ID = "hear.reconcile.receipt.v1"
RECEIPT_CONTRACT_MAJOR = 1
"""The only receipt major this reader supports. A larger major is refused, not guessed."""

RECEIPT_CONTRACT_URI = (
    "https://schemas.dama.example/hear/reconcile/v1/receipt.schema.json"
)
RECEIPT_MEDIA_TYPE = "application/vnd.dama.hear.reconcile.receipt.v1+json"

CORRELATION_NAMESPACE = uuid.UUID("2b6f0d4e-7d1a-5a7e-9f31-4c8b6a2d905f")
"""Fixed namespace for deterministic receipt ids. Changing it re-identifies every receipt
ever written, so it is frozen for the life of the v1 major."""

# --- vocabularies (all closed) -----------------------------------------------------------

SIDES: Tuple[str, ...] = ("legacy", "canonical")

SIDE_OUTCOMES: Tuple[str, ...] = ("written", "refused", "errored", "absent")
"""What one side did with an observation. `absent` is "no row at all", which is different
from `refused` ("a durable record saying no") and from `errored` ("the write itself failed
and we know it failed")."""

BINDINGS: Tuple[str, ...] = ("bound", "legacy_orphan", "canonical_orphan")
"""How the two sides were joined. `bound` means the dual-writer recorded the
(legacy_uid, event_id) pair; an orphan means only one side has an identity at all."""

CREDENTIAL_SCOPES: Tuple[str, ...] = ("device", "site", "unknown")

CLASSIFICATIONS: Tuple[str, ...] = (
    "match",
    "pending",
    "missing_canonical",
    "missing_legacy",
    "duplicate_canonical",
    "duplicate_legacy",
    "stale_canonical",
    "stale_legacy",
    "outcome_divergence",
    "value_divergence",
    "order_divergence",
    "identity_divergence",
    "error_legacy",
    "error_canonical",
    "unclassified",
)

NON_TERMINAL: Tuple[str, ...] = ("pending",)
TERMINAL: Tuple[str, ...] = tuple(c for c in CLASSIFICATIONS if c not in NON_TERMINAL)

MISMATCH_CLASSIFICATIONS: Tuple[str, ...] = tuple(
    c for c in TERMINAL if c != "match"
)

SEVERITIES: Tuple[str, ...] = ("info", "warn", "critical")

SEVERITY_BY_CLASSIFICATION: Dict[str, str] = {
    "match": "info",
    "pending": "info",
    # A canonical row that never arrived is the loss the whole exercise exists to find.
    "missing_canonical": "critical",
    # Canonical-only is not data loss -- legacy is authoritative and still has everything it
    # ever had -- but it means something wrote canonically outside the dual-writer.
    "missing_legacy": "warn",
    "duplicate_canonical": "critical",
    "duplicate_legacy": "warn",
    "stale_canonical": "warn",
    "stale_legacy": "warn",
    "outcome_divergence": "critical",
    "value_divergence": "critical",
    "order_divergence": "warn",
    # Two sides disagreeing about WHO sent something is an authentication defect, not a
    # data-quality one. It is never sampled away and never auto-repaired.
    "identity_divergence": "critical",
    "error_legacy": "critical",
    "error_canonical": "warn",
    # Undecidable is louder than wrong: a comparator that cannot classify has stopped being
    # evidence, and pretending otherwise is how a dashboard goes green over a hole.
    "unclassified": "critical",
}

REPAIR_ACTIONS: Tuple[str, ...] = (
    "none",
    "replay_inbox",
    "replay_outbox",
    "manual_review",
)
"""Repair is replay-only. There is no action here that edits, deletes or re-derives a row on
either side, because a comparator that can write to the thing it audits is not an audit."""

REPAIR_BY_CLASSIFICATION: Dict[str, str] = {
    "match": "none",
    "pending": "none",
    "missing_canonical": "replay_inbox",
    "missing_legacy": "manual_review",
    "duplicate_canonical": "manual_review",
    "duplicate_legacy": "manual_review",
    "stale_canonical": "replay_outbox",
    "stale_legacy": "manual_review",
    "outcome_divergence": "manual_review",
    "value_divergence": "manual_review",
    "order_divergence": "none",
    "identity_divergence": "manual_review",
    "error_legacy": "manual_review",
    "error_canonical": "replay_inbox",
    "unclassified": "manual_review",
}

# --- tolerances --------------------------------------------------------------------------

LIVE_GRACE_S = 300
"""Seconds a live-path canonical write may lag its legacy write before absence becomes
`missing_canonical`. Below this, absence is `pending`."""

BACKFILL_GRACE_S = 86_400
"""The same bound for drain/import paths, where a node's SD backlog is replayed in bulk and
a day-late canonical write is normal rather than anomalous."""

ORDER_GRACE_S = 300
"""A producer-sequence gap is only `order_divergence` once it has outlived this. Batches
legitimately arrive out of order; a gap that closes itself was never a defect."""

MAX_DIFFERENCES = 32
"""Differences retained per receipt. Beyond this the receipt records how many were dropped
rather than growing without bound; the raw refs remain the full evidence."""

MAX_RECEIPT_BYTES = 8_192
"""Encoded receipt bound. A receipt is an index into evidence, not the evidence."""

DEFAULT_AUDIT_DENOMINATOR = 1_000
"""Matches are sampled 1-in-N for audit. Mismatches are never sampled (`sample_receipt()`)."""

COMPARATORS: Tuple[str, ...] = ("exact", "hash", "tier", "numeric", "count")

TOLERANCE_BY_COMPARATOR: Dict[str, str] = {
    # Raw objects: byte-exact. A tolerance on retained bytes is a tolerance on corruption.
    "hash": "sha256 equal",
    # GPS-anchored instants and identity strings.
    "exact": "equal",
    # An unanchored instant is compared as its clock tier, never as a value: two honest
    # writers with no GPS fix will disagree on the number and agree on the meaning.
    "tier": "clock tier equal, value ignored",
    # Solver/model outputs carry a recorded numerical tolerance plus model-card identity.
    "numeric": "abs diff <= declared tolerance and model card equal",
    # Counters and row counts close per source/day/node.
    "count": "equal",
}

# --- redaction ---------------------------------------------------------------------------

REDACTION_PROFILE = "hear.reconcile.redact.v1"

VALUE_ALLOW_PATHS: Tuple[str, ...] = (
    "telemetry_path",
    "telemetry_schema_version",
    "schema_version",
    "batch_schema_version",
    "event_type",
    "event_seq",
    "uptime_s",
    "class",
    "device_class",
    "fw_version",
    "firmware_version",
    "source",
    "kind",
    "status",
    "outcome",
    "reason",
    "clock.tier",
    "gps.fix",
    "time.valid",
    "wifi.rssi_dbm",
    "counters.scene_rows_written",
    "counters.dets_rows_written",
    "counters.clips_written",
    "counters.clips_evicted",
    "producer.boot_id",
    "producer.batch_sequence",
    "producer.cursor",
)
"""Field paths whose value may be quoted literally in a receipt. Everything else is hashed.
An allow list rather than a deny list, because the next field added upstream must default to
redacted -- the other way round, it defaults to leaked."""

VALUE_DENY_PATTERN = re.compile(
    r"(?:^|[._-])(?:lat|lon|lng|latitude|longitude|coord|coords|coordinate|position|"
    r"gps_fix_lat|gps_fix_lon|token|secret|password|passwd|credential|bearer|auth|"
    r"authorization|private|pem|payload|body|raw|audio|pcm|samples|clip_bytes)"
    r"(?:$|[._-])",
    re.IGNORECASE,
)
"""Deny always beats allow. `tools/coord_guard.py` already makes a real-world coordinate a
merge-blocking defect in the tree; a receipt store is the obvious way to reintroduce one,
so location-shaped paths are unquotable by construction rather than by reviewer attention.
`key_id` is deliberately NOT matched: an opaque key identifier is how a rotation is
attributed, and it is not key material."""


def _hash_token(value: Any) -> str:
    """Stable 16-hex digest of a value's canonical JSON form."""
    try:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True,
                             allow_nan=False, ensure_ascii=True)
    except (TypeError, ValueError):
        encoded = json.dumps(repr(value), separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def path_is_quotable(path: str) -> bool:
    """True when a field path's value may appear literally in a receipt."""
    if not isinstance(path, str) or not path:
        return False
    if VALUE_DENY_PATTERN.search(path):
        return False
    return path in VALUE_ALLOW_PATHS


def redact_value(path: str, value: Any) -> Any:
    """A receipt-safe rendering of one compared value."""
    if path_is_quotable(path) and isinstance(value, (str, int, float, bool, type(None))):
        return value
    if value is None:
        return None
    return {"redacted": "sha256:" + _hash_token(value)}


def state_hash(projection: Mapping[str, Any]) -> str:
    """Comparable digest of one side's *projected* state.

    The projection is an explicit field mapping built by the comparator, never a whole
    stored row: hashing a whole row would make every additive field a mismatch, which is the
    failure that makes people turn a reconciler off.
    """
    if not isinstance(projection, Mapping):
        raise TypeError("projection must be a mapping")
    encoded = json.dumps(dict(projection), separators=(",", ":"), sort_keys=True,
                         allow_nan=False, ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --- correlation identity ----------------------------------------------------------------


def correlation_id(*, legacy_uid: Optional[str], event_id: Optional[str],
                   site_id: str, device_id: str, telemetry_path: str) -> str:
    """Join key for one observation across both writers.

    The key is prefixed by the side that owns it. Legacy wins whenever a legacy row exists,
    because legacy is authoritative for all of Phase 4 and an authoritative record must not
    change identity when the claimant appears or disappears.

    Site, device and path are folded in so that a legacy `record_uid` (itself already scoped
    by `telemetry_path` + `device_id` in `tools/hear_heartbeat_receiver.py`) and an
    `event_id` (a UUID over ADR 0001's closed tuple) live in one namespace without either
    being recomputed from the other.
    """
    site = _require_token("site_id", site_id)
    device = _require_token("device_id", device_id)
    path = _require_token("telemetry_path", telemetry_path)
    if legacy_uid:
        side, anchor = "legacy", legacy_uid
    elif event_id:
        side, anchor = "canonical", event_id
    else:
        raise ValueError("correlation needs a legacy_uid or an event_id")
    scope = "\x1f".join((RECEIPT_CONTRACT_ID, side, site, device, path, str(anchor)))
    return side + ":" + hashlib.sha256(scope.encode("utf-8")).hexdigest()


def binding_of(*, legacy_uid: Optional[str], event_id: Optional[str]) -> str:
    """How the pair was joined, from the identities the dual-writer recorded."""
    if legacy_uid and event_id:
        return "bound"
    if legacy_uid:
        return "legacy_orphan"
    if event_id:
        return "canonical_orphan"
    raise ValueError("a pair with neither identity is not a pair")


def receipt_id(*, run_id: str, correlation: str) -> str:
    """Deterministic receipt identity, so a re-run of a window converges instead of
    duplicating. Two runs over the same window produce the same receipt ids for the same
    findings, which is what lets a receipt store be idempotent."""
    return str(uuid.uuid5(CORRELATION_NAMESPACE,
                          "\x1f".join((RECEIPT_CONTRACT_ID, str(run_id), str(correlation)))))


def _require_token(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % name)
    return value.strip()


# --- credential binding ------------------------------------------------------------------


def credential_binding(legacy: Mapping[str, Any],
                       canonical: Mapping[str, Any]) -> Dict[str, Any]:
    """The authenticated principal on each side, and whether they agree.

    No secret, no token, no header value: `principal_id` is the enrolled identity and
    `key_id` is the opaque rotation label the ingest path already carries. This function
    introduces no credential of its own -- the comparator authenticates as nothing; it reads
    what each writer recorded.
    """
    left = _principal(legacy)
    right = _principal(canonical)
    matched = (
        left["principal_id"] is not None
        and left["principal_id"] == right["principal_id"]
        and left["scope"] == right["scope"]
    )
    return {
        "legacy": left,
        "canonical": right,
        "matched": bool(matched),
    }


def _principal(side: Mapping[str, Any]) -> Dict[str, Any]:
    scope = side.get("credential_scope")
    if scope not in CREDENTIAL_SCOPES:
        scope = "unknown"
    principal = side.get("principal_id")
    return {
        "principal_id": principal if isinstance(principal, str) and principal else None,
        "scope": scope,
        "key_id": side.get("key_id") if isinstance(side.get("key_id"), str) else None,
    }


# --- comparison --------------------------------------------------------------------------


class Difference(object):
    """One compared field that did not agree."""

    __slots__ = ("path", "legacy", "canonical", "comparator", "tolerance")

    def __init__(self, path: str, legacy: Any, canonical: Any,
                 comparator: str = "exact", tolerance: Optional[str] = None) -> None:
        if comparator not in COMPARATORS:
            raise ValueError("unknown comparator %r" % (comparator,))
        self.path = path
        self.legacy = legacy
        self.canonical = canonical
        self.comparator = comparator
        self.tolerance = tolerance or TOLERANCE_BY_COMPARATOR[comparator]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "legacy": redact_value(self.path, self.legacy),
            "canonical": redact_value(self.path, self.canonical),
            "comparator": self.comparator,
            "tolerance": self.tolerance,
            "quoted": path_is_quotable(self.path),
        }


class Comparison(object):
    """The comparator's verdict for one correlation."""

    __slots__ = ("classification", "severity", "repair_action", "actionable", "notes")

    def __init__(self, classification: str, *, notes: Sequence[str] = ()) -> None:
        self.classification = classification
        known = classification in CLASSIFICATIONS
        self.severity = SEVERITY_BY_CLASSIFICATION.get(classification, "critical")
        self.repair_action = REPAIR_BY_CLASSIFICATION.get(classification, "manual_review")
        # An unknown member from a newer comparator is preserved and held back from any
        # automated action, exactly as an unknown enum member is non-dispatchable upstream.
        self.actionable = bool(known)
        self.notes = list(notes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "classification": self.classification,
            "severity": self.severity,
            "repair_action": self.repair_action,
            "actionable": self.actionable,
            "notes": list(self.notes),
        }


def _side(pair: Mapping[str, Any], name: str) -> Dict[str, Any]:
    raw = pair.get(name)
    side = dict(raw) if isinstance(raw, Mapping) else {}
    outcome = side.get("outcome")
    if outcome not in SIDE_OUTCOMES:
        outcome = "absent"
    side["outcome"] = outcome
    count = side.get("row_count")
    side["row_count"] = count if isinstance(count, int) and count >= 0 else (
        1 if outcome != "absent" else 0
    )
    return side


def classify(pair: Mapping[str, Any]) -> Comparison:
    """Terminal classification for one correlated pair.

    `pair` carries, for each of `legacy` and `canonical`: `outcome`, `row_count`,
    `state_hash`, `principal_id`, `credential_scope`, `device_id`, `site_id`. At the top
    level it carries `differences` (a sequence of `Difference`), `age_s` (seconds since the
    legacy write) and `grace_s`.

    Order of judgement is deliberate. Identity beats everything, because a pair that is not
    about the same producer is not a pair at all and every later comparison would be
    meaningless. Errors beat absence, because "the write failed and we know it" is evidence
    and "nothing is there" is only the absence of evidence. Duplication beats divergence,
    because two canonical rows make "which value differs" unanswerable.
    """
    legacy = _side(pair, "legacy")
    canonical = _side(pair, "canonical")
    differences = list(pair.get("differences") or ())
    grace = pair.get("grace_s")
    grace = float(grace) if isinstance(grace, (int, float)) else float(LIVE_GRACE_S)
    age = pair.get("age_s")
    age = float(age) if isinstance(age, (int, float)) else 0.0

    legacy_present = legacy["outcome"] != "absent"
    canonical_present = canonical["outcome"] != "absent"

    if not legacy_present and not canonical_present:
        return Comparison("unclassified", notes=["neither side has a record for this key"])

    if legacy_present and canonical_present and not _identities_agree(legacy, canonical):
        return Comparison("identity_divergence",
                          notes=["the two sides disagree about the authenticated producer"])

    if legacy["outcome"] == "errored":
        return Comparison("error_legacy", notes=["the authoritative write reported failure"])
    if canonical["outcome"] == "errored":
        return Comparison("error_canonical",
                          notes=["legacy stands; the canonical writer reported failure"])

    if canonical["row_count"] > 1:
        return Comparison("duplicate_canonical",
                          notes=["%d canonical rows for one legacy identity"
                                 % canonical["row_count"]])
    if legacy["row_count"] > 1:
        return Comparison("duplicate_legacy",
                          notes=["%d legacy rows for one canonical identity"
                                 % legacy["row_count"]])

    if legacy_present and not canonical_present:
        if age < grace:
            return Comparison("pending", notes=["within the %.0fs grace window" % grace])
        return Comparison("missing_canonical")
    if canonical_present and not legacy_present:
        return Comparison("missing_legacy",
                          notes=["a canonical row exists with no authoritative counterpart"])

    if legacy["outcome"] != canonical["outcome"]:
        return Comparison("outcome_divergence",
                          notes=["legacy=%s canonical=%s"
                                 % (legacy["outcome"], canonical["outcome"])])

    if pair.get("sequence_gap") and age >= float(pair.get("order_grace_s", ORDER_GRACE_S)):
        return Comparison("order_divergence",
                          notes=["producer sequence gap outlived the order grace window"])

    if differences:
        return Comparison("value_divergence",
                          notes=["%d compared field(s) outside tolerance" % len(differences)])

    stale = pair.get("stale_side")
    if stale == "canonical":
        return Comparison("stale_canonical",
                          notes=["canonical row is older than the legacy row it mirrors"])
    if stale == "legacy":
        return Comparison("stale_legacy",
                          notes=["legacy row is older than the canonical row"])

    left, right = legacy.get("state_hash"), canonical.get("state_hash")
    if left is None or right is None:
        return Comparison("unclassified", notes=["a side did not publish a state hash"])
    if left != right:
        return Comparison("value_divergence",
                          notes=["state hashes differ with no field-level difference "
                                 "reported; the projection is incomplete"])
    return Comparison("match")


def _identities_agree(legacy: Mapping[str, Any], canonical: Mapping[str, Any]) -> bool:
    for field in ("device_id", "site_id"):
        left, right = legacy.get(field), canonical.get(field)
        if left is not None and right is not None and left != right:
            return False
    left, right = legacy.get("principal_id"), canonical.get("principal_id")
    if left is not None and right is not None and left != right:
        return False
    return True


# --- sampling ----------------------------------------------------------------------------


def sample_receipt(classification: str, correlation: str, *,
                   denominator: int = DEFAULT_AUDIT_DENOMINATOR,
                   forced_reason: Optional[str] = None) -> Dict[str, Any]:
    """Whether this comparison is written down, and why.

    A mismatch is never sampled away: sampling a failure signal is how a reconciler becomes
    a coin flip. Matches are audit evidence, so they are sampled deterministically on the
    correlation id -- deterministic so a re-run of a window produces the same audit set, and
    so coverage can be computed instead of assumed.
    """
    if classification != "match" or forced_reason:
        return {
            "sampled": True,
            "rate_denominator": 1,
            "forced_reason": forced_reason or (
                "mismatches are never sampled" if classification != "match" else None
            ),
        }
    n = int(denominator) if isinstance(denominator, int) and denominator > 0 else 1
    bucket = int(hashlib.sha256(str(correlation).encode("utf-8")).hexdigest()[:8], 16)
    return {
        "sampled": (bucket % n) == 0,
        "rate_denominator": n,
        "forced_reason": None,
    }


# --- accounting --------------------------------------------------------------------------


def conservation(counts: Mapping[str, int]) -> bool:
    """`compared == pending + match + sum(mismatch classes)`.

    The reconciler's analogue of the batch path's conservation equation. If this does not
    close, the run is not evidence and must not be reported as one.
    """
    compared = int(counts.get("compared", 0))
    total = 0
    for name in CLASSIFICATIONS:
        total += int(counts.get(name, 0))
    return compared == total


def tally(comparisons: Iterable[Comparison]) -> Dict[str, int]:
    """Per-classification counts plus `compared`, always closing `conservation()`."""
    out: Dict[str, int] = {name: 0 for name in CLASSIFICATIONS}
    compared = 0
    for comparison in comparisons:
        compared += 1
        name = comparison.classification
        if name not in out:
            # An unknown member is counted as unclassified rather than dropped: the count
            # must still close, and a member this build does not understand is exactly the
            # thing an operator has to be told about.
            name = "unclassified"
        out[name] += 1
    out["compared"] = compared
    return out


# --- receipt -----------------------------------------------------------------------------


def build_receipt(*, run_id: str, window_start: str, window_end: str, emitted_at: str,
                  correlation: Mapping[str, Any], pair: Mapping[str, Any],
                  comparison: Comparison, differences: Sequence[Difference] = (),
                  sampling: Optional[Mapping[str, Any]] = None,
                  evidence: Optional[Mapping[str, Any]] = None,
                  comparator_build: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """A durable, redacted record of one comparison.

    Raises `ValueError` for a non-terminal comparison: `pending` is a scheduling state, not
    a finding, and writing it down would fill the store with rows that mean "ask again".
    """
    if comparison.classification in NON_TERMINAL:
        raise ValueError(
            "%s is not a terminal classification; a receipt records a finding, not a wait"
            % comparison.classification
        )
    legacy = _side(pair, "legacy")
    canonical = _side(pair, "canonical")
    kept = list(differences)[:MAX_DIFFERENCES]
    dropped = max(0, len(list(differences)) - len(kept))
    cid = str(correlation.get("correlation_id"))

    receipt: Dict[str, Any] = {
        "schema": RECEIPT_CONTRACT_ID,
        "schema_version": RECEIPT_CONTRACT_MAJOR,
        "receipt_id": receipt_id(run_id=run_id, correlation=cid),
        "run_id": str(run_id),
        "emitted_at": emitted_at,
        "window": {"start": window_start, "end": window_end},
        "correlation": {
            "correlation_id": cid,
            "binding": correlation.get("binding"),
            "legacy_uid": correlation.get("legacy_uid"),
            "event_id": correlation.get("event_id"),
            "site_id": correlation.get("site_id"),
            "device_id": correlation.get("device_id"),
            "telemetry_path": correlation.get("telemetry_path"),
            "source": correlation.get("source"),
        },
        "credential": credential_binding(legacy, canonical),
        "sides": {
            "legacy": _side_record(legacy),
            "canonical": _side_record(canonical),
        },
        "verdict": comparison.to_dict(),
        "differences": [d.to_dict() for d in kept],
        "differences_dropped": dropped,
        "disposition": {
            "repairable": comparison.repair_action in ("replay_inbox", "replay_outbox"),
            "repair_action": comparison.repair_action,
            "queued": False,
            "queue_ref": None,
        },
        "sampling": dict(sampling or sample_receipt(comparison.classification, cid)),
        "evidence": {
            "redaction_profile": REDACTION_PROFILE,
            "legacy_raw_ref": (evidence or {}).get("legacy_raw_ref"),
            "canonical_raw_ref": (evidence or {}).get("canonical_raw_ref"),
        },
        "comparator": dict(comparator_build or {"name": "unset", "version": "unset",
                                                "receipt_major": RECEIPT_CONTRACT_MAJOR}),
    }
    return receipt


def _side_record(side: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "outcome": side.get("outcome"),
        "row_count": side.get("row_count"),
        "state_hash": side.get("state_hash"),
        "written_at": side.get("written_at"),
        "observed_at": side.get("observed_at"),
        "clock_tier": side.get("clock_tier"),
        "writer": side.get("writer"),
        "writer_version": side.get("writer_version"),
        "reasons": sorted(set(side.get("reasons") or ())),
    }


def encode_receipt(receipt: Mapping[str, Any]) -> str:
    """Canonical JSON form. Raises when the receipt exceeds `MAX_RECEIPT_BYTES`."""
    encoded = json.dumps(dict(receipt), separators=(",", ":"), sort_keys=True,
                         allow_nan=False, ensure_ascii=True)
    if len(encoded.encode("utf-8")) > MAX_RECEIPT_BYTES:
        raise ValueError("receipt exceeds %d bytes; drop differences, not evidence refs"
                         % MAX_RECEIPT_BYTES)
    return encoded


def receipt_major(receipt: Mapping[str, Any]) -> int:
    value = receipt.get("schema_version")
    return value if isinstance(value, int) else 0


def supported(receipt: Mapping[str, Any]) -> bool:
    """False for a receipt from a newer major. Refused with a reason, never guessed."""
    return receipt_major(receipt) == RECEIPT_CONTRACT_MAJOR


# --- published form ----------------------------------------------------------------------


def _enum(values: Sequence[str], description: str) -> Dict[str, Any]:
    return {"type": "string", "enum": list(values), "description": description}


def _nullable(kind: str, description: str) -> Dict[str, Any]:
    return {"type": [kind, "null"], "description": description}


def receipt_schema_document() -> Dict[str, Any]:
    """JSON Schema for `hear.reconcile.receipt.v1`, derived from this module.

    Generated by `tools/gen_reconcile_contracts.py`; never hand-edited. Additive within the
    major: `additionalProperties` stays open so a newer comparator's extra field is
    preserved by an older reader instead of failing validation.
    """
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": RECEIPT_CONTRACT_URI,
        "title": RECEIPT_CONTRACT_ID,
        "description": (
            "Durable record of one legacy/HTTPS dual-write comparison. Carries hashes, "
            "field paths and reason codes; never payload bodies, credentials or locations."
        ),
        "type": "object",
        "additionalProperties": True,
        "required": [
            "schema", "schema_version", "receipt_id", "run_id", "emitted_at", "window",
            "correlation", "credential", "sides", "verdict", "differences",
            "differences_dropped", "disposition", "sampling", "evidence", "comparator",
        ],
        "properties": {
            "schema": {"type": "string", "const": RECEIPT_CONTRACT_ID},
            "schema_version": {"type": "integer", "minimum": 1},
            "receipt_id": {"type": "string",
                           "description": "uuid5 over (contract, run_id, correlation_id); "
                                          "a re-run converges instead of duplicating."},
            "run_id": {"type": "string"},
            "emitted_at": {"type": "string"},
            "window": {
                "type": "object", "additionalProperties": True,
                "required": ["start", "end"],
                "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
            },
            "correlation": {
                "type": "object", "additionalProperties": True,
                "required": ["correlation_id", "binding", "legacy_uid", "event_id",
                             "site_id", "device_id", "telemetry_path"],
                "properties": {
                    "correlation_id": {"type": "string"},
                    "binding": _enum(BINDINGS, "how the two sides were joined"),
                    "legacy_uid": _nullable("string", "authoritative record identity"),
                    "event_id": _nullable("string", "canonical envelope identity"),
                    "site_id": {"type": "string"},
                    "device_id": {"type": "string"},
                    "telemetry_path": {"type": "string"},
                    "source": _nullable("string", "ingress family"),
                },
            },
            "credential": {
                "type": "object", "additionalProperties": True,
                "required": ["legacy", "canonical", "matched"],
                "properties": {
                    "legacy": _principal_schema(),
                    "canonical": _principal_schema(),
                    "matched": {"type": "boolean"},
                },
            },
            "sides": {
                "type": "object", "additionalProperties": True,
                "required": list(SIDES),
                "properties": {name: _side_schema() for name in SIDES},
            },
            "verdict": {
                "type": "object", "additionalProperties": True,
                "required": ["classification", "severity", "repair_action", "actionable"],
                "properties": {
                    "classification": _enum(CLASSIFICATIONS, "closed verdict vocabulary"),
                    "severity": _enum(SEVERITIES, "operator routing"),
                    "repair_action": _enum(REPAIR_ACTIONS, "replay-only repair boundary"),
                    "actionable": {"type": "boolean"},
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
            },
            "differences": {
                "type": "array", "maxItems": MAX_DIFFERENCES,
                "items": {
                    "type": "object", "additionalProperties": True,
                    "required": ["path", "legacy", "canonical", "comparator", "tolerance",
                                 "quoted"],
                    "properties": {
                        "path": {"type": "string"},
                        "legacy": {},
                        "canonical": {},
                        "comparator": _enum(COMPARATORS, "how the field was compared"),
                        "tolerance": {"type": "string"},
                        "quoted": {"type": "boolean",
                                   "description": "false means both values are redacted "
                                                  "digests, not literals"},
                    },
                },
            },
            "differences_dropped": {"type": "integer", "minimum": 0},
            "disposition": {
                "type": "object", "additionalProperties": True,
                "required": ["repairable", "repair_action", "queued", "queue_ref"],
                "properties": {
                    "repairable": {"type": "boolean"},
                    "repair_action": _enum(REPAIR_ACTIONS, "replay-only repair boundary"),
                    "queued": {"type": "boolean"},
                    "queue_ref": _nullable("string", "advisory work-list reference"),
                },
            },
            "sampling": {
                "type": "object", "additionalProperties": True,
                "required": ["sampled", "rate_denominator", "forced_reason"],
                "properties": {
                    "sampled": {"type": "boolean"},
                    "rate_denominator": {"type": "integer", "minimum": 1},
                    "forced_reason": _nullable("string", "why sampling was bypassed"),
                },
            },
            "evidence": {
                "type": "object", "additionalProperties": True,
                "required": ["redaction_profile", "legacy_raw_ref", "canonical_raw_ref"],
                "properties": {
                    "redaction_profile": {"type": "string", "const": REDACTION_PROFILE},
                    "legacy_raw_ref": _nullable("string", "object key of retained bytes"),
                    "canonical_raw_ref": _nullable("string", "object key of retained bytes"),
                },
            },
            "comparator": {
                "type": "object", "additionalProperties": True,
                "properties": {
                    "name": {"type": "string"},
                    "version": {"type": "string"},
                    "receipt_major": {"type": "integer", "minimum": 1},
                },
            },
        },
    }


def _principal_schema() -> Dict[str, Any]:
    return {
        "type": "object", "additionalProperties": True,
        "required": ["principal_id", "scope", "key_id"],
        "properties": {
            "principal_id": _nullable("string", "enrolled identity; never a token"),
            "scope": _enum(CREDENTIAL_SCOPES, "device-scoped or site-scoped credential"),
            "key_id": _nullable("string", "opaque rotation label; never key material"),
        },
    }


def _side_schema() -> Dict[str, Any]:
    return {
        "type": "object", "additionalProperties": True,
        "required": ["outcome", "row_count", "state_hash"],
        "properties": {
            "outcome": _enum(SIDE_OUTCOMES, "what this writer did with the observation"),
            "row_count": {"type": "integer", "minimum": 0},
            "state_hash": _nullable("string", "digest of the compared projection"),
            "written_at": _nullable("string", "when this writer committed"),
            "observed_at": _nullable("string", "producer instant, null when unanchored"),
            "clock_tier": _nullable("string", "clock evidence tier for observed_at"),
            "writer": _nullable("string", "writing component"),
            "writer_version": _nullable("string", "writing build"),
            "reasons": {"type": "array", "items": {"type": "string"}},
        },
    }
