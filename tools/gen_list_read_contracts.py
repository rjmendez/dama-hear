#!/usr/bin/env python3
"""Generate the staged `hear.readcompare.receipt.v1` artifacts from `hear/verify/list_read.py`.

    python3 tools/gen_list_read_contracts.py            # write
    python3 tools/gen_list_read_contracts.py --check    # fail if the tree has drifted

The output is a JSON Schema, a worked fixture per classification and a manifest of expected
outcomes, in the same shape `contracts/` uses -- but under `docs/phase6-list-read/`, because
the receipt is **not published yet**.
`docs/decisions/0009-phase6-list-read-comparison.md` records why (publishing a contract id
regenerates the Phase 0 freeze baseline, and the staged Phase 4 and Phase 5 receipts are
promoted in front of this one) and states the promotion procedure, which is a `git mv` plus a
baseline regeneration and no content change.

The drift gate is the point: a staged artifact with no gate stops describing its source just
as silently as a published one would.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hear.verify import list_read as LR  # noqa: E402

OUT_DIR = ROOT / "docs" / "phase6-list-read"
SCHEMA_PATH = OUT_DIR / (LR.RECEIPT_CONTRACT_ID + ".schema.json")
FIXTURE_DIR = OUT_DIR / "fixtures"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"

RUN_ID = "readcompare-2026-09-15T02"
WINDOW_START = "2026-09-15T01:00:00Z"
WINDOW_END = "2026-09-15T02:00:00Z"
AS_OF = "2026-09-15T02:00:00Z"
EMITTED_AT = "2026-09-15T02:00:45Z"
COMPARATOR = {"name": "hear-read-compare", "version": "0.0.0-design",
              "receipt_major": LR.RECEIPT_CONTRACT_MAJOR}

SITE = "site-alpha"
LEGACY_READER = "tools/hear_annotate server.py /api/queue + /pool products"
CANONICAL_READER = "canonical list reader (cursor paginated)"
THRESHOLDS = {"max_stale_s": 900, "unfetched_window_s": 3600, "clip_deferred_max": 25}
PROJECTION_FIELDS = ("row_uid", "captured_at", "state", "kind", "device_id", "digest")


def _query(surface: str = "detection_list", *, device: str = "gold", page_size: int = 50,
           projection_version: int = 1, settle_s: float = None,
           thresholds: Mapping[str, Any] = None, **over) -> Dict[str, Any]:
    query = {
        "surface": surface,
        "site_id": SITE,
        "device_ids": [device],
        "time_from": "2026-09-14T00:00:00Z",
        "time_to": "2026-09-15T01:45:00Z",
        "bound_kind": "half_open",
        "filters": {"kind": "gunshot", "state": "queued"},
        "sort": {"field": "captured_at", "direction": "asc", "tie_break": "row_uid"},
        "page_size": page_size,
        "as_of": AS_OF,
        "settle_s": LR.SETTLE_S if settle_s is None else settle_s,
        "projection_version": projection_version,
        "thresholds": dict(THRESHOLDS if thresholds is None else thresholds),
        "expected_difference_list": "legacy-defects-2026-09-15",
    }
    query.update(over)
    return query


def _row_keys(surface: str, device: str, count: int, *, start: int = 0) -> List[str]:
    return [LR.row_key(surface=surface, site_id=SITE, device_id=device,
                       logical_id="row-%04d" % (start + i)) for i in range(count)]


def _projection(device: str, index: int) -> Dict[str, Any]:
    """The explicit compared projection. Field paths only, never a whole stored row."""
    return {
        "row_uid": "row-%04d" % index,
        "captured_at": "2026-09-15T01:%02d:00Z" % (index % 60),
        "state": "queued",
        "kind": "gunshot",
        "device_id": device,
        "digest": "sha256:%064x" % index,
    }


def _rows(device: str, count: int, *, start: int = 0) -> List[Dict[str, Any]]:
    return [_projection(device, start + i) for i in range(count)]


def _side(*, surface: str = "detection_list", device: str = "gold",
          reader: str = LEGACY_READER, outcome: str = "answered",
          row_state: str = "present", row_count: int = 3, row_start: int = 0,
          pin_kind: str = "range_pin", snapshot_at: str = "2026-09-15T02:00:00Z",
          **over) -> Dict[str, Any]:
    keys = _row_keys(surface, device, row_count, start=row_start)
    side = {
        "outcome": outcome,
        "row_state": row_state,
        "row_count": row_count,
        "page_count": 1,
        "truncated": False,
        "pin_kind": pin_kind,
        "pin_id": "pin:%s:%s" % (pin_kind, surface),
        "pin_age_s": 30.0,
        "pin_bound": {"since_id_present": True, "max_id_present": True},
        "cursor_stable": True,
        "result_hash": LR.result_hash(_rows(device, row_count, start=row_start)),
        "membership_digest": LR.membership_digest(keys),
        "order_digest": LR.order_digest(keys),
        "projection_fields_digest": LR.projection_digest(PROJECTION_FIELDS),
        "snapshot_at": snapshot_at,
        "retention_class": "R0-derived",
        "retention_horizon_s": 90 * 86_400,
        "read_contract_major": 1,
        "projection_version": 1,
        "reader": reader,
        "reader_version": "0.1.6",
        "site_id": SITE,
        "scope_digest": LR.membership_digest([SITE, device]),
        "reasons": [],
    }
    side.update(over)
    return side


def _pair(*, surface: str = "detection_list", grain: str = "page", **over) -> Dict[str, Any]:
    pair = {
        "grain": grain,
        "surface": surface,
        "read_mode": "shadow",
        "snapshot_skew_s": 1.0,
        "age_s": 7_200.0,
        "settle_s": LR.SETTLE_S,
        "threshold_fingerprint_legacy": LR.threshold_fingerprint(THRESHOLDS),
        "threshold_fingerprint_canonical": LR.threshold_fingerprint(THRESHOLDS),
        "drift": {"kind": "none"},
        "legacy": _side(surface=surface),
        "canonical": _side(surface=surface, reader=CANONICAL_READER, pin_kind="cursor_pin"),
    }
    pair.update(over)
    return pair


def _page(index: int = 0, *, first: bool = True, last: bool = False, size: int = 50,
          count: int = 1, surface: str = "detection_list",
          fingerprint: str = None) -> Dict[str, Any]:
    keys = _row_keys(surface, "gold", 3)
    return {
        "index": index,
        "size": size,
        "count": count,
        "first": first,
        "last": last,
        "fingerprint": fingerprint or LR.page_fingerprint(
            fingerprint=LR.query_fingerprint(_query(surface)),
            pin_id="pin:range_pin:%s" % surface, page_index=index, page_size=size),
        "boundary_first_row_key": keys[0],
        "boundary_last_row_key": keys[-1],
    }


def _coverage(**over) -> Dict[str, Any]:
    counts = {
        "legacy_rows_offered": 1_000,
        "compared": 940,
        "excluded_unsettled": 30,
        "excluded_retention_edge": 10,
        "excluded_sampling": 20,
        "excluded_unpinned": 0,
        "excluded_unbounded": 0,
    }
    counts.update(over)
    return LR.coverage(counts)


def _case(name: str, description: str, pair: Mapping[str, Any], query: Mapping[str, Any], *,
          differences: Sequence = (), key: str = None, page: Mapping[str, Any] = None,
          aggregate: Mapping[str, Any] = None, export: Mapping[str, Any] = None,
          sampling: Mapping[str, Any] = None,
          coverage_block: Mapping[str, Any] = None) -> Dict[str, Any]:
    comparison = LR.classify(pair)
    receipt = None
    if comparison.classification not in LR.NON_TERMINAL:
        receipt = LR.build_receipt(
            run_id=RUN_ID, window_start=WINDOW_START, window_end=WINDOW_END, as_of=AS_OF,
            emitted_at=EMITTED_AT, query=query, pair=pair, comparison=comparison,
            differences=differences, key=key, page=page, aggregate=aggregate, export=export,
            sampling=sampling, coverage_block=coverage_block or _coverage(),
            evidence={"legacy_result_ref": "readcompare/legacy/%s.json" % name,
                      "canonical_result_ref": "readcompare/canonical/%s.json" % name},
            comparator_build=COMPARATOR)
        LR.encode_receipt(receipt)
    return {
        "name": name,
        "description": description,
        "classification": comparison.classification,
        "severity": comparison.severity,
        "repair_action": comparison.repair_action,
        "gate_impact": comparison.gate_impact,
        "receipt": receipt,
    }


def cases() -> List[Dict[str, Any]]:
    """One worked case per classification, plus the false positives this design would
    otherwise have. A vocabulary member with no worked example is a claim nobody checked."""
    out: List[Dict[str, Any]] = []
    query = _query()
    fingerprint = LR.query_fingerprint(query)

    out.append(_case(
        "match-two-pinned-pages-agree",
        "Both sides pinned, same page, same rows, same order.",
        _pair(), query, page=_page(),
        sampling={"sampled": True, "rate_denominator": 1,
                  "forced_reason": "forced exhaustive: first 24h of the lane"}))

    out.append(_case(
        "pending-inside-the-settle-horizon",
        "The canonical list has not caught up with rows minutes old; no receipt is written.",
        _pair(age_s=120.0,
              canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin",
                              row_state="absent", row_count=0)),
        query, page=_page()))

    out.append(_case(
        "late-arrival-after-the-settle-horizon",
        "The canonical list returned the page 300 s after the settle horizon.",
        _pair(late_s=300.0,
              canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin",
                              row_state="absent", row_count=0)),
        query, page=_page()))

    out.append(_case(
        "snapshot-drift-legacy-list-has-no-isolation",
        "A pinned legacy re-read returned a different page. Expected on a surface with no "
        "snapshot isolation; row-level verdicts for the page are withheld.",
        _pair(drift={"kind": "snapshot_drift", "rows_inserted": 2, "rows_deleted": 0,
                     "rows_updated": 0}),
        query, page=_page()))

    out.append(_case(
        "boundary-drift-insert-at-a-page-edge",
        "A row landed at the page boundary while the window was open. The page shifted; the "
        "data did not.",
        _pair(drift={"kind": "insert_drift", "rows_inserted": 1, "rows_deleted": 0,
                     "rows_updated": 0}),
        query, page=_page(index=2, first=False, last=False, count=5)))

    out.append(_case(
        "cursor-divergence-unstable-round-trip",
        "Re-reading with the same canonical cursor returned different rows.",
        _pair(canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin",
                              cursor_stable=False)),
        query, page=_page()))

    out.append(_case(
        "cursor-divergence-row-skipped-across-a-page-boundary",
        "Paging dropped a row between two pages; contiguity is judged before content.",
        _pair(cursor_gaps=1), query, page=_page(index=1, first=False, count=4)))

    out.append(_case(
        "tie-break-divergence-equal-sort-keys-ordered-differently",
        "Two rows share a `captured_at` and the two sides ordered them differently; the sort "
        "key is not a total order on at least one side.",
        _pair(tie_rows_reordered=2), query, page=_page()))

    out.append(_case(
        "order-divergence-past-the-order-grace-window",
        "Ordering inversions outlived the order grace window.",
        _pair(inversions=3, age_s=7_200.0), query, page=_page()))

    out.append(_case(
        "match-inversions-inside-the-order-grace-window",
        "The same inversions, inside the grace window: a gap that closes itself was never a "
        "defect. Proves the comparator does not fire on correct behaviour.",
        _pair(inversions=3, age_s=120.0, order_grace_s=LR.ORDER_GRACE_S), query,
        page=_page()))

    out.append(_case(
        "pagination-divergence-one-side-truncated",
        "One side stopped at the page cap and the other did not; the two answers cover "
        "different extents.",
        _pair(truncated_side_only=True,
              canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin", truncated=True,
                              page_count=LR.MAX_PAGES_COMPARED)),
        query, page=_page(index=19, first=False, last=True, count=20)))

    out.append(_case(
        "count-divergence-on-a-settled-window",
        "Same settled question, different row counts.",
        _pair(grain="window",
              canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin", row_count=2)),
        query))

    out.append(_case(
        "membership-divergence-same-count-different-rows",
        "Identical row counts over the same pinned window, but not the same rows.",
        _pair(grain="window",
              canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin", row_start=90)),
        query))

    agg_query = _query("health_snapshot")
    agg_legacy = {"gold|2026-09-15": 412, "kasami|2026-09-15": 388, "rankine|2026-09-15": 3}
    agg_canonical = {"gold|2026-09-15": 412, "kasami|2026-09-15": 371, "rankine|2026-09-15": 3}
    agg_differences = LR.compare_aggregate(agg_legacy, agg_canonical, function="count")
    out.append(_case(
        "aggregate-divergence-one-cell-differs",
        "A per-device daily count differs. Counts are compared with zero tolerance, always.",
        _pair(surface="health_snapshot", grain="aggregate",
              legacy=_side(surface="health_snapshot"),
              canonical=_side(surface="health_snapshot", reader=CANONICAL_READER,
                              pin_kind="snapshot_id"),
              differences=[d.to_dict() for d in agg_differences]),
        agg_query, differences=agg_differences,
        aggregate={"function": "count", "dimensions": ["device_id", "day"],
                   "cells_compared": 3, "cells_suppressed": 1, "tolerance": 0.0},
        key="aggregate:count:device_id,day"))

    out.append(_case(
        "aggregate-small-cell-key-suppressed",
        "Two aggregates agree, and the below-k cell is still counted with its key redacted; "
        "the verdict is never suppressed, only the identity.",
        _pair(surface="health_snapshot", grain="aggregate",
              legacy=_side(surface="health_snapshot"),
              canonical=_side(surface="health_snapshot", reader=CANONICAL_READER,
                              pin_kind="snapshot_id")),
        agg_query,
        aggregate={"function": "count", "dimensions": ["device_id", "day"],
                   "cells_compared": 3, "cells_suppressed": 1, "tolerance": 0.0},
        key="aggregate:count:device_id,day"))

    small_differences = LR.compare_aggregate(
        {"rankine|2026-09-15": 3}, {"rankine|2026-09-15": 1}, function="count")
    out.append(_case(
        "aggregate-divergence-small-cell-values-redacted",
        "A below-k cell diverges: the finding is reported in full, and both numbers are "
        "redacted because a cell of three is a person or a node, not a population.",
        _pair(surface="health_snapshot", grain="aggregate",
              legacy=_side(surface="health_snapshot"),
              canonical=_side(surface="health_snapshot", reader=CANONICAL_READER,
                              pin_kind="snapshot_id"),
              differences=[d.to_dict() for d in small_differences]),
        agg_query, differences=small_differences,
        aggregate={"function": "count", "dimensions": ["device_id", "day"],
                   "cells_compared": 1, "cells_suppressed": 1, "tolerance": 0.0},
        key="aggregate:count:device_id,day:small"))

    export_query = _query("annotation_export", page_size=LR.EXPORT_SLICE_ROWS)
    paged = ["x-slice-%d" % i for i in range(4)]
    export_ok = LR.export_equivalence(
        paged_slice_digests=paged, export_slice_digests=paged, rows_paged=3_500,
        rows_exported=3_500, bounded=True, rows_declared=3_500)
    out.append(_case(
        "export-equivalence-bounded-range-matches",
        "Page exhaustion over a recorded (since_id, max_id) range is digest-identical to the "
        "bounded export of the same range.",
        _pair(surface="annotation_export", grain="export",
              legacy=_side(surface="annotation_export"),
              canonical=_side(surface="annotation_export", reader=CANONICAL_READER,
                              pin_kind="cursor_pin"),
              export=export_ok),
        export_query, export=dict(export_ok, bounded=True, since_id_present=True,
                                  max_id_present=True, rows_declared=3_500),
        key="export:since-max"))

    export_bad = LR.export_equivalence(
        paged_slice_digests=paged, export_slice_digests=paged[:3] + ["x-slice-changed"],
        rows_paged=3_500, rows_exported=3_500, bounded=True, rows_declared=3_500)
    out.append(_case(
        "export-divergence-same-rows-different-content",
        "Same bounded range, same row count, different ordered content digest.",
        _pair(surface="annotation_export", grain="export",
              legacy=_side(surface="annotation_export"),
              canonical=_side(surface="annotation_export", reader=CANONICAL_READER,
                              pin_kind="cursor_pin"),
              export=export_bad),
        export_query, export=dict(export_bad, bounded=True, since_id_present=True,
                                  max_id_present=True, rows_declared=3_500),
        key="export:since-max"))

    export_unbounded = LR.export_equivalence(
        paged_slice_digests=(), export_slice_digests=(), rows_paged=0, rows_exported=0,
        bounded=False)
    out.append(_case(
        "export-bound-exceeded-comparator-refuses-the-unbounded-dump",
        "The legacy export offers no range bound, so the comparison is refused and the "
        "coverage loss recorded. The comparator never issues the unbounded read.",
        _pair(surface="annotation_export", grain="export",
              legacy=_side(surface="annotation_export"),
              canonical=_side(surface="annotation_export", reader=CANONICAL_READER,
                              pin_kind="cursor_pin"),
              export=export_unbounded),
        export_query, export=dict(export_unbounded, bounded=False, since_id_present=False,
                                  max_id_present=False),
        coverage_block=_coverage(compared=0, excluded_unbounded=1_000, excluded_unsettled=0,
                                 excluded_retention_edge=0, excluded_sampling=0),
        key="export:unbounded"))

    out.append(_case(
        "filter-divergence-inclusive-versus-half-open-bound",
        "The same request became two questions; every row verdict downstream would be an "
        "artefact.",
        _pair(fingerprint_legacy=fingerprint,
              fingerprint_canonical=LR.query_fingerprint(_query(bound_kind="closed"))),
        query, page=_page()))

    out.append(_case(
        "projection-divergence-different-compared-field-sets",
        "The two surfaces expose different field sets; one shape difference is one finding, "
        "not one per row.",
        _pair(canonical=_side(
            reader=CANONICAL_READER, pin_kind="cursor_pin",
            projection_fields_digest=LR.projection_digest(PROJECTION_FIELDS + ("notes",)))),
        query, page=_page()))

    out.append(_case(
        "retention-divergence-pruned-away-from-any-horizon",
        "One surface has pruned rows the other still lists, nowhere near either declared "
        "horizon.",
        _pair(age_s=86_400.0,
              canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin",
                              row_state="retention_expired", row_count=0)),
        query, page=_page()))

    out.append(_case(
        "match-rows-past-the-legacy-retention-horizon",
        "Rows aged past the legacy 7-day raw horizon are excluded as a retention edge, not "
        "reported as loss. Proves the comparator does not fire on correct behaviour.",
        _pair(age_s=7 * 86_400.0,
              legacy=_side(retention_class="R2-raw", retention_horizon_s=7 * 86_400),
              canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin",
                              retention_class="R2-raw", retention_horizon_s=7 * 86_400,
                              row_state="retention_expired", row_count=0)),
        query, page=_page()))

    out.append(_case(
        "tombstone-divergence-deleted-row-still-listed",
        "A deletion did not propagate: the row is tombstoned on one side and still listed on "
        "the other.",
        _pair(legacy=_side(row_state="present"),
              canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin",
                              row_state="tombstoned")),
        query, page=_page()))

    out.append(_case(
        "threshold-divergence-two-sides-invoked-differently",
        "The two sides used different operator thresholds (P6-R3). The run is void, not a "
        "mismatch.",
        _pair(threshold_fingerprint_canonical=LR.threshold_fingerprint(
            dict(THRESHOLDS, max_stale_s=1_800))),
        query, page=_page()))

    out.append(_case(
        "scope-divergence-two-sides-answered-for-different-scopes",
        "The two answers cover different sites or device sets; a pair about two scopes is "
        "not a pair.",
        _pair(canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin",
                              scope_digest=LR.membership_digest([SITE, "kasami"]))),
        query, page=_page()))

    out.append(_case(
        "version-divergence-mixed-read-majors",
        "A staged rollout is a mixed-version deployment; comparison continues over the "
        "intersection.",
        _pair(canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin",
                              read_contract_major=2)),
        query, page=_page()))

    out.append(_case(
        "comparator-fault-unpinned-legacy-list",
        "The legacy list could not be pinned, so there is no snapshot to compare against.",
        _pair(legacy=_side(pin_kind="none", pin_id=None)), query, page=_page()))

    out.append(_case(
        "comparator-fault-expired-pin",
        "The comparator presented a pin older than its 24 h lifetime; the auditor's own "
        "scheduling defect, never a store's.",
        _pair(canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin",
                              pin_age_s=LR.PIN_TTL_S + 60)),
        query, page=_page()))

    out.append(_case(
        "comparator-fault-snapshot-skew",
        "The two reads are 45 s apart and describe different moments.",
        _pair(snapshot_skew_s=45.0), query, page=_page()))

    out.append(_case(
        "comparator-fault-legacy-surface-unavailable",
        "The legacy list surface did not answer; absence of an answer is not an answer about "
        "absence.",
        _pair(legacy=_side(outcome="unavailable", row_state="absent", row_count=0)),
        query, page=_page()))

    out.append(_case(
        "unclassified-missing-result-digest",
        "One surface published no result digest, so no verdict is derivable.",
        _pair(canonical=_side(reader=CANONICAL_READER, pin_kind="cursor_pin",
                              result_hash=None)),
        query, page=_page()))

    return out


def manifest(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return {
        "contract": LR.RECEIPT_CONTRACT_ID,
        "contract_major": LR.RECEIPT_CONTRACT_MAJOR,
        "media_type": LR.RECEIPT_MEDIA_TYPE,
        "status": "staged",
        "staged_reason": (
            "the artifacts live under docs/ rather than contracts/ until the Phase 4 and "
            "Phase 5 receipts ahead of them are promoted, because publishing a contract id "
            "regenerates the Phase 0 freeze baseline and two lanes regenerating one hashed "
            "file is a conflict with no meaning. Promotion is a git mv plus a baseline "
            "regeneration and no content change; see "
            "docs/decisions/0009-phase6-list-read-comparison.md."
        ),
        "generator": "tools/gen_list_read_contracts.py",
        "source_of_truth": "hear/verify/list_read.py",
        "read_authority": LR.READ_AUTHORITY,
        "surfaces": list(LR.LIST_SURFACES),
        "exhaustive_surfaces": list(LR.EXHAUSTIVE_SURFACES),
        "grains": list(LR.GRAINS),
        "pin_kinds": list(LR.PIN_KINDS),
        "drift_kinds": list(LR.DRIFT_KINDS),
        "classifications": list(LR.CLASSIFICATIONS),
        "blocking_classifications": list(LR.BLOCKING_CLASSIFICATIONS),
        "aggregate_functions": list(LR.AGGREGATE_FUNCTIONS),
        "group_key_dimensions": list(LR.GROUP_KEY_DIMENSIONS),
        "bounds": {
            "settle_s": LR.SETTLE_S,
            "late_arrival_s": LR.LATE_ARRIVAL_S,
            "backfill_settle_s": LR.BACKFILL_SETTLE_S,
            "snapshot_skew_s": LR.SNAPSHOT_SKEW_S,
            "retention_edge_s": LR.RETENTION_EDGE_S,
            "order_grace_s": LR.ORDER_GRACE_S,
            "pin_ttl_s": LR.PIN_TTL_S,
            "max_pages_compared": LR.MAX_PAGES_COMPARED,
            "max_rows_compared": LR.MAX_ROWS_COMPARED,
            "max_export_rows_compared": LR.MAX_EXPORT_ROWS_COMPARED,
            "export_slice_rows": LR.EXPORT_SLICE_ROWS,
            "max_aggregate_cells": LR.MAX_AGGREGATE_CELLS,
            "min_cell_count": LR.MIN_CELL_COUNT,
            "max_differences": LR.MAX_DIFFERENCES,
            "max_receipt_bytes": LR.MAX_RECEIPT_BYTES,
            "default_page_denominator": LR.DEFAULT_PAGE_DENOMINATOR,
            "default_query_denominator": LR.DEFAULT_QUERY_DENOMINATOR,
            "cost_ceiling_s_per_10k_rows": LR.COST_CEILING_S_PER_10K_ROWS,
        },
        "cases": [
            {
                "file": record["name"] + ".json",
                "description": record["description"],
                "expected_classification": record["classification"],
                "expected_severity": record["severity"],
                "expected_repair_action": record["repair_action"],
                "expected_gate_impact": record["gate_impact"],
                "emits_receipt": record["receipt"] is not None,
            }
            for record in records
        ],
    }


def artifacts() -> Dict[str, str]:
    records = cases()
    out: Dict[str, str] = {
        _rel(SCHEMA_PATH): _dump(LR.receipt_schema_document()),
        _rel(MANIFEST_PATH): _dump(manifest(records)),
    }
    for record in records:
        body = {
            "case": record["name"],
            "description": record["description"],
            "expected_classification": record["classification"],
            "expected_severity": record["severity"],
            "expected_repair_action": record["repair_action"],
            "expected_gate_impact": record["gate_impact"],
            "receipt": record["receipt"],
        }
        out[_rel(FIXTURE_DIR / (record["name"] + ".json"))] = _dump(body)
    return out


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: Sequence[str] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if the tree differs from the generator")
    args = parser.parse_args(list(argv) if argv is not None else None)

    generated = artifacts()
    if args.check:
        problems: List[str] = []
        for rel, text in sorted(generated.items()):
            path = ROOT / rel
            if not path.is_file():
                problems.append("%s: missing" % rel)
            elif path.read_text() != text:
                problems.append("%s: differs from its generator" % rel)
        existing = {
            _rel(p) for p in sorted(OUT_DIR.rglob("*.json"))
        } if OUT_DIR.is_dir() else set()
        for rel in sorted(existing - set(generated)):
            problems.append("%s: not produced by the generator" % rel)
        for problem in problems:
            print(problem, file=sys.stderr)
        if problems:
            print("rerun: python3 tools/gen_list_read_contracts.py", file=sys.stderr)
            return 1
        print("list-read artifacts current (%d files)" % len(generated))
        return 0

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for rel, text in sorted(generated.items()):
        (ROOT / rel).write_text(text)
    print("wrote %d files under %s" % (len(generated), _rel(OUT_DIR)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
