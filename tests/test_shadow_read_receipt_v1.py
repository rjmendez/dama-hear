"""`hear.shadowread.receipt.v1`: query identity, read classification, redaction and gating.

These tests exist to make the Phase 5 shadow-read design falsifiable before any comparator
is built. Six properties carry the weight, and each has a test that fails loudly:

1. Legacy stays authoritative. The comparator runs only in `shadow`/`compare`, and a build
   that flipped `READ_AUTHORITY` would be a read cutover disguised as a comparator change.
2. The classification vocabulary is **closed and total** -- every comparison reaches exactly
   one member, `conservation()` closes, and no input produces an "other" bucket.
3. Absence has five meanings, and only one of them is loss: in flight, late,
   retention-expired, tombstoned, lost.
4. A receipt can never carry a coordinate, a credential or audio -- not even as a hash --
   whatever a future field table calls the path.
5. Order, pagination and cursor behaviour are properties separate from content, so a page
   boundary is never evidence and an unstable cursor is never a content finding.
6. A mismatch is never sampled away, and every sampling decision is deterministic so
   coverage is computable rather than asserted.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hear.ingest import reconcile as RC  # noqa: E402
from hear.verify import shadow_read as SR  # noqa: E402
from tools import gen_shadow_read_contracts as GEN  # noqa: E402

OUT_DIR = ROOT / "docs" / "phase5-shadow-read"
FIXTURE_DIR = OUT_DIR / "fixtures"
SCHEMA_PATH = OUT_DIR / (SR.RECEIPT_CONTRACT_ID + ".schema.json")

SITE = "site-alpha"


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


def _side(**over):
    base = {
        "outcome": "answered",
        "row_state": "present",
        "row_count": 1,
        "page_count": 1,
        "truncated": False,
        "cursor_stable": True,
        "result_hash": SR.result_hash([{"counters.dets_rows_written": 7}]),
        "state_hash": SR.state_hash({"counters.dets_rows_written": 7}),
        "snapshot_at": "2026-09-15T02:00:00Z",
        "retention_class": "R0-derived",
        "retention_horizon_s": 90 * 86_400,
        "read_contract_major": 1,
        "projection_version": 1,
        "device_id": "gold",
        "site_id": SITE,
        "principal_id": "node:gold",
    }
    base.update(over)
    return base


def _pair(**over):
    base = {
        "grain": "row",
        "read_mode": "shadow",
        "snapshot_skew_s": 1.0,
        "legacy": _side(),
        "canonical": _side(),
        "age_s": 7_200.0,
        "settle_s": SR.SETTLE_S,
    }
    base.update(over)
    return base


def _query(**over):
    base = {
        "evidence_class": "detection",
        "site_id": SITE,
        "device_ids": ["gold"],
        "time_from": "2026-09-14T00:00:00Z",
        "time_to": "2026-09-15T01:45:00Z",
        "filters": {"kind": "gunshot"},
        "sort": {"field": "captured_at", "direction": "asc"},
        "page_size": 50,
        "as_of": "2026-09-15T02:00:00Z",
    }
    base.update(over)
    return base


class TestReadAuthority:
    """Legacy is the answer for the whole of Phase 5, and the comparator cannot be the thing
    that changes that."""

    def test_authority_is_legacy(self):
        assert SR.READ_AUTHORITY == "legacy"

    def test_only_shadow_modes_are_allowed(self):
        for mode in SR.SHADOW_MODES:
            assert SR.assert_shadow_only(mode) == mode

    @pytest.mark.parametrize("mode", ["prefer", "only", "off"])
    def test_moved_or_absent_readers_are_refused(self, mode):
        with pytest.raises(ValueError):
            SR.assert_shadow_only(mode)

    def test_unknown_mode_is_refused_not_guessed(self):
        with pytest.raises(ValueError):
            SR.assert_shadow_only("preferred-ish")

    def test_receipt_records_the_authority_it_ran_under(self):
        receipt = SR.build_receipt(
            run_id="r", window_start="a", window_end="b", as_of="c", emitted_at="d",
            query=_query(), pair=_pair(), comparison=SR.classify(_pair()))
        assert receipt["read_authority"] == "legacy"
        assert receipt["read_mode"] in SR.SHADOW_MODES


class TestVocabulariesAreClosed:

    def test_every_classification_has_a_severity_repair_and_gate_impact(self):
        for name in SR.CLASSIFICATIONS:
            assert SR.SEVERITY_BY_CLASSIFICATION[name] in SR.SEVERITIES
            assert SR.REPAIR_BY_CLASSIFICATION[name] in SR.REPAIR_ACTIONS
            assert SR.GATE_IMPACT_BY_CLASSIFICATION[name] in SR.GATE_IMPACTS

    def test_no_other_bucket(self):
        assert "other" not in SR.CLASSIFICATIONS
        assert "unknown" not in SR.CLASSIFICATIONS

    def test_terminal_and_non_terminal_partition_the_vocabulary(self):
        assert set(SR.TERMINAL) | set(SR.NON_TERMINAL) == set(SR.CLASSIFICATIONS)
        assert not set(SR.TERMINAL) & set(SR.NON_TERMINAL)

    def test_pending_is_the_only_non_terminal(self):
        assert SR.NON_TERMINAL == ("pending",)

    def test_every_comparator_declares_a_tolerance(self):
        assert set(SR.TOLERANCE_BY_COMPARATOR) == set(SR.COMPARATORS)

    def test_repair_is_advisory_and_never_mutates_a_store(self):
        for action in SR.REPAIR_ACTIONS:
            assert action in ("none", "replay_inbox", "replay_outbox", "reindex_request",
                              "manual_review")
        for forbidden in ("delete", "edit", "rewrite", "backfill_from_other_side",
                          "auto_repair"):
            assert forbidden not in SR.REPAIR_ACTIONS

    def test_undecidable_verdicts_are_critical_and_block_a_cutover(self):
        for name in ("unclassified", "comparator_fault"):
            assert SR.SEVERITY_BY_CLASSIFICATION[name] == "critical"
            assert SR.GATE_IMPACT_BY_CLASSIFICATION[name] == "blocks_cutover"

    def test_expected_classes_do_not_block_a_cutover(self):
        # These are correct behaviour with a recorded reason. If they blocked, the gate
        # could never be met and the exercise would be theatre.
        for name in ("match", "pending", "late_arrival", "order_divergence",
                     "duplicate_legacy_row", "retention_divergence", "version_divergence",
                     "missing_legacy_row", "pagination_divergence"):
            assert SR.GATE_IMPACT_BY_CLASSIFICATION[name] == "none"

    def test_exhaustive_classes_are_a_subset_of_the_evidence_classes(self):
        assert set(SR.EXHAUSTIVE_CLASSES) <= set(SR.EVIDENCE_CLASSES)
        assert set(SR.RESTRICTED_CLASSES) <= set(SR.EVIDENCE_CLASSES)


class TestQueryIdentity:

    def test_the_same_question_fingerprints_the_same_from_either_side(self):
        left = SR.query_fingerprint(_query(device_ids=["gold", "kasami"]))
        right = SR.query_fingerprint(_query(device_ids=["kasami", "gold"]))
        assert left == right, "device order is not part of the question"

    def test_a_different_bound_is_a_different_question(self):
        left = SR.query_fingerprint(_query(filters={"bound": "closed"}))
        right = SR.query_fingerprint(_query(filters={"bound": "half_open"}))
        assert left != right

    def test_an_unknown_filter_still_changes_the_fingerprint(self):
        """A newer comparator's filter must not be silently dropped from the identity of
        the question, or the two sides can be asked different things and agree."""
        base = SR.query_fingerprint(_query())
        extended = SR.query_fingerprint(_query(min_confidence=0.9))
        assert base != extended

    def test_unknown_evidence_class_is_refused(self):
        with pytest.raises(ValueError):
            SR.query_fingerprint(_query(evidence_class="telepathy"))

    def test_row_key_never_rederives_an_identity(self):
        source = Path(SR.__file__).read_text()
        assert "logical_id" in source
        key = SR.row_key(evidence_class="detection", site_id=SITE, device_id="gold",
                         logical_id="det-1")
        other = SR.row_key(evidence_class="scene", site_id=SITE, device_id="gold",
                           logical_id="det-1")
        assert key != other, "evidence class is part of a row identity"

    def test_receipt_ids_are_deterministic_so_a_rerun_converges(self):
        args = {"run_id": "run-1", "fingerprint": "q:abc", "key": "r:def"}
        assert SR.receipt_id(**args) == SR.receipt_id(**args)
        assert SR.receipt_id(**args) != SR.receipt_id(run_id="run-2", fingerprint="q:abc",
                                                      key="r:def")

    def test_result_hash_is_order_independent(self):
        rows = [{"a": 1}, {"a": 2}, {"a": 3}]
        assert SR.result_hash(rows) == SR.result_hash(list(reversed(rows)))

    def test_result_hash_still_notices_a_changed_row(self):
        assert SR.result_hash([{"a": 1}]) != SR.result_hash([{"a": 2}])


class TestWindowSemantics:

    def test_unsettled_rows_are_not_comparable(self):
        assert not SR.settled(age_s=10.0)
        assert SR.settled(age_s=SR.SETTLE_S)

    def test_backfill_uses_its_own_horizon(self):
        assert not SR.settled(age_s=6 * 3_600.0, settle_s=SR.BACKFILL_SETTLE_S)
        assert SR.settled(age_s=6 * 3_600.0, settle_s=SR.SETTLE_S)

    def test_snapshot_skew_beyond_the_limit_is_not_ok(self):
        assert SR.snapshot_skew_ok(1.0)
        assert not SR.snapshot_skew_ok(SR.SNAPSHOT_SKEW_S + 1)
        assert not SR.snapshot_skew_ok(None), "an unrecorded skew is not a small skew"

    def test_rows_near_or_past_a_retention_horizon_are_excluded(self):
        horizon = 7 * 86_400.0
        assert SR.retention_edge(age_s=horizon - 60, horizons_s=[horizon])
        assert SR.retention_edge(age_s=horizon + 10_000, horizons_s=[horizon])
        assert not SR.retention_edge(age_s=3_600.0, horizons_s=[horizon])

    def test_an_undeclared_horizon_never_excludes_anything(self):
        assert not SR.retention_edge(age_s=10 ** 9, horizons_s=[None])


class TestClassification:

    def test_agreeing_surfaces_match(self):
        assert SR.classify(_pair()).classification == "match"

    def test_comparator_competence_is_judged_before_the_stores(self):
        """A skewed snapshot must not be reported as a store divergence, even when the two
        answers also differ -- otherwise the comparator blames a store for its own bug."""
        pair = _pair(snapshot_skew_s=60.0,
                     canonical=_side(row_state="absent", row_count=0))
        assert SR.classify(pair).classification == "comparator_fault"

    def test_an_unreachable_surface_is_a_fault_not_a_missing_row(self):
        pair = _pair(legacy=_side(outcome="unavailable", row_state="absent", row_count=0))
        assert SR.classify(pair).classification == "comparator_fault"

    def test_an_expired_cursor_is_the_comparators_fault(self):
        pair = _pair(cursor_age_s=SR.CURSOR_TTL_S + 1)
        assert SR.classify(pair).classification == "comparator_fault"

    def test_identity_beats_content(self):
        pair = _pair(canonical=_side(device_id="kasami", principal_id="node:kasami",
                                     row_state="absent", row_count=0))
        assert SR.classify(pair).classification == "identity_divergence"

    def test_projection_version_mismatch_is_a_fault_not_a_divergence(self):
        pair = _pair(canonical=_side(projection_version=2))
        assert SR.classify(pair).classification == "comparator_fault"

    def test_mixed_reader_majors_are_a_supported_state(self):
        pair = _pair(canonical=_side(read_contract_major=2))
        verdict = SR.classify(pair)
        assert verdict.classification == "version_divergence"
        assert verdict.severity == "warn"
        assert verdict.gate_impact == "none"

    def test_a_different_question_is_never_a_content_finding(self):
        pair = _pair(fingerprint_legacy="q:a", fingerprint_canonical="q:b",
                     canonical=_side(row_count=9))
        assert SR.classify(pair).classification == "filter_divergence"

    def test_an_unstable_cursor_is_judged_before_content(self):
        pair = _pair(canonical=_side(cursor_stable=False, row_count=9))
        assert SR.classify(pair).classification == "cursor_divergence"

    def test_a_row_duplicated_across_a_page_boundary_is_a_cursor_finding(self):
        assert SR.classify(_pair(cursor_duplicates=1)).classification == "cursor_divergence"
        assert SR.classify(_pair(cursor_gaps=1)).classification == "cursor_divergence"

    def test_duplication_beats_divergence(self):
        pair = _pair(canonical=_side(row_count=2, state_hash="sha256:different"))
        assert SR.classify(pair).classification == "duplicate_canonical_row"

    def test_tombstones_are_judged_before_absence(self):
        pair = _pair(legacy=_side(row_state="tombstoned", row_count=0))
        verdict = SR.classify(pair)
        assert verdict.classification == "tombstone_divergence"
        assert verdict.gate_impact == "blocks_cutover"

    def test_retention_expiry_near_a_horizon_is_excluded_not_reported(self):
        pair = _pair(legacy=_side(row_state="retention_expired", row_count=0,
                                  retention_horizon_s=7 * 86_400),
                     canonical=_side(retention_horizon_s=7 * 86_400),
                     age_s=7 * 86_400 + 600.0)
        assert SR.classify(pair).classification == "match"

    def test_retention_expiry_far_from_a_horizon_is_a_divergence(self):
        pair = _pair(legacy=_side(row_state="retention_expired", row_count=0),
                     age_s=2 * 86_400.0)
        assert SR.classify(pair).classification == "retention_divergence"

    def test_absence_inside_the_settle_horizon_is_pending(self):
        pair = _pair(canonical=_side(row_state="absent", row_count=0), age_s=10.0)
        assert SR.classify(pair).classification == "pending"

    def test_absence_that_resolved_late_is_not_loss(self):
        pair = _pair(canonical=_side(row_state="absent", row_count=0), late_s=300.0)
        verdict = SR.classify(pair)
        assert verdict.classification == "late_arrival"
        assert verdict.gate_impact == "none"

    def test_absence_past_both_windows_is_the_loss_we_are_looking_for(self):
        pair = _pair(canonical=_side(row_state="absent", row_count=0))
        verdict = SR.classify(pair)
        assert verdict.classification == "missing_canonical_row"
        assert verdict.severity == "critical"
        assert verdict.repair_action == "replay_inbox"
        assert verdict.gate_impact == "blocks_cutover"

    def test_canonical_only_is_not_loss(self):
        pair = _pair(legacy=_side(row_state="absent", row_count=0))
        verdict = SR.classify(pair)
        assert verdict.classification == "missing_legacy_row"
        assert verdict.severity == "warn"

    def test_both_absent_and_agreeing_is_a_match(self):
        pair = _pair(legacy=_side(row_state="absent", row_count=0),
                     canonical=_side(row_state="absent", row_count=0))
        assert SR.classify(pair).classification == "match"

    def test_counts_are_compared_at_result_set_grain(self):
        pair = _pair(grain="result_set", legacy=_side(row_count=3),
                     canonical=_side(row_count=2))
        assert SR.classify(pair).classification == "count_divergence"

    def test_one_sided_truncation_is_a_pagination_finding(self):
        pair = _pair(grain="result_set", truncated_side_only=True)
        verdict = SR.classify(pair)
        assert verdict.classification == "pagination_divergence"
        assert verdict.repair_action == "reindex_request"

    def test_ordering_inside_the_grace_window_is_not_a_finding(self):
        pair = _pair(grain="result_set", inversions=4, age_s=60.0, settle_s=30.0)
        assert SR.classify(pair).classification == "match"

    def test_ordering_past_the_grace_window_is_a_finding(self):
        pair = _pair(grain="result_set", inversions=4)
        verdict = SR.classify(pair)
        assert verdict.classification == "order_divergence"
        assert verdict.severity == "warn"

    def test_a_field_outside_tolerance_is_a_value_divergence(self):
        pair = _pair(differences=[SR.Difference("counters.clips_written", 1, 2,
                                                comparator="count")])
        assert SR.classify(pair).classification == "value_divergence"

    def test_a_missing_digest_is_unclassified_not_a_match(self):
        pair = _pair(canonical=_side(state_hash=None))
        verdict = SR.classify(pair)
        assert verdict.classification == "unclassified"
        assert verdict.severity == "critical"

    def test_differing_digests_with_no_reported_field_is_still_a_divergence(self):
        pair = _pair(canonical=_side(state_hash="sha256:other"))
        assert SR.classify(pair).classification == "value_divergence"

    def test_an_unknown_grain_is_a_fault(self):
        assert SR.classify(_pair(grain="galaxy")).classification == "comparator_fault"

    def test_an_unknown_member_from_a_newer_build_is_not_actionable(self):
        verdict = SR.Comparison("fanciful_new_class")
        assert verdict.actionable is False
        assert verdict.severity == "critical"
        assert verdict.gate_impact == "blocks_cutover"


class TestAccounting:

    def test_conservation_closes_on_a_tally(self):
        counts = SR.tally([SR.classify(_pair()) for _ in range(5)])
        assert SR.conservation(counts)

    def test_an_unknown_member_is_counted_not_dropped(self):
        counts = SR.tally([SR.Comparison("fanciful_new_class"), SR.classify(_pair())])
        assert counts["unclassified"] == 1
        assert SR.conservation(counts)

    def test_conservation_fails_when_a_class_is_lost(self):
        counts = SR.tally([SR.classify(_pair())])
        counts["match"] = 0
        assert not SR.conservation(counts)

    def test_gate_blocking_classes_are_reported_in_vocabulary_order(self):
        blocked = SR.gate_blocked({"missing_canonical_row": 1, "cursor_divergence": 2,
                                   "late_arrival": 9, "match": 100})
        assert blocked == ["missing_canonical_row", "cursor_divergence"]

    def test_a_clean_run_blocks_nothing(self):
        assert SR.gate_blocked(SR.tally([SR.classify(_pair())])) == []

    def test_coverage_separates_exclusions_from_comparisons(self):
        block = SR.coverage({"legacy_rows_offered": 100, "compared": 80,
                             "excluded_unsettled": 10, "excluded_retention_edge": 5,
                             "excluded_sampling": 5})
        assert block["eligible"] == 85
        assert block["ratio"] == pytest.approx(80 / 85)
        assert block["accounted"] is True

    def test_unaccounted_rows_are_visible(self):
        block = SR.coverage({"legacy_rows_offered": 100, "compared": 80})
        assert block["accounted"] is False, "20 rows vanished and the block must say so"


class TestRedaction:

    @pytest.mark.parametrize("path", [
        "solution.latitude", "gps.longitude", "site.coords", "node.position",
        "bearer_token", "auth.header", "api_secret", "private.pem", "clip.payload",
        "raw.body", "audio.samples", "pcm_buffer",
    ])
    def test_restricted_paths_are_never_quoted(self, path):
        assert not SR.path_is_quotable(path)
        assert SR.redact_value(path, "sensitive") != "sensitive"

    def test_the_deny_pattern_is_the_write_sides_pattern(self):
        """One pattern for both audits. Two lists drift, and only one of them gets tested on
        the day it matters."""
        assert SR.path_is_quotable.__module__ == SR.__name__
        source = Path(SR.__file__).read_text()
        assert "_rc.VALUE_DENY_PATTERN" in source

    def test_the_read_allow_list_is_a_superset_of_the_write_one(self):
        assert set(RC.VALUE_ALLOW_PATHS) <= set(SR.READ_VALUE_ALLOW_PATHS)

    def test_an_unknown_path_defaults_to_redacted(self):
        value = SR.redact_value("some.future.field", "plain")
        assert isinstance(value, dict) and "redacted" in value

    def test_key_id_stays_attributable(self):
        """An opaque rotation label is how a credential change is audited; redacting it
        would make rotation unauditable."""
        assert not RC.VALUE_DENY_PATTERN.search("key_id")

    def test_a_blind_difference_carries_no_value_at_all(self):
        """Not even a hash: a stable digest of a coordinate is still a stable identifier
        for that coordinate."""
        diff = SR.Difference("solution.latitude", 12.5, 12.6, comparator="blind").to_dict()
        assert diff["legacy"] is None and diff["canonical"] is None
        assert diff["blind"] is True and diff["quoted"] is False
        assert "12.5" not in json.dumps(diff) and "12.6" not in json.dumps(diff)

    def test_a_presence_comparison_is_equally_blind(self):
        diff = SR.Difference("gps.position", "a", "b", comparator="presence").to_dict()
        assert diff["legacy"] is None and diff["canonical"] is None

    def test_an_allowed_scalar_is_quoted(self):
        assert SR.redact_value("page_size", 50) == 50
        assert SR.redact_value("evidence_class", "detection") == "detection"

    def test_a_quotable_path_with_a_structured_value_is_still_redacted(self):
        value = SR.redact_value("page_size", {"nested": 1})
        assert isinstance(value, dict) and "redacted" in value

    def test_filter_values_in_a_receipt_go_through_redaction(self):
        receipt = SR.build_receipt(
            run_id="r", window_start="a", window_end="b", as_of="c", emitted_at="d",
            query=_query(filters={"near_latitude": "51.5"}), pair=_pair(),
            comparison=SR.classify(_pair()))
        assert "51.5" not in json.dumps(receipt["query"]["filters"])

    def test_an_unknown_comparator_is_refused(self):
        with pytest.raises(ValueError):
            SR.Difference("a", 1, 2, comparator="vibes")


class TestSampling:

    def test_mismatches_are_never_sampled(self):
        for name in SR.MISMATCH_CLASSIFICATIONS:
            decision = SR.sample_receipt(name, "r:any", denominator=10_000)
            assert decision["sampled"] is True
            assert decision["rate_denominator"] == 1

    def test_match_sampling_is_deterministic(self):
        first = SR.sample_receipt("match", "r:stable")
        second = SR.sample_receipt("match", "r:stable")
        assert first == second

    def test_a_broken_denominator_samples_everything(self):
        """Failing open costs storage; failing closed costs the evidence the comparator
        exists to produce."""
        for bad in (0, -5, None, "many"):
            assert SR.sample_receipt("match", "r:x", denominator=bad)["sampled"] is True

    def test_aggregate_classes_are_never_sampled(self):
        for name in SR.EXHAUSTIVE_CLASSES:
            assert SR.coverage_mode(name) == "exhaustive"
            assert SR.sample_query(name, "q:any")["selected"] is True

    def test_row_classes_are_sampled_by_query(self):
        assert SR.coverage_mode("detection") == "sampled"
        decision = SR.sample_query("detection", "q:abc", denominator=50)
        assert decision["mode"] == "sampled"
        assert decision["rate_denominator"] == 50

    def test_query_sampling_is_deterministic_so_a_rerun_repeats_the_set(self):
        assert SR.sample_query("detection", "q:abc") == SR.sample_query("detection", "q:abc")

    def test_a_forced_reason_makes_a_sampled_class_exhaustive(self):
        decision = SR.sample_query("detection", "q:abc", forced_reason="first day of a lane")
        assert decision["selected"] is True
        assert decision["mode"] == "exhaustive"

    def test_unknown_evidence_class_cannot_be_sampled_away(self):
        with pytest.raises(ValueError):
            SR.coverage_mode("telepathy")


class TestReceipt:

    def test_a_pending_comparison_cannot_produce_a_receipt(self):
        pair = _pair(canonical=_side(row_state="absent", row_count=0), age_s=10.0)
        with pytest.raises(ValueError):
            SR.build_receipt(run_id="r", window_start="a", window_end="b", as_of="c",
                             emitted_at="d", query=_query(), pair=pair,
                             comparison=SR.classify(pair))

    def test_a_receipt_is_never_queued_by_the_comparator(self):
        receipt = SR.build_receipt(
            run_id="r", window_start="a", window_end="b", as_of="c", emitted_at="d",
            query=_query(), pair=_pair(), comparison=SR.classify(_pair()))
        assert receipt["disposition"]["queued"] is False
        assert receipt["disposition"]["queue_ref"] is None

    def test_differences_beyond_the_cap_are_recorded_as_dropped(self):
        many = [SR.Difference("counters.clips_written", i, i + 1, comparator="count")
                for i in range(SR.MAX_DIFFERENCES + 7)]
        pair = _pair(differences=many)
        receipt = SR.build_receipt(
            run_id="r", window_start="a", window_end="b", as_of="c", emitted_at="d",
            query=_query(), pair=pair, comparison=SR.classify(pair), differences=many)
        assert len(receipt["differences"]) == SR.MAX_DIFFERENCES
        assert receipt["differences_dropped"] == 7

    def test_an_oversized_receipt_is_refused_not_truncated(self):
        receipt = SR.build_receipt(
            run_id="r", window_start="a", window_end="b", as_of="c", emitted_at="d",
            query=_query(), pair=_pair(), comparison=SR.classify(_pair()))
        receipt["bloat"] = "x" * (SR.MAX_RECEIPT_BYTES + 1)
        with pytest.raises(ValueError):
            SR.encode_receipt(receipt)

    def test_a_newer_major_is_refused_with_a_reason(self):
        assert SR.supported({"schema_version": SR.RECEIPT_CONTRACT_MAJOR})
        assert not SR.supported({"schema_version": SR.RECEIPT_CONTRACT_MAJOR + 1})
        assert not SR.supported({})

    def test_the_receipt_records_which_comparator_build_produced_it(self):
        receipt = SR.build_receipt(
            run_id="r", window_start="a", window_end="b", as_of="c", emitted_at="d",
            query=_query(), pair=_pair(), comparison=SR.classify(_pair()),
            comparator_build={"name": "hear-shadow-read", "version": "1.2.3",
                              "receipt_major": 1})
        assert receipt["comparator"]["version"] == "1.2.3"


class TestGeneratedArtifacts:

    def test_the_tree_matches_its_generator(self):
        assert GEN.main(["--check"]) == 0, "rerun tools/gen_shadow_read_contracts.py"

    def test_every_classification_has_a_worked_fixture(self):
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
        covered = {case["expected_classification"] for case in manifest["cases"]}
        missing = set(SR.CLASSIFICATIONS) - covered
        assert not missing, "a vocabulary member with no worked example is an unchecked claim"

    def test_the_manifest_declares_itself_staged_with_a_reason(self):
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
        assert manifest["status"] == "staged"
        assert "contracts/" in manifest["staged_reason"]
        assert manifest["source_of_truth"] == "hear/verify/shadow_read.py"
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
        """A field *path* is evidence and may be recorded; a field *value* on a denied path
        never may. A test that cannot tell the two apart is deleted the first time it blocks
        an honest path name."""
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            receipt = json.loads(path.read_text()).get("receipt")
            if not receipt:
                continue
            for difference in receipt["differences"]:
                if RC.VALUE_DENY_PATTERN.search(difference["path"]):
                    assert difference["quoted"] is False
                    assert isinstance(difference["legacy"], (dict, type(None)))
                    assert isinstance(difference["canonical"], (dict, type(None)))
                if difference["blind"]:
                    assert difference["legacy"] is None
                    assert difference["canonical"] is None

    def test_no_fixture_carries_a_credential_body_or_sample_value(self):
        """Scans receipt VALUES, not the file text: a case description is allowed to say the
        word "bearer", and a test that cannot tell prose from data gets deleted the first
        time it blocks an honest comment."""
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
                SR.encode_receipt(body["receipt"])

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
        disposition = schema["properties"]["disposition"]["properties"]
        assert disposition["queued"]["const"] is False

    def test_the_schema_pins_legacy_as_the_read_authority(self):
        schema = json.loads(SCHEMA_PATH.read_text())
        assert schema["properties"]["read_authority"]["const"] == "legacy"
        assert schema["properties"]["read_mode"]["enum"] == list(SR.SHADOW_MODES)


class TestBoundaries:
    """The comparator is an auditor: no I/O, no credential, no dependency, no write path."""

    def test_the_module_performs_no_io(self):
        tree = ast.parse(Path(SR.__file__).read_text())
        forbidden = {"open", "input", "exec", "eval", "compile", "__import__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden, node.func.id

    def test_the_module_imports_only_the_standard_library_and_the_write_side(self):
        tree = ast.parse(Path(SR.__file__).read_text())
        allowed = {"hashlib", "json", "uuid", "typing", "__future__", "hear"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] in allowed, alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] in allowed, node.module

    def test_the_contract_id_is_not_harvestable_as_a_published_contract(self):
        """`tools/freeze_contracts.py` harvests `[A-Z_]*SCHEMA[A-Z_]*` assignments as
        published contract ids. Registering an unpublished id in the frozen baseline would
        make the layout rule pass for a contract with no published artifact."""
        import re
        source = Path(SR.__file__).read_text()
        harvestable = re.findall(r'\b[A-Z_]*SCHEMA[A-Z_]*\s*=\s*["\']([A-Za-z0-9._-]+)["\']',
                                 source)
        assert SR.RECEIPT_CONTRACT_ID not in harvestable

    def test_nothing_in_the_runtime_tree_imports_the_comparator(self):
        """A design module reachable from a running reader is not a design module.

        The other **design** modules are exempt and named individually: `hear/verify/
        list_read.py` imports this module's authority constant, redaction list and time
        bounds on purpose, because two copies of "who is authoritative" drift and only one of
        them gets tested on the day it matters. Exempting the design lane by name, rather
        than by a directory glob, keeps the check failing the moment a *runtime* reader picks
        either module up.
        """
        design_lane = ("shadow_read.py", "list_read.py", "gen_shadow_read_contracts.py",
                       "gen_list_read_contracts.py", "__init__.py")
        importers = []
        for path in sorted((ROOT / "hear").rglob("*.py")) + sorted(
                (ROOT / "tools").rglob("*.py")):
            if path.name in design_lane:
                continue
            if "shadow_read" in path.read_text():
                importers.append(path.relative_to(ROOT).as_posix())
        assert importers == [], importers

    def test_the_comparator_has_no_credential_of_its_own(self):
        source = Path(SR.__file__).read_text().lower()
        for token in ("password", "api_key =", "bearer ", "secret ="):
            assert token not in source
