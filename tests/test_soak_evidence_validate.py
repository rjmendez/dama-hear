"""tools/soak_evidence_validate.py -- a day-1/7/14 review has to be reproducible from files alone.

Every snapshot in this file is synthetic. Nothing here reads a cluster, Redis, a database or the
network, which is the same property the module under test has to hold: a soak review taken months
later, by someone with no access to the fleet, must reach the same verdict from the same files.

The failures worth catching are the quiet ones:
  * accepted records that disappear between two snapshots with no prune to explain them
  * a node that stops publishing, or an excluded node that is simply absent instead of documented
  * a Redis heartbeat key that loses its expiry, or stops being re-armed
  * a refusal flood graded as a durability defect, or refusal caps that stopped holding
  * a SQLite ledger that was replaced and relabelled rather than pruned
  * a mid-soak ConfigMap/image/durable-env change that silently resets the semantics
"""
import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bridge_soak_evidence as B  # noqa: E402
from tools import soak_evidence_validate as V  # noqa: E402

T0_AT = B.SOAK_BASELINE_T0                 # 2026-09-15T18:26:22Z, corrected durable semantics
DAY1_AT = "2026-09-16T18:26:22Z"
DAY7_AT = "2026-09-22T18:26:22Z"
DAY14_AT = "2026-09-29T18:26:22Z"
RATE = B.DEFAULT_RECORDS_PER_DAY


def snapshot(milestone="T0", captured_at=T0_AT, records=2810, pending=0, failed=0,
             succeeded=None, oldest=T0_AT, newest=None, devices=None,
             ttl=22, refused=3, restarts=0, digest="a" * 64, image="python:3.13-slim",
             generation=7, **extra):
    """One collected snapshot file, shaped exactly as bridge_soak_evidence writes it."""
    devices = devices if devices is not None else {n: records // 5 for n in B.EXPECTED_NODES}
    outbox = {
        "available": True,
        "path": "/state/mqtt-bridge.sqlite3",
        "tables": ["cache_attempts", "durable_records"],
        "records": records,
        "pending": pending,
        "succeeded": records - pending if succeeded is None else succeeded,
        "failed": failed,
        "last_failure_at": None,
        "failed_by_target": {},
        "by_path": {"hear/heartbeat": records},
        "by_device": dict(devices),
        "schema_versions": [1],
        "oldest_record_at": oldest,
        "newest_record_at": newest or captured_at,
        "db_bytes": 4186112,
        "journal_mode": "wal",
        "sampled_at": captured_at,
    }
    snap = {
        "schema": "dama-hear/bridge-soak-evidence/v1",
        "milestone": milestone,
        "soak_baseline": B.SOAK_BASELINE_T0,
        "captured_at": captured_at,
        "window": {"started_at": captured_at, "finished_at": captured_at},
        "namespace": "dama",
        "deployment": {
            "available": True, "name": "hear-mqtt-bridge", "image": image, "container": "bridge",
            "strategy": "Recreate", "host_network": True, "generation": generation,
            "observed_generation": generation, "replicas": 1, "ready_replicas": 1,
            "unavailable_replicas": 0, "rolled_out": True,
            "durable_env": {"HEAR_DURABLE_STORE": "sqlite",
                            "HEAR_DURABLE_DB": "/state/mqtt-bridge.sqlite3",
                            "HEAR_DURABLE_REPLAY_LIMIT": "256",
                            "HEAR_DURABLE_REPLAY_INTERVAL_S": "5.0",
                            "HEAR_DURABLE_RETENTION_DAYS": "30",
                            "HEAR_DURABLE_PRUNE_INTERVAL_S": "3600"},
            "network_env": {"MQTT_HOST": "127.0.0.1", "MQTT_PORT": "31883"},
            "network_drift": {}, "volume_mounts": ["/state"],
        },
        "configmap": {"available": True, "name": "hear-mqtt-bridge-code",
                      "checksum_sha256": digest, "items": [], "annotations": {}},
        "pvc": {"available": True, "name": "hear-mqtt-bridge-state", "phase": "Bound",
                "bound": True, "capacity": "5Gi", "storage_class": "local-path",
                "volume": "pvc-94a98467"},
        "pod": {"available": True, "name": "hear-mqtt-bridge-866997c48b-4dw7n", "phase": "Running",
                "ready": True, "restarts": restarts},
        "outbox": outbox,
        "coverage": B.node_coverage(outbox, B.EXPECTED_NODES),
        "state_dir": {"available": True, "files": {}, "total_bytes": 5632 * 1024},
        "logs": {"available": True, "lines": 12, "counts": {}, "samples": {},
                 "graded": {"sqlite_locked": 0, "durable_error": 0, "traceback": 0}},
        "receiver": {"available": True, "status": "ok", "backend": "sqlite", "enabled": True,
                     "pending_records": 0, "cache_successes": 9, "cache_failures": 0,
                     "last_cache_failure_at": None},
        "refusals": {"available": True, "backend": "sqlite", "enabled": True,
                     "refused_messages": refused, "last_refusal_at": None, "max_rows": 5000,
                     "max_bytes": 16 * 1024 * 1024, "evicted_refusals": 0,
                     "suppressed_refusals": 0, "within_caps": True,
                     "separate_from_durable_health": True},
        "cache": {"available": True, "reachable": True, "authenticated": True, "dbsize": 41,
                  "error": None, "streams": {}, "key_prefix": "dama:hear:", "ttl_limit_s": 30,
                  "ttl_seconds": {n: ttl for n in B.EXPECTED_NODES},
                  "volatile": sorted(B.EXPECTED_NODES) if ttl > 0 else [],
                  "persistent": [] if ttl != -1 else sorted(B.EXPECTED_NODES),
                  "absent": [] if ttl != -2 else sorted(B.EXPECTED_NODES),
                  "over_limit": []},
        "warnings": [],
    }
    for key, value in extra.items():
        snap[key] = value
    return snap


def sample(snap):
    return V.Sample("/evidence/%s/snapshot.json" % snap["milestone"].replace("+", ""), snap)


def healthy_series():
    """T0 plus the three scheduled reviews, growing at the measured five-node rate."""
    day1 = snapshot("T+24h", DAY1_AT, records=2810 + RATE)
    day7 = snapshot("T+7d", DAY7_AT, records=2810 + RATE * 7)
    day14 = snapshot("T+14d", DAY14_AT, records=2810 + RATE * 14)
    return [sample(s) for s in (snapshot(), day1, day7, day14)]


def verdicts(result):
    return {c["name"]: c["verdict"] for c in result["checks"]}


def detail(result, name):
    return next(c["detail"] for c in result["checks"] if c["name"] == name)


# ---------------------------------------------------------------- loading

class TestLoading:
    def test_a_written_snapshot_directory_loads(self, tmp_path):
        target = tmp_path / "T0-20260915T182622Z"
        target.mkdir()
        (target / "snapshot.json").write_text(json.dumps(snapshot()))
        loaded = V.load_snapshot(str(target))
        assert loaded.milestone == "T0"
        assert loaded.label.endswith("(baseline)")

    def test_a_series_is_ordered_by_capture_time_not_argument_order(self, tmp_path):
        paths = []
        for snap in (snapshot("T+7d", DAY7_AT), snapshot(), snapshot("T+24h", DAY1_AT)):
            path = tmp_path / snap["milestone"].replace("+", "")
            path.mkdir()
            (path / "snapshot.json").write_text(json.dumps(snap))
            paths.append(str(path))
        assert [s.milestone for s in V.load_series(paths)] == ["T0", "T+24h", "T+7d"]

    def test_a_foreign_file_is_refused_rather_than_half_graded(self, tmp_path):
        path = tmp_path / "snapshot.json"
        path.write_text(json.dumps({"schema": "something/else", "milestone": "T0"}))
        with pytest.raises(V.EvidenceError):
            V.load_snapshot(str(path))

    def test_an_unreadable_file_is_an_error_not_a_pass(self, tmp_path):
        with pytest.raises(V.EvidenceError):
            V.load_snapshot(str(tmp_path / "absent.json"))

    def test_a_collection_directory_is_discovered(self, tmp_path):
        for name, snap in (("T0-20260915T182622Z", snapshot()),
                           ("T24h-20260916T182622Z", snapshot("T+24h", DAY1_AT))):
            (tmp_path / name).mkdir()
            (tmp_path / name / "snapshot.json").write_text(json.dumps(snap))
        assert len(V.discover(str(tmp_path))) == 2


# ---------------------------------------------------------------- the reproducible verdict

class TestHealthySeries:
    def test_a_clean_soak_passes_every_series_check(self):
        result = V.validate(healthy_series(), required_milestones=("T0", "T+24h", "T+7d", "T+14d"))
        assert result["verdict"] == "pass", result["failed"] + result["unknown"]

    def test_the_verdict_is_reproducible_from_the_same_files(self):
        first = V.validate(healthy_series())
        second = V.validate(healthy_series())
        assert [c["name"] for c in first["checks"]] == [c["name"] for c in second["checks"]]
        assert verdicts(first) == verdicts(second)

    def test_an_empty_series_cannot_pass(self):
        assert V.validate([])["verdict"] == "incomplete"


class TestBaselineAndMilestones:
    def test_a_snapshot_that_names_another_baseline_fails(self):
        series = healthy_series()
        series[2].snapshot["soak_baseline"] = "2026-09-15T16:48:00Z"
        result = V.validate(series)
        assert verdicts(result)["baseline_declared"] == "fail"

    def test_a_t0_taken_before_the_corrected_rollout_fails(self):
        # the pre-correction sample is not the baseline, no matter how it is labelled
        series = [sample(snapshot(captured_at="2026-09-15T13:12:44Z"))] + healthy_series()[1:]
        assert verdicts(V.validate(series))["baseline_sample_present"] == "fail"

    def test_a_day_one_review_without_its_t24h_sample_fails(self):
        series = [s for s in healthy_series() if s.milestone in ("T0", "T+7d")]
        result = V.validate(series, required_milestones=V.REVIEW_REQUIREMENTS["day-1"])
        assert verdicts(result)["required_milestones_present"] == "fail"

    def test_a_sample_labelled_as_a_milestone_it_was_not_taken_at_fails(self):
        series = healthy_series()
        series[1].snapshot["captured_at"] = "2026-09-18T18:26:22Z"  # 48h, labelled T+24h
        assert verdicts(V.validate(series))["milestone_timing"] == "fail"

    def test_two_snapshots_claiming_the_same_milestone_fail(self):
        series = healthy_series()
        series[2].snapshot["milestone"] = "T+24h"
        assert verdicts(V.validate(series))["milestone_labels_distinct"] == "fail"


# ---------------------------------------------------------------- 1. record conservation

class TestRecordConservation:
    def test_records_that_vanish_without_a_prune_fail(self):
        series = healthy_series()
        series[2].snapshot["outbox"]["records"] = 900  # smaller than T+24h, same oldest row
        result = V.validate(series)
        assert verdicts(result)["accepted_records_conserved"] == "fail"
        assert "no prune" in detail(result, "accepted_records_conserved")

    def test_a_real_prune_may_shrink_the_ledger(self):
        series = healthy_series()
        later = series[3].snapshot
        later["outbox"]["records"] = 2810                      # 30-day retention caught up
        later["outbox"]["oldest_record_at"] = "2026-09-29T00:00:00Z"
        later["outbox"]["succeeded"] = 2810
        later["coverage"] = B.node_coverage(later["outbox"], B.EXPECTED_NODES)
        assert verdicts(V.validate(series))["accepted_records_conserved"] == "pass"

    def test_a_shrinking_per_node_count_fails_even_when_the_total_grows(self):
        series = healthy_series()
        later = series[2].snapshot
        later["coverage"]["counts"]["mach"] = 1
        assert verdicts(V.validate(series))["per_node_records_conserved"] == "fail"

    def test_cumulative_cache_successes_may_not_go_backwards(self):
        series = healthy_series()
        series[2].snapshot["outbox"]["succeeded"] = 10
        assert verdicts(V.validate(series))["accepted_records_conserved"] == "fail"

    def test_pending_may_not_exceed_the_records_it_is_drawn_from(self):
        series = healthy_series()
        series[1].snapshot["outbox"]["pending"] = 10 ** 9
        assert verdicts(V.validate(series))["record_accounting_consistent"] == "fail"

    def test_acknowledged_records_must_be_backed_by_successes(self):
        series = healthy_series()
        series[1].snapshot["outbox"]["succeeded"] = 5
        assert verdicts(V.validate(series))["record_accounting_consistent"] == "fail"


# ---------------------------------------------------------------- 2. pending and failed

class TestPendingAndFailed:
    def test_a_backlog_at_any_sample_fails(self):
        series = healthy_series()
        series[2].snapshot["outbox"]["pending"] = 17
        result = V.validate(series)
        assert verdicts(result)["pending_drained_at_every_sample"] == "fail"
        assert "T+7d" in detail(result, "pending_drained_at_every_sample")

    def test_cache_failures_that_grow_after_the_baseline_fail(self):
        series = healthy_series()
        series[2].snapshot["outbox"]["failed"] = 44
        assert verdicts(V.validate(series))["cache_failures_did_not_grow"] == "fail"

    def test_failures_that_predate_the_baseline_and_stay_flat_pass(self):
        series = healthy_series()
        for s in series:
            s.snapshot["outbox"]["failed"] = 12
        assert verdicts(V.validate(series))["cache_failures_did_not_grow"] == "pass"

    def test_a_receiver_backlog_fails_independently_of_the_bridge(self):
        series = healthy_series()
        series[3].snapshot["receiver"]["pending_records"] = 4
        assert verdicts(V.validate(series))["receiver_pending_drained"] == "fail"


# ---------------------------------------------------------------- 3. five-node coverage

class TestCoverage:
    def test_the_five_expected_nodes_are_the_graded_fleet(self):
        result = V.validate(healthy_series())
        assert result["expected_nodes"] == list(B.EXPECTED_NODES)
        assert len(result["expected_nodes"]) == 5
        assert "rankine" not in result["expected_nodes"]

    def test_a_node_that_stops_publishing_fails(self):
        series = healthy_series()
        outbox = series[2].snapshot["outbox"]
        outbox["by_device"].pop("kasami")
        series[2].snapshot["coverage"] = B.node_coverage(outbox, B.EXPECTED_NODES)
        result = V.validate(series)
        assert verdicts(result)["all_expected_nodes_covered"] == "fail"
        assert "kasami" in detail(result, "all_expected_nodes_covered")

    def test_rankine_is_excluded_with_a_documented_reason_not_silently_missing(self):
        result = V.validate(healthy_series())
        assert verdicts(result)["exclusions_documented"] == "pass"
        assert "rankine" in detail(result, "exclusions_documented")
        assert "recovery" in detail(result, "exclusions_documented")

    def test_an_exclusion_without_a_reason_fails(self):
        series = healthy_series()
        series[1].snapshot["coverage"]["excluded"] = {"rankine": ""}
        assert verdicts(V.validate(series))["exclusions_documented"] == "fail"

    def test_a_snapshot_with_no_exclusion_record_fails(self):
        # five-node coverage that does not say which node is out reads as complete-fleet evidence
        series = healthy_series()
        series[1].snapshot["coverage"].pop("excluded")
        assert verdicts(V.validate(series))["exclusions_documented"] == "fail"

    def test_quietly_shrinking_the_expectation_mid_soak_fails(self):
        series = healthy_series()
        series[2].snapshot["coverage"]["expected"] = ["gold", "mach"]
        assert verdicts(V.validate(series))["expectation_stable"] == "fail"

    def test_changing_who_is_excluded_mid_soak_fails(self):
        series = healthy_series()
        series[3].snapshot["coverage"]["excluded"] = {"rankine": "offline", "gold": "offline"}
        assert verdicts(V.validate(series))["exclusions_stable"] == "fail"

    def test_an_undeclared_device_in_the_ledger_fails(self):
        series = healthy_series()
        series[2].snapshot["coverage"]["unexpected"] = ["unknown-node"]
        assert verdicts(V.validate(series))["exclusions_documented"] == "fail"


# ---------------------------------------------------------------- 4. Redis cache TTL

class TestCacheTtl:
    def test_volatile_heartbeat_keys_across_the_series_pass(self):
        result = V.validate(healthy_series())
        assert verdicts(result)["cache_ttl_volatile"] == "pass"
        assert verdicts(result)["cache_ttl_rearmed"] == "pass"

    def test_a_key_that_lost_its_expiry_fails(self):
        series = healthy_series()
        series[2].snapshot["cache"]["persistent"] = ["gold"]
        result = V.validate(series)
        assert verdicts(result)["cache_ttl_volatile"] == "fail"
        assert "lost_expiry" in detail(result, "cache_ttl_volatile")

    def test_a_ttl_above_the_configured_bound_fails(self):
        series = healthy_series()
        series[1].snapshot["cache"]["over_limit"] = ["mach"]
        assert verdicts(V.validate(series))["cache_ttl_volatile"] == "fail"

    def test_a_missing_key_for_a_node_that_is_still_writing_fails(self):
        series = healthy_series()
        series[3].snapshot["cache"]["absent"] = ["ageev"]
        result = V.validate(series)
        assert verdicts(result)["cache_ttl_volatile"] == "fail"
        assert "absent_while_writing" in detail(result, "cache_ttl_volatile")

    def test_a_key_absent_for_a_node_that_is_not_writing_is_not_a_ttl_defect(self):
        series = healthy_series()
        series[3].snapshot["cache"]["absent"] = ["rankine"]  # excluded node, not in coverage
        assert verdicts(V.validate(series))["cache_ttl_volatile"] == "pass"

    def test_keys_that_stopped_being_rearmed_fail(self):
        series = healthy_series()
        for s in series[1:]:
            s.snapshot["cache"]["volatile"] = []
        assert verdicts(V.validate(series))["cache_ttl_rearmed"] == "fail"

    def test_absent_ttl_evidence_is_unknown_never_a_pass(self):
        series = healthy_series()
        for s in series:
            s.snapshot["cache"] = {"available": True, "reachable": True, "authenticated": False,
                                   "error": "NOAUTH Authentication required."}
        result = V.validate(series)
        assert verdicts(result)["cache_ttl_volatile"] == "unknown"
        assert result["verdict"] == "incomplete"


# ---------------------------------------------------------------- 5. refusals

class TestRefusals:
    def test_refusals_within_their_caps_pass(self):
        assert verdicts(V.validate(healthy_series()))["refusal_caps_hold"] == "pass"

    def test_stored_refusals_beyond_the_row_cap_fail(self):
        series = healthy_series()
        series[2].snapshot["refusals"]["refused_messages"] = 500000
        result = V.validate(series)
        assert verdicts(result)["refusal_caps_hold"] == "fail"
        assert "cap" in detail(result, "refusal_caps_hold")

    def test_a_refusal_flood_alone_never_fails_the_durable_verdict(self):
        # refused input is dropped by validation before the outbox commit: it is a capped,
        # separate signal, not a durability defect.
        series = healthy_series()
        for s in series:
            s.snapshot["refusals"]["refused_messages"] = 5000
            s.snapshot["refusals"]["evicted_refusals"] = 120000
        result = V.validate(series)
        assert result["verdict"] == "pass", result["failed"]
        assert verdicts(result)["pending_drained_at_every_sample"] == "pass"

    def test_refusals_folded_into_the_durable_surface_fail(self):
        series = healthy_series()
        series[1].snapshot["refusals"]["separate_from_durable_health"] = False
        assert verdicts(V.validate(series))["refusals_separate_from_health"] == "fail"

    def test_refused_input_that_reached_the_durable_path_fails(self):
        series = healthy_series()
        series[1].snapshot["receiver"]["cache_failures"] = 9
        assert verdicts(V.validate(series))["refusals_separate_from_health"] == "fail"


# ---------------------------------------------------------------- 6. SQLite continuity

class TestDurableContinuity:
    def test_a_continuous_ledger_passes(self):
        result = V.validate(healthy_series())
        assert verdicts(result)["ledger_identity_stable"] == "pass"
        assert verdicts(result)["ledger_not_restarted"] == "pass"

    def test_a_moved_database_file_fails(self):
        series = healthy_series()
        series[2].snapshot["outbox"]["path"] = "/state/mqtt-bridge-2.sqlite3"
        assert verdicts(V.validate(series))["ledger_identity_stable"] == "fail"

    def test_a_journal_mode_change_fails(self):
        series = healthy_series()
        series[3].snapshot["outbox"]["journal_mode"] = "delete"
        assert verdicts(V.validate(series))["ledger_identity_stable"] == "fail"

    def test_a_new_durable_schema_version_mid_soak_fails(self):
        series = healthy_series()
        series[2].snapshot["outbox"]["schema_versions"] = [1, 2]
        assert verdicts(V.validate(series))["ledger_identity_stable"] == "fail"

    def test_a_replaced_database_is_caught_even_when_it_looks_healthy(self):
        # a fresh ledger has a plausible record count and a perfectly recent window; what gives it
        # away is that its oldest row is newer than everything the previous snapshot held.
        series = healthy_series()
        later = series[3].snapshot
        later["outbox"]["records"] = 2810
        later["outbox"]["succeeded"] = 2810
        later["outbox"]["oldest_record_at"] = "2026-09-29T17:00:00Z"
        assert verdicts(V.validate(series))["ledger_not_restarted"] == "fail"

    def test_a_rebound_state_volume_fails(self):
        series = healthy_series()
        series[2].snapshot["pvc"]["volume"] = "pvc-00000000"
        assert verdicts(V.validate(series))["state_volume_stable"] == "fail"

    def test_a_writer_restart_fails(self):
        series = healthy_series()
        series[3].snapshot["pod"]["restarts"] = 1
        assert verdicts(V.validate(series))["writer_never_restarted"] == "fail"


# ---------------------------------------------------------------- 7. semantic reset

class TestDigestStability:
    def test_a_stable_deployment_and_configmap_pass(self):
        result = V.validate(healthy_series())
        assert verdicts(result)["code_digest_stable"] == "pass"
        assert verdicts(result)["deployment_identity_stable"] == "pass"
        assert verdicts(result)["durable_semantics_stable"] == "pass"

    def test_a_configmap_change_mid_soak_voids_the_baseline(self):
        series = healthy_series()
        series[2].snapshot["configmap"]["checksum_sha256"] = "b" * 64
        result = V.validate(series)
        assert verdicts(result)["code_digest_stable"] == "fail"
        assert "new baseline" in detail(result, "code_digest_stable")

    def test_a_re_rollout_mid_soak_fails(self):
        series = healthy_series()
        series[3].snapshot["deployment"]["generation"] = 8
        series[3].snapshot["deployment"]["observed_generation"] = 8
        assert verdicts(V.validate(series))["deployment_identity_stable"] == "fail"

    def test_an_image_swap_mid_soak_fails(self):
        series = healthy_series()
        series[1].snapshot["deployment"]["image"] = "python:3.14-slim"
        assert verdicts(V.validate(series))["deployment_identity_stable"] == "fail"

    def test_changed_durable_semantics_mid_soak_fail(self):
        series = healthy_series()
        series[2].snapshot["deployment"]["durable_env"]["HEAR_DURABLE_RETENTION_DAYS"] = "7"
        result = V.validate(series)
        assert verdicts(result)["durable_semantics_stable"] == "fail"
        assert "HEAR_DURABLE_RETENTION_DAYS" in detail(result, "durable_semantics_stable")

    def test_a_tls_cutover_during_the_soak_fails(self):
        series = healthy_series()
        series[3].snapshot["deployment"]["network_drift"] = {
            "MQTT_PORT": {"want": "31883", "have": "8883"}}
        assert verdicts(V.validate(series))["durable_semantics_stable"] == "fail"


# ---------------------------------------------------------------- 8. CLI

def write_series(tmp_path, samples):
    paths = []
    for s in samples:
        directory = tmp_path / ("%s-%s" % (s.milestone.replace("+", ""),
                                           s.snapshot["captured_at"].replace(":", "")))
        directory.mkdir()
        (directory / "snapshot.json").write_text(json.dumps(s.snapshot))
        paths.append(str(directory))
    return paths


class TestCli:
    def test_a_day_one_review_is_graded_from_files(self, tmp_path, capsys):
        series = healthy_series()[:2]
        paths = write_series(tmp_path, series)
        rc = V.main(["--review", "day-1", "--format", "json", *paths])
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["verdict"] == "pass"
        assert [s["review"] for s in payload["samples"]] == ["baseline", "day-1"]

    def test_the_series_directory_is_discovered_without_listing_each_file(self, tmp_path, capsys):
        write_series(tmp_path, healthy_series())
        rc = V.main(["--series-dir", str(tmp_path), "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0 and len(payload["samples"]) == 4

    def test_the_markdown_report_names_the_baseline_and_the_exclusion(self, tmp_path, capsys):
        paths = write_series(tmp_path, healthy_series())
        V.main(paths)
        out = capsys.readouterr().out
        assert B.SOAK_BASELINE_T0 in out
        assert "rankine" in out
        assert "no cluster, cache or database was read" in out

    def test_the_default_run_only_reports(self, tmp_path, capsys):
        series = healthy_series()
        series[2].snapshot["outbox"]["pending"] = 31
        paths = write_series(tmp_path, series)
        assert V.main(paths) == 0, "reporting is the default, as in tools/fleet.py"

    def test_require_pass_gates_a_broken_series(self, tmp_path, capsys):
        series = healthy_series()
        series[2].snapshot["outbox"]["pending"] = 31
        paths = write_series(tmp_path, series)
        assert V.main(["--require-pass", *paths]) == 2

    def test_require_pass_gates_missing_evidence_too(self, tmp_path):
        series = healthy_series()
        for s in series:
            s.snapshot.pop("refusals")
        paths = write_series(tmp_path, series)
        assert V.main(["--require-pass", *paths]) == 2

    def test_an_unreadable_series_exits_nonzero_without_a_verdict(self, tmp_path, capsys):
        (tmp_path / "snapshot.json").write_text("{not json")
        rc = V.main([str(tmp_path / "snapshot.json")])
        assert rc == 1 and "error:" in capsys.readouterr().err

    def test_no_arguments_is_an_error_not_an_empty_pass(self, capsys):
        assert V.main([]) == 1

    def test_validation_never_shells_out(self, tmp_path, monkeypatch, capsys):
        # the module's whole point is that a review needs no cluster; make that enforceable.
        import subprocess

        def forbidden(*args, **kwargs):
            raise AssertionError("soak_evidence_validate must not run a subprocess")

        monkeypatch.setattr(subprocess, "run", forbidden)
        monkeypatch.setattr(subprocess, "Popen", forbidden)
        paths = write_series(tmp_path, healthy_series())
        assert V.main(["--require-pass", *paths]) == 0


# ---------------------------------------------------------------- 9. end-to-end with the collector

class TestCollectorInterop:
    def test_snapshots_written_by_the_collector_validate_as_a_series(self, tmp_path, monkeypatch):
        """The two tools have to agree on the file format, not just on the idea of one."""
        from tests import test_bridge_soak_evidence as C

        stamps = [(T0_AT, "T0", 2810), (DAY1_AT, "T+24h", 2810 + RATE)]
        for captured_at, milestone, records in stamps:
            monkeypatch.setattr(B, "utc_now_iso", lambda when=None, v=captured_at: v)
            monkeypatch.setattr(B, "utc_stamp", lambda when=None, v=captured_at: v.replace(
                "-", "").replace(":", ""))
            fake = C.FakeKubectl(outbox=C.outbox_sample(
                records=records, newest=captured_at, oldest=T0_AT,
                devices=[[n, records // 5] for n in B.EXPECTED_NODES]))
            rc = B.main(["--milestone", milestone, "--out-dir", str(tmp_path)], runner=fake)
            assert rc == 0

        samples = V.load_series(V.discover(str(tmp_path)))
        result = V.validate(samples, required_milestones=("T0", "T+24h"))
        assert [s.milestone for s in samples] == ["T0", "T+24h"]
        assert result["failed"] == [], result["failed"]
        assert "rankine" in detail(result, "exclusions_documented")
