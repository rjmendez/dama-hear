"""Phase 6 operator-read fixtures: the rules underneath a receipt, as executable cases.

`tests/test_list_read_receipt_v1.py` and `tests/test_shadow_read_receipt_v1.py` pin the
comparator contracts themselves. This module drives the hand-written case tables in
`tests/data/phase6-operator-read/`, which exist because six rules that Phase 6 depends on
had prose and a docstring but no worked, executable example:

1. **Cursors and pins.** A list answer is a function of a snapshot. Every pin shape the design
   admits, every shape it refuses, and the fact that two cursors are never comparable across
   sides, is a declared case rather than a remembered rule.
2. **Bounded exports.** The comparator never issues the unbounded `/api/export` dump. Every
   bounded-equivalence outcome and every refusal, with the coverage the refusal costs, is a
   declared case.
3. **Aggregate k-anonymity.** Closed dimensions, suppression below the floor, redaction of a
   small cell's numbers while its verdict survives, and refusal -- not blinding -- of an
   aggregate over a denied path.
4. **The never-retain field list.** `docs/phase6-read-audit-retention.md` §4.2 rendered as
   field paths, each checked against the three modules that enforce it, so "unknown paths
   default to hidden" is a test and not a hope.
5. **Receipt minimisation and retention windows.** The receipt field set is closed and
   named; the retention families are classified; and the periods themselves are explicitly
   *not* asserted, because they are an operator's decision and a test that froze a default
   would quietly make the default the decision.
6. **Legacy is still the sole read authority.** No live comparator, no reader, no route, no
   credential, no audit table, no I/O — asserted over the trees that run, on every fixture
   receipt in the repository, and on the published schema constants.

Everything here is offline. Nothing in this module or its fixtures opens a socket, touches a
store, reads a credential, enables a read path or authorises anything.
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

FIXTURES = ROOT / "tests" / "data" / "phase6-operator-read"

SITE = "site-alpha"
THRESHOLDS = {"max_stale_s": 900, "unfetched_window_s": 3600}
ROW_KEYS = ["r:one", "r:two", "r:three"]


def load(name):
    return json.loads((FIXTURES / name).read_text())


MANIFEST = load("manifest.json")


# --- shared builders ---------------------------------------------------------------------


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
        "result_hash": LR.result_hash([{"row_uid": k} for k in ROW_KEYS]),
        "membership_digest": LR.membership_digest(ROW_KEYS),
        "order_digest": LR.order_digest(ROW_KEYS),
        "projection_fields_digest": LR.projection_digest(("row_uid", "state")),
        "snapshot_at": "2026-09-15T02:00:00Z",
        "retention_class": "R0-derived",
        "retention_horizon_s": 90 * 86_400,
        "read_contract_major": 1,
        "projection_version": 1,
        "site_id": SITE,
        "scope_digest": LR.membership_digest([SITE, "gold"]),
        "reader": "design-only; no reader runs",
        "reader_version": "0.0.0-design",
        "reasons": [],
    }
    base.update(over)
    return base


def _pair(legacy_over=None, canonical_over=None, **over):
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
        "legacy": _side(**(legacy_over or {})),
        "canonical": _side(**dict({"pin_kind": "cursor_pin",
                                   "pin_id": "pin:canonical"},
                                  **(canonical_over or {}))),
    }
    base.update(over)
    return base


def _receipt(pair=None, comparison=None, **query_over):
    pair = pair or _pair()
    comparison = comparison or LR.classify(pair)
    return LR.build_receipt(
        run_id="run-fixture", window_start="2026-09-15T01:00:00Z",
        window_end="2026-09-15T02:00:00Z", as_of="2026-09-15T02:00:00Z",
        emitted_at="2026-09-15T02:00:45Z", query=_query(**query_over), pair=pair,
        comparison=comparison, page={"index": 0, "size": 50, "first": True},
        evidence={"legacy_result_ref": "readcompare/legacy/fixture.json",
                  "canonical_result_ref": "readcompare/canonical/fixture.json"})


def _keys(node):
    """Every mapping key at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _keys(value)


def _fixture_receipts():
    for directory in ("docs/phase6-list-read/fixtures", "docs/phase5-shadow-read/fixtures"):
        for path in sorted((ROOT / directory).glob("*.json")):
            if path.name == "manifest.json":
                continue
            receipt = json.loads(path.read_text()).get("receipt")
            if receipt is not None:
                yield path, receipt


def _ids(cases):
    return [case["name"] for case in cases]


# --- the fixture set itself --------------------------------------------------------------


class TestFixtureSet:
    """The set describes itself, and the description is checked."""

    def test_every_declared_file_exists_and_every_file_is_declared(self):
        declared = {entry["file"] for entry in MANIFEST["files"]}
        on_disk = {p.name for p in FIXTURES.glob("*.json") if p.name != "manifest.json"}
        assert declared == on_disk

    def test_every_declared_case_key_is_present_and_non_empty(self):
        for entry in MANIFEST["files"]:
            body = load(entry["file"])
            for key in entry["case_keys"]:
                assert body.get(key), (entry["file"], key)

    def test_the_set_declares_itself_authored_rather_than_generated(self):
        """`tools/gen_list_read_contracts.py --check` owns docs/phase6-list-read/fixtures.
        A hand-written case landing in that tree would fail CI for a reason nobody could
        read, so the two sets are kept apart and the separation is asserted here."""
        assert MANIFEST["generated"] is False and MANIFEST["generator"] is None
        assert FIXTURES.is_relative_to(ROOT / "tests")
        assert not FIXTURES.is_relative_to(ROOT / "tests" / "fixtures"), (
            "tests/fixtures is inventoried by tools/freeze_contracts.py; a case table landing "
            "there moves a frozen hash for no contract reason")

    def test_the_set_claims_to_enable_nothing(self):
        assert MANIFEST["read_authority"] == "legacy"
        assert "no route" in MANIFEST["enables"]

    def test_every_source_module_and_doc_referenced_by_the_set_exists(self):
        for relative in MANIFEST["source_modules"] + MANIFEST["source_docs"]:
            assert (ROOT / relative).exists(), relative

    def test_no_fixture_in_this_set_carries_a_credential_or_a_clip_body(self):
        """Scans for an actual secret or payload, not for the words.

        These fixtures are *about* tokens, passwords and clip bytes, so a scan that cannot
        tell a field path from a credential would block the only file whose job is to name
        them. What must never appear is a real one: a PEM block, an audio data URI, or a
        header line with a value after it.
        """
        banned = ("-----begin", "data:audio/", "authorization: bearer",
                  "x-hear-token:", "password:", "password=")
        for path in sorted(FIXTURES.glob("*.json")):
            lowered = path.read_text().lower()
            assert not [b for b in banned if b in lowered], path.name


# --- 1. cursors and pins -----------------------------------------------------------------

CURSORS = load("cursor-pins.json")


class TestCursorAndPinRefusal:
    """No pin, no comparison. And a cursor is a handle, never a fact about the other side."""

    @pytest.mark.parametrize("case", CURSORS["pin_cases"], ids=_ids(CURSORS["pin_cases"]))
    def test_pin_acceptance_matches_the_declared_case(self, case):
        side = _side(**case["side_overrides"])
        assert LR.pin_ok(side) is case["pin_ok"], case["name"]
        if case["assert_pinned"] is None:
            with pytest.raises(ValueError):
                LR.assert_pinned(side, name=case["name"])
        else:
            assert LR.assert_pinned(side, name=case["name"]) == case["assert_pinned"]

    @pytest.mark.parametrize("case", CURSORS["cursor_opacity_cases"],
                             ids=_ids(CURSORS["cursor_opacity_cases"]))
    def test_a_cursor_is_only_ever_an_opaque_handle(self, case):
        assert LR.cursor_opaque(case["token"]) is case["opaque"]

    @pytest.mark.parametrize("case", CURSORS["cursor_comparability_cases"],
                             ids=_ids(CURSORS["cursor_comparability_cases"]))
    def test_two_cursors_are_never_comparable_across_sides(self, case):
        assert LR.cursors_comparable(case["left"], case["right"]) is False

    @pytest.mark.parametrize("case", CURSORS["classify_cases"],
                             ids=_ids(CURSORS["classify_cases"]))
    def test_the_verdict_matches_the_declared_case(self, case):
        pair = _pair(legacy_over=case["legacy_overrides"],
                     canonical_over=case["canonical_overrides"],
                     **case["pair_overrides"])
        verdict = LR.classify(pair)
        assert verdict.classification == case["expected_classification"], case["name"]
        assert verdict.severity == case["expected_severity"]
        assert verdict.gate_impact == case["expected_gate_impact"]

    def test_a_pin_is_bounded_by_the_cursor_lifetime_the_api_boundary_already_states(self):
        assert LR.PIN_TTL_S == SR.CURSOR_TTL_S == 86_400

    def test_no_receipt_carries_a_cursor_token(self):
        receipt = _receipt()
        for side in receipt["sides"].values():
            assert "cursor_token" not in side
            assert side["pin_id"] == "pin:detection_list" or side["pin_id"] == "pin:canonical"
        for _, fixture in _fixture_receipts():
            assert "cursor_token" not in set(_keys(fixture))

    def test_page_identity_is_scoped_to_a_pin_so_page_zero_is_not_one_page(self):
        first = LR.page_fingerprint(fingerprint="q:x", pin_id="pin-1", page_index=0,
                                    page_size=50)
        second = LR.page_fingerprint(fingerprint="q:x", pin_id="pin-2", page_index=0,
                                     page_size=50)
        assert first != second


# --- 2. bounded exports ------------------------------------------------------------------

EXPORTS = load("export-bounds.json")


class TestBoundedExport:
    """Unbounded work is refused, and the refusal is accounted for."""

    @pytest.mark.parametrize("case", EXPORTS["equivalence_cases"],
                             ids=_ids(EXPORTS["equivalence_cases"]))
    def test_equivalence_outcome_matches_the_declared_case(self, case):
        result = LR.export_equivalence(**case["kwargs"])
        for key, value in case["expected"].items():
            assert result[key] == value, (case["name"], key)
        if case["reason_contains"] is None:
            assert result["reason"] is None
        else:
            assert case["reason_contains"] in result["reason"], case["name"]

    @pytest.mark.parametrize("case", EXPORTS["classify_cases"],
                             ids=_ids(EXPORTS["classify_cases"]))
    def test_an_export_verdict_carries_its_declared_gate_impact(self, case):
        pair = _pair(grain="export", surface="annotation_export", export=case["export"])
        verdict = LR.classify(pair)
        assert verdict.classification == case["expected_classification"], case["name"]
        assert verdict.gate_impact == case["expected_gate_impact"]
        assert verdict.severity == case["expected_severity"]

    @pytest.mark.parametrize("case", EXPORTS["coverage_cases"],
                             ids=_ids(EXPORTS["coverage_cases"]))
    def test_coverage_names_what_the_comparator_did_not_read(self, case):
        block = LR.coverage(case["counts"])
        for key, value in case["expected"].items():
            assert block[key] == value, (case["name"], key)

    def test_the_declared_bounds_are_the_modules_bounds(self):
        bounds = EXPORTS["bounds"]
        assert LR.MAX_EXPORT_ROWS_COMPARED == bounds["max_export_rows_compared"]
        assert LR.EXPORT_SLICE_ROWS == bounds["export_slice_rows"]
        assert LR.EXPORT_SLICE_ROWS <= LR.MAX_EXPORT_ROWS_COMPARED

    def test_a_refused_export_is_the_only_export_outcome_that_blocks_nothing(self):
        """A read the comparator may not issue is coverage loss, not a store defect. Every
        other export outcome is a finding about the data."""
        assert LR.GATE_IMPACT_BY_CLASSIFICATION["export_bound_exceeded"] == "none"
        assert LR.GATE_IMPACT_BY_CLASSIFICATION["export_divergence"] == "blocks_cutover"

    def test_the_comparator_never_names_the_unbounded_legacy_export(self):
        tree = ast.parse(Path(LR.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value != "/api/export"


# --- 3. aggregates -----------------------------------------------------------------------

AGGREGATES = load("aggregate-k-anonymity.json")


class TestAggregatePrivacy:
    """Counts, not people -- and a refusal where a summary would be a disclosure."""

    def test_the_declared_floor_is_the_modules_floor(self):
        assert AGGREGATES["k"] == LR.MIN_CELL_COUNT

    def test_the_floor_is_still_an_operator_decision(self):
        """k = 5 is the design default, not a ratified number (§14-D10). The fixture says so
        rather than presenting the default as a decision somebody made."""
        assert AGGREGATES["k_is_decided"] is False
        doc = (ROOT / "docs" / "phase6-read-audit-retention.md").read_text()
        assert "**%s**" % AGGREGATES["k_decision_ref"] in doc

    @pytest.mark.parametrize("case", AGGREGATES["dimension_cases"],
                             ids=_ids(AGGREGATES["dimension_cases"]))
    def test_group_keys_are_closed(self, case):
        if case["accepted"] is None:
            with pytest.raises(ValueError):
                LR.assert_group_keys(case["dimensions"])
        else:
            assert LR.assert_group_keys(case["dimensions"]) == case["accepted"]

    @pytest.mark.parametrize("case", AGGREGATES["aggregatable_path_cases"],
                             ids=_ids(AGGREGATES["aggregatable_path_cases"]))
    def test_an_aggregate_over_a_denied_path_is_refused_not_blinded(self, case):
        if case["refused"]:
            with pytest.raises(ValueError):
                LR.assert_aggregatable(case["path"])
        else:
            assert LR.assert_aggregatable(case["path"]) == case["path"]

    @pytest.mark.parametrize("case", AGGREGATES["cell_key_cases"],
                             ids=_ids(AGGREGATES["cell_key_cases"]))
    def test_small_cells_lose_their_keys_and_keep_their_verdicts(self, case):
        out = LR.redact_cell_key(case["cell"], count=case["count"])
        assert out["suppressed"] is case["suppressed"], case["name"]
        assert out["values"] == case["expected_values"]
        assert out["dimensions"] == sorted(case["cell"])
        if case.get("reason_contains"):
            assert case["reason_contains"] in out["reason"]

    @pytest.mark.parametrize("case", AGGREGATES["comparison_cases"],
                             ids=_ids(AGGREGATES["comparison_cases"]))
    def test_cell_comparison_matches_the_declared_case(self, case):
        differences = LR.compare_aggregate(
            case["legacy"], case["canonical"], function=case["function"],
            tolerance=case["tolerance"], cell_counts=case["cell_counts"])
        assert len(differences) == case["expected_difference_count"], case["name"]
        for difference in differences:
            rendered = difference.to_dict()
            assert rendered["path"].startswith(case["expected_paths_start_with"])
            assert rendered["quoted"] is case["expected_quoted"]
            if case.get("expected_values"):
                assert rendered["legacy"] == case["expected_values"]["legacy"]
                assert rendered["canonical"] == case["expected_values"]["canonical"]
            elif not case["expected_quoted"]:
                for value in (rendered["legacy"], rendered["canonical"]):
                    assert value is None or isinstance(value, dict)
            if case.get("expected_tolerance"):
                assert rendered["tolerance"] == case["expected_tolerance"]

    @pytest.mark.parametrize("case", AGGREGATES["cell_reference_cases"],
                             ids=_ids(AGGREGATES["cell_reference_cases"]))
    def test_a_cell_key_never_reaches_a_difference_path(self, case):
        reference = LR.cell_ref(case["cell_key"])
        for fragment in case.get("must_not_appear_in_path", ()):
            assert fragment not in reference
        if case.get("stable"):
            assert reference == LR.cell_ref(case["cell_key"])

    @pytest.mark.parametrize("case", AGGREGATES["cap_cases"], ids=_ids(AGGREGATES["cap_cases"]))
    def test_an_aggregate_past_the_cell_cap_is_a_list(self, case):
        cells = {"cell-%04d" % i: 10 + i for i in range(case["cells"])}
        if case["refused"]:
            with pytest.raises(ValueError):
                LR.compare_aggregate(cells, cells, function="count")
        else:
            assert LR.compare_aggregate(cells, cells, function="count") == []

    @pytest.mark.parametrize("case", AGGREGATES["classify_cases"],
                             ids=_ids(AGGREGATES["classify_cases"]))
    def test_a_suppressed_cell_still_produces_its_verdict(self, case):
        pair = _pair(grain=case["grain"], surface=case["surface"],
                     differences=case["differences"])
        verdict = LR.classify(pair)
        assert verdict.classification == case["expected_classification"], case["name"]
        assert verdict.gate_impact == case["expected_gate_impact"]
        assert verdict.severity == case["expected_severity"]

    def test_a_small_cell_path_is_not_quotable_and_a_large_one_is(self):
        assert LR.path_is_quotable(LR.CELL_PATH + ".c:0123456789abcdef") is True
        assert LR.path_is_quotable(LR.SMALL_CELL_PATH + ".c:0123456789abcdef") is False


# --- 4. the never-retain field list ------------------------------------------------------

NEVER = load("never-retained-fields.json")


class TestNeverRetainedFields:
    """§4.2 of the retention design, as paths, against the code that enforces it."""

    @pytest.mark.parametrize("case", NEVER["never_retained"],
                             ids=[c["path"] for c in NEVER["never_retained"]])
    def test_the_path_is_unquotable_in_every_module_that_writes_a_receipt(self, case):
        path = case["path"]
        assert LR.path_is_quotable(path) is False, path
        assert SR.path_is_quotable(path) is False, path
        assert RC.path_is_quotable(path) is False, path

    @pytest.mark.parametrize("case", NEVER["never_retained"],
                             ids=[c["path"] for c in NEVER["never_retained"]])
    def test_the_declared_enforcement_is_the_actual_enforcement(self, case):
        """Two mechanisms hold this list, and which one holds a given path matters: a
        deny-pattern path stays refused if somebody adds it to an allow list by mistake, an
        unknown path is only safe while nobody does."""
        denied = bool(RC.VALUE_DENY_PATTERN.search(case["path"]))
        assert denied is (case["enforced_by"] == "deny_pattern"), case["path"]

    @pytest.mark.parametrize("case", NEVER["never_retained"],
                             ids=[c["path"] for c in NEVER["never_retained"]])
    def test_no_rendering_of_the_value_is_ever_literal(self, case):
        for value in ("48.1", "a note a reviewer typed", 12345):
            rendered = LR.redact_value(case["path"], value)
            assert rendered != value
            assert isinstance(rendered, dict) and "redacted" in rendered

    @pytest.mark.parametrize("case", [c for c in NEVER["never_retained"]
                                      if c["rule"] == "blind"],
                             ids=[c["path"] for c in NEVER["never_retained"]
                                  if c["rule"] == "blind"])
    def test_a_blind_family_path_has_no_durable_representation_at_all(self, case):
        """Not a value, not a hash. A stable digest of a coordinate is still a stable
        identifier for that coordinate, which is why these are compared blind."""
        rendered = LR.Difference(case["path"], "left", "right", comparator="blind").to_dict()
        assert rendered["legacy"] is None and rendered["canonical"] is None
        assert rendered["blind"] is True and rendered["quoted"] is False

    @pytest.mark.parametrize("case", NEVER["blind_rendering_cases"],
                             ids=_ids(NEVER["blind_rendering_cases"]))
    def test_every_blind_comparator_carries_no_values(self, case):
        assert case["comparator"] in LR.BLIND_COMPARATORS
        rendered = LR.Difference(case["path"], 1, 2, comparator=case["comparator"]).to_dict()
        assert rendered["legacy"] is None and rendered["canonical"] is None

    @pytest.mark.parametrize("case", NEVER["still_quotable"],
                             ids=[c["path"] for c in NEVER["still_quotable"]])
    def test_the_allow_list_is_narrow_but_not_empty(self, case):
        """A deny list with nothing on the other side of it produces receipts nobody can
        act on, which is its own kind of failure."""
        assert LR.path_is_quotable(case["path"]) is True, case["path"]

    @pytest.mark.parametrize("case", NEVER["filter_redaction_cases"],
                             ids=_ids(NEVER["filter_redaction_cases"]))
    def test_raw_filter_values_never_reach_a_receipt(self, case):
        receipt = _receipt(filters=case["filters"])
        filters = receipt["query"]["filters"]
        for key in case["quoted_keys"]:
            assert filters[key] == case["filters"][key]
        for key in case["redacted_keys"]:
            assert filters[key] != case["filters"][key]
            assert isinstance(filters[key], dict) and "redacted" in filters[key]

    def test_the_allow_lists_are_inherited_rather_than_copied(self):
        """A tightening upstream must be inherited here; a copy is a tightening that is lost
        the first time somebody edits one file."""
        assert set(RC.VALUE_ALLOW_PATHS) <= set(SR.READ_VALUE_ALLOW_PATHS)
        assert set(SR.READ_VALUE_ALLOW_PATHS) <= set(LR.VALUE_ALLOW_PATHS)

    def test_one_deny_pattern_governs_all_three_modules(self):
        assert SR._rc.VALUE_DENY_PATTERN is RC.VALUE_DENY_PATTERN
        assert LR._rc.VALUE_DENY_PATTERN is RC.VALUE_DENY_PATTERN

    def test_no_fixture_receipt_in_the_repository_quotes_a_denied_path(self):
        for path, receipt in _fixture_receipts():
            for difference in receipt["differences"]:
                if RC.VALUE_DENY_PATTERN.search(difference["path"]):
                    assert difference["quoted"] is False, path.name
                    assert isinstance(difference["legacy"], (dict, type(None))), path.name


# --- 5. receipt minimisation -------------------------------------------------------------

MINIMISATION = load("receipt-minimisation.json")


class TestReceiptMinimisation:
    """A receipt is an index into evidence, and the index is a closed field set."""

    def test_the_readcompare_receipt_carries_exactly_the_declared_fields(self):
        receipt = _receipt()
        declared = MINIMISATION["readcompare"]
        assert sorted(receipt) == sorted(declared["top_level"])
        assert sorted(receipt["query"]) == sorted(declared["query"])
        assert sorted(receipt["sides"]["legacy"]) == sorted(declared["side"])
        assert sorted(receipt["sides"]["canonical"]) == sorted(declared["side"])
        assert sorted(receipt["window"]) == sorted(declared["window"])
        assert sorted(receipt["disposition"]) == sorted(declared["disposition"])
        assert sorted(receipt["evidence"]) == sorted(declared["evidence"])
        assert sorted(receipt["verdict"]) == sorted(declared["verdict"])

    def test_the_shadowread_receipt_carries_exactly_the_declared_fields(self):
        declared = MINIMISATION["shadowread"]
        for path, receipt in _fixture_receipts():
            if receipt["schema"] != declared["contract"]:
                continue
            assert sorted(receipt) == sorted(declared["top_level"]), path.name
            assert sorted(receipt["sides"]["legacy"]) == sorted(declared["side"]), path.name

    @pytest.mark.parametrize("case", MINIMISATION["forbidden_key_names"],
                             ids=[c["key"] for c in MINIMISATION["forbidden_key_names"]])
    def test_a_forbidden_key_name_appears_in_no_receipt_anywhere(self, case):
        assert case["key"] not in set(_keys(_receipt()))
        for path, receipt in _fixture_receipts():
            assert case["key"] not in set(_keys(receipt)), (path.name, case["key"])

    def test_the_declared_bounds_are_the_modules_bounds(self):
        bounds = MINIMISATION["bounds"]
        assert LR.MAX_RECEIPT_BYTES == bounds["max_receipt_bytes"]
        assert LR.MAX_DIFFERENCES == bounds["max_differences"]

    def test_an_oversized_receipt_is_refused_rather_than_truncated(self):
        assert MINIMISATION["bounds"]["oversize_is_refused_not_truncated"] is True
        receipt = dict(_receipt())
        receipt["coverage"] = {"padding": "x" * (LR.MAX_RECEIPT_BYTES + 1)}
        with pytest.raises(ValueError):
            LR.encode_receipt(receipt)

    def test_differences_past_the_cap_are_counted_rather_than_dropped_silently(self):
        pair = _pair()
        many = [LR.Difference("aggregate.value.c:%016x" % i, i, i + 1, comparator="count")
                for i in range(LR.MAX_DIFFERENCES + 7)]
        receipt = LR.build_receipt(
            run_id="run-fixture", window_start="a", window_end="b", as_of="c",
            emitted_at="d", query=_query(), pair=pair, comparison=LR.classify(pair),
            differences=many)
        assert len(receipt["differences"]) == LR.MAX_DIFFERENCES
        assert receipt["differences_dropped"] == 7

    @pytest.mark.parametrize("case", [c for c in MINIMISATION["invariants"]
                                      if c.get("path")],
                             ids=[c["name"] for c in MINIMISATION["invariants"]
                                  if c.get("path")])
    def test_the_declared_receipt_invariants_hold(self, case):
        node = _receipt()
        for part in case["path"].split("."):
            node = node[part]
        if case.get("kind") == "reference":
            assert isinstance(node, str) and node.endswith(".json")
        else:
            assert node == case["value"]

    def test_a_pending_comparison_produces_no_receipt(self):
        pair = _pair(age_s=60.0,
                     canonical_over={"row_state": "absent", "row_count": 0})
        assert LR.classify(pair).classification == "pending"
        with pytest.raises(ValueError):
            LR.build_receipt(run_id="r", window_start="a", window_end="b", as_of="c",
                             emitted_at="d", query=_query(), pair=pair,
                             comparison=LR.classify(pair))

    def test_every_fixture_receipt_in_the_repository_fits_its_size_bound(self):
        for path, receipt in _fixture_receipts():
            if receipt["schema"] == LR.RECEIPT_CONTRACT_ID:
                LR.encode_receipt(receipt)
            else:
                SR.encode_receipt(receipt)


# --- 6. retention windows ----------------------------------------------------------------

RETENTION = load("retention-windows.json")
RETENTION_DOC = (ROOT / "docs" / "phase6-read-audit-retention.md").read_text()


class TestRetentionWindows:
    """Classified here, decided nowhere, enforced nowhere."""

    @pytest.mark.parametrize("case", RETENTION["families"],
                             ids=["%s-%s" % (c["family"], c["name"]) for c
                                  in RETENTION["families"]])
    def test_every_family_is_classified_as_the_design_classifies_it(self, case):
        assert case["governance_class"] in RETENTION["governance_classes"]
        assert case["doc_default_text"] in RETENTION_DOC, case["name"]
        assert case["decided"] is False
        assert "**%s**" % case["decision_ref"] in RETENTION_DOC

    def test_the_governance_classes_are_the_governance_documents_classes(self):
        governance = (ROOT / "docs" / "data-governance.md").read_text()
        for name, horizon in RETENTION["governance_classes"].items():
            assert "`%s`" % name in governance
            assert horizon in governance

    @pytest.mark.parametrize("case", RETENTION["undecided_decisions"],
                             ids=[c["ref"] for c in RETENTION["undecided_decisions"]])
    def test_every_open_decision_is_recorded_with_a_default_in_the_design(self, case):
        assert "**%s**" % case["ref"] in RETENTION_DOC, case["ref"]

    def test_no_default_period_is_asserted_as_a_decision(self):
        """RA7 makes recording a choice a gate. A test that pinned a default period would
        make the default the decision by the back door, so this set pins only that each
        period is still open and where it is written down."""
        assert all(not case["decided"] for case in RETENTION["families"])
        assert MANIFEST["undecided"]["owner"] == "operator"
        assert set(MANIFEST["undecided"]["refs"]) == {
            case["ref"] for case in RETENTION["undecided_decisions"]}

    def test_the_rt1_classification_conflict_is_still_open_in_both_places(self):
        finding = RETENTION["rt1_finding"]
        assert finding["still_open"] is True
        assert finding["ref"] in RETENTION_DOC
        sql = (ROOT / finding["sql_file"]).read_text()
        assert "p_days    integer DEFAULT %d" % finding["code_default_days"] in sql
        assert "p_days > %d" % finding["code_ceiling_days"] in sql
        api = (ROOT / "docs" / "phase6-operator-api-contract.md").read_text()
        assert "`R4-audit` | 1 y | `enforce_refusal_retention()`" in api

    @pytest.mark.parametrize("case", RETENTION["row_state_cases"],
                             ids=_ids(RETENTION["row_state_cases"]))
    def test_expiry_is_a_state_rather_than_a_missing_row(self, case):
        expired = {"row_state": "retention_expired", "row_count": 0}
        legacy = {"retention_horizon_s": case["legacy_horizon_s"]}
        canonical = {"retention_horizon_s": case["canonical_horizon_s"]}
        if case["expired_side"] == "legacy":
            legacy.update(expired)
        else:
            canonical.update(expired)
        verdict = LR.classify(_pair(legacy_over=legacy, canonical_over=canonical,
                                    age_s=case["age_s"]))
        assert verdict.classification == case["expected_classification"], case["name"]
        assert verdict.gate_impact == case["expected_gate_impact"]
        if case.get("expected_severity"):
            assert verdict.severity == case["expected_severity"]
        if case.get("expected_repair_action"):
            assert verdict.repair_action == case["expected_repair_action"]

    @pytest.mark.parametrize("case", RETENTION["retention_edge_cases"],
                             ids=_ids(RETENTION["retention_edge_cases"]))
    def test_the_retention_edge_window_is_the_record_comparators_window(self, case):
        assert SR.retention_edge(age_s=case["age_s"],
                                 horizons_s=case["horizons_s"]) is case["edge"]
        assert LR.retention_edge is SR.retention_edge
        assert LR.RETENTION_EDGE_S == SR.RETENTION_EDGE_S

    def test_retention_expired_is_a_first_class_row_state_on_both_contracts(self):
        assert "retention_expired" in LR.ROW_STATES
        assert LR.ROW_STATES == SR.ROW_STATES

    def test_a_receipt_records_the_retention_class_and_horizon_it_compared_under(self):
        side = _receipt()["sides"]["legacy"]
        assert side["retention_class"] == "R0-derived"
        assert side["retention_horizon_s"] == 90 * 86_400

    def test_nothing_in_the_tree_enforces_any_of_this_yet(self):
        """The design creates no schema, no migration, no job and no role. A quiet arrival
        would make its central claim -- that it is design only -- untestable."""
        rule = RETENTION["no_enforcement_yet"]
        migrations = ROOT / "deploy" / "postgres" / "migrations"
        existing = {p.name for p in migrations.glob("*.sql")}
        for name in rule["forbidden_migration_files"]:
            assert name not in existing, name
        offenders = []
        for tree in rule["searched_trees"]:
            root = ROOT / tree
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix not in (".py", ".sql", ".yaml", ".yml"):
                    continue
                text = path.read_text(errors="ignore")
                for identifier in rule["forbidden_identifiers"]:
                    if identifier in text:
                        offenders.append((path.relative_to(ROOT).as_posix(), identifier))
        assert offenders == [], offenders


# --- 7. legacy remains the sole read authority -------------------------------------------

AUTHORITY = load("read-authority.json")


class TestReadAuthority:
    """Nothing here is live, and that is the property under test."""

    def test_the_authority_constant_is_imported_rather_than_restated(self):
        assert LR.READ_AUTHORITY is SR.READ_AUTHORITY == AUTHORITY["authority"]["read_authority"]

    def test_the_lane_vocabulary_is_the_declared_one(self):
        assert list(LR.READ_MODES) == AUTHORITY["authority"]["read_modes"]
        assert list(LR.SHADOW_MODES) == AUTHORITY["authority"]["shadow_modes"]

    @pytest.mark.parametrize("mode", AUTHORITY["authority"]["refused_modes"])
    def test_a_mode_where_a_reader_already_moved_is_refused(self, mode):
        with pytest.raises(ValueError):
            LR.assert_shadow_only(mode)
        with pytest.raises(ValueError):
            SR.assert_shadow_only(mode)

    @pytest.mark.parametrize("mode", ["shadow", "compare"])
    def test_only_the_shadow_lanes_are_accepted(self, mode):
        assert LR.assert_shadow_only(mode) == mode

    @pytest.mark.parametrize("case", AUTHORITY["design_modules"],
                             ids=[c["module"] for c in AUTHORITY["design_modules"]])
    def test_no_runtime_module_imports_a_design_comparator(self, case):
        """A design module reachable from a running reader is not a design module."""
        stem = Path(case["module"]).stem
        allowed = set(case["importable_by"])
        importers = []
        for tree in ("hear", "tools", "modules"):
            root = ROOT / tree
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.py")):
                relative = path.relative_to(ROOT).as_posix()
                if relative == case["module"] or relative in allowed:
                    continue
                if re.search(r"\b%s\b" % stem, path.read_text()):
                    importers.append(relative)
        assert len(importers) == case["runtime_importers_allowed"], importers

    def test_no_canonical_read_route_has_appeared_in_a_tree_that_runs(self):
        rule = AUTHORITY["forbidden_in_runtime_trees"]
        offenders = []
        for tree in rule["searched_trees"]:
            root = ROOT / tree
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.py")):
                source = path.read_text()
                for node in ast.walk(ast.parse(source)):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        if node.value in rule["endpoint_literals"]:
                            offenders.append((path.relative_to(ROOT).as_posix(), node.value))
        assert offenders == [], offenders

    @pytest.mark.parametrize("module", AUTHORITY["no_auth_enforcement"]["must_not_appear_in"])
    def test_the_comparators_authenticate_nothing_and_hold_no_credential(self, module):
        source = (ROOT / module).read_text().lower()
        for token in AUTHORITY["no_auth_enforcement"]["tokens"]:
            assert token.lower() not in source, (module, token)

    @pytest.mark.parametrize("module", ["hear/verify/list_read.py", "hear/verify/shadow_read.py"])
    def test_the_comparators_perform_no_io_and_open_no_socket(self, module):
        rule = AUTHORITY["no_live_read_path"]
        tree = ast.parse((ROOT / module).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in rule["io_builtins_forbidden"], node.func.id
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in rule["network_modules_forbidden"]
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in rule["network_modules_forbidden"]
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in rule["node_endpoints_forbidden"], node.value

    @pytest.mark.parametrize("case", AUTHORITY["schema_constants"],
                             ids=["%s:%s" % (Path(c["schema"]).stem, "/".join(c["pointer"]))
                                  for c in AUTHORITY["schema_constants"]])
    def test_the_published_schemas_pin_the_authority_and_the_repair_boundary(self, case):
        node = json.loads((ROOT / case["schema"]).read_text())
        for part in case["pointer"]:
            node = node[part]
        assert node == case["value"]

    @pytest.mark.parametrize("case", AUTHORITY["manifest_claims"],
                             ids=["%s:%s" % (Path(c["manifest"]).parent.parent.name, c["key"])
                                  for c in AUTHORITY["manifest_claims"]])
    def test_the_generated_manifests_still_claim_legacy_and_staged(self, case):
        manifest = json.loads((ROOT / case["manifest"]).read_text())
        assert manifest[case["key"]] == case["value"]

    def test_every_fixture_receipt_in_the_repository_was_produced_under_legacy_authority(self):
        rule = AUTHORITY["every_emitted_receipt_declares"]
        seen = 0
        for path, receipt in _fixture_receipts():
            assert receipt["read_authority"] == rule["read_authority"], path.name
            assert receipt["read_mode"] in rule["read_mode_in"], path.name
            assert receipt["disposition"]["queued"] is False, path.name
            seen += 1
        assert seen > 0, "no fixture receipts were scanned; the scan is the evidence"

    def test_the_repair_vocabulary_contains_no_write_into_either_subject(self):
        assert set(LR.REPAIR_ACTIONS) == set(SR.REPAIR_ACTIONS)
        for action in LR.REPAIR_ACTIONS:
            assert action in ("none", "replay_inbox", "replay_outbox", "reindex_request",
                              "manual_review")

    def test_this_test_module_reads_nothing_outside_the_repository(self):
        """The suite is offline by construction: every path it opens is repository-relative,
        and it makes no network call of any kind."""
        tree = ast.parse(Path(__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in (
                        "socket", "http", "urllib", "requests", "asyncio", "psycopg")
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in (
                    "socket", "http", "urllib", "requests", "asyncio", "psycopg")
