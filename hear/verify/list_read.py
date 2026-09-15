"""List, aggregate and export read comparison: pinning, page identity and drift verdicts.

Phase 5's comparator (`hear/verify/shadow_read.py`) compares **records**: a row on the legacy
surface against the same row on the canonical surface, joined on an identity both sides
already recorded. The Phase 6 read audit
(`docs/phase6-operator-read-audit.md` §8.2) deferred everything that is *not* a record, and
named the reason: the legacy list surfaces have no cursor at all (`/api/queue` carries a
`limit` and nothing else), `/api/export` serialises the whole annotations table in one
unbounded response, and an aggregate has no per-record identity pair for
`reconcile.correlation_id()` to join on. Synthesising one would defeat the guard that makes
the record receipt trustworthy, so a separately allocated contract was reserved instead --
working name `hear.readcompare.receipt.v1`, which is what this module defines.

This module is **design only**. It performs no I/O of any kind: no socket, no store, no
cluster client, no scheduler, no credential material, no clock. A future comparator process
is expected to pin each side, hand two page/aggregate/export descriptors to `classify()`, and
persist whatever `build_receipt()` returns. Nothing in the runtime tree imports it, and a
test asserts that stays true.

Six positions carry the design, each asserted by a test rather than described:

1. **Legacy stays authoritative**, and the lane restriction is Phase 5's, imported rather
   than restated: `READ_AUTHORITY` and `assert_shadow_only()` come from `shadow_read`, so a
   build that moved read authority moves it in exactly one place.
2. **An unpinned list read is not comparable.** A list answer is a function of a *snapshot*,
   and the legacy surfaces have no snapshot isolation and no cursor. The comparator therefore
   pins each side by a declared mechanism -- an opaque canonical cursor, or a legacy
   half-open `(since_id, max_id)` range plus `as_of` -- and refuses to compare a side that
   reports no pin. Refusing is the honest answer; inventing a cursor for the legacy surface
   would make the comparator the author of the very ordering it is auditing.
3. **A cursor is opaque and side-local.** The comparator never parses one, never re-derives
   one, and never compares one across sides: an opaque token from one surface has no meaning
   on the other. `cursors_comparable()` returns `False` unconditionally, and it exists so
   that fact is testable rather than remembered.
4. **A total order or no comparison.** A sort key without a unique tie-break makes paging
   non-deterministic, which makes every page-level verdict in the run unfalsifiable. Ties
   ordered differently are `tie_break_divergence`, judged before content.
5. **Unbounded work is not audited; it is refused.** The comparator never issues the
   unbounded legacy export. Export equivalence is proved over a bounded, recorded range, in
   streamed slices, against a manifest digest -- and a range that cannot be bounded is
   `export_bound_exceeded` with the coverage loss recorded, never a silently skipped read.
6. **An aggregate receipt carries counts, not people.** Group keys come from a closed
   dimension list (a reviewer id is not a dimension), an aggregate over a denied path is
   refused at construction rather than blinded, and a cell whose count is below
   `MIN_CELL_COUNT` has its key redacted while its verdict is kept.

Deliberately **not** named `*SCHEMA*`: `tools/freeze_contracts.py` harvests published contract
ids from `[A-Z_]*SCHEMA[A-Z_]*` assignments, and this receipt is staged rather than published.
`docs/decisions/0009-phase6-list-read-comparison.md` records why and states the promotion
procedure.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from hear.ingest import reconcile as _rc
from hear.verify import shadow_read as _sr

RECEIPT_CONTRACT_ID = "hear.readcompare.receipt.v1"
RECEIPT_CONTRACT_MAJOR = 1
"""The only receipt major this reader supports. A larger major is refused, not guessed."""

RECEIPT_CONTRACT_URI = (
    "https://schemas.dama.example/hear/readcompare/v1/receipt.schema.json"
)
RECEIPT_MEDIA_TYPE = "application/vnd.dama.hear.readcompare.receipt.v1+json"

CORRELATION_NAMESPACE = uuid.UUID("2b6d9c41-8f73-5a0e-9c58-6de0b1f47a92")
"""Fixed namespace for deterministic receipt ids. Changing it re-identifies every receipt
ever written, so it is frozen for the life of the v1 major."""

# --- authority ---------------------------------------------------------------------------

READ_AUTHORITY = _sr.READ_AUTHORITY
"""Imported, never restated. Two copies of "who is authoritative" drift, and the copy that
is wrong on the day it matters is always the one nobody tested."""

READ_MODES: Tuple[str, ...] = _sr.READ_MODES
SHADOW_MODES: Tuple[str, ...] = _sr.SHADOW_MODES
assert_shadow_only = _sr.assert_shadow_only

SIDES: Tuple[str, ...] = _sr.SIDES

# --- vocabularies (all closed) -----------------------------------------------------------

GRAINS: Tuple[str, ...] = ("page", "window", "aggregate", "export")
"""Four grains, because four different findings exist and they have four different repairs:
one page of a list, a whole settled window of it, one aggregate cell set, and one bounded
export. A record-grain finding belongs to `hear.shadowread.receipt.v1` and is not re-homed
here."""

LIST_SURFACES: Tuple[str, ...] = (
    "annotation_queue",
    "annotation_export",
    "fleet_health_table",
    "health_snapshot",
    "clip_index",
    "detection_list",
    "scene_list",
    "tag_list",
    "score_list",
    "localization_list",
    "audit_list",
    "ledger_stat",
)
"""Every legacy list/aggregate/export surface the Phase 6 audit inventoried. Closed: a
surface with no entry here is a `comparator_fault`, never a silently skipped read."""

EXHAUSTIVE_SURFACES: Tuple[str, ...] = (
    "health_snapshot",
    "ledger_stat",
    "audit_list",
    "annotation_export",
)
"""Low-cardinality, high-consequence surfaces. Sampling an aggregate is not cost control --
it is deleting the only number anybody reads."""

RESTRICTED_SURFACES: Tuple[str, ...] = ("clip_index", "annotation_export")
"""Surfaces whose rows carry bytes or human text the comparator never reads. A clip is
compared as its manifest; an annotation is compared as row identity, state and digest, and
its free-text `notes` and reviewer identity never enter a receipt."""

SIDE_OUTCOMES: Tuple[str, ...] = _sr.SIDE_OUTCOMES
ROW_STATES: Tuple[str, ...] = _sr.ROW_STATES

PIN_KINDS: Tuple[str, ...] = ("cursor_pin", "range_pin", "snapshot_id", "none")
"""How one side was pinned to a snapshot for the duration of a paged read.

`cursor_pin` is the canonical surface's opaque cursor, which `docs/api-boundaries.md` already
requires to be stable over `(created_at, id)` and to expire in 24 h. `range_pin` is what the
legacy surfaces can actually offer today: a half-open `(since_id, max_id)` bound plus an
`as_of`, which makes a legacy page reproducible without changing the legacy reader.
`snapshot_id` is a store-level snapshot (a Postgres export snapshot, an object-store
generation). `none` means the side could not be pinned, and a comparison against it is not
evidence."""

PINNED_KINDS: Tuple[str, ...] = ("cursor_pin", "range_pin", "snapshot_id")

DRIFT_KINDS: Tuple[str, ...] = (
    "none",
    "insert_drift",
    "delete_drift",
    "update_drift",
    "reorder_drift",
    "snapshot_drift",
    "pin_absent",
)
"""What changed underneath a paged read while it was being read.

The first five are observations about rows; `snapshot_drift` is the observation that a
*pinned* re-read returned a different page at all, which on a surface with no snapshot
isolation is expected legacy behaviour rather than a defect; `pin_absent` is the comparator
admitting it never had a snapshot to begin with."""

AGGREGATE_FUNCTIONS: Tuple[str, ...] = (
    "count", "distinct", "sum", "min", "max", "p50", "p95", "p99",
)

GROUP_KEY_DIMENSIONS: Tuple[str, ...] = (
    "site_id",
    "device_id",
    "day",
    "hour",
    "evidence_class",
    "kind",
    "state",
    "retention_class",
    "lane_mode",
)
"""The only dimensions an aggregate may be grouped by. Closed on purpose: `user_id` is not
here, and neither is any free-text field, because a group-by is a projection and a projection
over a reviewer identity is a per-reviewer productivity report nobody approved."""

COMPARATORS: Tuple[str, ...] = _sr.COMPARATORS
TOLERANCE_BY_COMPARATOR: Dict[str, str] = dict(_sr.TOLERANCE_BY_COMPARATOR)
BLIND_COMPARATORS: Tuple[str, ...] = _sr.BLIND_COMPARATORS

CLASSIFICATIONS: Tuple[str, ...] = (
    "match",
    "pending",
    "late_arrival",
    "snapshot_drift",
    "boundary_drift",
    "cursor_divergence",
    "tie_break_divergence",
    "order_divergence",
    "pagination_divergence",
    "count_divergence",
    "membership_divergence",
    "aggregate_divergence",
    "export_divergence",
    "export_bound_exceeded",
    "filter_divergence",
    "projection_divergence",
    "retention_divergence",
    "tombstone_divergence",
    "threshold_divergence",
    "scope_divergence",
    "version_divergence",
    "comparator_fault",
    "unclassified",
)

NON_TERMINAL: Tuple[str, ...] = ("pending",)
TERMINAL: Tuple[str, ...] = tuple(c for c in CLASSIFICATIONS if c not in NON_TERMINAL)
MISMATCH_CLASSIFICATIONS: Tuple[str, ...] = tuple(c for c in TERMINAL if c != "match")

SEVERITIES: Tuple[str, ...] = _sr.SEVERITIES

SEVERITY_BY_CLASSIFICATION: Dict[str, str] = {
    "match": "info",
    "pending": "info",
    "late_arrival": "warn",
    # A legacy list has no snapshot isolation. A pinned re-read that shifted is a fact about
    # a surface nobody is changing in this phase, and paging it as a defect trains an
    # operator to mute the comparator in week two.
    "snapshot_drift": "warn",
    "boundary_drift": "warn",
    # Unstable paging makes every page-level verdict in the run unfalsifiable.
    "cursor_divergence": "critical",
    "tie_break_divergence": "critical",
    "order_divergence": "warn",
    "pagination_divergence": "warn",
    "count_divergence": "critical",
    "membership_divergence": "critical",
    "aggregate_divergence": "critical",
    "export_divergence": "critical",
    # Not a defect in either store: a range the comparator may not bound is a read it must
    # not issue. It costs coverage, and coverage loss is reported, never hidden.
    "export_bound_exceeded": "warn",
    "filter_divergence": "critical",
    "projection_divergence": "critical",
    "retention_divergence": "warn",
    "tombstone_divergence": "critical",
    # Two sides invoked with different operator thresholds did not disagree about data.
    # The run is void, and a void run must never be reported as a clean one.
    "threshold_divergence": "critical",
    "scope_divergence": "critical",
    "version_divergence": "warn",
    "comparator_fault": "critical",
    "unclassified": "critical",
}

REPAIR_ACTIONS: Tuple[str, ...] = _sr.REPAIR_ACTIONS

REPAIR_BY_CLASSIFICATION: Dict[str, str] = {
    "match": "none",
    "pending": "none",
    "late_arrival": "none",
    "snapshot_drift": "none",
    "boundary_drift": "none",
    "cursor_divergence": "reindex_request",
    "tie_break_divergence": "reindex_request",
    "order_divergence": "none",
    "pagination_divergence": "reindex_request",
    "count_divergence": "manual_review",
    "membership_divergence": "replay_inbox",
    "aggregate_divergence": "manual_review",
    "export_divergence": "manual_review",
    "export_bound_exceeded": "manual_review",
    "filter_divergence": "manual_review",
    "projection_divergence": "manual_review",
    "retention_divergence": "manual_review",
    "tombstone_divergence": "manual_review",
    "threshold_divergence": "manual_review",
    "scope_divergence": "manual_review",
    "version_divergence": "manual_review",
    "comparator_fault": "manual_review",
    "unclassified": "manual_review",
}

GATE_IMPACTS: Tuple[str, ...] = _sr.GATE_IMPACTS

BLOCKING_CLASSIFICATIONS: Tuple[str, ...] = (
    "cursor_divergence",
    "tie_break_divergence",
    "count_divergence",
    "membership_divergence",
    "aggregate_divergence",
    "export_divergence",
    "filter_divergence",
    "projection_divergence",
    "tombstone_divergence",
    "threshold_divergence",
    "scope_divergence",
    "comparator_fault",
    "unclassified",
)

#: Kept apart from `repair_action` because "who fixes this" and "may we proceed" are
#: different questions, and conflating them is how a blocking finding gets closed as a ticket.
GATE_IMPACT_BY_CLASSIFICATION: Dict[str, str] = {
    name: ("blocks_cutover" if name in BLOCKING_CLASSIFICATIONS else "none")
    for name in CLASSIFICATIONS
}

# --- tolerances --------------------------------------------------------------------------

SETTLE_S = _sr.SETTLE_S
LATE_ARRIVAL_S = _sr.LATE_ARRIVAL_S
BACKFILL_SETTLE_S = _sr.BACKFILL_SETTLE_S
SNAPSHOT_SKEW_S = _sr.SNAPSHOT_SKEW_S
RETENTION_EDGE_S = _sr.RETENTION_EDGE_S
ORDER_GRACE_S = _sr.ORDER_GRACE_S
"""Time semantics are Phase 5's, imported unchanged. A list comparison that used a different
settle horizon from the record comparison auditing the same rows would produce two
irreconcilable accounts of one window."""

PIN_TTL_S = _sr.CURSOR_TTL_S
"""A pin -- cursor, range or snapshot -- is valid for 24 h, matching the cursor lifetime
`docs/api-boundaries.md` already states. A comparator that presents an older pin has produced
a fault of its own, not found a divergence."""

MAX_PAGES_COMPARED = _sr.MAX_PAGES
MAX_ROWS_COMPARED = _sr.MAX_ROWS_COMPARED

MAX_EXPORT_ROWS_COMPARED = 50_000
"""Bound on a single bounded-export equivalence proof. Beyond it the range is split; it is
never widened, because the one thing this comparator may not do is reproduce the unbounded
export whose cost is the reason `/api/export` is a recorded defect (L1)."""

EXPORT_SLICE_ROWS = 1_000
"""Export equivalence is proved slice by slice, digest by digest. The comparator never holds
a whole export in memory, on either side: an auditor that needs the resources of the defect
it is auditing is a second outage."""

MAX_AGGREGATE_CELLS = 500
"""Per-aggregate cell cap. A group-by that produces more cells than this is a list, and it is
compared as one."""

MIN_CELL_COUNT = 5
"""An aggregate cell whose count is below this has its **group key redacted** in the receipt,
while its verdict is kept. A per-device, per-hour cell of size one is a record with extra
steps, and a receipt store full of them is a movement log."""

MAX_DIFFERENCES = _sr.MAX_DIFFERENCES
MAX_RECEIPT_BYTES = _sr.MAX_RECEIPT_BYTES

DEFAULT_PAGE_DENOMINATOR = 10
"""Sampled surfaces compare 1-in-N *pages*, deterministic on the page fingerprint. Boundary
pages are never sampled away (`sample_page`), because a page boundary is exactly where an
unstable order shows itself."""

DEFAULT_QUERY_DENOMINATOR = _sr.DEFAULT_QUERY_DENOMINATOR
COST_CEILING_S_PER_10K_ROWS = _sr.COST_CEILING_S_PER_10K_ROWS

# --- redaction ---------------------------------------------------------------------------

REDACTION_PROFILE = "hear.readcompare.redact.v1"

VALUE_ALLOW_PATHS: Tuple[str, ...] = tuple(sorted(set(_sr.READ_VALUE_ALLOW_PATHS) | {
    "surface",
    "grain",
    "pin_kind",
    "pin_age_s",
    "drift_kind",
    "page.index",
    "page.size",
    "page.count",
    "sort.tie_break",
    "aggregate.function",
    "aggregate.dimension",
    "aggregate.cells",
    "aggregate.value",
    "export.rows_declared",
    "export.rows_compared",
    "export.slices",
    "export.bounded",
    "threshold.name",
    "threshold_fingerprint",
    "expected_difference_list",
    "rows_inserted",
    "rows_deleted",
    "rows_updated",
}))
"""Phase 5's read allow list, itself the union of the write side's, plus the list-shaped paths
this comparator adds. Built by union rather than by copy so a tightening anywhere upstream is
inherited here and cannot be lost by editing one file."""

VALUE_ALLOW_PREFIXES: Tuple[str, ...] = ("aggregate.value.",)
"""The one prefix rule in this contract, and it exists for one reason: an aggregate cell's
*value* is a count, and a count with both sides hidden is a finding an operator cannot act on.
The cell's *identity* is never in the path -- `cell_ref()` puts an opaque digest there -- and a
cell below the k-anonymity floor is pathed under `aggregate.value_small.`, which is not
quotable, so its numbers are redacted like any other small-population fact."""

CELL_PATH = "aggregate.value"
SMALL_CELL_PATH = "aggregate.value_small"


def cell_ref(cell_key: Any) -> str:
    """Opaque, stable handle for one aggregate cell.

    A cell key is a tuple of group-key values -- a device and an hour, say -- and putting it
    in a difference path would smuggle the dimension values past the k-anonymity rule that
    governs `aggregate.cells`. The handle is stable across runs, so two receipts about the
    same cell are joinable, and it resolves to a key only through the retained answer the
    receipt references.
    """
    encoded = json.dumps(cell_key, separators=(",", ":"), sort_keys=True, default=str,
                         ensure_ascii=True)
    return "c:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def path_is_quotable(path: str) -> bool:
    """True when a field path's value may appear literally in a receipt.

    Deny beats allow, always, and the deny pattern is the write side's own -- one pattern, one
    test, one place a future location-shaped field name has to get past.
    """
    if not isinstance(path, str) or not path:
        return False
    if _rc.VALUE_DENY_PATTERN.search(path):
        return False
    if path in VALUE_ALLOW_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in VALUE_ALLOW_PREFIXES)


def redact_value(path: str, value: Any) -> Any:
    """A receipt-safe rendering of one compared value."""
    if path_is_quotable(path) and isinstance(value, (str, int, float, bool, type(None))):
        return value
    if value is None:
        return None
    return {"redacted": "sha256:" + _rc._hash_token(value)}


def assert_aggregatable(path: str) -> str:
    """Refuse an aggregate over a restricted path, instead of blinding it.

    A `min`/`max` over a coordinate is a bounding box, a `distinct` over a token is a
    credential census, and a `count` grouped by reviewer identity is a performance review. A
    blind comparator can hide a compared *value*; it cannot make an aggregate over a
    restricted field a thing anybody asked for. So this raises.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("aggregate path must be a non-empty string")
    if _rc.VALUE_DENY_PATTERN.search(path):
        raise ValueError(
            "refusing to aggregate over restricted path %r; an aggregate over a coordinate, "
            "a credential or a payload is a disclosure with a summary in front of it" % path)
    return path.strip()


def assert_group_keys(dimensions: Sequence[str]) -> List[str]:
    """Closed dimension list for a group-by, sorted and deduplicated."""
    out: List[str] = []
    for dim in dimensions or ():
        name = str(dim)
        if name not in GROUP_KEY_DIMENSIONS:
            raise ValueError(
                "%r is not a permitted aggregate dimension; group keys are closed so a "
                "reviewer identity or a free-text field cannot become one" % name)
        if name not in out:
            out.append(name)
    return sorted(out)


def redact_cell_key(cell: Mapping[str, Any], *, count: int) -> Dict[str, Any]:
    """Group key for a receipt, with small cells suppressed.

    The *verdict* for a small cell is always kept -- suppressing the finding would be the
    comparator hiding its own evidence -- but its key is redacted, because a cell of size one
    identified by device and hour is a record, and this contract exists precisely because
    records belong in the other receipt.
    """
    keys = assert_group_keys(list(cell.keys()))
    if int(count) < MIN_CELL_COUNT:
        return {"suppressed": True, "dimensions": keys, "values": None,
                "reason": "cell count below k=%d" % MIN_CELL_COUNT}
    return {
        "suppressed": False,
        "dimensions": keys,
        "values": {k: redact_value("aggregate.dimension", cell[k]) for k in keys},
        "reason": None,
    }


# --- digests -----------------------------------------------------------------------------

state_hash = _sr.state_hash
result_hash = _sr.result_hash
"""Row and result-set digests are Phase 5's, so one row audited by both comparators produces
one digest and not two."""


def membership_digest(row_keys: Sequence[str]) -> str:
    """Order-independent digest of *which rows* a page or window contained.

    Kept apart from `order_digest` on purpose: "the same rows in a different order" and "a
    different set of rows" are different findings with different repairs, and one digest over
    both makes them indistinguishable.
    """
    encoded = json.dumps(sorted(str(k) for k in row_keys), separators=(",", ":"),
                         ensure_ascii=True)
    return "m:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def order_digest(row_keys: Sequence[str]) -> str:
    """Order-dependent digest of a page's sequence."""
    encoded = json.dumps([str(k) for k in row_keys], separators=(",", ":"), ensure_ascii=True)
    return "o:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def projection_digest(fields: Sequence[str]) -> str:
    """Digest of the compared field set, so two surfaces exposing different shapes are a
    finding rather than a hundred per-row value findings."""
    encoded = json.dumps(sorted(str(f) for f in fields), separators=(",", ":"),
                         ensure_ascii=True)
    return "j:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def slice_digest(slice_digests: Sequence[str]) -> str:
    """Digest of an ordered list of export slice digests.

    Order-dependent: an export's equivalence proof is over a *bounded, ordered range*, and a
    reordered export is a different artifact even when it holds the same rows.
    """
    encoded = json.dumps([str(d) for d in slice_digests], separators=(",", ":"),
                         ensure_ascii=True)
    return "x:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --- query, threshold and page identity --------------------------------------------------


def normalise_query(query: Mapping[str, Any]) -> Dict[str, Any]:
    """The closed normal form of one list/aggregate/export question.

    Total and explicit: an unknown key is folded into `extra` rather than dropped, so a newer
    comparator's filter cannot vanish from the fingerprint and let two sides be asked
    different things while still "agreeing". Bounds carry their inclusivity (`bound_kind`)
    because an inclusive versus half-open upper bound is the single most common way one
    request becomes two questions.
    """
    surface = query.get("surface")
    if surface not in LIST_SURFACES:
        raise ValueError("unknown list surface %r" % (surface,))
    filters = query.get("filters") or {}
    sort = assert_total_order(query.get("sort") or {})
    normal: Dict[str, Any] = {
        "surface": surface,
        "site_id": _require_token("site_id", query.get("site_id")),
        "device_ids": sorted(str(d) for d in (query.get("device_ids") or ())),
        "time_from": query.get("time_from"),
        "time_to": query.get("time_to"),
        "bound_kind": str(query.get("bound_kind") or "half_open"),
        "filters": {str(k): filters[k] for k in sorted(filters)},
        "sort": sort,
        "page_size": int(query.get("page_size") or 50),
        "as_of": query.get("as_of"),
        "settle_s": float(query.get("settle_s", SETTLE_S)),
        "projection_version": int(query.get("projection_version") or 1),
        "thresholds": normalise_thresholds(query.get("thresholds") or {}),
        "expected_difference_list": query.get("expected_difference_list"),
    }
    if normal["bound_kind"] not in ("half_open", "closed"):
        raise ValueError("bound_kind must be half_open or closed, not %r"
                         % (normal["bound_kind"],))
    extra = {k: query[k] for k in sorted(query) if k not in normal and k != "read_mode"}
    if extra:
        normal["extra"] = extra
    return normal


def assert_total_order(sort: Mapping[str, Any]) -> Dict[str, Any]:
    """A sort key plus a unique tie-break, or no comparison at all.

    `docs/phase6-operator-api-contract.md` §"total order or no cursor" already requires this
    of the canonical API. It is restated as an assertion here because the *comparator* needs
    it for a different reason: without a unique tie-break, two honest readers can return the
    same rows in two orders and page them into two different sets, and every page-level
    verdict downstream of that is noise.
    """
    field = str(sort.get("field") or "created_at")
    direction = str(sort.get("direction") or "asc")
    tie_break = sort.get("tie_break")
    if direction not in ("asc", "desc"):
        raise ValueError("sort direction must be asc or desc, not %r" % (direction,))
    if not isinstance(tie_break, str) or not tie_break.strip():
        raise ValueError(
            "a list comparison requires a unique tie-break column (for example `id`); a sort "
            "key that is not a total order makes paging non-deterministic")
    return {"field": field, "direction": direction, "tie_break": tie_break.strip()}


def normalise_thresholds(thresholds: Mapping[str, Any]) -> Dict[str, Any]:
    """The operator-tunable inputs that change a verdict without changing the data.

    `--max-stale-s`, `--unfetched-window-s` and the clip deferred/lost thresholds are
    invocation flags today (P6-R3, L5): two operators get two verdicts from identical rows.
    They are pinned into the query's normal form so a comparison whose two sides were invoked
    differently is `threshold_divergence` -- a void run -- rather than a mismatch.
    """
    return {str(k): thresholds[k] for k in sorted(thresholds or {})}


def query_fingerprint(query: Mapping[str, Any]) -> str:
    """Stable identity of one question, shared by both sides."""
    normal = normalise_query(query)
    encoded = json.dumps(normal, separators=(",", ":"), sort_keys=True, allow_nan=False,
                         ensure_ascii=True)
    return "q:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def threshold_fingerprint(thresholds: Mapping[str, Any]) -> str:
    """Identity of the threshold set a side was invoked with."""
    encoded = json.dumps(normalise_thresholds(thresholds), separators=(",", ":"),
                         sort_keys=True, allow_nan=False, ensure_ascii=True)
    return "t:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def page_fingerprint(*, fingerprint: str, pin_id: str, page_index: int,
                     page_size: int) -> str:
    """Identity of one page *within one pinned snapshot*.

    `pin_id` is a side-local, non-secret handle the comparator records -- a snapshot id, a
    recorded `(since_id, max_id)` bound, or a digest the side published for its cursor. It is
    never the cursor token itself, which is opaque and may encode state that has no business
    in a durable receipt.
    """
    scope = "\x1f".join((
        RECEIPT_CONTRACT_ID, str(fingerprint), str(pin_id), str(int(page_index)),
        str(int(page_size)),
    ))
    return "p:" + hashlib.sha256(scope.encode("utf-8")).hexdigest()


def row_key(*, surface: str, site_id: str, logical_id: str,
            device_id: Optional[str] = None) -> str:
    """Identity of one list row across both surfaces.

    `logical_id` is whatever identity the surfaces **already recorded** -- an annotation row
    id, a clip id, an `event_id`, an object logical id. It is never re-derived here: a
    comparator that reimplements the identity it audits can make both sides agree on a bug,
    which is the trap ADR 0005 rejected on the write side.
    """
    if surface not in LIST_SURFACES:
        raise ValueError("unknown list surface %r" % (surface,))
    scope = "\x1f".join((
        RECEIPT_CONTRACT_ID,
        surface,
        _require_token("site_id", site_id),
        str(device_id or ""),
        _require_token("logical_id", logical_id),
    ))
    return "r:" + hashlib.sha256(scope.encode("utf-8")).hexdigest()


def receipt_id(*, run_id: str, fingerprint: str, key: Optional[str] = None) -> str:
    """Deterministic receipt identity, so a re-run of a window converges instead of
    duplicating -- which is what lets a rolled-back comparator build be compared directly
    against the build that produced a finding."""
    return str(uuid.uuid5(CORRELATION_NAMESPACE, "\x1f".join(
        (RECEIPT_CONTRACT_ID, str(run_id), str(fingerprint), str(key or "")))))


def _require_token(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % name)
    return value.strip()


# --- pinning and cursors -----------------------------------------------------------------


def pin_ok(side: Mapping[str, Any], *, ttl_s: float = PIN_TTL_S) -> bool:
    """True when this side was pinned to a snapshot the comparator may still rely on."""
    kind = side.get("pin_kind")
    if kind not in PINNED_KINDS:
        return False
    age = side.get("pin_age_s")
    if isinstance(age, (int, float)) and float(age) > float(ttl_s):
        return False
    if kind == "range_pin":
        bound = side.get("pin_bound") or {}
        return bool(bound.get("since_id_present")) and bool(bound.get("max_id_present"))
    return bool(side.get("pin_id"))


def assert_pinned(side: Mapping[str, Any], *, name: str = "side") -> str:
    """Refuse an unpinned list read rather than compare it.

    The legacy list surfaces have no cursor and no snapshot isolation, so the comparator
    supplies the pin *as a bound on its own request* -- a half-open `(since_id, max_id)` plus
    an `as_of` -- and does not change the reader to get one. A side that cannot be pinned is
    not compared, and the coverage loss is recorded. Comparing it anyway would report
    concurrent writes as data loss, which is the false positive that ends comparators.
    """
    if not pin_ok(side):
        raise ValueError(
            "%s is not pinned to a snapshot; a list answer is a function of an instant and "
            "an unpinned page is not evidence" % name)
    return str(side.get("pin_kind"))


def cursors_comparable(left: Any, right: Any) -> bool:
    """Always `False`.

    An opaque token from one surface has no meaning on the other, and a comparator that
    diffed two cursors would be inventing a contract neither side signed. This is a function
    rather than a comment so the property has a test.
    """
    return False


def cursor_opaque(token: Any) -> bool:
    """True when a cursor is treated as an opaque handle: a string the comparator stores,
    never parses, and never places in a receipt."""
    return isinstance(token, str) and bool(token)


# --- window semantics --------------------------------------------------------------------

settled = _sr.settled
retention_edge = _sr.retention_edge
snapshot_skew_ok = _sr.snapshot_skew_ok


# --- aggregates --------------------------------------------------------------------------


def compare_aggregate(legacy_cells: Mapping[str, Any], canonical_cells: Mapping[str, Any], *,
                      function: str, tolerance: float = 0.0,
                      cell_counts: Optional[Mapping[str, int]] = None,
                      path: str = CELL_PATH) -> List["Difference"]:
    """Cell-by-cell comparison of one aggregate, with counts compared exactly.

    `tolerance` applies to the continuous functions only. A `count` or a `distinct` is
    compared with `tolerance = 0` whatever the caller passes: an aggregate with a fuzzy count
    is a number nobody can gate on, and "close enough" on a row count is how a missing day
    survives a review.

    Each difference is pathed by an opaque `cell_ref()`, never by the cell's group-key values,
    and a cell whose population is below `MIN_CELL_COUNT` is pathed under the non-quotable
    small-cell prefix so its numbers are redacted. For a `count`/`distinct` the population *is*
    the value; for every other function the caller supplies `cell_counts`, and a cell with no
    declared population is treated as small. Defaulting to suppression is the only safe
    default: the alternative leaks exactly the cells that identify one person or one node.
    """
    if function not in AGGREGATE_FUNCTIONS:
        raise ValueError("unknown aggregate function %r" % (function,))
    assert_aggregatable(path)
    if function in ("count", "distinct"):
        tolerance = 0.0
    keys = sorted(set(legacy_cells) | set(canonical_cells))
    if len(keys) > MAX_AGGREGATE_CELLS:
        raise ValueError("aggregate has %d cells; beyond %d it is a list and is compared as "
                         "one" % (len(keys), MAX_AGGREGATE_CELLS))
    comparator = "count" if function in ("count", "distinct") else "numeric"
    out: List[Difference] = []
    for key in keys:
        left = legacy_cells.get(key)
        right = canonical_cells.get(key)
        population = _cell_population(function, left, right, (cell_counts or {}).get(key))
        base = path if population >= MIN_CELL_COUNT else SMALL_CELL_PATH
        cell_path = "%s.%s" % (base, cell_ref(key))
        if left is None or right is None:
            out.append(Difference(cell_path, left, right, comparator=comparator,
                                  tolerance="cell present on one side only"))
            continue
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if abs(float(left) - float(right)) > float(tolerance):
                out.append(Difference(cell_path, left, right, comparator=comparator))
        elif left != right:
            out.append(Difference(cell_path, left, right, comparator="exact"))
    return out


def _cell_population(function: str, left: Any, right: Any,
                     declared: Optional[int]) -> int:
    if isinstance(declared, int):
        return declared
    if function in ("count", "distinct"):
        values = [v for v in (left, right) if isinstance(v, (int, float))]
        return int(min(values)) if values else 0
    return 0


# --- bounded export equivalence ----------------------------------------------------------


def export_equivalence(*, paged_slice_digests: Sequence[str], export_slice_digests:
                       Sequence[str], rows_paged: int, rows_exported: int,
                       bounded: bool, rows_declared: Optional[int] = None) -> Dict[str, Any]:
    """Is a bounded export byte-equivalent to the paged read of the same range?

    This is the read-side statement of the property `docs/legacy-operator-read-boundaries.md`
    already owes an operator: "page exhaustion with a recorded `(since_id, max_id)` pair must
    be byte-identical to the unbounded export". It is proved here over a **bounded** range, in
    ordered slices of at most `EXPORT_SLICE_ROWS` rows, comparing digests rather than bodies,
    so neither the comparator nor the legacy service ever materialises the whole dump.

    An unbounded request is not attempted. `bounded=False` returns `export_bound_exceeded`,
    which costs coverage and says so, and is the honest outcome for a surface whose unbounded
    cost is itself a recorded defect (L1).
    """
    if not bounded:
        return {
            "equivalent": False,
            "classification": "export_bound_exceeded",
            "rows_compared": 0,
            "slices": 0,
            "reason": "the range could not be bounded; the comparator does not issue the "
                      "unbounded export it exists to make unnecessary",
        }
    if max(int(rows_paged), int(rows_exported)) > MAX_EXPORT_ROWS_COMPARED:
        return {
            "equivalent": False,
            "classification": "export_bound_exceeded",
            "rows_compared": 0,
            "slices": 0,
            "reason": "range exceeds %d rows; split the range, never widen the read"
                      % MAX_EXPORT_ROWS_COMPARED,
        }
    if int(rows_paged) != int(rows_exported):
        return {
            "equivalent": False,
            "classification": "export_divergence",
            "rows_compared": int(rows_paged),
            "slices": len(list(paged_slice_digests)),
            "reason": "paged read returned %d rows, the bounded export %d"
                      % (int(rows_paged), int(rows_exported)),
        }
    if rows_declared is not None and int(rows_declared) != int(rows_exported):
        return {
            "equivalent": False,
            "classification": "export_divergence",
            "rows_compared": int(rows_exported),
            "slices": len(list(export_slice_digests)),
            "reason": "the export manifest declares %d rows and carries %d"
                      % (int(rows_declared), int(rows_exported)),
        }
    left = slice_digest(paged_slice_digests)
    right = slice_digest(export_slice_digests)
    if left != right:
        return {
            "equivalent": False,
            "classification": "export_divergence",
            "rows_compared": int(rows_paged),
            "slices": len(list(paged_slice_digests)),
            "reason": "same row count, different ordered content digest",
        }
    return {
        "equivalent": True,
        "classification": "match",
        "rows_compared": int(rows_paged),
        "slices": len(list(paged_slice_digests)),
        "reason": None,
    }


# --- comparison --------------------------------------------------------------------------


class Difference(object):
    """One compared field, cell or property that did not agree.

    A `blind` or `presence` difference carries no values at all, not even redacted ones: a
    stable hash of a coordinate is still a stable identifier for that coordinate.
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
            return {"path": self.path, "legacy": None, "canonical": None,
                    "comparator": self.comparator, "tolerance": self.tolerance,
                    "quoted": False, "blind": True}
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
    """The comparator's verdict for one page, window, aggregate or export."""

    __slots__ = ("classification", "severity", "repair_action", "gate_impact",
                 "actionable", "notes")

    def __init__(self, classification: str, *, notes: Sequence[str] = ()) -> None:
        self.classification = classification
        known = classification in CLASSIFICATIONS
        self.severity = SEVERITY_BY_CLASSIFICATION.get(classification, "critical")
        self.repair_action = REPAIR_BY_CLASSIFICATION.get(classification, "manual_review")
        # An unknown member from a newer comparator blocks a cutover: a verdict this build
        # cannot interpret is exactly the thing a gate must not be allowed to step over.
        self.gate_impact = GATE_IMPACT_BY_CLASSIFICATION.get(classification, "blocks_cutover")
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
    if side.get("pin_kind") not in PIN_KINDS:
        side["pin_kind"] = "none"
    return side


def classify(pair: Mapping[str, Any]) -> Comparison:
    """Terminal classification for one list, aggregate or export comparison.

    `pair` carries `grain`, `surface`, `read_mode`, `fingerprint_legacy`,
    `fingerprint_canonical`, `threshold_fingerprint_legacy`, `threshold_fingerprint_canonical`,
    `snapshot_skew_s`, `age_s`, `settle_s`, `late_s`, `drift`, `differences`, `inversions`,
    `tie_rows_reordered`, `truncated_side_only`, `export`, and a `legacy`/`canonical`
    descriptor each.

    **The order of judgement is load-bearing, and it puts the comparator's own competence
    first.** A list answer is a function of a snapshot, a question and a threshold set; if
    those do not match, everything beneath is a fact about the comparator rather than about
    the stores, and reporting it as a hundred missing rows is the most expensive kind of
    wrong.

    fault -> thresholds -> scope -> projection -> version -> question -> pin -> snapshot ->
    cursor -> tie-break -> deletion -> retention -> absence -> counts -> membership ->
    aggregate -> export -> extent -> order -> boundary drift -> values.
    """
    legacy = _side(pair, "legacy")
    canonical = _side(pair, "canonical")
    differences = list(pair.get("differences") or ())
    grain = pair.get("grain", "page")
    surface = pair.get("surface")

    settle_s = pair.get("settle_s")
    settle_s = float(settle_s) if isinstance(settle_s, (int, float)) else float(SETTLE_S)
    age_s = pair.get("age_s")
    age_s = float(age_s) if isinstance(age_s, (int, float)) else float(settle_s)

    # 1. Competence, before anything is believed about either store.
    if grain not in GRAINS:
        return Comparison("comparator_fault", notes=["unknown receipt grain %r" % (grain,)])
    if surface is not None and surface not in LIST_SURFACES:
        return Comparison("comparator_fault", notes=[
            "%r is not an inventoried list surface; an unknown read surface is a gap in the "
            "audit, not an agreement" % (surface,)])
    if not snapshot_skew_ok(pair.get("snapshot_skew_s")):
        return Comparison("comparator_fault", notes=[
            "the two reads are more than %ds apart and describe different moments"
            % SNAPSHOT_SKEW_S])
    for name, side in (("legacy", legacy), ("canonical", canonical)):
        if side["outcome"] == "unavailable":
            return Comparison("comparator_fault", notes=[
                "the %s read surface was not reachable; absence of an answer is not an "
                "answer about absence" % name])

    # 2. Thresholds: two sides invoked differently did not disagree about data (P6-R3).
    left_t = pair.get("threshold_fingerprint_legacy")
    right_t = pair.get("threshold_fingerprint_canonical")
    if left_t is not None and right_t is not None and left_t != right_t:
        return Comparison("threshold_divergence", notes=[
            "the two sides were invoked with different operator thresholds; the run is void, "
            "not a mismatch"])

    # 3. Scope: a pair about two scopes is not a pair.
    if not _scopes_agree(legacy, canonical):
        return Comparison("scope_divergence", notes=[
            "the two surfaces answered for different sites or devices"])

    # 4. Projection shape, before any value comparison.
    left_pv, right_pv = legacy.get("projection_version"), canonical.get("projection_version")
    if left_pv is not None and right_pv is not None and left_pv != right_pv:
        return Comparison("comparator_fault", notes=[
            "projection versions differ (%s vs %s); the two sides were asked for different "
            "shapes" % (left_pv, right_pv)])
    left_pd = legacy.get("projection_fields_digest")
    right_pd = canonical.get("projection_fields_digest")
    if left_pd is not None and right_pd is not None and left_pd != right_pd:
        return Comparison("projection_divergence", notes=[
            "the two surfaces expose different compared field sets; one shape difference is "
            "one finding, not one finding per row"])

    # 5. Read-contract majors: a mixed fleet is a supported state.
    left_major = legacy.get("read_contract_major")
    right_major = canonical.get("read_contract_major")
    if left_major is not None and right_major is not None and left_major != right_major:
        return Comparison("version_divergence", notes=[
            "read contract majors differ (%s vs %s); comparison is restricted to the "
            "intersection of known fields" % (left_major, right_major)])

    # 6. The question itself.
    left_fp = pair.get("fingerprint_legacy")
    right_fp = pair.get("fingerprint_canonical")
    if left_fp is not None and right_fp is not None and left_fp != right_fp:
        return Comparison("filter_divergence", notes=[
            "the same request became two questions; an inclusive versus half-open bound is "
            "the usual cause"])

    # 7. The pin. An unpinned or expired page is the comparator's problem, not the store's.
    for name, side in (("legacy", legacy), ("canonical", canonical)):
        if side["pin_kind"] == "none":
            return Comparison("comparator_fault", notes=[
                "the %s side was not pinned to a snapshot; a list answer is a function of an "
                "instant and an unpinned page is not evidence" % name])
        if not pin_ok(side):
            return Comparison("comparator_fault", notes=[
                "the %s pin is expired or incomplete; the comparator presented a pin older "
                "than its %ds lifetime, or a range with no recorded bound"
                % (name, PIN_TTL_S)])

    # 8. Snapshot stability, before any content is believed.
    drift = pair.get("drift") or {}
    drift_kind = drift.get("kind", "none")
    if drift_kind not in DRIFT_KINDS:
        return Comparison("comparator_fault", notes=["unknown drift kind %r" % (drift_kind,)])
    if drift_kind == "pin_absent":
        return Comparison("comparator_fault", notes=[
            "the comparator recorded no pin for this read"])
    if drift_kind == "snapshot_drift":
        return Comparison("snapshot_drift", notes=[
            "a pinned re-read returned a different page; the legacy list surface has no "
            "snapshot isolation, so this is a property of the surface and not a loss. "
            "Row-level verdicts for this page are withheld"])

    # 9. Cursor and paging contiguity.
    for name, side in (("legacy", legacy), ("canonical", canonical)):
        if side.get("cursor_stable") is False:
            return Comparison("cursor_divergence", notes=[
                "the %s cursor did not round-trip to the same rows" % name])
    if pair.get("cursor_duplicates") or pair.get("cursor_gaps"):
        return Comparison("cursor_divergence", notes=[
            "paging produced a duplicated or skipped row across a page boundary"])

    # 10. Tie-break stability, before order or content: ties ordered differently page
    #     differently, and then every page in the run is a different set of rows.
    reordered_ties = pair.get("tie_rows_reordered")
    if isinstance(reordered_ties, int) and reordered_ties > 0:
        return Comparison("tie_break_divergence", notes=[
            "%d row(s) with equal sort keys were ordered differently; the sort key is not a "
            "total order on at least one side" % reordered_ties])

    # 11. Deletion, before absence: a tombstone is a recorded fact.
    states = (legacy["row_state"], canonical["row_state"])
    if "tombstoned" in states and "present" in states:
        readable = "legacy" if legacy["row_state"] == "present" else "canonical"
        return Comparison("tombstone_divergence", notes=[
            "a deleted row is still listed on the %s side" % readable])

    # 12. Retention, before absence: a TTL that fired is correct behaviour on both sides.
    horizons = [legacy.get("retention_horizon_s"), canonical.get("retention_horizon_s")]
    if "retention_expired" in states:
        if retention_edge(age_s=age_s, horizons_s=horizons):
            return Comparison("match", notes=[
                "outside the intersection of both retention horizons; excluded from "
                "comparison rather than reported as loss"])
        return Comparison("retention_divergence", notes=[
            "one surface has pruned rows the other still lists, and the window is not near "
            "either declared horizon"])

    # 13. Absence: in flight, late, or lost.
    legacy_present = legacy["row_state"] == "present"
    canonical_present = canonical["row_state"] == "present"
    if legacy_present and not canonical_present:
        if not settled(age_s=age_s, settle_s=settle_s):
            return Comparison("pending", notes=[
                "inside the %.0fs settle horizon; not yet comparable" % settle_s])
        late = pair.get("late_s")
        if isinstance(late, (int, float)) and 0 < float(late) <= LATE_ARRIVAL_S:
            return Comparison("late_arrival", notes=[
                "the canonical answer landed %.0fs after the settle horizon" % float(late)])
        return Comparison("membership_divergence", notes=[
            "the canonical surface cannot list rows the authoritative surface returns"])
    if canonical_present and not legacy_present:
        return Comparison("membership_divergence", notes=[
            "the canonical surface lists rows the authoritative surface never had"])
    if not legacy_present and not canonical_present:
        return Comparison("match", notes=[
            "neither surface lists rows here, and both agree on why"])

    # 14. Counts, then membership: "how many" and "which ones" are different repairs.
    if legacy["row_count"] != canonical["row_count"]:
        if not settled(age_s=age_s, settle_s=settle_s):
            return Comparison("pending", notes=[
                "row counts differ inside the settle horizon; the leading edge of a write "
                "stream is scheduling jitter, not loss"])
        return Comparison("count_divergence", notes=[
            "legacy listed %d settled rows, canonical %d"
            % (legacy["row_count"], canonical["row_count"])])

    left_m = legacy.get("membership_digest")
    right_m = canonical.get("membership_digest")
    if left_m is not None and right_m is not None and left_m != right_m:
        return Comparison("membership_divergence", notes=[
            "the same count of rows, but not the same rows"])

    # 15. Aggregates and exports, each with its own evidence.
    if grain == "aggregate" and differences:
        return Comparison("aggregate_divergence", notes=[
            "%d aggregate cell(s) outside tolerance" % len(differences)])
    if grain == "export":
        export = pair.get("export") or {}
        outcome = export.get("classification")
        if outcome in ("export_divergence", "export_bound_exceeded"):
            return Comparison(outcome, notes=[str(export.get("reason") or "")])
        if outcome not in ("match", None):
            return Comparison("unclassified", notes=[
                "export equivalence returned %r, which this build cannot interpret"
                % (outcome,)])

    # 16. Extent, then order: a page boundary is not evidence, a different extent is.
    if pair.get("truncated_side_only"):
        return Comparison("pagination_divergence", notes=[
            "one side truncated at the page cap and the other did not; the two answers cover "
            "different extents"])

    inversions = pair.get("inversions")
    if isinstance(inversions, int) and inversions > 0:
        if age_s >= float(pair.get("order_grace_s", ORDER_GRACE_S)):
            return Comparison("order_divergence", notes=[
                "%d ordering inversion(s) outlived the order grace window" % inversions])
        return Comparison("match", notes=[
            "ordering inversions inside the order grace window; a gap that closes itself was "
            "never a defect"])

    # 17. Boundary drift: rows legitimately arriving or leaving at the edge of a page while
    #     the window was open. Reported, never blocking, and never confused with loss.
    if drift_kind in ("insert_drift", "delete_drift", "update_drift", "reorder_drift"):
        return Comparison("boundary_drift", notes=[
            "%s at a page boundary inside the settle horizon; the page shifted, the data did "
            "not" % drift_kind.replace("_", " ")])

    # 18. Values last.
    if differences:
        return Comparison("aggregate_divergence" if grain == "aggregate" else "count_divergence",
                          notes=["%d compared value(s) outside tolerance" % len(differences)])

    left_h = legacy.get("result_hash")
    right_h = canonical.get("result_hash")
    if left_h is None or right_h is None:
        return Comparison("unclassified", notes=["a surface did not publish a result digest"])
    if left_h != right_h:
        return Comparison("membership_divergence", notes=[
            "result digests differ with the same membership and no reported difference; the "
            "compared projection is incomplete"])
    if legacy["outcome"] != canonical["outcome"]:
        return Comparison("unclassified", notes=[
            "legacy=%s canonical=%s with identical content; the outcome vocabulary does not "
            "explain the pair" % (legacy["outcome"], canonical["outcome"])])
    return Comparison("match")


def _scopes_agree(legacy: Mapping[str, Any], canonical: Mapping[str, Any]) -> bool:
    for field in ("site_id", "scope_digest"):
        left, right = legacy.get(field), canonical.get(field)
        if left is not None and right is not None and left != right:
            return False
    return True


# --- sampling, coverage and cost ---------------------------------------------------------


def coverage_mode(surface: str, *, forced: bool = False) -> str:
    """`exhaustive` or `sampled`, for one list surface."""
    if surface not in LIST_SURFACES:
        raise ValueError("unknown list surface %r" % (surface,))
    if forced or surface in EXHAUSTIVE_SURFACES:
        return "exhaustive"
    return "sampled"


def sample_page(surface: str, page: Mapping[str, Any], *,
                denominator: int = DEFAULT_PAGE_DENOMINATOR,
                forced_reason: Optional[str] = None) -> Dict[str, Any]:
    """Whether this *page* is compared at all this run.

    Deterministic on the page fingerprint, so a re-run of a window compares the same pages and
    coverage is computable rather than asserted. First and last pages are always compared: a
    boundary is where an unstable order becomes visible, and a sampler that skips boundaries
    is a sampler that cannot see the defect it was deployed for.

    A misconfigured denominator compares **everything**. Failing open costs storage; failing
    closed costs the evidence.
    """
    mode = coverage_mode(surface, forced=bool(forced_reason))
    index = page.get("index")
    is_boundary = bool(page.get("first") or page.get("last"))
    if mode == "exhaustive":
        return {"selected": True, "mode": mode, "rate_denominator": 1,
                "forced_reason": forced_reason or "exhaustive surface"}
    if is_boundary:
        return {"selected": True, "mode": mode, "rate_denominator": 1,
                "forced_reason": "page boundary"}
    n = int(denominator) if isinstance(denominator, int) and denominator > 0 else 1
    token = str(page.get("fingerprint") or index)
    bucket = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16)
    return {"selected": (bucket % n) == 0, "mode": mode, "rate_denominator": n,
            "forced_reason": None}


def sample_receipt(classification: str, key: str, *,
                   denominator: int = _sr.DEFAULT_ROW_DENOMINATOR,
                   forced_reason: Optional[str] = None) -> Dict[str, Any]:
    """Whether this comparison is written down, and why. A mismatch is never sampled away."""
    return _sr.sample_receipt(classification, key, denominator=denominator,
                              forced_reason=forced_reason)


def coverage(counts: Mapping[str, int]) -> Dict[str, Any]:
    """Compared pages/rows against what the authoritative surface offered.

    `excluded_unbounded` is this contract's addition to the Phase 5 block: a range the
    comparator refused to read because it could not be bounded is a coverage loss with a name,
    not a row that quietly never appeared in a denominator.
    """
    offered = int(counts.get("legacy_rows_offered", 0))
    compared = int(counts.get("compared", 0))
    unsettled = int(counts.get("excluded_unsettled", 0))
    retention = int(counts.get("excluded_retention_edge", 0))
    sampling = int(counts.get("excluded_sampling", 0))
    unpinned = int(counts.get("excluded_unpinned", 0))
    unbounded = int(counts.get("excluded_unbounded", 0))
    excluded = unsettled + retention + sampling + unpinned + unbounded
    eligible = max(0, offered - unsettled - retention - unpinned - unbounded)
    return {
        "legacy_rows_offered": offered,
        "compared": compared,
        "excluded_unsettled": unsettled,
        "excluded_retention_edge": retention,
        "excluded_sampling": sampling,
        "excluded_unpinned": unpinned,
        "excluded_unbounded": unbounded,
        "eligible": eligible,
        "ratio": (float(compared) / float(eligible)) if eligible else 0.0,
        "accounted": offered == compared + excluded,
    }


def conservation(counts: Mapping[str, int]) -> bool:
    """`compared == pending + match + sum(mismatch classes)`.

    If it does not close, the run is not evidence and must not be reported as one.
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
            name = "unclassified"
        out[name] += 1
    out["compared"] = compared
    return out


def gate_blocked(counts: Mapping[str, int]) -> List[str]:
    """Which observed classifications block a read cutover, in vocabulary order."""
    return [name for name in CLASSIFICATIONS
            if GATE_IMPACT_BY_CLASSIFICATION[name] == "blocks_cutover"
            and int(counts.get(name, 0)) > 0]


def within_cost_ceiling(*, rows_compared: int, seconds: float,
                        ceiling_s_per_10k: float = COST_CEILING_S_PER_10K_ROWS) -> bool:
    """One vCPU-minute per 10 000 compared rows, matching both sibling audits so one cost
    story covers all three."""
    budget = (max(1, int(rows_compared)) / 10_000.0) * float(ceiling_s_per_10k)
    return float(seconds) <= budget


# --- receipt -----------------------------------------------------------------------------


def build_receipt(*, run_id: str, window_start: str, window_end: str, as_of: str,
                  emitted_at: str, query: Mapping[str, Any], pair: Mapping[str, Any],
                  comparison: Comparison, differences: Sequence[Difference] = (),
                  key: Optional[str] = None, page: Optional[Mapping[str, Any]] = None,
                  aggregate: Optional[Mapping[str, Any]] = None,
                  export: Optional[Mapping[str, Any]] = None,
                  sampling: Optional[Mapping[str, Any]] = None,
                  coverage_block: Optional[Mapping[str, Any]] = None,
                  evidence: Optional[Mapping[str, Any]] = None,
                  comparator_build: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """A durable, redacted record of one list, aggregate or export comparison.

    Raises `ValueError` for a non-terminal comparison: `pending` is a scheduling state, not a
    finding, and writing it down fills the store with rows that mean "ask again".
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
    grain = pair.get("grain", "page")
    drift = dict(pair.get("drift") or {"kind": "none"})

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
            "surface": normal["surface"],
            "site_id": normal["site_id"],
            "device_ids": list(normal["device_ids"]),
            "time_from": normal["time_from"],
            "time_to": normal["time_to"],
            "bound_kind": normal["bound_kind"],
            "filters": {path: redact_value("filter." + path, value)
                        for path, value in normal["filters"].items()},
            "sort": dict(normal["sort"]),
            "page_size": normal["page_size"],
            "projection_version": normal["projection_version"],
            "threshold_fingerprint": threshold_fingerprint(normal["thresholds"]),
            "expected_difference_list": normal["expected_difference_list"],
        },
        "page": _page_record(page),
        "aggregate": _aggregate_record(aggregate),
        "export": _export_record(export),
        "drift": {
            "kind": drift.get("kind", "none"),
            "rows_inserted": drift.get("rows_inserted"),
            "rows_deleted": drift.get("rows_deleted"),
            "rows_updated": drift.get("rows_updated"),
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


def _page_record(page: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not page:
        return None
    return {
        "index": page.get("index"),
        "size": page.get("size"),
        "count": page.get("count"),
        "fingerprint": page.get("fingerprint"),
        "first": bool(page.get("first", False)),
        "last": bool(page.get("last", False)),
        "boundary_first_row_key": page.get("boundary_first_row_key"),
        "boundary_last_row_key": page.get("boundary_last_row_key"),
    }


def _aggregate_record(aggregate: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not aggregate:
        return None
    return {
        "function": aggregate.get("function"),
        "dimensions": assert_group_keys(aggregate.get("dimensions") or ()),
        "cells_compared": aggregate.get("cells_compared"),
        "cells_suppressed": aggregate.get("cells_suppressed"),
        "tolerance": aggregate.get("tolerance"),
    }


def _export_record(export: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not export:
        return None
    return {
        "bounded": bool(export.get("bounded", False)),
        "since_id_present": bool(export.get("since_id_present", False)),
        "max_id_present": bool(export.get("max_id_present", False)),
        "rows_declared": export.get("rows_declared"),
        "rows_compared": export.get("rows_compared"),
        "slices": export.get("slices"),
        "manifest_digest_equal": export.get("equivalent"),
        "reason": export.get("reason"),
    }


def _side_record(side: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "outcome": side.get("outcome"),
        "row_state": side.get("row_state"),
        "row_count": side.get("row_count"),
        "page_count": side.get("page_count"),
        "truncated": bool(side.get("truncated", False)),
        "pin_kind": side.get("pin_kind"),
        "pin_id": side.get("pin_id"),
        "pin_age_s": side.get("pin_age_s"),
        "cursor_stable": side.get("cursor_stable"),
        "result_hash": side.get("result_hash"),
        "membership_digest": side.get("membership_digest"),
        "order_digest": side.get("order_digest"),
        "projection_fields_digest": side.get("projection_fields_digest"),
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
        "required": ["outcome", "row_state", "row_count", "pin_kind"],
        "properties": {
            "outcome": _enum(SIDE_OUTCOMES, "what the read surface did"),
            "row_state": _enum(ROW_STATES, "what this surface says about the listed rows"),
            "row_count": {"type": "integer", "minimum": 0},
            "page_count": _nullable("integer", "pages consumed for this answer"),
            "truncated": {"type": "boolean", "description": "a page or row cap was reached"},
            "pin_kind": _enum(PIN_KINDS, "how this side was pinned to a snapshot"),
            "pin_id": _nullable("string", "non-secret handle for the pin; never the cursor "
                                          "token, which is opaque and side-local"),
            "pin_age_s": _nullable("number", "age of the pin when the read was issued"),
            "cursor_stable": _nullable("boolean", "the cursor round-tripped to the same rows"),
            "result_hash": _nullable("string", "order-independent digest of the settled set"),
            "membership_digest": _nullable("string", "which rows, order ignored"),
            "order_digest": _nullable("string", "the sequence of row keys"),
            "projection_fields_digest": _nullable("string", "the compared field set"),
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
    """JSON Schema for `hear.readcompare.receipt.v1`, derived from this module.

    Generated by `tools/gen_list_read_contracts.py`; never hand-edited. Additive within the
    major: `additionalProperties` stays open so a newer comparator's extra field is preserved
    by an older reader instead of failing validation. A new major is a new file.
    """
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$id": RECEIPT_CONTRACT_URI,
        "title": RECEIPT_CONTRACT_ID,
        "description": (
            "Durable record of one legacy/canonical list, aggregate or bounded-export read "
            "comparison. Carries pins, digests, counts, cell keys above a k-anonymity floor "
            "and reason codes; never result bodies, cursor tokens, credentials, coordinates, "
            "reviewer identities, free text or clip audio."
        ),
        "type": "object",
        "additionalProperties": True,
        "required": [
            "schema", "schema_version", "receipt_id", "run_id", "emitted_at",
            "read_authority", "read_mode", "grain", "window", "query", "drift", "sides",
            "verdict", "differences", "differences_dropped", "disposition", "sampling",
            "coverage", "evidence", "comparator",
        ],
        "properties": {
            "schema": {"type": "string", "const": RECEIPT_CONTRACT_ID},
            "schema_version": {"type": "integer", "minimum": 1},
            "receipt_id": {
                "type": "string",
                "description": "uuid5 over (contract, run_id, query fingerprint, page or "
                               "aggregate key); a re-run converges instead of duplicating.",
            },
            "run_id": {"type": "string"},
            "emitted_at": {"type": "string"},
            "read_authority": {
                "type": "string", "const": READ_AUTHORITY,
                "description": "legacy remains authoritative; this comparator serves nobody",
            },
            "read_mode": _enum(SHADOW_MODES, "the lane mode this comparison ran under"),
            "grain": _enum(GRAINS, "one page, one settled window, one aggregate, one "
                                   "bounded export"),
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
                "required": ["fingerprint", "surface", "site_id", "sort", "page_size",
                             "projection_version", "threshold_fingerprint"],
                "properties": {
                    "fingerprint": {"type": "string"},
                    "surface": _enum(LIST_SURFACES, "which list/aggregate/export surface"),
                    "site_id": {"type": "string"},
                    "device_ids": {"type": "array", "items": {"type": "string"}},
                    "time_from": _nullable("string", "lower bound"),
                    "time_to": _nullable("string", "upper bound"),
                    "bound_kind": _enum(("half_open", "closed"),
                                        "inclusivity of the bounds, folded into the "
                                        "fingerprint because it is how one request becomes "
                                        "two questions"),
                    "filters": {"type": "object", "additionalProperties": True,
                                "description": "redacted filter values; a query is user "
                                               "input and an unknown filter path defaults "
                                               "to hidden"},
                    "sort": {
                        "type": "object", "additionalProperties": True,
                        "required": ["field", "direction", "tie_break"],
                        "properties": {
                            "field": {"type": "string"},
                            "direction": _enum(("asc", "desc"), "sort direction"),
                            "tie_break": {"type": "string",
                                          "description": "unique column making the sort a "
                                                         "total order"},
                        },
                    },
                    "page_size": {"type": "integer", "minimum": 1},
                    "projection_version": {"type": "integer", "minimum": 1},
                    "threshold_fingerprint": {
                        "type": "string",
                        "description": "digest of the operator threshold set both sides were "
                                       "invoked with; two different sets void the run",
                    },
                    "expected_difference_list": _nullable(
                        "string", "version of the dated list of known legacy defects whose "
                                  "differences are expected rather than findings"),
                },
            },
            "page": {
                "type": ["object", "null"], "additionalProperties": True,
                "properties": {
                    "index": _nullable("integer", "0-based page index within the pin"),
                    "size": _nullable("integer", "rows requested"),
                    "count": _nullable("integer", "pages in the pinned answer"),
                    "fingerprint": _nullable("string", "identity of this page in this pin"),
                    "first": {"type": "boolean"},
                    "last": {"type": "boolean"},
                    "boundary_first_row_key": _nullable("string", "hashed identity only"),
                    "boundary_last_row_key": _nullable("string", "hashed identity only"),
                },
            },
            "aggregate": {
                "type": ["object", "null"], "additionalProperties": True,
                "properties": {
                    "function": _enum(AGGREGATE_FUNCTIONS, "how the cells were computed"),
                    "dimensions": {
                        "type": "array",
                        "items": _enum(GROUP_KEY_DIMENSIONS, "closed dimension list"),
                    },
                    "cells_compared": _nullable("integer", "cells in the comparison"),
                    "cells_suppressed": _nullable(
                        "integer", "cells whose key was redacted for being below the "
                                   "k-anonymity floor; their verdicts are still counted"),
                    "tolerance": _nullable("number", "0 for count and distinct, always"),
                },
            },
            "export": {
                "type": ["object", "null"], "additionalProperties": True,
                "properties": {
                    "bounded": {"type": "boolean",
                                "description": "false means the comparator refused the read"},
                    "since_id_present": {"type": "boolean"},
                    "max_id_present": {"type": "boolean"},
                    "rows_declared": _nullable("integer", "rows the manifest claims"),
                    "rows_compared": _nullable("integer", "rows actually compared"),
                    "slices": _nullable("integer", "ordered digest slices compared"),
                    "manifest_digest_equal": _nullable("boolean", "paged concatenation equals "
                                                                  "the bounded export"),
                    "reason": _nullable("string", "why, when it is not equal"),
                },
            },
            "drift": {
                "type": "object", "additionalProperties": True,
                "required": ["kind"],
                "properties": {
                    "kind": _enum(DRIFT_KINDS, "what changed underneath the paged read"),
                    "rows_inserted": _nullable("integer", ""),
                    "rows_deleted": _nullable("integer", ""),
                    "rows_updated": _nullable("integer", ""),
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
                        "comparator": _enum(COMPARATORS, "how the value was compared"),
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
