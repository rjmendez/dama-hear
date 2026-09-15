"""Shadow-read comparison: query identity, result classification and read-side receipts.

This module is the source of truth for how a *future* Phase 5 shadow read compares the
**legacy** read surface against the **canonical** read surface, before any read cutover. It
decides nothing about transport or storage and performs no I/O: no sockets, no store, no
cluster client, no scheduler, no credential material. A comparator process is expected to
issue one normalised query to each side, hand the two result descriptors to `classify()`,
and persist whatever `build_receipt()` returns.

`hear/ingest/reconcile.py` is the write-side analogue and is deliberately reused here for
redaction and digesting, because two redaction lists drift and only one of them is tested on
the day it matters.

Five positions carry the design, and each is asserted by a test rather than described:

1. **Legacy stays authoritative.** `READ_AUTHORITY` is `"legacy"` for the whole of Phase 5.
   The comparator runs only in the `shadow` and `compare` lane modes of the object-store
   read ladder; `prefer` and `only` are refused by `assert_shadow_only()`, because those
   modes mean a reader already moved and there is nothing left to shadow.
2. **A read is a snapshot, not a row.** Two stores answer at two instants. Everything newer
   than the settle horizon is *excluded from comparison*, not compared and forgiven -- and
   the exclusion is counted, so coverage stays computable instead of assumed.
3. **Absence has five distinct meanings** -- in flight, late, retention-expired, tombstoned,
   and lost -- and collapsing them is how a read comparator produces a permanently red board
   that an operator mutes on day three.
4. **Order, pagination and cursors are compared as separate properties from content.** Two
   honest readers may page differently and still answer the same question. A page boundary
   is not evidence; a duplicated or skipped row across a page boundary is.
5. **A receipt never carries a payload, a coordinate or a clip body.** Restricted values are
   compared by the `blind` comparator: equality is decided in memory and only the verdict is
   durable. There is no field in a receipt that audio or a coordinate can occupy.

Deliberately **not** named `*SCHEMA*`: `tools/freeze_contracts.py` harvests published
contract ids from `[A-Z_]*SCHEMA[A-Z_]*` assignments, and this receipt is staged rather than
published. `docs/decisions/0006-phase5-shadow-read-comparator.md` records why and states the
exact promotion procedure.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from hear.ingest import reconcile as _rc

RECEIPT_CONTRACT_ID = "hear.shadowread.receipt.v1"
RECEIPT_CONTRACT_MAJOR = 1
"""The only receipt major this reader supports. A larger major is refused, not guessed."""

RECEIPT_CONTRACT_URI = (
    "https://schemas.dama.example/hear/shadowread/v1/receipt.schema.json"
)
RECEIPT_MEDIA_TYPE = "application/vnd.dama.hear.shadowread.receipt.v1+json"

CORRELATION_NAMESPACE = uuid.UUID("7f2c1ab8-53d4-5e61-8b0a-19d7c4e6f230")
"""Fixed namespace for deterministic receipt ids. Changing it re-identifies every receipt
ever written, so it is frozen for the life of the v1 major."""

# --- authority ---------------------------------------------------------------------------

READ_AUTHORITY = "legacy"
"""Which side an operator's answer comes from during Phase 5. The comparator never returns a
result to anybody, but a build that flipped this constant would be a read cutover disguised
as a comparator change, so it is a constant with a test rather than a comment."""

READ_MODES: Tuple[str, ...] = ("off", "shadow", "compare", "prefer", "only")
"""The per-lane read ladder from the Phase 3 object-key design. `off` reads nothing new,
`shadow` reads both and compares nothing, `compare` reads both and logs, `prefer` and `only`
have already moved authority."""

SHADOW_MODES: Tuple[str, ...] = ("shadow", "compare")
"""The only modes in which this comparator may run."""


def assert_shadow_only(mode: str) -> str:
    """Refuse to run in a mode where a reader has already moved.

    A comparator that keeps reporting `match` while `prefer` silently serves canonical
    answers is not evidence for a cutover -- it is a record of the cutover that already
    happened.
    """
    if mode not in READ_MODES:
        raise ValueError("unknown read mode %r" % (mode,))
    if mode not in SHADOW_MODES:
        raise ValueError(
            "shadow comparison is only defined for %s; %r means a reader already moved"
            % (" and ".join(SHADOW_MODES), mode)
        )
    return mode


# --- vocabularies (all closed) -----------------------------------------------------------

SIDES: Tuple[str, ...] = ("legacy", "canonical")

GRAINS: Tuple[str, ...] = ("result_set", "row")
"""A receipt is about a whole answer or about one row inside it. Both exist because "the
count is wrong" and "this row is wrong" are different findings with different repairs."""

EVIDENCE_CLASSES: Tuple[str, ...] = (
    "heartbeat",
    "health",
    "detection",
    "scene",
    "clip_manifest",
    "tag",
    "score",
    "localization",
    "annotation",
    "export_manifest",
    "ledger_stat",
    "model_card",
    "audit_record",
)
"""Every read surface a Phase 5 cutover would move. Closed: an evidence class with no entry
here is a `comparator_fault`, never a silently skipped read."""

RESTRICTED_CLASSES: Tuple[str, ...] = ("clip_manifest",)
"""Classes whose underlying bytes are never read by the comparator at all. A clip is
compared as its manifest -- id, digest, length, retention state -- and the audio stays where
the clip store already keeps it."""

EXHAUSTIVE_CLASSES: Tuple[str, ...] = (
    "ledger_stat",
    "model_card",
    "export_manifest",
    "audit_record",
)
"""Low-cardinality, high-consequence surfaces. Sampling an aggregate is sampling the only
number anybody checks, so these are compared in full every run."""

SIDE_OUTCOMES: Tuple[str, ...] = ("answered", "refused", "errored", "unavailable")
"""What one read surface did. `refused` is a durable typed refusal (a scope denial, an
out-of-range window); `errored` is a failed read we know failed; `unavailable` is a surface
that was not reachable at all."""

ROW_STATES: Tuple[str, ...] = ("present", "absent", "tombstoned", "retention_expired")
"""What one side says about one row. Three of these are forms of "not returned", and the
whole point of separating them is that only one of them is loss."""

COMPARATORS: Tuple[str, ...] = (
    "exact", "hash", "tier", "numeric", "count", "set", "order", "presence", "blind",
)

TOLERANCE_BY_COMPARATOR: Dict[str, str] = {
    # Inherited unchanged from the write side, so one vocabulary governs both audits.
    "hash": "sha256 equal",
    "exact": "equal",
    "tier": "clock tier equal, value ignored",
    "numeric": "abs diff <= declared tolerance and model card equal",
    "count": "equal",
    # Read-side additions.
    "set": "identity-keyed set equality over the settled window",
    "order": "identical key sequence over the intersection; inversions counted",
    "presence": "null-ness and precision class equal, value ignored",
    "blind": "equality decided in memory; only the verdict is durable",
}

BLIND_COMPARATORS: Tuple[str, ...] = ("blind", "presence")
"""Comparators that may never place either value in a receipt, even redacted. A redacted
hash of a coordinate is still a stable identifier for a coordinate."""

CLASSIFICATIONS: Tuple[str, ...] = (
    "match",
    "pending",
    "late_arrival",
    "missing_canonical_row",
    "missing_legacy_row",
    "duplicate_canonical_row",
    "duplicate_legacy_row",
    "count_divergence",
    "value_divergence",
    "order_divergence",
    "pagination_divergence",
    "cursor_divergence",
    "filter_divergence",
    "retention_divergence",
    "tombstone_divergence",
    "version_divergence",
    "identity_divergence",
    "comparator_fault",
    "unclassified",
)

NON_TERMINAL: Tuple[str, ...] = ("pending",)
TERMINAL: Tuple[str, ...] = tuple(c for c in CLASSIFICATIONS if c not in NON_TERMINAL)
MISMATCH_CLASSIFICATIONS: Tuple[str, ...] = tuple(c for c in TERMINAL if c != "match")

SEVERITIES: Tuple[str, ...] = ("info", "warn", "critical")

SEVERITY_BY_CLASSIFICATION: Dict[str, str] = {
    "match": "info",
    "pending": "info",
    # A row that showed up after the settle horizon but inside the late window is a freshness
    # fact, not a loss. It is warn because a rising late rate is what precedes a real loss.
    "late_arrival": "warn",
    # The loss the whole exercise exists to find: legacy can answer and canonical cannot.
    "missing_canonical_row": "critical",
    # Canonical-only is not loss -- legacy still has everything it ever had -- but something
    # is answering from a row the authoritative surface never had.
    "missing_legacy_row": "warn",
    "duplicate_canonical_row": "critical",
    "duplicate_legacy_row": "warn",
    "count_divergence": "critical",
    "value_divergence": "critical",
    "order_divergence": "warn",
    "pagination_divergence": "warn",
    # A cursor that is not stable makes every paged answer unreproducible, which makes every
    # other comparison in this run unfalsifiable.
    "cursor_divergence": "critical",
    # Two surfaces that resolve the same filter differently are answering two questions, and
    # every row-level verdict downstream of that is meaningless.
    "filter_divergence": "critical",
    "retention_divergence": "warn",
    # A deletion that did not propagate is a governance defect, not a data-quality one.
    "tombstone_divergence": "critical",
    "version_divergence": "warn",
    "identity_divergence": "critical",
    # The comparator's own failure, said out loud. Undecidable is louder than wrong.
    "comparator_fault": "critical",
    "unclassified": "critical",
}

REPAIR_ACTIONS: Tuple[str, ...] = (
    "none",
    "replay_inbox",
    "replay_outbox",
    "reindex_request",
    "manual_review",
)
"""Advisory only. `reindex_request` is a *request* recorded for the owner of the canonical
read index; it is not an instruction the comparator may execute, and there is no action here
that edits, deletes or re-derives a row on either side."""

REPAIR_BY_CLASSIFICATION: Dict[str, str] = {
    "match": "none",
    "pending": "none",
    "late_arrival": "none",
    "missing_canonical_row": "replay_inbox",
    "missing_legacy_row": "manual_review",
    "duplicate_canonical_row": "manual_review",
    "duplicate_legacy_row": "manual_review",
    "count_divergence": "manual_review",
    "value_divergence": "manual_review",
    "order_divergence": "none",
    "pagination_divergence": "reindex_request",
    "cursor_divergence": "reindex_request",
    "filter_divergence": "manual_review",
    "retention_divergence": "manual_review",
    "tombstone_divergence": "manual_review",
    "version_divergence": "manual_review",
    "identity_divergence": "manual_review",
    "comparator_fault": "manual_review",
    "unclassified": "manual_review",
}

GATE_IMPACTS: Tuple[str, ...] = ("none", "blocks_cutover")

#: Findings that stop a read cutover on their own, separately from how they are repaired.
#: Kept apart from `repair_action` because "who fixes this" and "may we proceed" are
#: different questions and conflating them is how a blocking finding gets closed as a ticket.
GATE_IMPACT_BY_CLASSIFICATION: Dict[str, str] = {
    name: ("blocks_cutover" if name in (
        "missing_canonical_row",
        "duplicate_canonical_row",
        "count_divergence",
        "value_divergence",
        "cursor_divergence",
        "filter_divergence",
        "tombstone_divergence",
        "identity_divergence",
        "comparator_fault",
        "unclassified",
    ) else "none")
    for name in CLASSIFICATIONS
}

# --- tolerances --------------------------------------------------------------------------

SETTLE_S = 900
"""Rows whose recorded instant is newer than `as_of - SETTLE_S` are **excluded** from
comparison. Two stores do not commit at the same instant; comparing the leading edge of a
write stream measures scheduling jitter and calls it data loss."""

LATE_ARRIVAL_S = 3_600
"""An absence that resolves within this of the settle horizon is `late_arrival`, not loss.
Beyond it, absence on the canonical side is `missing_canonical_row`."""

BACKFILL_SETTLE_S = 86_400
"""The settle horizon for drain/import lanes, where a day-late row is the designed behaviour
of a bulk SD replay rather than an anomaly."""

SNAPSHOT_SKEW_S = 5
"""Maximum permitted difference between the two sides' snapshot instants. Beyond it the two
answers are about different moments, and the honest verdict is `comparator_fault` rather
than a row-by-row report of the gap."""

RETENTION_EDGE_S = 3_600
"""Rows whose capture instant is within this of either side's retention horizon are excluded
as `retention_edge`. A TTL that expires between the two reads is correct behaviour on both
sides, and reporting it is the single most reliable way to produce a permanently red
dashboard out of two healthy stores."""

ORDER_GRACE_S = 300
"""Reused from the write side: a sequence gap that closes itself was never a defect."""

CURSOR_TTL_S = 86_400
"""`docs/api-boundaries.md` expires a cursor after 24 h. A comparator that presents an older
cursor has produced a `comparator_fault`; it has not found a divergence."""

MAX_PAGES = 20
MAX_ROWS_COMPARED = 10_000
"""Per-query bounds. A comparison that hits either is `truncated`: coverage drops and the
receipt records it, because an unrecorded truncation is a coverage claim nobody can check."""

MAX_DIFFERENCES = 32
MAX_RECEIPT_BYTES = 8_192
"""Receipt bounds, inherited from the write side. A receipt is an index into evidence."""

DEFAULT_QUERY_DENOMINATOR = 50
"""Sampled classes compare 1-in-N *queries*, deterministic on the query fingerprint."""

DEFAULT_ROW_DENOMINATOR = 1_000
"""Within a sampled query, matching rows are audited 1-in-N. Mismatching rows never are."""

COST_CEILING_S_PER_10K_ROWS = 60.0
"""One vCPU-minute per 10 000 compared rows, on existing hardware, matching the write-side
budget so one cost story covers both audits."""

# --- redaction ---------------------------------------------------------------------------

REDACTION_PROFILE = "hear.shadowread.redact.v1"

READ_VALUE_ALLOW_PATHS: Tuple[str, ...] = tuple(sorted(set(_rc.VALUE_ALLOW_PATHS) | {
    "evidence_class",
    "grain",
    "row_state",
    "page_index",
    "page_size",
    "page_count",
    "row_count",
    "sort.field",
    "sort.direction",
    "filter.kind",
    "filter.bound",
    "retention_class",
    "retention_horizon",
    "tombstoned",
    "read_contract_major",
    "projection_version",
    "cursor_stable",
    "digest_algo",
    "byte_length",
    "sample_rate_hz",
    "duration_ms",
}))
"""The write side's allow list plus the read-shaped paths this comparator adds. Built by
union rather than by copy so a tightening on the write side is inherited here, and a
divergence between the two lists cannot be introduced by editing only one file."""


def path_is_quotable(path: str) -> bool:
    """True when a field path's value may appear literally in a read receipt.

    Deny beats allow, always, and the deny pattern is the write side's -- one pattern, one
    test, one place a future location-shaped field name has to get past.
    """
    if not isinstance(path, str) or not path:
        return False
    if _rc.VALUE_DENY_PATTERN.search(path):
        return False
    return path in READ_VALUE_ALLOW_PATHS


def redact_value(path: str, value: Any) -> Any:
    """A receipt-safe rendering of one compared value."""
    if path_is_quotable(path) and isinstance(value, (str, int, float, bool, type(None))):
        return value
    if value is None:
        return None
    return {"redacted": "sha256:" + _rc._hash_token(value)}


state_hash = _rc.state_hash
"""One row's projection digest, reused verbatim from the write side so a row compared by
both audits produces one digest and not two."""


def result_hash(rows: Sequence[Mapping[str, Any]]) -> str:
    """Order-independent digest of a settled result set's projections.

    Order-independent on purpose: ordering is compared as its own property by the `order`
    comparator. Folding order into the content digest would make one late row and one
    re-sorted page indistinguishable, and they have different repairs.
    """
    digests = sorted(_rc.state_hash(row) for row in rows)
    encoded = json.dumps(digests, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --- query identity ----------------------------------------------------------------------


def normalise_query(query: Mapping[str, Any]) -> Dict[str, Any]:
    """The closed normal form of one read question.

    Both sides must be asked the *same* question, and "the same" has to survive two clients
    that spell a filter differently. Normalisation is explicit and total: an unknown key is
    kept (so a newer comparator's filter is not silently dropped from the fingerprint) and
    every collection is sorted.
    """
    evidence_class = query.get("evidence_class")
    if evidence_class not in EVIDENCE_CLASSES:
        raise ValueError("unknown evidence class %r" % (evidence_class,))
    devices = query.get("device_ids") or ()
    filters = query.get("filters") or {}
    sort = query.get("sort") or {}
    normal: Dict[str, Any] = {
        "evidence_class": evidence_class,
        "site_id": _require_token("site_id", query.get("site_id")),
        "device_ids": sorted(str(d) for d in devices),
        "time_from": query.get("time_from"),
        "time_to": query.get("time_to"),
        "filters": {str(k): filters[k] for k in sorted(filters)},
        "sort": {
            "field": sort.get("field", "captured_at"),
            "direction": sort.get("direction", "asc"),
        },
        "page_size": int(query.get("page_size") or 50),
        "as_of": query.get("as_of"),
        "settle_s": float(query.get("settle_s", SETTLE_S)),
        "projection_version": int(query.get("projection_version") or 1),
    }
    extra = {k: query[k] for k in sorted(query) if k not in normal and k != "read_mode"}
    if extra:
        normal["extra"] = extra
    return normal


def query_fingerprint(query: Mapping[str, Any]) -> str:
    """Stable identity of one read question, shared by both sides.

    A receipt whose two sides carry different fingerprints is not a comparison -- it is two
    unrelated reads -- so the fingerprint is recorded once per receipt and the comparator
    refuses a pair that disagrees (`filter_divergence`).
    """
    normal = normalise_query(query)
    encoded = json.dumps(normal, separators=(",", ":"), sort_keys=True, allow_nan=False,
                         ensure_ascii=True)
    return "q:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def row_key(*, evidence_class: str, site_id: str, device_id: str,
            logical_id: str) -> str:
    """Identity of one row across both surfaces.

    `logical_id` is whatever identity the two surfaces **already recorded** for that row --
    a `record_uid`, an `event_id`, a clip id, an object logical id. It is never re-derived
    here, for the same reason the write-side comparator never re-derives one: a comparator
    that reimplements the identity it audits can make both sides agree on a bug.
    """
    if evidence_class not in EVIDENCE_CLASSES:
        raise ValueError("unknown evidence class %r" % (evidence_class,))
    scope = "\x1f".join((
        RECEIPT_CONTRACT_ID,
        evidence_class,
        _require_token("site_id", site_id),
        _require_token("device_id", device_id),
        _require_token("logical_id", logical_id),
    ))
    return "r:" + hashlib.sha256(scope.encode("utf-8")).hexdigest()


def receipt_id(*, run_id: str, fingerprint: str, key: Optional[str] = None) -> str:
    """Deterministic receipt identity, so a re-run of a window converges instead of
    duplicating -- which is what lets a rolled-back comparator build be compared directly
    against the build that produced the finding."""
    return str(uuid.uuid5(CORRELATION_NAMESPACE, "\x1f".join(
        (RECEIPT_CONTRACT_ID, str(run_id), str(fingerprint), str(key or "")))))


def _require_token(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % name)
    return value.strip()


# --- window semantics --------------------------------------------------------------------


def settled(*, age_s: float, settle_s: float = SETTLE_S) -> bool:
    """True when a row is old enough to be comparable at all."""
    return float(age_s) >= float(settle_s)


def retention_edge(*, age_s: float, horizons_s: Sequence[Optional[float]],
                   edge_s: float = RETENTION_EDGE_S) -> bool:
    """True when a row sits close enough to either side's retention horizon that its absence
    on one side is expected rather than anomalous."""
    for horizon in horizons_s:
        if horizon is None:
            continue
        if abs(float(age_s) - float(horizon)) <= float(edge_s):
            return True
        if float(age_s) > float(horizon):
            return True
    return False


def snapshot_skew_ok(skew_s: Any, *, limit_s: float = SNAPSHOT_SKEW_S) -> bool:
    """False when the two reads are about different moments."""
    if not isinstance(skew_s, (int, float)):
        return False
    return abs(float(skew_s)) <= float(limit_s)


# --- comparison --------------------------------------------------------------------------


class Difference(object):
    """One compared field that did not agree.

    A `blind` or `presence` difference carries **no values at all**, not even redacted ones:
    a stable hash of a coordinate is still a stable identifier for a coordinate, and the
    only thing a receipt needs from a restricted field is whether the two sides agreed.
    """

    __slots__ = ("path", "legacy", "canonical", "comparator", "tolerance")

    def __init__(self, path: str, legacy: Any = None, canonical: Any = None,
                 comparator: str = "exact", tolerance: Optional[str] = None) -> None:
        if comparator not in COMPARATORS:
            raise ValueError("unknown comparator %r" % (comparator,))
        self.path = path
        self.legacy = legacy
        self.canonical = canonical
        self.comparator = comparator
        self.tolerance = tolerance or TOLERANCE_BY_COMPARATOR[comparator]

    @property
    def blind(self) -> bool:
        return self.comparator in BLIND_COMPARATORS

    def to_dict(self) -> Dict[str, Any]:
        if self.blind:
            return {
                "path": self.path,
                "legacy": None,
                "canonical": None,
                "comparator": self.comparator,
                "tolerance": self.tolerance,
                "quoted": False,
                "blind": True,
            }
        return {
            "path": self.path,
            "legacy": redact_value(self.path, self.legacy),
            "canonical": redact_value(self.path, self.canonical),
            "comparator": self.comparator,
            "tolerance": self.tolerance,
            "quoted": path_is_quotable(self.path),
            "blind": False,
        }


class Comparison(object):
    """The comparator's verdict for one query or one row."""

    __slots__ = ("classification", "severity", "repair_action", "gate_impact",
                 "actionable", "notes")

    def __init__(self, classification: str, *, notes: Sequence[str] = ()) -> None:
        self.classification = classification
        known = classification in CLASSIFICATIONS
        self.severity = SEVERITY_BY_CLASSIFICATION.get(classification, "critical")
        self.repair_action = REPAIR_BY_CLASSIFICATION.get(classification, "manual_review")
        # An unknown member from a newer comparator blocks a cutover: a verdict this build
        # cannot interpret is exactly the thing a gate must not be allowed to step over.
        self.gate_impact = GATE_IMPACT_BY_CLASSIFICATION.get(classification,
                                                             "blocks_cutover")
        self.actionable = bool(known)
        self.notes = list(notes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "classification": self.classification,
            "severity": self.severity,
            "repair_action": self.repair_action,
            "gate_impact": self.gate_impact,
            "actionable": self.actionable,
            "notes": list(self.notes),
        }


def _side(pair: Mapping[str, Any], name: str) -> Dict[str, Any]:
    raw = pair.get(name)
    side = dict(raw) if isinstance(raw, Mapping) else {}
    outcome = side.get("outcome")
    if outcome not in SIDE_OUTCOMES:
        outcome = "unavailable"
    side["outcome"] = outcome
    state = side.get("row_state")
    if state not in ROW_STATES:
        state = "present" if outcome == "answered" else "absent"
    side["row_state"] = state
    count = side.get("row_count")
    side["row_count"] = count if isinstance(count, int) and count >= 0 else 0
    return side


def classify(pair: Mapping[str, Any]) -> Comparison:
    """Terminal classification for one compared read.

    `pair` carries, for each of `legacy` and `canonical`: `outcome`, `row_state`,
    `row_count`, `result_hash`, `state_hash`, `read_contract_major`, `projection_version`,
    `principal_id`, `device_id`, `site_id`, `page_count`, `truncated`, `cursor_stable`,
    `retention_horizon_s`. At the top level it carries `grain`, `fingerprint_legacy` and
    `fingerprint_canonical`, `differences`, `age_s`, `settle_s`, `snapshot_skew_s`,
    `cursor_age_s`, `late_s` and `inversions`.

    **The order of judgement is load-bearing, and differs from the write side in one
    respect: comparator competence is judged first.** A read comparison is a comparison of
    two *snapshots*; if the snapshots are not about the same moment, the same question and
    the same projection version, then every row-level verdict beneath that is an artefact of
    the comparator rather than a fact about the stores.

    fault -> identity -> version -> question -> cursor -> duplication -> deletion ->
    retention -> absence -> counts -> pagination -> order -> values -> hashes.
    """
    legacy = _side(pair, "legacy")
    canonical = _side(pair, "canonical")
    differences = list(pair.get("differences") or ())
    grain = pair.get("grain", "result_set")

    settle_s = pair.get("settle_s")
    settle_s = float(settle_s) if isinstance(settle_s, (int, float)) else float(SETTLE_S)
    age_s = pair.get("age_s")
    age_s = float(age_s) if isinstance(age_s, (int, float)) else float(settle_s)

    # 1. Comparator competence, before anything is believed about either store.
    if grain not in GRAINS:
        return Comparison("comparator_fault", notes=["unknown receipt grain %r" % (grain,)])
    if not snapshot_skew_ok(pair.get("snapshot_skew_s")):
        return Comparison("comparator_fault", notes=[
            "the two reads are more than %ds apart and describe different moments"
            % SNAPSHOT_SKEW_S])
    cursor_age = pair.get("cursor_age_s")
    if isinstance(cursor_age, (int, float)) and float(cursor_age) > CURSOR_TTL_S:
        return Comparison("comparator_fault", notes=[
            "the comparator presented a cursor older than its %ds lifetime" % CURSOR_TTL_S])
    for name, side in (("legacy", legacy), ("canonical", canonical)):
        if side["outcome"] == "unavailable":
            return Comparison("comparator_fault", notes=[
                "the %s read surface was not reachable; absence of an answer is not an "
                "answer about absence" % name])

    # 2. Identity: a pair that is not about the same producer is not a pair.
    if not _identities_agree(legacy, canonical):
        return Comparison("identity_divergence", notes=[
            "the two surfaces disagree about the producer behind this row"])

    # 3. Version: comparing across incompatible projections invents findings.
    left_pv, right_pv = legacy.get("projection_version"), canonical.get("projection_version")
    if left_pv is not None and right_pv is not None and left_pv != right_pv:
        return Comparison("comparator_fault", notes=[
            "projection versions differ (%s vs %s); the two sides were asked for different "
            "shapes" % (left_pv, right_pv)])
    left_major = legacy.get("read_contract_major")
    right_major = canonical.get("read_contract_major")
    if left_major is not None and right_major is not None and left_major != right_major:
        return Comparison("version_divergence", notes=[
            "read contract majors differ (%s vs %s); comparison is restricted to the "
            "intersection of known fields" % (left_major, right_major)])

    # 4. The question itself.
    left_fp = pair.get("fingerprint_legacy")
    right_fp = pair.get("fingerprint_canonical")
    if left_fp is not None and right_fp is not None and left_fp != right_fp:
        return Comparison("filter_divergence", notes=[
            "the two surfaces resolved the same query into different questions"])

    # 5. Cursor stability, before any paged content is believed.
    for name, side in (("legacy", legacy), ("canonical", canonical)):
        if side.get("cursor_stable") is False:
            return Comparison("cursor_divergence", notes=[
                "the %s cursor did not round-trip to the same rows" % name])
    if pair.get("cursor_duplicates") or pair.get("cursor_gaps"):
        return Comparison("cursor_divergence", notes=[
            "paging produced a duplicated or skipped row across a page boundary"])

    # 6. Duplication, before any value question: with two rows, "which value" has no answer.
    if canonical["row_count"] > 1 and grain == "row":
        return Comparison("duplicate_canonical_row", notes=[
            "%d canonical rows for one identity" % canonical["row_count"]])
    if legacy["row_count"] > 1 and grain == "row":
        return Comparison("duplicate_legacy_row", notes=[
            "%d legacy rows for one identity" % legacy["row_count"]])

    # 7. Deletion, before absence: a tombstone is a recorded fact, not a missing row.
    states = (legacy["row_state"], canonical["row_state"])
    if "tombstoned" in states and "present" in states:
        readable = "legacy" if legacy["row_state"] == "present" else "canonical"
        return Comparison("tombstone_divergence", notes=[
            "the row is tombstoned on one side and still readable on the %s side" % readable])

    # 8. Retention, before absence: a TTL that fired is correct behaviour on both sides.
    horizons = [legacy.get("retention_horizon_s"), canonical.get("retention_horizon_s")]
    if "retention_expired" in states:
        if retention_edge(age_s=age_s, horizons_s=horizons):
            return Comparison("match", notes=[
                "outside the intersection of both retention horizons; excluded from "
                "comparison rather than reported as loss"])
        return Comparison("retention_divergence", notes=[
            "one surface has pruned this row while the other still returns it, and the row "
            "is not near either declared horizon"])

    # 9. Absence: in flight, late, or lost -- three different findings.
    legacy_present = legacy["row_state"] == "present"
    canonical_present = canonical["row_state"] == "present"
    if legacy_present and not canonical_present:
        if not settled(age_s=age_s, settle_s=settle_s):
            return Comparison("pending", notes=[
                "inside the %.0fs settle horizon; not yet comparable" % settle_s])
        late = pair.get("late_s")
        if isinstance(late, (int, float)) and 0 < float(late) <= LATE_ARRIVAL_S:
            return Comparison("late_arrival", notes=[
                "the canonical row landed %.0fs after the settle horizon" % float(late)])
        return Comparison("missing_canonical_row")
    if canonical_present and not legacy_present:
        return Comparison("missing_legacy_row", notes=[
            "a canonical row with no authoritative counterpart"])
    if not legacy_present and not canonical_present:
        return Comparison("match", notes=["neither surface returns this row, and both agree "
                                          "on why"])

    # 10. Counts, then shape, then order, then content.
    if grain == "result_set" and legacy["row_count"] != canonical["row_count"]:
        return Comparison("count_divergence", notes=[
            "legacy returned %d settled rows, canonical %d"
            % (legacy["row_count"], canonical["row_count"])])

    if pair.get("truncated_side_only"):
        return Comparison("pagination_divergence", notes=[
            "one side truncated at the page cap and the other did not; coverage is not "
            "comparable for this query"])

    inversions = pair.get("inversions")
    if isinstance(inversions, int) and inversions > 0:
        if age_s >= float(pair.get("order_grace_s", ORDER_GRACE_S)):
            return Comparison("order_divergence", notes=[
                "%d ordering inversion(s) outlived the order grace window" % inversions])
        return Comparison("match", notes=[
            "ordering inversions inside the order grace window; a gap that closes itself "
            "was never a defect"])

    if differences:
        return Comparison("value_divergence", notes=[
            "%d compared field(s) outside tolerance" % len(differences)])

    left = legacy.get("result_hash") if grain == "result_set" else legacy.get("state_hash")
    right = (canonical.get("result_hash") if grain == "result_set"
             else canonical.get("state_hash"))
    if left is None or right is None:
        return Comparison("unclassified", notes=["a surface did not publish a digest"])
    if left != right:
        return Comparison("value_divergence", notes=[
            "digests differ with no field-level difference reported; the projection is "
            "incomplete"])
    if legacy["outcome"] != canonical["outcome"]:
        return Comparison("value_divergence", notes=[
            "legacy=%s canonical=%s" % (legacy["outcome"], canonical["outcome"])])
    return Comparison("match")


def _identities_agree(legacy: Mapping[str, Any], canonical: Mapping[str, Any]) -> bool:
    for field in ("device_id", "site_id", "principal_id"):
        left, right = legacy.get(field), canonical.get(field)
        if left is not None and right is not None and left != right:
            return False
    return True


# --- sampling and coverage ---------------------------------------------------------------


def coverage_mode(evidence_class: str, *, forced: bool = False) -> str:
    """`exhaustive` or `sampled`, for one evidence class.

    Aggregates and audit surfaces are always exhaustive: sampling the only number anybody
    reads is not cost control, it is deleting the measurement.
    """
    if evidence_class not in EVIDENCE_CLASSES:
        raise ValueError("unknown evidence class %r" % (evidence_class,))
    if forced or evidence_class in EXHAUSTIVE_CLASSES:
        return "exhaustive"
    return "sampled"


def sample_query(evidence_class: str, fingerprint: str, *,
                 denominator: int = DEFAULT_QUERY_DENOMINATOR,
                 forced_reason: Optional[str] = None) -> Dict[str, Any]:
    """Whether this *query* is issued at all this run.

    Deterministic on the fingerprint, so a re-run of a window issues the same query set and
    coverage is computable rather than asserted.
    """
    mode = coverage_mode(evidence_class, forced=bool(forced_reason))
    if mode == "exhaustive":
        return {"selected": True, "mode": mode, "rate_denominator": 1,
                "forced_reason": forced_reason or "exhaustive evidence class"}
    n = int(denominator) if isinstance(denominator, int) and denominator > 0 else 1
    bucket = int(hashlib.sha256(str(fingerprint).encode("utf-8")).hexdigest()[:8], 16)
    return {"selected": (bucket % n) == 0, "mode": mode, "rate_denominator": n,
            "forced_reason": None}


def sample_receipt(classification: str, key: str, *,
                   denominator: int = DEFAULT_ROW_DENOMINATOR,
                   forced_reason: Optional[str] = None) -> Dict[str, Any]:
    """Whether this comparison is written down, and why.

    A mismatch is never sampled away. Sampling a failure signal makes the comparator a coin
    flip, and the storage it saves is storage nobody was short of.
    """
    if classification != "match" or forced_reason:
        return {"sampled": True, "rate_denominator": 1,
                "forced_reason": forced_reason or (
                    "mismatches are never sampled" if classification != "match" else None)}
    n = int(denominator) if isinstance(denominator, int) and denominator > 0 else 1
    bucket = int(hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:8], 16)
    return {"sampled": (bucket % n) == 0, "rate_denominator": n, "forced_reason": None}


def coverage(counts: Mapping[str, int]) -> Dict[str, Any]:
    """Compared rows against what the authoritative surface offered.

    `excluded` is reported separately from `compared` on purpose: a comparator that silently
    folds unsettled and retention-edge rows into its denominator reports 100 % coverage of a
    window it mostly skipped.
    """
    offered = int(counts.get("legacy_rows_offered", 0))
    compared = int(counts.get("compared", 0))
    excluded_unsettled = int(counts.get("excluded_unsettled", 0))
    excluded_retention = int(counts.get("excluded_retention_edge", 0))
    excluded_sampling = int(counts.get("excluded_sampling", 0))
    excluded = excluded_unsettled + excluded_retention + excluded_sampling
    eligible = max(0, offered - excluded_unsettled - excluded_retention)
    return {
        "legacy_rows_offered": offered,
        "compared": compared,
        "excluded_unsettled": excluded_unsettled,
        "excluded_retention_edge": excluded_retention,
        "excluded_sampling": excluded_sampling,
        "eligible": eligible,
        "ratio": (float(compared) / float(eligible)) if eligible else 0.0,
        "accounted": offered == compared + excluded,
    }


# --- accounting --------------------------------------------------------------------------


def conservation(counts: Mapping[str, int]) -> bool:
    """`compared == pending + match + sum(mismatch classes)`.

    If it does not close, the run is not evidence and must not be reported as one -- the
    same discipline the batch path applies to `submitted = accepted + duplicate + refused +
    deferred`, and the write-side comparator to its own tally.
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


def gate_blocked(counts: Mapping[str, int]) -> List[str]:
    """Which observed classifications block a read cutover, in vocabulary order.

    Separate from `conservation()` because a run can close its accounting perfectly and
    still be a run nobody may promote on.
    """
    return [name for name in CLASSIFICATIONS
            if GATE_IMPACT_BY_CLASSIFICATION[name] == "blocks_cutover"
            and int(counts.get(name, 0)) > 0]


# --- receipt -----------------------------------------------------------------------------


def build_receipt(*, run_id: str, window_start: str, window_end: str, as_of: str,
                  emitted_at: str, query: Mapping[str, Any], pair: Mapping[str, Any],
                  comparison: Comparison, differences: Sequence[Difference] = (),
                  key: Optional[str] = None,
                  sampling: Optional[Mapping[str, Any]] = None,
                  coverage_block: Optional[Mapping[str, Any]] = None,
                  evidence: Optional[Mapping[str, Any]] = None,
                  comparator_build: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """A durable, redacted record of one shadow-read comparison.

    Raises `ValueError` for a non-terminal comparison: `pending` is a scheduling state, not
    a finding, and writing it down fills the store with rows that mean "ask again".
    """
    if comparison.classification in NON_TERMINAL:
        raise ValueError(
            "%s is not a terminal classification; a receipt records a finding, not a wait"
            % comparison.classification)
    normal = normalise_query(query)
    fingerprint = query_fingerprint(query)
    legacy = _side(pair, "legacy")
    canonical = _side(pair, "canonical")
    kept = list(differences)[:MAX_DIFFERENCES]
    dropped = max(0, len(list(differences)) - len(kept))
    grain = pair.get("grain", "result_set")

    return {
        "schema": RECEIPT_CONTRACT_ID,
        "schema_version": RECEIPT_CONTRACT_MAJOR,
        "receipt_id": receipt_id(run_id=run_id, fingerprint=fingerprint, key=key),
        "run_id": str(run_id),
        "emitted_at": emitted_at,
        "read_authority": READ_AUTHORITY,
        "read_mode": assert_shadow_only(str(pair.get("read_mode", "shadow"))),
        "grain": grain,
        "window": {"start": window_start, "end": window_end, "as_of": as_of,
                   "settle_s": normal["settle_s"]},
        "query": {
            "fingerprint": fingerprint,
            "evidence_class": normal["evidence_class"],
            "site_id": normal["site_id"],
            "device_ids": list(normal["device_ids"]),
            "time_from": normal["time_from"],
            "time_to": normal["time_to"],
            "filters": {path: redact_value("filter." + path, value)
                        for path, value in normal["filters"].items()},
            "sort": dict(normal["sort"]),
            "page_size": normal["page_size"],
            "projection_version": normal["projection_version"],
        },
        "row": {
            "row_key": key,
            "logical_id_present": key is not None,
        },
        "sides": {"legacy": _side_record(legacy), "canonical": _side_record(canonical)},
        "verdict": comparison.to_dict(),
        "differences": [d.to_dict() for d in kept],
        "differences_dropped": dropped,
        "disposition": {
            "repairable": comparison.repair_action in ("replay_inbox", "replay_outbox"),
            "repair_action": comparison.repair_action,
            "gate_impact": comparison.gate_impact,
            "queued": False,
            "queue_ref": None,
        },
        "sampling": dict(sampling or sample_receipt(comparison.classification,
                                                    key or fingerprint)),
        "coverage": dict(coverage_block or {}),
        "evidence": {
            "redaction_profile": REDACTION_PROFILE,
            "legacy_result_ref": (evidence or {}).get("legacy_result_ref"),
            "canonical_result_ref": (evidence or {}).get("canonical_result_ref"),
        },
        "comparator": dict(comparator_build or {"name": "unset", "version": "unset",
                                                "receipt_major": RECEIPT_CONTRACT_MAJOR}),
    }


def _side_record(side: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "outcome": side.get("outcome"),
        "row_state": side.get("row_state"),
        "row_count": side.get("row_count"),
        "page_count": side.get("page_count"),
        "truncated": bool(side.get("truncated", False)),
        "cursor_stable": side.get("cursor_stable"),
        "result_hash": side.get("result_hash"),
        "state_hash": side.get("state_hash"),
        "snapshot_at": side.get("snapshot_at"),
        "retention_class": side.get("retention_class"),
        "retention_horizon_s": side.get("retention_horizon_s"),
        "read_contract_major": side.get("read_contract_major"),
        "projection_version": side.get("projection_version"),
        "reader": side.get("reader"),
        "reader_version": side.get("reader_version"),
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


def _side_schema() -> Dict[str, Any]:
    return {
        "type": "object", "additionalProperties": True,
        "required": ["outcome", "row_state", "row_count"],
        "properties": {
            "outcome": _enum(SIDE_OUTCOMES, "what the read surface did"),
            "row_state": _enum(ROW_STATES, "what this surface says about the row"),
            "row_count": {"type": "integer", "minimum": 0},
            "page_count": _nullable("integer", "pages consumed for this answer"),
            "truncated": {"type": "boolean",
                          "description": "the page or row cap was reached"},
            "cursor_stable": _nullable("boolean", "the cursor round-tripped to the same rows"),
            "result_hash": _nullable("string", "order-independent digest of the settled set"),
            "state_hash": _nullable("string", "digest of one row's projection"),
            "snapshot_at": _nullable("string", "when this surface answered"),
            "retention_class": _nullable("string", "governance retention class"),
            "retention_horizon_s": _nullable("number", "declared retention horizon"),
            "read_contract_major": _nullable("integer", "read API/contract major"),
            "projection_version": _nullable("integer", "compared projection version"),
            "reader": _nullable("string", "which reader answered"),
            "reader_version": _nullable("string", "its build"),
            "reasons": {"type": "array", "items": {"type": "string"}},
        },
    }


def receipt_schema_document() -> Dict[str, Any]:
    """JSON Schema for `hear.shadowread.receipt.v1`, derived from this module.

    Generated by `tools/gen_shadow_read_contracts.py`; never hand-edited. Additive within the
    major: `additionalProperties` stays open so a newer comparator's extra field is preserved
    by an older reader instead of failing validation.
    """
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": RECEIPT_CONTRACT_URI,
        "title": RECEIPT_CONTRACT_ID,
        "description": (
            "Durable record of one legacy/canonical shadow-read comparison. Carries digests, "
            "field paths, counts and reason codes; never result bodies, credentials, "
            "coordinates or clip audio."
        ),
        "type": "object",
        "additionalProperties": True,
        "required": [
            "schema", "schema_version", "receipt_id", "run_id", "emitted_at",
            "read_authority", "read_mode", "grain", "window", "query", "row", "sides",
            "verdict", "differences", "differences_dropped", "disposition", "sampling",
            "coverage", "evidence", "comparator",
        ],
        "properties": {
            "schema": {"type": "string", "const": RECEIPT_CONTRACT_ID},
            "schema_version": {"type": "integer", "minimum": 1},
            "receipt_id": {
                "type": "string",
                "description": "uuid5 over (contract, run_id, query fingerprint, row key); "
                               "a re-run converges instead of duplicating.",
            },
            "run_id": {"type": "string"},
            "emitted_at": {"type": "string"},
            "read_authority": {
                "type": "string", "const": READ_AUTHORITY,
                "description": "legacy remains authoritative for the whole of Phase 5",
            },
            "read_mode": _enum(SHADOW_MODES, "the lane mode this comparison ran under"),
            "grain": _enum(GRAINS, "a whole answer, or one row inside it"),
            "window": {
                "type": "object", "additionalProperties": True,
                "required": ["start", "end", "as_of", "settle_s"],
                "properties": {
                    "start": {"type": "string"}, "end": {"type": "string"},
                    "as_of": {"type": "string"},
                    "settle_s": {"type": "number", "minimum": 0},
                },
            },
            "query": {
                "type": "object", "additionalProperties": True,
                "required": ["fingerprint", "evidence_class", "site_id", "sort", "page_size",
                             "projection_version"],
                "properties": {
                    "fingerprint": {"type": "string"},
                    "evidence_class": _enum(EVIDENCE_CLASSES, "which read surface"),
                    "site_id": {"type": "string"},
                    "device_ids": {"type": "array", "items": {"type": "string"}},
                    "time_from": _nullable("string", "inclusive lower bound"),
                    "time_to": _nullable("string", "exclusive upper bound"),
                    "filters": {"type": "object", "additionalProperties": True,
                                "description": "redacted filter values; an unknown filter "
                                               "path defaults to hidden"},
                    "sort": {"type": "object", "additionalProperties": True},
                    "page_size": {"type": "integer", "minimum": 1},
                    "projection_version": {"type": "integer", "minimum": 1},
                },
            },
            "row": {
                "type": "object", "additionalProperties": True,
                "required": ["row_key", "logical_id_present"],
                "properties": {
                    "row_key": _nullable("string", "identity of one compared row"),
                    "logical_id_present": {"type": "boolean"},
                },
            },
            "sides": {
                "type": "object", "additionalProperties": True,
                "required": list(SIDES),
                "properties": {name: _side_schema() for name in SIDES},
            },
            "verdict": {
                "type": "object", "additionalProperties": True,
                "required": ["classification", "severity", "repair_action", "gate_impact",
                             "actionable"],
                "properties": {
                    "classification": _enum(CLASSIFICATIONS, "closed verdict vocabulary"),
                    "severity": _enum(SEVERITIES, "operator routing"),
                    "repair_action": _enum(REPAIR_ACTIONS, "advisory, replay-only boundary"),
                    "gate_impact": _enum(GATE_IMPACTS, "whether this stops a read cutover"),
                    "actionable": {"type": "boolean"},
                    "notes": {"type": "array", "items": {"type": "string"}},
                },
            },
            "differences": {
                "type": "array", "maxItems": MAX_DIFFERENCES,
                "items": {
                    "type": "object", "additionalProperties": True,
                    "required": ["path", "legacy", "canonical", "comparator", "tolerance",
                                 "quoted", "blind"],
                    "properties": {
                        "path": {"type": "string"},
                        "legacy": {"description": "redacted, or null for a blind comparator"},
                        "canonical": {"description": "redacted, or null for a blind "
                                                     "comparator"},
                        "comparator": _enum(COMPARATORS, "how the field was compared"),
                        "tolerance": {"type": "string"},
                        "quoted": {"type": "boolean"},
                        "blind": {"type": "boolean",
                                  "description": "equality decided in memory; no value is "
                                                 "durable, not even a hash"},
                    },
                },
            },
            "differences_dropped": {"type": "integer", "minimum": 0},
            "disposition": {
                "type": "object", "additionalProperties": True,
                "required": ["repairable", "repair_action", "gate_impact", "queued"],
                "properties": {
                    "repairable": {"type": "boolean"},
                    "repair_action": _enum(REPAIR_ACTIONS, "advisory only"),
                    "gate_impact": _enum(GATE_IMPACTS, "cutover consequence"),
                    "queued": {"type": "boolean", "const": False,
                               "description": "the comparator never queues repair; a human "
                                              "dequeues from an advisory work list"},
                    "queue_ref": _nullable("string", "set by a human, never by this tool"),
                },
            },
            "sampling": {
                "type": "object", "additionalProperties": True,
                "required": ["sampled", "rate_denominator"],
                "properties": {
                    "sampled": {"type": "boolean"},
                    "rate_denominator": {"type": "integer", "minimum": 1},
                    "forced_reason": _nullable("string", "why sampling was overridden"),
                },
            },
            "coverage": {"type": "object", "additionalProperties": True},
            "evidence": {
                "type": "object", "additionalProperties": True,
                "required": ["redaction_profile"],
                "properties": {
                    "redaction_profile": {"type": "string", "const": REDACTION_PROFILE},
                    "legacy_result_ref": _nullable("string", "object key of the retained "
                                                             "legacy answer"),
                    "canonical_result_ref": _nullable("string", "object key of the retained "
                                                                "canonical answer"),
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
