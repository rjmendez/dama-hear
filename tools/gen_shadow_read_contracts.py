#!/usr/bin/env python3
"""Generate the staged `hear.shadowread.receipt.v1` artifacts from `hear/verify/shadow_read.py`.

    python3 tools/gen_shadow_read_contracts.py            # write
    python3 tools/gen_shadow_read_contracts.py --check    # fail if the tree has drifted

The output is a JSON Schema, a worked fixture per classification and a manifest of expected
outcomes, in the same shape `contracts/` uses -- but under `docs/phase5-shadow-read/`,
because the receipt is **not published yet**.
`docs/decisions/0006-phase5-shadow-read-comparator.md` records why (publishing a contract id
regenerates the Phase 0 freeze baseline, and the staged Phase 4 receipt in front of this one
must be promoted first) and states the promotion procedure, which is a `git mv` plus a
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

from hear.verify import shadow_read as SR  # noqa: E402

OUT_DIR = ROOT / "docs" / "phase5-shadow-read"
SCHEMA_PATH = OUT_DIR / (SR.RECEIPT_CONTRACT_ID + ".schema.json")
FIXTURE_DIR = OUT_DIR / "fixtures"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"

RUN_ID = "shadow-2026-09-15T02"
WINDOW_START = "2026-09-15T01:00:00Z"
WINDOW_END = "2026-09-15T02:00:00Z"
AS_OF = "2026-09-15T02:00:00Z"
EMITTED_AT = "2026-09-15T02:00:45Z"
COMPARATOR = {"name": "hear-shadow-read", "version": "0.0.0-design",
              "receipt_major": SR.RECEIPT_CONTRACT_MAJOR}

SITE = "site-alpha"
LEGACY_READER = "hear/pool.py + tools/hear_heartbeat_receiver.py views"
CANONICAL_READER = "hear.objectstore + durable postgres reader"


def _query(evidence_class: str = "detection", *, device: str = "gold",
           page_size: int = 50, projection_version: int = 1,
           settle_s: float = None, **over) -> Dict[str, Any]:
    query = {
        "evidence_class": evidence_class,
        "site_id": SITE,
        "device_ids": [device],
        "time_from": "2026-09-14T00:00:00Z",
        "time_to": "2026-09-15T01:45:00Z",
        "filters": {"kind": "gunshot", "bound": "half_open"},
        "sort": {"field": "captured_at", "direction": "asc"},
        "page_size": page_size,
        "as_of": AS_OF,
        "settle_s": SR.SETTLE_S if settle_s is None else settle_s,
        "projection_version": projection_version,
    }
    query.update(over)
    return query


def _projection(device: str, *, rows: int = 4210, seq: int = 17) -> Dict[str, Any]:
    """The explicit compared projection. Field paths only, never a whole stored row."""
    return {
        "evidence_class": "detection",
        "device_id": device,
        "class": "esp32s3-i2s-gps",
        "fw_version": "hear-node-0.1.6",
        "clock.tier": "gps_pps",
        "counters.dets_rows_written": rows,
        "producer.batch_sequence": seq,
    }


def _rows(device: str, count: int = 3) -> List[Dict[str, Any]]:
    return [_projection(device, rows=4210 + i, seq=17 + i) for i in range(count)]


def _side(device: str, *, reader: str = LEGACY_READER, outcome: str = "answered",
          row_state: str = "present", row_count: int = 3, rows: Sequence = None,
          snapshot_at: str = "2026-09-15T02:00:00Z", **over) -> Dict[str, Any]:
    side = {
        "outcome": outcome,
        "row_state": row_state,
        "row_count": row_count,
        "page_count": 1,
        "truncated": False,
        "cursor_stable": True,
        "result_hash": SR.result_hash(list(rows) if rows is not None
                                      else _rows(device, row_count)),
        "state_hash": SR.state_hash(_projection(device)),
        "snapshot_at": snapshot_at,
        "retention_class": "R0-derived",
        "retention_horizon_s": 90 * 86_400,
        "read_contract_major": 1,
        "projection_version": 1,
        "reader": reader,
        "reader_version": "0.1.6",
        "device_id": device,
        "site_id": SITE,
        "principal_id": "node:" + device,
        "reasons": [],
    }
    side.update(over)
    return side


def _case(name: str, description: str, pair: Mapping[str, Any],
          query: Mapping[str, Any], *, differences: Sequence = (),
          key: str = None, sampling: Mapping[str, Any] = None,
          coverage_block: Mapping[str, Any] = None) -> Dict[str, Any]:
    comparison = SR.classify(pair)
    receipt = None
    if comparison.classification not in SR.NON_TERMINAL:
        receipt = SR.build_receipt(
            run_id=RUN_ID, window_start=WINDOW_START, window_end=WINDOW_END, as_of=AS_OF,
            emitted_at=EMITTED_AT, query=query, pair=pair, comparison=comparison,
            differences=differences, key=key, sampling=sampling,
            coverage_block=coverage_block,
            evidence={"legacy_result_ref": "hear/v1/blob/sha256/aa/legacy-answer",
                      "canonical_result_ref": "hear/v1/blob/sha256/bb/canonical-answer"},
            comparator_build=COMPARATOR)
        SR.encode_receipt(receipt)
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
    out: List[Dict[str, Any]] = []

    # 1. The control. Both surfaces answer the same settled question identically.
    device = "gold"
    out.append(_case(
        "match-both-surfaces-agree",
        "Both read surfaces answer the same settled query with the same rows, the same "
        "count and the same order-independent digest. Forced into the audit sample so the "
        "evidence bundle contains worked positives, not only failures.",
        {"grain": "result_set", "read_mode": "shadow", "snapshot_skew_s": 1.0,
         "legacy": _side(device), "canonical": _side(device, reader=CANONICAL_READER),
         "age_s": 3_600.0, "settle_s": SR.SETTLE_S},
        _query(device=device),
        sampling=SR.sample_receipt("match", "audit", forced_reason="first day of a lane"),
        coverage_block=SR.coverage({"legacy_rows_offered": 3, "compared": 3}),
    ))

    # 2. A row still inside the settle horizon. Not comparable, and therefore not a finding.
    device = "rankine"
    out.append(_case(
        "pending-inside-settle-horizon",
        "The legacy surface already returns a row whose write is minutes old. Two stores do "
        "not commit at the same instant, so anything newer than the settle horizon is "
        "excluded from comparison rather than reported. No receipt is emitted.",
        {"grain": "row", "read_mode": "shadow", "snapshot_skew_s": 0.5,
         "legacy": _side(device, row_count=1),
         "canonical": _side(device, reader=CANONICAL_READER, outcome="answered",
                            row_state="absent", row_count=0),
         "age_s": 120.0, "settle_s": SR.SETTLE_S},
        _query("heartbeat", device=device),
        key=SR.row_key(evidence_class="heartbeat", site_id=SITE, device_id=device,
                       logical_id="hb-000120"),
    ))

    # 3. Late, not lost. The distinction the whole absence ladder exists to make.
    device = "kasami"
    out.append(_case(
        "late-arrival-after-settle-horizon",
        "The canonical row landed after the settle horizon but inside the late window. A "
        "freshness fact, warn severity, no repair and no cutover block -- but a rising late "
        "rate is the signal that precedes a real loss, which is why it is not 'match'.",
        {"grain": "row", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_count=1),
         "canonical": _side(device, reader=CANONICAL_READER, row_state="absent",
                            row_count=0),
         "age_s": 1_200.0, "settle_s": SR.SETTLE_S, "late_s": 300.0},
        _query("detection", device=device),
        key=SR.row_key(evidence_class="detection", site_id=SITE, device_id=device,
                       logical_id="det-000771"),
    ))

    # 4. The loss this exercise exists to find.
    device = "ageev"
    out.append(_case(
        "missing-canonical-row-after-late-window",
        "Legacy answers and canonical does not, past both the settle horizon and the late "
        "window, with no tombstone and no retention explanation. Critical, replayable from "
        "the inbox, and it blocks a read cutover on its own.",
        {"grain": "row", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_count=1),
         "canonical": _side(device, reader=CANONICAL_READER, row_state="absent",
                            row_count=0),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("detection", device=device),
        key=SR.row_key(evidence_class="detection", site_id=SITE, device_id=device,
                       logical_id="det-000772"),
    ))

    # 5. Canonical-only. Not loss, but something answered from a row legacy never had.
    device = "nyquist"
    out.append(_case(
        "missing-legacy-row-canonical-only",
        "The canonical surface returns a row the authoritative surface never had. Legacy "
        "still holds everything it ever held, so this is not data loss -- it is a claimant "
        "with an extra answer, which is a review, not a replay.",
        {"grain": "row", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_state="absent", row_count=0),
         "canonical": _side(device, reader=CANONICAL_READER, row_count=1),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("detection", device=device),
        key=SR.row_key(evidence_class="detection", site_id=SITE, device_id=device,
                       logical_id="det-000773"),
    ))

    # 6. Two canonical rows: judged before any value question, because with two rows the
    #    question "which value differs" has no answer.
    device = "mach"
    out.append(_case(
        "duplicate-canonical-rows",
        "Identity-based dedup failed on the canonical side and one logical row is returned "
        "twice. Judged before any value comparison, because a duplicate makes every "
        "field-level verdict beneath it ambiguous.",
        {"grain": "row", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_count=1),
         "canonical": _side(device, reader=CANONICAL_READER, row_count=2),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("detection", device=device),
        key=SR.row_key(evidence_class="detection", site_id=SITE, device_id=device,
                       logical_id="det-000774"),
    ))

    # 7. The mirror image, and deliberately only warn: MQTT QoS 1 redelivery is pre-existing
    #    legacy behaviour, and blaming the new path for the old one's shape trains an
    #    operator to ignore the counter.
    device = "rankine"
    out.append(_case(
        "duplicate-legacy-rows",
        "The legacy surface returns one logical row twice -- pre-existing redelivery "
        "behaviour. Warn, not critical: the canonical side collapsing it is the designed "
        "outcome, and paging on the old path's shape is how a new counter gets ignored.",
        {"grain": "row", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_count=2),
         "canonical": _side(device, reader=CANONICAL_READER, row_count=1),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("heartbeat", device=device),
        key=SR.row_key(evidence_class="heartbeat", site_id=SITE, device_id=device,
                       logical_id="hb-000121"),
    ))

    # 8. The aggregate case: counts are the number an operator actually reads.
    device = "gold"
    out.append(_case(
        "count-divergence-on-a-settled-window",
        "Both surfaces answered the same settled question and returned different row "
        "counts. Compared exhaustively per device and day rather than sampled, because "
        "sampling an aggregate deletes the only number anybody checks.",
        {"grain": "result_set", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_count=3),
         "canonical": _side(device, reader=CANONICAL_READER, row_count=2,
                            rows=_rows(device, 2)),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("ledger_stat", device=device),
        coverage_block=SR.coverage({"legacy_rows_offered": 3, "compared": 3}),
    ))

    # 9. A compared field outside tolerance.
    device = "kasami"
    out.append(_case(
        "value-divergence-on-a-counter",
        "One counter in the compared projection differs. The projection is explicit rather "
        "than a whole stored row, so an additive field upstream cannot manufacture this "
        "finding -- which is the failure that makes people switch a comparator off.",
        {"grain": "row", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_count=1),
         "canonical": _side(device, reader=CANONICAL_READER, row_count=1),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S,
         "differences": [1]},
        _query("detection", device=device),
        differences=[SR.Difference("counters.dets_rows_written", 4210, 4209,
                                   comparator="count")],
        key=SR.row_key(evidence_class="detection", site_id=SITE, device_id=device,
                       logical_id="det-000775"),
    ))

    # 10. Restricted fields: compared, never quoted, not even as a hash.
    device = "ageev"
    out.append(_case(
        "blind-comparison-hides-location-and-audio",
        "A localisation result and a clip body differ. Both are compared by the blind "
        "comparator: equality is decided in memory and only the verdict is durable, so the "
        "receipt records that the two surfaces disagreed without recording a coordinate, a "
        "credential or a sample. A stable hash of a coordinate is still an identifier for "
        "that coordinate, which is why blind differences carry null on both sides.",
        {"grain": "row", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_count=1),
         "canonical": _side(device, reader=CANONICAL_READER, row_count=1),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S, "differences": [1, 2, 3]},
        _query("localization", device=device),
        differences=[
            SR.Difference("solution.latitude", 1.0, 2.0, comparator="blind"),
            SR.Difference("clip.audio_samples", "x", "y", comparator="blind"),
            SR.Difference("bearer_token", "a", "b", comparator="blind"),
        ],
        key=SR.row_key(evidence_class="localization", site_id=SITE, device_id=device,
                       logical_id="loc-000031"),
    ))

    # 11. Ordering is its own property, and only past the order grace window.
    device = "nyquist"
    out.append(_case(
        "order-divergence-outlived-the-grace-window",
        "The two surfaces return the same rows in a different sequence on the declared sort "
        "key, and the inversions have outlived the order grace window. Warn and no repair: "
        "an ordering difference is not a lost row, and the content digest is deliberately "
        "order-independent so the two findings stay separable.",
        {"grain": "result_set", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device), "canonical": _side(device, reader=CANONICAL_READER),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S, "inversions": 4},
        _query("detection", device=device),
        coverage_block=SR.coverage({"legacy_rows_offered": 3, "compared": 3}),
    ))

    # 12. A false positive this design refuses to produce.
    device = "nyquist"
    out.append(_case(
        "order-inversions-inside-the-grace-window",
        "The same inversions, inside the order grace window. Classified match: batches "
        "legitimately arrive out of order and a gap that closes itself was never a defect. "
        "This case exists to prove the comparator does not fire on correct behaviour.",
        {"grain": "result_set", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device), "canonical": _side(device, reader=CANONICAL_READER),
         "age_s": 60.0, "settle_s": 30.0, "inversions": 4},
        _query("detection", device=device, settle_s=30.0),
        sampling=SR.sample_receipt("match", "audit", forced_reason="drill window"),
        coverage_block=SR.coverage({"legacy_rows_offered": 3, "compared": 3}),
    ))

    # 13. One side hit the page cap; coverage is not comparable, and that is recorded.
    device = "mach"
    out.append(_case(
        "pagination-divergence-one-side-truncated",
        "One surface stopped at the page cap and the other did not, so the two answers "
        "cover different amounts of the window. Warn, with a reindex request for the "
        "canonical index owner. A page boundary is never itself evidence; an unequal "
        "*extent* is.",
        {"grain": "result_set", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, page_count=SR.MAX_PAGES, truncated=True),
         "canonical": _side(device, reader=CANONICAL_READER, page_count=2),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S, "truncated_side_only": True},
        _query("detection", device=device, page_size=500),
        coverage_block=SR.coverage({"legacy_rows_offered": 10_000, "compared": 3,
                                    "excluded_sampling": 9_997}),
    ))

    # 14. A cursor that does not round-trip makes every paged answer unreproducible.
    device = "gold"
    out.append(_case(
        "cursor-divergence-unstable-round-trip",
        "Re-reading with the same opaque cursor returned a different row set. Critical and "
        "cutover-blocking, judged before any content comparison: if paging is not stable "
        "then every row-level verdict in this run is unfalsifiable.",
        {"grain": "result_set", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device),
         "canonical": _side(device, reader=CANONICAL_READER, cursor_stable=False),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("detection", device=device),
    ))

    # 15. Same request, two questions.
    device = "kasami"
    out.append(_case(
        "filter-divergence-same-request-two-questions",
        "The two surfaces normalised the same request into different questions -- an "
        "inclusive versus half-open upper bound is the usual cause. Critical: every row "
        "verdict downstream of a different question is an artefact, not a finding.",
        {"grain": "result_set", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device), "canonical": _side(device, reader=CANONICAL_READER),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S,
         "fingerprint_legacy": SR.query_fingerprint(_query("detection", device=device)),
         "fingerprint_canonical": SR.query_fingerprint(
             _query("detection", device=device, filters={"kind": "gunshot",
                                                         "bound": "closed"}))},
        _query("detection", device=device),
    ))

    # 16. Retention is not loss -- when the row is near a declared horizon.
    device = "rankine"
    out.append(_case(
        "retention-edge-excluded-not-reported",
        "A raw row has aged past the legacy surface's 7-day horizon while the canonical "
        "copy still holds it. Excluded from comparison rather than reported: a TTL that "
        "fires between two reads is correct behaviour on both sides, and reporting it is "
        "the most reliable way to turn two healthy stores into a permanently red board.",
        {"grain": "row", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_state="retention_expired", row_count=0,
                         retention_class="R2-raw", retention_horizon_s=7 * 86_400),
         "canonical": _side(device, reader=CANONICAL_READER, row_count=1,
                            retention_class="R2-raw", retention_horizon_s=7 * 86_400),
         "age_s": 7 * 86_400 + 600.0, "settle_s": SR.SETTLE_S},
        _query("scene", device=device),
        key=SR.row_key(evidence_class="scene", site_id=SITE, device_id=device,
                       logical_id="scene-000044"),
    ))

    # 17. The same shape, far from any horizon: now it is a real divergence.
    device = "rankine"
    out.append(_case(
        "retention-divergence-far-from-any-horizon",
        "One surface has pruned a row that is nowhere near either declared horizon. Warn "
        "and manual review: the repair boundary is replay-only and nothing here may rewrite "
        "an authoritative retention clock to make the two agree.",
        {"grain": "row", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_state="retention_expired", row_count=0,
                         retention_class="R0-derived", retention_horizon_s=90 * 86_400),
         "canonical": _side(device, reader=CANONICAL_READER, row_count=1,
                            retention_class="R0-derived", retention_horizon_s=90 * 86_400),
         "age_s": 2 * 86_400, "settle_s": SR.SETTLE_S},
        _query("scene", device=device),
        key=SR.row_key(evidence_class="scene", site_id=SITE, device_id=device,
                       logical_id="scene-000045"),
    ))

    # 18. A deletion that did not propagate is a governance defect, not a data-quality one.
    device = "ageev"
    out.append(_case(
        "tombstone-divergence-deleted-row-still-readable",
        "A row is tombstoned on one surface and still readable on the other. Judged before "
        "absence, because a tombstone is a recorded fact rather than a missing row, and "
        "critical because an unpropagated deletion is a governance failure that a read "
        "cutover would make permanent.",
        {"grain": "row", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_state="tombstoned", row_count=0),
         "canonical": _side(device, reader=CANONICAL_READER, row_count=1),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("clip_manifest", device=device),
        key=SR.row_key(evidence_class="clip_manifest", site_id=SITE, device_id=device,
                       logical_id="clip-000908"),
    ))

    # 19. Mixed-version fleets are a supported state, not an error.
    device = "mach"
    out.append(_case(
        "version-divergence-mixed-reader-majors",
        "The two surfaces answer under different read-contract majors. Warn, not critical: "
        "a staged rollout *is* a mixed-version deployment, and comparison continues over "
        "the intersection of fields both majors define rather than being abandoned.",
        {"grain": "result_set", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, read_contract_major=1),
         "canonical": _side(device, reader=CANONICAL_READER, read_contract_major=2),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("detection", device=device),
    ))

    # 20. Who sent it beats what it said.
    device = "gold"
    out.append(_case(
        "identity-divergence-forged-device",
        "The two surfaces disagree about the producer behind a row. An authentication "
        "defect, not a data-quality one: judged before every content question, never "
        "sampled away, and cutover-blocking.",
        {"grain": "row", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_count=1),
         "canonical": _side(device, reader=CANONICAL_READER, row_count=1,
                            device_id="kasami", principal_id="node:kasami"),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("detection", device=device),
        key=SR.row_key(evidence_class="detection", site_id=SITE, device_id=device,
                       logical_id="det-000776"),
    ))

    # 21. The comparator's own failure, said out loud.
    device = "kasami"
    out.append(_case(
        "comparator-fault-snapshot-skew",
        "The two reads are further apart than the permitted snapshot skew, so they describe "
        "different moments. The honest verdict is the comparator's own failure rather than "
        "a row-by-row report of the gap -- and it is critical, because a comparator that "
        "cannot compare has stopped being evidence.",
        {"grain": "result_set", "read_mode": "compare", "snapshot_skew_s": 45.0,
         "legacy": _side(device),
         "canonical": _side(device, reader=CANONICAL_READER,
                            snapshot_at="2026-09-15T02:00:45Z"),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("detection", device=device),
    ))

    # 22. An expired cursor is a comparator defect, never a store divergence.
    device = "nyquist"
    out.append(_case(
        "comparator-fault-expired-cursor",
        "The comparator presented a cursor older than the 24 h lifetime the API contract "
        "gives it. That is the comparator's bug, and calling it a divergence would blame a "
        "store for the auditor's scheduling.",
        {"grain": "result_set", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device), "canonical": _side(device, reader=CANONICAL_READER),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S,
         "cursor_age_s": SR.CURSOR_TTL_S + 60.0},
        _query("detection", device=device),
    ))

    # 23. A surface that did not answer is not a surface that answered "nothing".
    device = "ageev"
    out.append(_case(
        "comparator-fault-legacy-surface-unavailable",
        "The authoritative surface was unreachable. Absence of an answer is not an answer "
        "about absence, so this is a fault rather than a missing row -- otherwise every "
        "legacy outage would be logged as canonical data loss, or worse, as a clean run.",
        {"grain": "result_set", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, outcome="unavailable", row_state="absent", row_count=0),
         "canonical": _side(device, reader=CANONICAL_READER),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("detection", device=device),
    ))

    # 24. Undecidable, and therefore loud.
    device = "mach"
    out.append(_case(
        "unclassified-missing-result-digest",
        "One surface published no digest for its answer. No verdict is derivable, so the "
        "comparison is unclassified and critical: a quiet unclassified bucket is how a "
        "dashboard goes green over a hole.",
        {"grain": "result_set", "read_mode": "compare", "snapshot_skew_s": 1.0,
         "legacy": _side(device, result_hash=None),
         "canonical": _side(device, reader=CANONICAL_READER),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("detection", device=device),
    ))

    # 25. The unanchored-clock false positive, proven absent.
    device = "rankine"
    unanchored = [dict(row, **{"clock.tier": "free_running"}) for row in _rows(device, 3)]
    out.append(_case(
        "unanchored-clock-compared-as-tier",
        "Neither surface has a GPS-anchored instant for these rows. Time is compared as its "
        "clock tier and the value is ignored, so two honest readers with no fix agree. "
        "Without this rule the comparator manufactures a permanent red signal out of "
        "correct behaviour.",
        {"grain": "result_set", "read_mode": "shadow", "snapshot_skew_s": 1.0,
         "legacy": _side(device, rows=unanchored),
         "canonical": _side(device, reader=CANONICAL_READER, rows=unanchored),
         "age_s": 7_200.0, "settle_s": SR.SETTLE_S},
        _query("detection", device=device),
        sampling=SR.sample_receipt("match", "audit", forced_reason="board-class first day"),
        coverage_block=SR.coverage({"legacy_rows_offered": 3, "compared": 3}),
    ))

    # 26. A drain backfill lane, on its own settle horizon.
    device = "kasami"
    out.append(_case(
        "backfill-lag-inside-backfill-settle",
        "An SD backlog is being replayed hours late. Judged against the backfill settle "
        "horizon rather than the live one, so a day-late canonical row on an import lane is "
        "pending rather than loss. Judging a bulk replay on the live window pages on every "
        "drain run.",
        {"grain": "row", "read_mode": "shadow", "snapshot_skew_s": 1.0,
         "legacy": _side(device, row_count=1),
         "canonical": _side(device, reader=CANONICAL_READER, row_state="absent",
                            row_count=0),
         "age_s": 21_600.0, "settle_s": SR.BACKFILL_SETTLE_S},
        _query("detection", device=device, settle_s=SR.BACKFILL_SETTLE_S),
        key=SR.row_key(evidence_class="detection", site_id=SITE, device_id=device,
                       logical_id="det-000777"),
    ))

    return out


def manifest(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    return {
        "contract": SR.RECEIPT_CONTRACT_ID,
        "contract_major": SR.RECEIPT_CONTRACT_MAJOR,
        "status": "staged",
        "staged_reason": (
            "Publishing under contracts/ regenerates docs/data/phase0-freeze-contracts.v1.json. "
            "The Phase 4 reconcile receipt is already staged in front of this one and is "
            "promoted first; promoting both at once would regenerate one hashed baseline from "
            "two lanes. Promotion is a move plus a baseline regeneration and no content "
            "change; see docs/decisions/0006-phase5-shadow-read-comparator.md."
        ),
        "generator": "tools/gen_shadow_read_contracts.py",
        "source_of_truth": "hear/verify/shadow_read.py",
        "read_authority": SR.READ_AUTHORITY,
        "shadow_modes": list(SR.SHADOW_MODES),
        "tolerances": {
            "settle_s": SR.SETTLE_S,
            "backfill_settle_s": SR.BACKFILL_SETTLE_S,
            "late_arrival_s": SR.LATE_ARRIVAL_S,
            "snapshot_skew_s": SR.SNAPSHOT_SKEW_S,
            "retention_edge_s": SR.RETENTION_EDGE_S,
            "order_grace_s": SR.ORDER_GRACE_S,
            "cursor_ttl_s": SR.CURSOR_TTL_S,
            "max_pages": SR.MAX_PAGES,
            "max_rows_compared": SR.MAX_ROWS_COMPARED,
            "max_differences": SR.MAX_DIFFERENCES,
            "max_receipt_bytes": SR.MAX_RECEIPT_BYTES,
            "default_query_denominator": SR.DEFAULT_QUERY_DENOMINATOR,
            "default_row_denominator": SR.DEFAULT_ROW_DENOMINATOR,
            "cost_ceiling_s_per_10k_rows": SR.COST_CEILING_S_PER_10K_ROWS,
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
        _rel(SCHEMA_PATH): _dump(SR.receipt_schema_document()),
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
            print("rerun: python3 tools/gen_shadow_read_contracts.py", file=sys.stderr)
            return 1
        print("shadow-read artifacts current (%d files)" % len(generated))
        return 0

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for rel, text in sorted(generated.items()):
        (ROOT / rel).write_text(text)
    print("wrote %d files under %s" % (len(generated), _rel(OUT_DIR)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
