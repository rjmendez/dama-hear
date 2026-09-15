"""`hear.reconcile.receipt.v1`: correlation, classification, redaction and accounting.

These tests exist to make the dual-write reconciliation design falsifiable before any of it
is built. Four properties carry the weight, and each has a test that fails loudly:

1. The classification vocabulary is **closed and total** -- every pair reaches exactly one
   member, `conservation()` closes, and no input produces an "other" bucket.
2. Absence inside the grace window is `pending`, not loss. A reconciler that pages on its
   own scheduling jitter gets muted, and then the real loss arrives muted too.
3. A receipt can never quote a location, a credential or a payload body, whatever a future
   field table calls the path.
4. A mismatch is never sampled away, and an audit sample is deterministic so coverage is
   computable rather than assumed.
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
from tools import gen_reconcile_contracts as GEN  # noqa: E402

FIXTURE_DIR = ROOT / "docs" / "phase4-dual-write-reconciliation" / "fixtures"
SCHEMA_PATH = (ROOT / "docs" / "phase4-dual-write-reconciliation"
               / (RC.RECEIPT_CONTRACT_ID + ".schema.json"))

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
        "outcome": "written",
        "row_count": 1,
        "state_hash": RC.state_hash({"counters.dets_rows_written": 7}),
        "written_at": "2026-09-15T00:30:00Z",
        "device_id": "gold",
        "site_id": SITE,
        "principal_id": "node:gold",
        "credential_scope": "device",
        "key_id": "k-2026-09",
    }
    base.update(over)
    return base


def _pair(**over):
    base = {"legacy": _side(), "canonical": _side(),
            "age_s": 10.0, "grace_s": RC.LIVE_GRACE_S}
    base.update(over)
    return base


def _correlation(**over):
    base = {
        "correlation_id": RC.correlation_id(legacy_uid="a" * 64, event_id=None,
                                            site_id=SITE, device_id="gold",
                                            telemetry_path="hear/heartbeat"),
        "binding": "bound",
        "legacy_uid": "a" * 64,
        "event_id": "11111111-1111-5111-8111-111111111111",
        "site_id": SITE,
        "device_id": "gold",
        "telemetry_path": "hear/heartbeat",
        "source": "node-http",
    }
    base.update(over)
    return base


def _receipt(pair=None, comparison=None, **over):
    pair = pair or _pair()
    comparison = comparison or RC.classify(pair)
    kwargs = {
        "run_id": "run-1",
        "window_start": "2026-09-15T00:00:00Z",
        "window_end": "2026-09-15T01:00:00Z",
        "emitted_at": "2026-09-15T01:00:30Z",
        "correlation": _correlation(),
        "pair": pair,
        "comparison": comparison,
    }
    kwargs.update(over)
    return RC.build_receipt(**kwargs)


class TestVocabulariesAreClosed:
    def test_every_classification_has_a_severity_and_a_repair_action(self):
        """A member with no severity routes nowhere and a member with no repair action
        leaves an operator guessing. Both maps must be total over the vocabulary."""
        assert set(RC.SEVERITY_BY_CLASSIFICATION) == set(RC.CLASSIFICATIONS)
        assert set(RC.REPAIR_BY_CLASSIFICATION) == set(RC.CLASSIFICATIONS)
        assert set(RC.SEVERITY_BY_CLASSIFICATION.values()) <= set(RC.SEVERITIES)
        assert set(RC.REPAIR_BY_CLASSIFICATION.values()) <= set(RC.REPAIR_ACTIONS)

    def test_repair_actions_never_mutate_either_side(self):
        """The repair boundary is replay-only. An action that edits, deletes or re-derives a
        row would make the comparator a writer to the thing it audits."""
        assert RC.REPAIR_ACTIONS == ("none", "replay_inbox", "replay_outbox",
                                     "manual_review")
        forbidden = re.compile(r"(?:edit|delete|rewrite|patch|upsert|derive|backfill)",
                               re.IGNORECASE)
        assert not [a for a in RC.REPAIR_ACTIONS if forbidden.search(a)]

    def test_pending_is_the_only_non_terminal_member(self):
        assert RC.NON_TERMINAL == ("pending",)
        assert set(RC.TERMINAL) | set(RC.NON_TERMINAL) == set(RC.CLASSIFICATIONS)
        assert "match" not in RC.MISMATCH_CLASSIFICATIONS

    def test_undecidable_is_louder_than_wrong(self):
        """`unclassified` is critical on purpose. A comparator that cannot classify has
        stopped being evidence, and a quiet unclassified bucket is how a dashboard goes
        green over a hole."""
        assert RC.SEVERITY_BY_CLASSIFICATION["unclassified"] == "critical"


class TestCorrelationIdentity:
    def test_legacy_owns_the_key_whenever_a_legacy_row_exists(self):
        """Legacy is authoritative for all of Phase 4, so an authoritative record must not
        change identity when the claimant appears."""
        without = RC.correlation_id(legacy_uid="a" * 64, event_id=None, site_id=SITE,
                                    device_id="gold", telemetry_path="hear/heartbeat")
        with_canonical = RC.correlation_id(legacy_uid="a" * 64, event_id="11111111-1111",
                                           site_id=SITE, device_id="gold",
                                           telemetry_path="hear/heartbeat")
        assert without == with_canonical
        assert without.startswith("legacy:")

    def test_a_canonical_only_observation_gets_a_canonical_key(self):
        cid = RC.correlation_id(legacy_uid=None, event_id="11111111-1111", site_id=SITE,
                                device_id="gold", telemetry_path="hear/heartbeat")
        assert cid.startswith("canonical:")

    def test_the_two_namespaces_cannot_collide(self):
        """A legacy `record_uid` and an `event_id` are different identity schemes. Folding
        them into one unprefixed key would let one shadow the other."""
        shared = "a" * 64
        legacy = RC.correlation_id(legacy_uid=shared, event_id=None, site_id=SITE,
                                   device_id="gold", telemetry_path="hear/heartbeat")
        canonical = RC.correlation_id(legacy_uid=None, event_id=shared, site_id=SITE,
                                      device_id="gold", telemetry_path="hear/heartbeat")
        assert legacy != canonical
        assert legacy.split(":", 1)[1] != canonical.split(":", 1)[1]

    def test_site_device_and_path_are_all_identity_inputs(self):
        base = dict(legacy_uid="a" * 64, event_id=None, site_id=SITE, device_id="gold",
                    telemetry_path="hear/heartbeat")
        seen = {RC.correlation_id(**base)}
        for field, value in (("site_id", "site-beta"), ("device_id", "kasami"),
                             ("telemetry_path", "hear/event")):
            seen.add(RC.correlation_id(**dict(base, **{field: value})))
        assert len(seen) == 4

    @pytest.mark.parametrize("kwargs", [
        dict(legacy_uid=None, event_id=None),
    ])
    def test_a_pair_with_no_identity_at_all_is_refused(self, kwargs):
        with pytest.raises(ValueError):
            RC.correlation_id(site_id=SITE, device_id="gold",
                              telemetry_path="hear/heartbeat", **kwargs)
        with pytest.raises(ValueError):
            RC.binding_of(**kwargs)

    def test_binding_names_which_side_has_an_identity(self):
        assert RC.binding_of(legacy_uid="a", event_id="b") == "bound"
        assert RC.binding_of(legacy_uid="a", event_id=None) == "legacy_orphan"
        assert RC.binding_of(legacy_uid=None, event_id="b") == "canonical_orphan"

    def test_a_rerun_of_a_window_converges_instead_of_duplicating(self):
        """Receipt identity is deterministic on (run, correlation), so re-running a window
        after a comparator crash produces the same receipt ids, not a second copy."""
        cid = _correlation()["correlation_id"]
        assert RC.receipt_id(run_id="r1", correlation=cid) == \
            RC.receipt_id(run_id="r1", correlation=cid)
        assert RC.receipt_id(run_id="r1", correlation=cid) != \
            RC.receipt_id(run_id="r2", correlation=cid)


class TestCredentialBinding:
    def test_no_secret_material_reaches_a_receipt(self):
        """`principal_id` is the enrolled identity and `key_id` is an opaque rotation label.
        A token, header or key body has no field to live in."""
        binding = RC.credential_binding(
            _side(principal_id="node:gold", key_id="k-1", token="hunter2"),
            _side(principal_id="node:gold", key_id="k-1", token="hunter2"),
        )
        encoded = json.dumps(binding)
        assert "hunter2" not in encoded
        assert set(binding["legacy"]) == {"principal_id", "scope", "key_id"}

    def test_a_disagreeing_principal_is_never_reported_as_matched(self):
        binding = RC.credential_binding(_side(principal_id="node:gold"),
                                        _side(principal_id="node:kasami"))
        assert binding["matched"] is False

    def test_a_missing_principal_is_not_a_match(self):
        """`None == None` must not read as agreement: two sides that recorded no principal
        have not agreed on anything, and Phase 4 must not learn to accept that."""
        binding = RC.credential_binding(_side(principal_id=None), _side(principal_id=None))
        assert binding["matched"] is False

    def test_an_unknown_scope_degrades_to_unknown_rather_than_being_trusted(self):
        binding = RC.credential_binding(_side(credential_scope="root"), _side())
        assert binding["legacy"]["scope"] == "unknown"
        assert binding["matched"] is False


class TestClassification:
    def test_two_agreeing_sides_match(self):
        assert RC.classify(_pair()).classification == "match"

    def test_absence_inside_the_grace_window_is_pending_not_loss(self):
        pair = _pair(canonical={"outcome": "absent"}, age_s=30.0,
                     grace_s=RC.LIVE_GRACE_S)
        assert RC.classify(pair).classification == "pending"

    def test_the_same_absence_after_the_window_is_missing_canonical(self):
        pair = _pair(canonical={"outcome": "absent"}, age_s=RC.LIVE_GRACE_S + 1)
        verdict = RC.classify(pair)
        assert verdict.classification == "missing_canonical"
        assert verdict.severity == "critical"
        assert verdict.repair_action == "replay_inbox"

    def test_a_backfill_lag_is_pending_on_the_backfill_window(self):
        """A drain replaying an SD backlog is normal. Judging it on the live window would
        page on every drain run, which is how a real alert gets turned off."""
        pair = _pair(canonical={"outcome": "absent"}, age_s=21_600.0,
                     grace_s=RC.BACKFILL_GRACE_S)
        assert RC.classify(pair).classification == "pending"

    def test_identity_divergence_is_judged_before_anything_else(self):
        """A pair that is not about the same producer is not a pair. Every later comparison
        would be meaningless, so identity is checked first even when the row also
        duplicates, diverges and errors."""
        pair = _pair(
            legacy=_side(device_id="gold", principal_id="node:gold"),
            canonical=_side(device_id="kasami", principal_id="node:kasami", row_count=3,
                            state_hash="sha256:different"),
            differences=[RC.Difference("counters.clips_written", 1, 2, comparator="count")],
        )
        assert RC.classify(pair).classification == "identity_divergence"

    def test_an_error_beats_an_absence(self):
        """'The write failed and we know it' is evidence; 'nothing is there' is only the
        absence of evidence, and the two must not be reported as the same thing."""
        pair = _pair(canonical={"outcome": "errored"}, age_s=1.0)
        assert RC.classify(pair).classification == "error_canonical"

    def test_a_failed_authoritative_write_is_always_critical(self):
        pair = _pair(legacy={"outcome": "errored"})
        verdict = RC.classify(pair)
        assert verdict.classification == "error_legacy"
        assert verdict.severity == "critical"

    def test_duplication_is_judged_before_divergence(self):
        """With two canonical rows, 'which value differs' has no answer."""
        pair = _pair(canonical=_side(row_count=2, state_hash="sha256:other"))
        assert RC.classify(pair).classification == "duplicate_canonical"

    def test_duplicate_legacy_is_warn_because_legacy_always_did_this(self):
        """MQTT QoS 1 redelivery is pre-existing legacy behaviour. Calling canonical
        collapsing it a critical defect would blame the new path for the old one's shape."""
        pair = _pair(legacy=_side(row_count=2))
        verdict = RC.classify(pair)
        assert verdict.classification == "duplicate_legacy"
        assert verdict.severity == "warn"

    def test_a_canonical_only_row_is_not_reported_as_data_loss(self):
        pair = _pair(legacy={"outcome": "absent"})
        verdict = RC.classify(pair)
        assert verdict.classification == "missing_legacy"
        assert verdict.severity == "warn"

    def test_differing_outcomes_are_a_divergence_even_though_nothing_was_dropped(self):
        pair = _pair(canonical=_side(outcome="refused", reasons=["item_unrecognized"]))
        assert RC.classify(pair).classification == "outcome_divergence"

    def test_a_sequence_gap_inside_the_order_window_is_not_a_finding(self):
        """Late arrival is normal and the server never reorders. Only a gap that outlives
        the order window is evidence of anything."""
        pair = _pair(sequence_gap=True, age_s=10.0)
        assert RC.classify(pair).classification == "match"
        persistent = _pair(sequence_gap=True, age_s=RC.ORDER_GRACE_S + 1)
        assert RC.classify(persistent).classification == "order_divergence"

    def test_a_reported_difference_is_a_value_divergence(self):
        pair = _pair(differences=[RC.Difference("counters.dets_rows_written", 7, 6,
                                                comparator="count")])
        assert RC.classify(pair).classification == "value_divergence"

    def test_differing_state_hashes_with_no_reported_field_is_still_a_divergence(self):
        """The projection being incomplete is not permission to call it a match."""
        pair = _pair(canonical=_side(state_hash="sha256:other"))
        verdict = RC.classify(pair)
        assert verdict.classification == "value_divergence"
        assert "projection is incomplete" in " ".join(verdict.notes)

    def test_a_missing_state_hash_is_unclassified_not_a_match(self):
        pair = _pair(canonical=_side(state_hash=None))
        verdict = RC.classify(pair)
        assert verdict.classification == "unclassified"
        assert verdict.severity == "critical"

    def test_neither_side_present_is_unclassified(self):
        pair = _pair(legacy={"outcome": "absent"}, canonical={"outcome": "absent"})
        assert RC.classify(pair).classification == "unclassified"

    def test_an_unknown_outcome_degrades_to_absent_rather_than_being_trusted(self):
        pair = _pair(canonical={"outcome": "probably-fine"}, age_s=1.0)
        assert RC.classify(pair).classification == "pending"

    def test_classification_is_total_over_arbitrary_inputs(self):
        """No pair may fall out of the vocabulary. An 'other' bucket is where a real loss
        goes to be ignored."""
        outcomes = list(RC.SIDE_OUTCOMES) + ["nonsense"]
        for left in outcomes:
            for right in outcomes:
                for rows in (0, 1, 2):
                    for age in (1.0, 10_000.0):
                        pair = _pair(legacy=_side(outcome=left, row_count=rows),
                                     canonical=_side(outcome=right), age_s=age)
                        assert RC.classify(pair).classification in RC.CLASSIFICATIONS

    def test_an_unknown_member_from_a_newer_comparator_is_held_back_not_coerced(self):
        """Unknown is not invalid, and it is not `match` either. It is preserved and marked
        non-actionable, mirroring rule 3 of ADR 0001."""
        verdict = RC.Comparison("some_future_class")
        assert verdict.classification == "some_future_class"
        assert verdict.actionable is False
        assert verdict.severity == "critical"
        assert verdict.repair_action == "manual_review"


class TestAccounting:
    def test_conservation_closes_for_a_real_tally(self):
        comparisons = [RC.classify(_pair()) for _ in range(3)]
        comparisons.append(RC.classify(_pair(canonical={"outcome": "absent"},
                                             age_s=RC.LIVE_GRACE_S + 1)))
        comparisons.append(RC.classify(_pair(canonical={"outcome": "absent"}, age_s=1.0)))
        counts = RC.tally(comparisons)
        assert counts["compared"] == 5
        assert counts["match"] == 3
        assert counts["missing_canonical"] == 1
        assert counts["pending"] == 1
        assert RC.conservation(counts)

    def test_a_tally_that_does_not_close_is_not_evidence(self):
        counts = dict(RC.tally([RC.classify(_pair())]))
        counts["compared"] = 9
        assert RC.conservation(counts) is False

    def test_an_unknown_member_is_counted_as_unclassified_never_dropped(self):
        counts = RC.tally([RC.Comparison("some_future_class")])
        assert counts["unclassified"] == 1
        assert RC.conservation(counts)


class TestRedaction:
    @pytest.mark.parametrize("path", [
        "survey.node_latitude", "node.longitude", "gps.lat", "gps.lon", "site.coords",
        "auth.bearer_token", "headers.authorization", "device.secret",
        "credential.password", "tls.private_pem", "item.payload", "http.body",
        "clip.audio", "frame.raw", "capture.pcm",
    ])
    def test_a_denied_path_can_never_be_quoted(self, path):
        assert RC.path_is_quotable(path) is False
        rendered = RC.redact_value(path, "37.000000")
        assert isinstance(rendered, dict)
        assert rendered["redacted"].startswith("sha256:")

    def test_deny_beats_allow_even_for_an_allow_listed_name(self):
        """An allow-listed leaf under a denied parent stays denied. The other ordering is
        how one field table change reintroduces a coordinate."""
        assert RC.path_is_quotable("counters.clips_written") is True
        assert RC.path_is_quotable("payload.counters.clips_written") is False

    def test_an_unknown_path_defaults_to_redacted(self):
        """Allow list, not deny list: the next field added upstream must default to hidden,
        because the other way round it defaults to leaked."""
        assert RC.path_is_quotable("some.brand.new.field") is False

    def test_an_opaque_key_id_is_not_treated_as_key_material(self):
        """A rotation label is how a credential change is attributed; redacting it would
        make rotation unauditable, and it carries no secret."""
        assert RC.VALUE_DENY_PATTERN.search("key_id") is None

    def test_a_quoted_value_survives_only_for_allow_listed_scalars(self):
        assert RC.redact_value("counters.dets_rows_written", 4210) == 4210
        rendered = RC.redact_value("counters.dets_rows_written", {"nested": 1})
        assert rendered["redacted"].startswith("sha256:")

    def test_the_digest_is_stable_and_does_not_invert(self):
        first = RC.redact_value("gps.lat", "value")
        assert first == RC.redact_value("gps.lat", "value")
        assert first != RC.redact_value("gps.lat", "other")
        assert len(first["redacted"].split(":")[1]) == 16

    def test_a_state_hash_covers_the_projection_not_a_whole_row(self):
        """Hashing a whole stored row makes every additive field a mismatch, which is what
        makes people turn a reconciler off."""
        left = RC.state_hash({"a": 1, "b": 2})
        assert left == RC.state_hash({"b": 2, "a": 1})
        assert left != RC.state_hash({"a": 1, "b": 3})
        with pytest.raises(TypeError):
            RC.state_hash([1, 2])


class TestSampling:
    def test_a_mismatch_is_never_sampled_away(self):
        for name in RC.MISMATCH_CLASSIFICATIONS:
            decision = RC.sample_receipt(name, "legacy:abc", denominator=10_000)
            assert decision["sampled"] is True
            assert decision["rate_denominator"] == 1

    def test_matches_are_sampled_deterministically_so_coverage_is_computable(self):
        first = RC.sample_receipt("match", "legacy:abc", denominator=1_000)
        assert first == RC.sample_receipt("match", "legacy:abc", denominator=1_000)
        sampled = sum(RC.sample_receipt("match", "legacy:%d" % i,
                                        denominator=10)["sampled"] for i in range(2_000))
        assert 100 < sampled < 300

    def test_a_forced_reason_bypasses_sampling_and_records_why(self):
        decision = RC.sample_receipt("match", "legacy:abc", denominator=1_000_000,
                                     forced_reason="first hour after enabling this source")
        assert decision["sampled"] is True
        assert decision["forced_reason"] == "first hour after enabling this source"

    def test_a_nonsense_denominator_samples_everything_rather_than_nothing(self):
        """Failing open on a misconfiguration costs storage. Failing closed costs evidence,
        and the evidence is the only reason the comparator exists."""
        assert RC.sample_receipt("match", "legacy:abc", denominator=0)["sampled"] is True
        assert RC.sample_receipt("match", "legacy:abc", denominator=-5)["sampled"] is True


class TestReceipt:
    def test_a_receipt_records_a_finding_never_a_wait(self):
        pending = RC.classify(_pair(canonical={"outcome": "absent"}, age_s=1.0))
        with pytest.raises(ValueError):
            _receipt(pair=_pair(canonical={"outcome": "absent"}, age_s=1.0),
                     comparison=pending)

    def test_a_receipt_carries_the_closed_field_set(self):
        receipt = _receipt()
        required = set(RC.receipt_schema_document()["required"])
        assert required <= set(receipt)
        assert receipt["schema"] == RC.RECEIPT_CONTRACT_ID
        assert receipt["schema_version"] == RC.RECEIPT_CONTRACT_MAJOR

    def test_only_replay_dispositions_are_marked_repairable(self):
        missing = _pair(canonical={"outcome": "absent"}, age_s=RC.LIVE_GRACE_S + 1)
        assert _receipt(pair=missing)["disposition"]["repairable"] is True
        forged = _pair(canonical=_side(principal_id="node:kasami"))
        assert _receipt(pair=forged)["disposition"]["repairable"] is False

    def test_a_receipt_is_never_queued_by_the_comparator_itself(self):
        """The repair queue is an advisory work list. A comparator that enqueues its own
        repairs is a writer to the thing it audits."""
        receipt = _receipt(pair=_pair(canonical={"outcome": "absent"},
                                      age_s=RC.LIVE_GRACE_S + 1))
        assert receipt["disposition"]["queued"] is False
        assert receipt["disposition"]["queue_ref"] is None

    def test_differences_are_capped_and_the_loss_is_recorded(self):
        many = [RC.Difference("counters.clips_written", i, i + 1, comparator="count")
                for i in range(RC.MAX_DIFFERENCES + 7)]
        receipt = _receipt(pair=_pair(differences=many), differences=many)
        assert len(receipt["differences"]) == RC.MAX_DIFFERENCES
        assert receipt["differences_dropped"] == 7

    def test_a_receipt_stays_inside_its_size_bound(self):
        many = [RC.Difference("counters.clips_written", i, i + 1, comparator="count")
                for i in range(RC.MAX_DIFFERENCES)]
        encoded = RC.encode_receipt(_receipt(pair=_pair(differences=many),
                                             differences=many))
        assert len(encoded.encode("utf-8")) <= RC.MAX_RECEIPT_BYTES

    def test_an_oversized_receipt_is_refused_rather_than_truncated(self):
        receipt = dict(_receipt())
        receipt["padding"] = "x" * RC.MAX_RECEIPT_BYTES
        with pytest.raises(ValueError):
            RC.encode_receipt(receipt)

    def test_a_newer_major_is_refused_not_guessed(self):
        assert RC.supported(_receipt()) is True
        assert RC.supported(dict(_receipt(), schema_version=2)) is False
        assert RC.supported({}) is False

    def test_an_unknown_comparator_is_refused_at_construction(self):
        with pytest.raises(ValueError):
            RC.Difference("a", 1, 2, comparator="vibes")

    def test_every_comparator_declares_its_tolerance(self):
        assert set(RC.TOLERANCE_BY_COMPARATOR) == set(RC.COMPARATORS)
        assert "ignored" in RC.TOLERANCE_BY_COMPARATOR["tier"]


class TestGeneratedArtifacts:
    def test_the_checked_in_artifacts_match_their_generator(self):
        assert GEN.main(["--check"]) == 0

    def test_the_schema_is_generated_from_the_module(self):
        checked_in = json.loads(SCHEMA_PATH.read_text())
        assert checked_in == RC.receipt_schema_document()

    def test_the_schema_stays_additive_within_the_major(self):
        """An older reader must accept a newer comparator's extra field. Closing the object
        would turn every additive change into a validation failure."""
        document = RC.receipt_schema_document()
        assert document["additionalProperties"] is True
        assert document["properties"]["correlation"]["additionalProperties"] is True
        assert document["properties"]["verdict"]["additionalProperties"] is True

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
            assert body["expected_severity"] == case["expected_severity"]
            assert body["expected_repair_action"] == case["expected_repair_action"]
            assert (body["receipt"] is not None) == case["emits_receipt"]
            if body["receipt"] is not None:
                verdict = body["receipt"]["verdict"]
                assert verdict["classification"] == case["expected_classification"]
                assert verdict["severity"] == case["expected_severity"]

    def test_the_fixture_set_covers_every_classification(self):
        """A vocabulary member with no worked example is a claim nobody checked."""
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
        covered = {case["expected_classification"] for case in manifest["cases"]}
        assert covered == set(RC.CLASSIFICATIONS)

    def test_a_pending_case_emits_no_receipt(self):
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
        pending = [c for c in manifest["cases"]
                   if c["expected_classification"] == "pending"]
        assert pending
        assert all(c["emits_receipt"] is False for c in pending)

    def test_no_fixture_quotes_a_denied_path(self):
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            body = json.loads(path.read_text())
            receipt = body.get("receipt")
            if not receipt:
                continue
            for difference in receipt["differences"]:
                if RC.VALUE_DENY_PATTERN.search(difference["path"]):
                    assert difference["quoted"] is False
                    assert isinstance(difference["legacy"], (dict, type(None)))
                    assert isinstance(difference["canonical"], (dict, type(None)))

    def test_no_fixture_carries_a_credential_or_a_payload_body(self):
        """Scans receipt VALUES, not the file text: a case description is allowed to say
        the word "bearer", and a test that cannot tell prose from data gets deleted the
        first time it blocks an honest comment (`tools/recoupling_guard.py` makes the same
        argument about AST versus grep)."""
        banned = ("bearer ", "authorization:", "x-hear-token", "password",
                  "private key", "-----begin")
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            receipt = json.loads(path.read_text()).get("receipt")
            if not receipt:
                continue
            for value in _string_values(receipt):
                lowered = value.lower()
                assert not [b for b in banned if b in lowered], (path.name, value)

    def test_the_manifest_records_the_staging_reason_and_its_source_of_truth(self):
        """A staged artifact with no stated reason is indistinguishable from a misplaced
        one, and the next reader deletes it or publishes it by accident."""
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text())
        assert manifest["status"] == "staged"
        assert manifest["source_of_truth"] == "hear/ingest/reconcile.py"
        assert manifest["generator"] == "tools/gen_reconcile_contracts.py"
        assert "0005" in manifest["staged_reason"]


class TestBoundaries:
    def test_the_module_imports_only_the_standard_library(self):
        """`hear/ingest/` is the no-dependency contract core. A transport, cloud or vendor
        import here is the recoupling the layout decision exists to prevent."""
        source = (ROOT / "hear" / "ingest" / "reconcile.py").read_text()
        imports = set(re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)",
                                 source, re.M))
        assert imports <= {"hashlib", "json", "re", "uuid", "typing", "__future__"}

    def test_the_module_performs_no_io(self):
        """AST, not grep: the docstring says "no sockets" on purpose, and a text scan that
        cannot tell a promise from a call would fail on the sentence making the promise."""
        tree = ast.parse((ROOT / "hear" / "ingest" / "reconcile.py").read_text())
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called.add(func.attr)
        for banned in ("open", "connect", "request", "urlopen", "execute", "write",
                       "read_text", "write_text", "run", "Popen"):
            assert banned not in called

    def test_the_contract_id_is_deliberately_not_a_schema_constant(self):
        """`tools/freeze_contracts.py` harvests `[A-Z_]*SCHEMA[A-Z_]*` assignments as
        published contract ids. This receipt is staged, not published, so naming it that way
        would register an unpublished id in the frozen baseline."""
        source = (ROOT / "hear" / "ingest" / "reconcile.py").read_text()
        harvested = re.findall(r'\b[A-Z_]*SCHEMA[A-Z_]*\s*=\s*["\']([A-Za-z0-9._-]+)["\']',
                               source)
        assert harvested == []
