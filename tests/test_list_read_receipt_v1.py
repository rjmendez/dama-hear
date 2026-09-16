"""`hear.readcompare.receipt.v1`: pinning, page identity, aggregates, exports and gating.

These tests exist to make the Phase 6 list/aggregate/export comparison design falsifiable
before any comparator is built. Phase 5 compares records; the Phase 6 read audit deferred
everything that is not a record because the legacy list surfaces have no cursor and
`/api/export` is unbounded. Seven properties carry this design, and each has a test that
fails loudly:

1. Legacy stays authoritative, and the authority constant is **imported** from the Phase 5
   comparator rather than restated, so a read cutover cannot be introduced by editing the
   newer file.
2. An unpinned or expired list read is refused, not compared. A list answer is a function of
   an instant, and comparing two unpinned pages reports concurrent writes as data loss.
3. A cursor is opaque and side-local: never parsed, never re-derived, never compared across
   sides, never durable in a receipt.
4. A sort key without a unique tie-break is rejected at normalisation, and ties ordered
   differently are judged before content.
5. Unbounded work is refused rather than audited: export equivalence is proved over a
   bounded, recorded, sliced range, and an unboundable range costs recorded coverage.
6. An aggregate receipt carries counts, not people: closed group-key dimensions, no
   aggregate over a denied path at all, and cells below the k-anonymity floor reported with
   their keys and numbers redacted but their verdicts kept.
7. The classification vocabulary is closed and total, conservation closes, mismatches are
   never sampled away, and the comparator has no write path into either subject.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hear.ingest import reconcile as RC  # noqa: E402
from hear.verify import list_read as LR  # noqa: E402
from hear.verify import shadow_read as SR  # noqa: E402
from tools import gen_list_read_contracts as GEN  # noqa: E402

OUT_DIR = ROOT / "docs" / "phase6-list-read"
FIXTURE_DIR = OUT_DIR / "fixtures"
SCHEMA_PATH = OUT_DIR / (LR.RECEIPT_CONTRACT_ID + ".schema.json")

SITE = "site-alpha"
THRESHOLDS = {"max_stale_s": 900, "unfetched_window_s": 3600}


def _string_values(node):
    """Every string that is DATA in a receipt, at any depth."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _string_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _string_values(value)


def _query(**over):
    base = {
        "surface": "detection_list",
        "site_id": SITE,
        "device_ids": ["gold"],
        "time_from": "2026-09-14T00:00:00Z",
        "time_to": "2026-09-15T01:45:00Z",
        "bound_kind": "half_open",
        "filters": {"kind": "gunshot"},
        "sort": {"field": "captured_at", "direction": "asc", "tie_break": "row_uid"},
        "page_size": 50,
        "as_of": "2026-09-15T02:00:00Z",
        "settle_s": LR.SETTLE_S,
        "projection_version": 1,
        "thresholds": dict(THRESHOLDS),
        "expected_difference_list": "legacy-defects-2026-09-15",
    }
    base.update(over)
    return base


def _side(**over):
    keys = ["r:one", "r:two", "r:three"]
    base = {
        "outcome": "answered",
        "row_state": "present",
        "row_count": 3,
        "page_count": 1,
        "truncated": False,
        "pin_kind": "range_pin",
        "pin_id": "pin:detection_list",
        "pin_age_s": 30.0,
        "pin_bound": {"since_id_present": True, "max_id_present": True},
        "cursor_stable": True,
        "result_hash": LR.result_hash([{"row_uid": k} for k in keys]),
        "membership_digest": LR.membership_digest(keys),
        "order_digest": LR.order_digest(keys),
        "projection_fields_digest": LR.projection_digest(("row_uid", "state")),
        "snapshot_at": "2026-09-15T02:00:00Z",
        "retention_class": "R0-derived",
        "retention_horizon_s": 90 * 86_400,
        "read_contract_major": 1,
        "projection_version": 1,
        "site_id": SITE,
        "scope_digest": LR.membership_digest([SITE, "gold"]),
    }
    base.update(over)
    return base


def _pair(**over):
    base = {
        "grain": "page",
        "surface": "detection_list",
        "read_mode": "shadow",
        "snapshot_skew_s": 1.0,
        "age_s": 7_200.0,
        "settle_s": LR.SETTLE_S,
        "threshold_fingerprint_legacy": LR.threshold_fingerprint(THRESHOLDS),
        "threshold_fingerprint_canonical": LR.threshold_fingerprint(THRESHOLDS),
        "drift": {"kind": "none"},
        "legacy": _side(),
        "canonical": _side(pin_kind="cursor_pin"),
    }
    base.update(over)
    return base


class TestAuthority:
    """Legacy answers, and nothing here changes that."""

    def test_read_authority_is_imported_from_the_record_comparator(self):
        assert LR.READ_AUTHORITY is SR.READ_AUTHORITY == "legacy"

    def test_the_comparator_refuses_modes_where_a_reader_already_moved(self):
        for mode in ("shadow", "compare"):
            assert LR.assert_shadow_only(mode) == mode
        for mode in ("prefer", "only", "off"):
            with pytest.raises(ValueError):
                LR.assert_shadow_only(mode)

    def test_time_bounds_are_the_record_comparators_bounds(self):
        """Two audits over one window must not disagree about what "settled" means."""
        assert (LR.SETTLE_S, LR.LATE_ARRIVAL_S, LR.BACKFILL_SETTLE_S) == (
            SR.SETTLE_S, SR.LATE_ARRIVAL_S, SR.BACKFILL_SETTLE_S)
        assert LR.SNAPSHOT_SKEW_S == SR.SNAPSHOT_SKEW_S
        assert LR.PIN_TTL_S == SR.CURSOR_TTL_S

    def test_this_contract_is_not_the_record_contract(self):
        assert LR.RECEIPT_CONTRACT_ID != SR.RECEIPT_CONTRACT_ID
        assert LR.CORRELATION_NAMESPACE != SR.CORRELATION_NAMESPACE


class TestPinning:
    """A list answer is a function of a snapshot. No pin, no comparison."""

    def test_an_unpinned_side_is_refused(self):
        with pytest.raises(ValueError):
            LR.assert_pinned(_side(pin_kind="none"), name="legacy")

    def test_a_range_pin_without_recorded_bounds_is_not_a_pin(self):
        side = _side(pin_bound={"since_id_present": True, "max_id_present": False})
        assert LR.pin_ok(side) is False

    def test_a_range_pin_with_both_bounds_is_how_a_cursorless_legacy_list_is_pinned(self):
        """The legacy surfaces have no cursor, and this design does not add one to them.
        The comparator bounds its own request instead."""
        assert LR.assert_pinned(_side(pin_kind="range_pin"), name="legacy") == "range_pin"

    def test_an_expired_pin_is_the_comparators_fault_not_a_divergence(self):
        pair = _pair(canonical=_side(pin_kind="cursor_pin", pin_age_s=LR.PIN_TTL_S + 1))
        assert LR.classify(pair).classification == "comparator_fault"

    def test_an_unpinned_page_is_a_fault_not_a_membership_finding(self):
        pair = _pair(legacy=_side(pin_kind="none"))
        verdict = LR.classify(pair)
        assert verdict.classification == "comparator_fault"
        assert verdict.gate_impact == "blocks_cutover"

    def test_snapshot_drift_on_a_legacy_list_is_reported_but_never_blocking(self):
        """The legacy list has no snapshot isolation. Paging that as a defect is the false
        positive that gets a comparator muted."""
        pair = _pair(drift={"kind": "snapshot_drift", "rows_inserted": 2})
        verdict = LR.classify(pair)
        assert verdict.classification == "snapshot_drift"
        assert verdict.severity == "warn"
        assert verdict.gate_impact == "none"


class TestCursors:
    """Opaque, side-local, and never durable."""

    def test_two_cursors_are_never_comparable_across_sides(self):
        assert LR.cursors_comparable("cursor-a", "cursor-a") is False
        assert LR.cursors_comparable("cursor-a", "cursor-b") is False

    def test_a_cursor_is_an_opaque_handle(self):
        assert LR.cursor_opaque("opaque-token") is True
        assert LR.cursor_opaque({"created_at": "2026-09-15T00:00:00Z"}) is False

    def test_an_unstable_cursor_is_judged_before_any_content(self):
        pair = _pair(canonical=_side(pin_kind="cursor_pin", cursor_stable=False,
                                     row_count=99))
        assert LR.classify(pair).classification == "cursor_divergence"

    def test_a_skipped_row_across_a_page_boundary_is_a_cursor_finding(self):
        assert LR.classify(_pair(cursor_gaps=1)).classification == "cursor_divergence"
        assert LR.classify(_pair(cursor_duplicates=1)).classification == "cursor_divergence"

    def test_no_receipt_field_can_hold_a_cursor_token(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        side = schema["properties"]["sides"]["properties"]["legacy"]["properties"]
        assert "cursor_token" not in side
        assert "never the cursor token" in side["pin_id"]["description"]

    def test_the_page_fingerprint_is_scoped_to_a_pin(self):
        one = LR.page_fingerprint(fingerprint="q:x", pin_id="pin-1", page_index=0,
                                  page_size=50)
        two = LR.page_fingerprint(fingerprint="q:x", pin_id="pin-2", page_index=0,
                                  page_size=50)
        assert one != two, "page 0 of two snapshots is not the same page"


class TestQueryIdentity:
    """Both sides must be asked the same question, including how the bounds are closed."""

    def test_a_sort_without_a_unique_tie_break_is_refused(self):
        with pytest.raises(ValueError):
            LR.assert_total_order({"field": "captured_at", "direction": "asc"})

    def test_bound_inclusivity_changes_the_fingerprint(self):
        assert LR.query_fingerprint(_query()) != LR.query_fingerprint(
            _query(bound_kind="closed"))

    def test_an_unknown_filter_key_is_folded_in_not_dropped(self):
        base = LR.query_fingerprint(_query())
        extra = LR.query_fingerprint(_query(filters={"kind": "gunshot", "new_facet": "x"}))
        assert base != extra

    def test_the_same_question_spelled_differently_fingerprints_the_same(self):
        assert LR.query_fingerprint(_query(device_ids=["gold", "kasami"])) == \
            LR.query_fingerprint(_query(device_ids=["kasami", "gold"]))

    def test_two_questions_are_a_filter_divergence_before_any_row_comparison(self):
        pair = _pair(fingerprint_legacy="q:one", fingerprint_canonical="q:two",
                     canonical=_side(pin_kind="cursor_pin", row_count=0,
                                     row_state="absent"))
        assert LR.classify(pair).classification == "filter_divergence"

    def test_an_unknown_surface_is_refused(self):
        with pytest.raises(ValueError):
            LR.query_fingerprint(_query(surface="nothing_we_inventoried"))


class TestThresholdsAndScope:
    """P6-R3: an operator flag that changes a verdict without changing the data."""

    def test_two_threshold_sets_void_the_run(self):
        pair = _pair(threshold_fingerprint_canonical=LR.threshold_fingerprint(
            dict(THRESHOLDS, max_stale_s=1_800)))
        verdict = LR.classify(pair)
        assert verdict.classification == "threshold_divergence"
        assert verdict.gate_impact == "blocks_cutover"

    def test_a_threshold_fingerprint_is_order_independent(self):
        assert LR.threshold_fingerprint({"a": 1, "b": 2}) == \
            LR.threshold_fingerprint({"b": 2, "a": 1})

    def test_two_scopes_are_not_a_pair(self):
        pair = _pair(canonical=_side(pin_kind="cursor_pin",
                                     scope_digest=LR.membership_digest([SITE, "kasami"])))
        assert LR.classify(pair).classification == "scope_divergence"

    def test_a_shape_difference_is_one_finding_not_one_per_row(self):
        pair = _pair(canonical=_side(
            pin_kind="cursor_pin",
            projection_fields_digest=LR.projection_digest(("row_uid", "state", "notes"))))
        assert LR.classify(pair).classification == "projection_divergence"


class TestOrderAndPagination:
    """A page boundary is not evidence; a different extent is."""

    def test_ties_ordered_differently_are_judged_before_content(self):
        pair = _pair(tie_rows_reordered=2, canonical=_side(pin_kind="cursor_pin",
                                                           row_count=2))
        verdict = LR.classify(pair)
        assert verdict.classification == "tie_break_divergence"
        assert verdict.gate_impact == "blocks_cutover"

    def test_inversions_inside_the_grace_window_are_not_a_finding(self):
        assert LR.classify(_pair(inversions=3, age_s=60.0)).classification == "match"

    def test_inversions_past_the_grace_window_are_reported_but_not_blocking(self):
        verdict = LR.classify(_pair(inversions=3, age_s=7_200.0))
        assert verdict.classification == "order_divergence"
        assert verdict.gate_impact == "none"

    def test_one_sided_truncation_is_a_difference_in_extent(self):
        verdict = LR.classify(_pair(truncated_side_only=True))
        assert verdict.classification == "pagination_divergence"
        assert verdict.repair_action == "reindex_request"

    def test_membership_and_order_are_separate_digests(self):
        keys = ["r:a", "r:b", "r:c"]
        assert LR.membership_digest(keys) == LR.membership_digest(list(reversed(keys)))
        assert LR.order_digest(keys) != LR.order_digest(list(reversed(keys)))

    def test_same_count_different_rows_is_membership_not_count(self):
        pair = _pair(grain="window",
                     canonical=_side(pin_kind="cursor_pin",
                                     membership_digest=LR.membership_digest(["r:x", "r:y",
                                                                             "r:z"])))
        assert LR.classify(pair).classification == "membership_divergence"

    def test_boundary_drift_is_warned_not_blocked(self):
        verdict = LR.classify(_pair(drift={"kind": "insert_drift", "rows_inserted": 1}))
        assert verdict.classification == "boundary_drift"
        assert verdict.gate_impact == "none"


class TestAbsenceAndRetention:
    """Not returned has five meanings, and only one of them is loss."""

    def test_inside_the_settle_horizon_is_pending(self):
        pair = _pair(age_s=60.0, canonical=_side(pin_kind="cursor_pin", row_state="absent",
                                                 row_count=0))
        assert LR.classify(pair).classification == "pending"

    def test_a_count_difference_inside_the_settle_horizon_is_also_pending(self):
        """The leading edge of a write stream is scheduling jitter, not loss."""
        pair = _pair(age_s=60.0, canonical=_side(pin_kind="cursor_pin", row_count=2))
        assert LR.classify(pair).classification == "pending"

    def test_late_is_a_freshness_fact_not_a_loss(self):
        pair = _pair(late_s=300.0, canonical=_side(pin_kind="cursor_pin", row_state="absent",
                                                   row_count=0))
        verdict = LR.classify(pair)
        assert verdict.classification == "late_arrival"
        assert verdict.gate_impact == "none"

    def test_absence_past_both_windows_is_a_membership_finding(self):
        pair = _pair(canonical=_side(pin_kind="cursor_pin", row_state="absent", row_count=0))
        verdict = LR.classify(pair)
        assert verdict.classification == "membership_divergence"
        assert verdict.repair_action == "replay_inbox"

    def test_a_tombstone_beats_absence(self):
        pair = _pair(canonical=_side(pin_kind="cursor_pin", row_state="tombstoned"))
        verdict = LR.classify(pair)
        assert verdict.classification == "tombstone_divergence"
        assert verdict.gate_impact == "blocks_cutover"

    def test_a_retention_edge_is_not_a_finding(self):
        pair = _pair(age_s=7 * 86_400.0,
                     legacy=_side(retention_horizon_s=7 * 86_400),
                     canonical=_side(pin_kind="cursor_pin", retention_horizon_s=7 * 86_400,
                                     row_state="retention_expired", row_count=0))
        assert LR.classify(pair).classification == "match"

    def test_a_prune_far_from_any_horizon_is_reported(self):
        pair = _pair(age_s=86_400.0,
                     canonical=_side(pin_kind="cursor_pin", row_state="retention_expired",
                                     row_count=0))
        assert LR.classify(pair).classification == "retention_divergence"


class TestExports:
    """Unbounded work is refused, not audited."""

    def test_a_bounded_range_that_matches_is_a_match(self):
        digests = ["x1", "x2"]
        result = LR.export_equivalence(paged_slice_digests=digests,
                                       export_slice_digests=digests, rows_paged=2_000,
                                       rows_exported=2_000, bounded=True,
                                       rows_declared=2_000)
        assert result["equivalent"] is True
        assert result["classification"] == "match"

    def test_an_unbounded_range_is_refused_and_costs_recorded_coverage(self):
        result = LR.export_equivalence(paged_slice_digests=(), export_slice_digests=(),
                                       rows_paged=0, rows_exported=0, bounded=False)
        assert result["classification"] == "export_bound_exceeded"
        assert result["rows_compared"] == 0
        verdict = LR.Comparison(result["classification"])
        assert verdict.gate_impact == "none", "a refused read is not a store defect"

    def test_a_range_beyond_the_cap_is_split_never_widened(self):
        result = LR.export_equivalence(
            paged_slice_digests=("x",), export_slice_digests=("x",),
            rows_paged=LR.MAX_EXPORT_ROWS_COMPARED + 1,
            rows_exported=LR.MAX_EXPORT_ROWS_COMPARED + 1, bounded=True)
        assert result["classification"] == "export_bound_exceeded"
        assert "split the range" in result["reason"]

    def test_a_manifest_that_lies_about_its_row_count_is_a_divergence(self):
        result = LR.export_equivalence(paged_slice_digests=("x",),
                                       export_slice_digests=("x",), rows_paged=10,
                                       rows_exported=10, bounded=True, rows_declared=11)
        assert result["classification"] == "export_divergence"

    def test_same_rows_different_order_is_a_divergence(self):
        result = LR.export_equivalence(paged_slice_digests=("x1", "x2"),
                                       export_slice_digests=("x2", "x1"), rows_paged=4,
                                       rows_exported=4, bounded=True)
        assert result["classification"] == "export_divergence"

    def test_export_slices_are_bounded_so_no_side_materialises_the_dump(self):
        assert LR.EXPORT_SLICE_ROWS <= 1_000
        assert LR.MAX_EXPORT_ROWS_COMPARED >= LR.EXPORT_SLICE_ROWS

    def test_an_export_verdict_this_build_cannot_read_is_unclassified(self):
        pair = _pair(grain="export", surface="annotation_export",
                     export={"classification": "something_newer"})
        assert LR.classify(pair).classification == "unclassified"


class TestAggregatePrivacy:
    """Counts, not people."""

    def test_an_aggregate_over_a_denied_path_is_refused_not_blinded(self):
        for path in ("location.lat", "position.lon", "auth.token", "clip.audio.samples"):
            with pytest.raises(ValueError):
                LR.assert_aggregatable(path)

    def test_group_keys_are_closed_and_exclude_reviewer_identity(self):
        assert LR.assert_group_keys(["device_id", "day"]) == ["day", "device_id"]
        for bad in ("user_id", "reviewer", "notes", "lat"):
            with pytest.raises(ValueError):
                LR.assert_group_keys([bad])

    def test_a_small_cell_key_is_suppressed_while_its_verdict_is_kept(self):
        cell = {"device_id": "gold", "day": "2026-09-15"}
        small = LR.redact_cell_key(cell, count=LR.MIN_CELL_COUNT - 1)
        assert small["suppressed"] is True and small["values"] is None
        big = LR.redact_cell_key(cell, count=LR.MIN_CELL_COUNT)
        assert big["suppressed"] is False and big["values"]["device_id"] == "gold"

    def test_a_small_cells_numbers_are_redacted_and_a_large_cells_are_quoted(self):
        small = LR.compare_aggregate({"k": 3}, {"k": 1}, function="count")[0].to_dict()
        assert small["quoted"] is False and isinstance(small["legacy"], dict)
        big = LR.compare_aggregate({"k": 412}, {"k": 371}, function="count")[0].to_dict()
        assert big["quoted"] is True and big["legacy"] == 412

    def test_a_cell_key_never_appears_in_a_difference_path(self):
        diff = LR.compare_aggregate({"gold|2026-09-15": 412}, {"gold|2026-09-15": 371},
                                    function="count")[0]
        assert "gold" not in diff.path
        assert diff.path.startswith(LR.CELL_PATH + ".c:")

    def test_counts_are_compared_with_zero_tolerance_whatever_the_caller_asks(self):
        assert LR.compare_aggregate({"k": 100}, {"k": 101}, function="count",
                                    tolerance=5.0)
        assert not LR.compare_aggregate({"k": 100.0}, {"k": 101.0}, function="p95",
                                        tolerance=5.0, cell_counts={"k": 50})

    def test_a_cell_present_on_one_side_only_is_a_difference(self):
        diffs = LR.compare_aggregate({"k": 10}, {}, function="count")
        assert len(diffs) == 1 and diffs[0].tolerance == "cell present on one side only"

    def test_an_aggregate_bigger_than_the_cell_cap_is_a_list(self):
        big = {str(i): i for i in range(LR.MAX_AGGREGATE_CELLS + 1)}
        with pytest.raises(ValueError):
            LR.compare_aggregate(big, big, function="count")

    def test_an_aggregate_difference_classifies_as_an_aggregate_finding(self):
        pair = _pair(grain="aggregate", surface="health_snapshot",
                     differences=[{"path": "aggregate.value.c:ab"}])
        verdict = LR.classify(pair)
        assert verdict.classification == "aggregate_divergence"
        assert verdict.gate_impact == "blocks_cutover"


class TestRedaction:
    """An unknown path defaults to hidden, because a deny list defaults to leaked."""

    def test_the_deny_pattern_is_the_write_sides_own(self):
        for path in ("device.lat", "node.position", "auth.bearer", "clip.pcm",
                     "annotation.notes.payload"):
            assert LR.path_is_quotable(path) is False

    def test_the_allow_list_inherits_both_upstream_lists(self):
        assert set(SR.READ_VALUE_ALLOW_PATHS) <= set(LR.VALUE_ALLOW_PATHS)
        assert set(RC.VALUE_ALLOW_PATHS) <= set(LR.VALUE_ALLOW_PATHS)

    def test_an_unknown_path_is_hidden(self):
        assert LR.path_is_quotable("some.future.field") is False
        assert LR.redact_value("some.future.field", "value") != "value"

    def test_a_filter_value_is_redacted_because_a_query_is_user_input(self):
        """`?near_latitude=...` is the obvious way to reintroduce a coordinate through the
        one field nobody thought of as data. A declared facet may still be quoted."""
        receipt = _receipt(filters={"near_latitude": "48.1", "kind": "gunshot"})
        filters = receipt["query"]["filters"]
        assert not isinstance(filters["near_latitude"], str)
        assert filters["kind"] == "gunshot"

    def test_a_blind_difference_carries_no_value_not_even_a_hash(self):
        difference = LR.Difference("node.position", 1, 2, comparator="blind").to_dict()
        assert difference["legacy"] is None and difference["canonical"] is None
        assert difference["blind"] is True


def _receipt(**query_over):
    pair = _pair()
    comparison = LR.classify(pair)
    return LR.build_receipt(
        run_id="run-1", window_start="2026-09-15T01:00:00Z",
        window_end="2026-09-15T02:00:00Z", as_of="2026-09-15T02:00:00Z",
        emitted_at="2026-09-15T02:00:45Z", query=_query(**query_over), pair=pair,
        comparison=comparison, page={"index": 0, "size": 50, "first": True})


class TestSamplingAndCost:
    """Coverage is computable rather than asserted, and a mismatch is never sampled away."""

    def test_a_boundary_page_is_never_sampled_away(self):
        for page in ({"index": 0, "first": True}, {"index": 9, "last": True}):
            decision = LR.sample_page("detection_list", page)
            assert decision["selected"] is True
            assert decision["forced_reason"] == "page boundary"

    def test_an_exhaustive_surface_is_never_sampled(self):
        for surface in LR.EXHAUSTIVE_SURFACES:
            decision = LR.sample_page(surface, {"index": 5, "fingerprint": "p:x"})
            assert decision["mode"] == "exhaustive" and decision["selected"] is True

    def test_page_sampling_is_deterministic_so_a_rerun_repeats_the_set(self):
        page = {"index": 7, "fingerprint": "p:deadbeef"}
        first = LR.sample_page("detection_list", page)
        assert first == LR.sample_page("detection_list", page)

    def test_a_broken_denominator_samples_everything(self):
        for denominator in (0, -3, None):
            decision = LR.sample_page("detection_list", {"index": 3, "fingerprint": "p:x"},
                                      denominator=denominator)
            assert decision["selected"] is True

    def test_a_mismatch_is_never_sampled_away(self):
        for name in LR.MISMATCH_CLASSIFICATIONS:
            assert LR.sample_receipt(name, "k")["sampled"] is True

    def test_coverage_names_its_exclusions_including_the_refused_reads(self):
        block = LR.coverage({"legacy_rows_offered": 100, "compared": 70,
                             "excluded_unsettled": 10, "excluded_retention_edge": 5,
                             "excluded_sampling": 5, "excluded_unpinned": 5,
                             "excluded_unbounded": 5})
        assert block["accounted"] is True
        assert block["eligible"] == 75
        assert block["excluded_unbounded"] == 5

    def test_coverage_accounting_fails_when_rows_vanish(self):
        block = LR.coverage({"legacy_rows_offered": 100, "compared": 70})
        assert block["accounted"] is False

    def test_the_cost_ceiling_matches_both_sibling_audits(self):
        assert LR.COST_CEILING_S_PER_10K_ROWS == SR.COST_CEILING_S_PER_10K_ROWS
        assert LR.within_cost_ceiling(rows_compared=10_000, seconds=60.0) is True
        assert LR.within_cost_ceiling(rows_compared=10_000, seconds=61.0) is False


class TestVocabularyAndAccounting:
    """Closed, total, and conserved."""

    def test_every_classification_has_a_severity_repair_and_gate_impact(self):
        for name in LR.CLASSIFICATIONS:
            assert LR.SEVERITY_BY_CLASSIFICATION[name] in LR.SEVERITIES
            assert LR.REPAIR_BY_CLASSIFICATION[name] in LR.REPAIR_ACTIONS
            assert LR.GATE_IMPACT_BY_CLASSIFICATION[name] in LR.GATE_IMPACTS

    def test_conservation_closes_over_a_tally(self):
        comparisons = [LR.classify(_pair()),
                       LR.classify(_pair(cursor_gaps=1)),
                       LR.classify(_pair(truncated_side_only=True))]
        counts = LR.tally(comparisons)
        assert LR.conservation(counts) is True

    def test_an_unknown_verdict_is_counted_and_blocks_a_cutover(self):
        unknown = LR.Comparison("a_member_from_a_newer_build")
        assert unknown.actionable is False
        assert unknown.gate_impact == "blocks_cutover"
        counts = LR.tally([unknown])
        assert counts["unclassified"] == 1
        assert LR.conservation(counts) is True
        assert "unclassified" in LR.gate_blocked(counts)

    def test_gate_blocking_is_separate_from_repair_action(self):
        assert set(LR.BLOCKING_CLASSIFICATIONS) == {
            name for name in LR.CLASSIFICATIONS
            if LR.GATE_IMPACT_BY_CLASSIFICATION[name] == "blocks_cutover"}
        assert LR.REPAIR_BY_CLASSIFICATION["order_divergence"] == "none"

    def test_every_classification_is_reachable_from_classify_or_a_helper(self):
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
        covered = {case["expected_classification"] for case in manifest["cases"]}
        assert set(LR.CLASSIFICATIONS) - covered == set()


class TestReceipt:
    """An index into evidence, never the evidence."""

    def test_a_pending_comparison_cannot_produce_a_receipt(self):
        pair = _pair(age_s=60.0, canonical=_side(pin_kind="cursor_pin", row_state="absent",
                                                 row_count=0))
        with pytest.raises(ValueError):
            LR.build_receipt(run_id="r", window_start="a", window_end="b", as_of="c",
                             emitted_at="d", query=_query(), pair=pair,
                             comparison=LR.classify(pair))

    def test_a_receipt_is_never_queued_by_the_comparator(self):
        assert _receipt()["disposition"]["queued"] is False

    def test_receipt_ids_are_deterministic_so_a_rerun_converges(self):
        assert _receipt()["receipt_id"] == _receipt()["receipt_id"]

    def test_differences_beyond_the_cap_are_recorded_as_dropped(self):
        pair = _pair()
        many = [LR.Difference("aggregate.value.c:%02x" % i, i, i + 1, comparator="count")
                for i in range(LR.MAX_DIFFERENCES + 5)]
        receipt = LR.build_receipt(run_id="r", window_start="a", window_end="b", as_of="c",
                                   emitted_at="d", query=_query(), pair=pair,
                                   comparison=LR.classify(pair), differences=many)
        assert len(receipt["differences"]) == LR.MAX_DIFFERENCES
        assert receipt["differences_dropped"] == 5

    def test_an_oversized_receipt_is_refused_not_truncated(self):
        receipt = dict(_receipt())
        receipt["coverage"] = {"padding": "x" * (LR.MAX_RECEIPT_BYTES + 1)}
        with pytest.raises(ValueError):
            LR.encode_receipt(receipt)

    def test_a_newer_major_is_refused_with_a_reason(self):
        assert LR.supported(_receipt()) is True
        assert LR.supported(dict(_receipt(), schema_version=2)) is False

    def test_the_receipt_records_the_threshold_set_both_sides_used(self):
        assert _receipt()["query"]["threshold_fingerprint"].startswith("t:")

    def test_the_receipt_records_which_comparator_build_produced_it(self):
        assert set(_receipt()["comparator"]) >= {"name", "version", "receipt_major"}


class TestGeneratedArtifacts:

    def test_the_tree_matches_its_generator(self):
        assert GEN.main(["--check"]) == 0, "rerun tools/gen_list_read_contracts.py"

    def test_every_classification_has_a_worked_fixture(self):
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
        covered = {case["expected_classification"] for case in manifest["cases"]}
        missing = set(LR.CLASSIFICATIONS) - covered
        assert not missing, "a vocabulary member with no worked example is an unchecked claim"

    def test_the_manifest_declares_itself_staged_with_a_reason(self):
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
        assert manifest["status"] == "staged"
        assert "contracts/" in manifest["staged_reason"]
        assert manifest["source_of_truth"] == "hear/verify/list_read.py"
        assert manifest["read_authority"] == "legacy"

    def test_every_fixture_is_declared_with_an_expected_outcome(self):
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
        declared = {case["file"] for case in manifest["cases"]}
        on_disk = {p.name for p in FIXTURE_DIR.glob("*.json") if p.name != "manifest.json"}
        assert declared == on_disk

    def test_every_fixture_reproduces_its_declared_verdict(self):
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
        for case in manifest["cases"]:
            body = json.loads((FIXTURE_DIR / case["file"]).read_text())
            assert body["expected_classification"] == case["expected_classification"]
            assert body["expected_gate_impact"] == case["expected_gate_impact"]
            assert (body["receipt"] is not None) == case["emits_receipt"]
            if body["receipt"] is not None:
                verdict = body["receipt"]["verdict"]
                assert verdict["classification"] == case["expected_classification"]
                assert verdict["gate_impact"] == case["expected_gate_impact"]

    def test_no_fixture_quotes_a_denied_path(self):
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            receipt = json.loads(path.read_text()).get("receipt")
            if not receipt:
                continue
            for difference in receipt["differences"]:
                if RC.VALUE_DENY_PATTERN.search(difference["path"]):
                    assert difference["quoted"] is False
                    assert isinstance(difference["legacy"], (dict, type(None)))
                if difference["blind"]:
                    assert difference["legacy"] is None
                    assert difference["canonical"] is None

    def test_no_fixture_carries_a_credential_body_or_sample_value(self):
        """Scans receipt VALUES, not the file text: a case description may say the word
        "bearer", and a test that cannot tell prose from data gets deleted the first time it
        blocks an honest comment."""
        banned = ("bearer ", "authorization:", "x-hear-token", "password", "private key",
                  "-----begin", "data:audio/")
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            receipt = json.loads(path.read_text()).get("receipt")
            if not receipt:
                continue
            for value in _string_values(receipt):
                lowered = value.lower()
                assert not [b for b in banned if b in lowered], (path.name, value)

    def test_every_emitted_fixture_receipt_fits_the_size_bound(self):
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            body = json.loads(path.read_text())
            if body.get("receipt") is not None:
                LR.encode_receipt(body["receipt"])

    def test_pending_fixtures_emit_no_receipt(self):
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
        for case in manifest["cases"]:
            if case["expected_classification"] == "pending":
                assert case["emits_receipt"] is False

    def test_the_schema_stays_additive_within_the_major(self):
        schema = json.loads(SCHEMA_PATH.read_text())

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node.get("additionalProperties", True) is not False
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(schema)

    def test_the_schema_pins_the_repair_boundary(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        assert schema["properties"]["disposition"]["properties"]["queued"]["const"] is False

    def test_the_schema_pins_legacy_as_the_read_authority(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        assert schema["properties"]["read_authority"]["const"] == "legacy"
        assert schema["properties"]["read_mode"]["enum"] == list(LR.SHADOW_MODES)

    def test_the_schema_requires_a_total_order(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        sort = schema["properties"]["query"]["properties"]["sort"]
        assert "tie_break" in sort["required"]


class TestBoundaries:
    """The comparator is an auditor: no I/O, no credential, no write path, no reader."""

    def test_the_module_performs_no_io(self):
        tree = ast.parse(Path(LR.__file__).read_text())
        forbidden = {"open", "input", "exec", "eval", "compile", "__import__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden, node.func.id

    def test_the_module_imports_only_the_standard_library_and_its_siblings(self):
        tree = ast.parse(Path(LR.__file__).read_text())
        allowed = {"hashlib", "json", "uuid", "typing", "__future__", "hear"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] in allowed, alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] in allowed, node.module

    def test_the_repair_vocabulary_has_no_write_into_either_subject(self):
        assert set(LR.REPAIR_ACTIONS) == set(SR.REPAIR_ACTIONS)
        for action in LR.REPAIR_ACTIONS:
            assert action in ("none", "replay_inbox", "replay_outbox", "reindex_request",
                              "manual_review")

    def test_the_contract_id_is_not_harvestable_as_a_published_contract(self):
        """`tools/freeze_contracts.py` harvests `[A-Z_]*SCHEMA[A-Z_]*` assignments as
        published contract ids. Registering an unpublished id in the frozen baseline would
        make the layout rule pass for a contract with no published artifact."""
        source = Path(LR.__file__).read_text()
        harvestable = re.findall(r'\b[A-Z_]*SCHEMA[A-Z_]*\s*=\s*["\']([A-Za-z0-9._-]+)["\']',
                                 source)
        assert LR.RECEIPT_CONTRACT_ID not in harvestable

    def test_nothing_in_the_runtime_tree_imports_the_comparator(self):
        """A design module reachable from a running reader is not a design module."""
        design_lane = ("list_read.py", "gen_list_read_contracts.py", "__init__.py")
        importers = []
        for path in sorted((ROOT / "hear").rglob("*.py")) + sorted(
                (ROOT / "tools").rglob("*.py")):
            if path.name in design_lane:
                continue
            if "list_read" in path.read_text():
                importers.append(path.relative_to(ROOT).as_posix())
        assert importers == [], importers

    def test_the_comparator_has_no_credential_of_its_own(self):
        source = Path(LR.__file__).read_text().lower()
        for token in ("password", "api_key =", "bearer ", "secret ="):
            assert token not in source

    def test_the_comparator_never_polls_a_node(self):
        """P6-R5: the node HTTP client is single-threaded and shared. A comparator that
        polled `/status` would be a competing fetcher against live capture.

        Asserted over string *literals* and imports rather than over the file text, because
        the module is allowed to explain in prose that it opens no socket.
        """
        tree = ast.parse(Path(LR.__file__).read_text())
        endpoints = {"/status", "/ls", "/sd", "/audio", "/api/queue", "/api/export"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in endpoints, node.value
                assert "json-schema.org" in node.value or not node.value.startswith(
                    "http://"), node.value
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in (
                        "socket", "http", "urllib", "requests", "asyncio")
