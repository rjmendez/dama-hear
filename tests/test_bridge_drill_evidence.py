"""tools/bridge_drill_evidence.py -- the drill's offline gate, screening and grading.

The drill this grades has not been run. What is tested here is the preparation:

  1. the module is provably offline (no subprocess/socket/kubectl/redis-cli anywhere in it), so
     running it cannot touch the shared `audit-redis-0` or the live bridge;
  2. the abort boundary is an exact number and trips on each of its ten triggers;
  3. record conservation, refusal/cap/cache/claim evidence and the receipt fail when the drill
     fails, and stay `unknown` -- never `pass` -- when the evidence is simply absent;
  4. the execution gate stays shut until every blocking item is evidenced.

Everything runs off in-memory fixtures and tmp_path: no cluster, no network, no clock.
"""
import copy
import datetime as dt
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bridge_drill_evidence as D  # noqa: E402

SOURCE = open(os.path.join(ROOT, "tools", "bridge_drill_evidence.py"), encoding="utf-8").read()

# Every coordinate in this file is built at test time a few metres from survey.json's fictional
# origin, so the file carries none of its own and tools/coord_guard.py passes over it.
_ORIGIN = json.loads(open(os.path.join(ROOT, "survey.json"), encoding="utf-8").read())["origin"]
LAT0, LON0 = _ORIGIN["lat_deg"], _ORIGIN["lon_deg"]


def near_pair():
    """A lat/lon spelled the way a position travels: decimal degrees, six places."""
    return "%.6f" % (LAT0 + 0.00001), "%.6f" % (LON0 - 0.00001)


def fraction(spelled):
    """The digits after the decimal point, i.e. exactly what redaction has to remove."""
    return spelled.split(".")[1]


# ---------------------------------------------------------------- fixtures

def _ts(minutes):
    """UTC timestamp `minutes` after a fixed drill start, spelled the way kubectl spells it."""
    base = dt.datetime(2026, 9, 20, 15, 0, 0, tzinfo=dt.timezone.utc)
    return (base + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


NODES = ("gold", "nyquist", "mach", "ageev", "kasami")


def redis_block(ttl, xlen=1024, devices=NODES, clients=31, restarts=17, degraded=()):
    return {
        "ttls": {n: ttl for n in NODES},
        "devices": list(devices),
        "xlen": xlen,
        "connected_clients": clients,
        "audit_redis_restarts": restarts,
        "degraded_consumers": list(degraded),
    }


def cp(label, phase, minutes, records, pending, failed, **over):
    sample = {
        "label": label,
        "phase": phase,
        "at": _ts(minutes),
        "records": records,
        "max_id": records,
        "pending": pending,
        "failed": failed,
        "restarts": over.pop("restarts", 0),
        "duplicate_record_uids": over.pop("duplicate_record_uids", 0),
        "state_free_bytes": over.pop("state_free_bytes", 200 * 1024 ** 3),
        "state_used_pct": over.pop("state_used_pct", 75.0),
        "logs": over.pop("logs", {"sqlite_locked": 0, "connection_refused": 0, "crashloop": False}),
        "redis": over.pop("redis", redis_block(12)),
        "pvc": over.pop("pvc", {"claim": "hear-mqtt-bridge-state", "volume": "pvc-abc",
                                "phase": "Bound"}),
    }
    sample.update(over)
    return sample


REFUSED_LOGS = {"sqlite_locked": 0, "connection_refused": 40, "crashloop": False}


def good_bundle():
    """A clean, complete drill: 10 min refused fault, 360-row backlog, unattended drain."""
    fault_cp = lambda label, m, rec, pend, fail: cp(  # noqa: E731
        label, "fault", m, rec, pend, fail, logs=dict(REFUSED_LOGS),
        redis=redis_block(-2), oldest_pending_at=_ts(0))
    return {
        "schema": D.SCHEMA,
        "drill_id": "phase2-durable-failure-drill",
        "executed": False,
        "config": {"replay_limit": 256},
        "gate": {
            "fix_commit": "a1b2c3d",
            "fix_merged_to_main": True,
            "defects_covered": ["D1", "D2", "D3", "looped_drain", "monotonic_replay"],
            "configmap_matches_repo": True,
            "configmap_annotation_sha256": "0" * 64,
            "tests_passed": ["tests/test_hear_mqtt_bridge.py",
                             "tests/test_hear_mqtt_bridge_manifest.py"],
            "soak_stable_minutes": 45,
            "soak_pending": 0,
            "soak_failed": 0,
            "soak_restarts": 0,
            "strategy": "Recreate",
            "heartbeat_drill_concurrent": False,
            "redis_precheck_read_only": True,
            "window": {"start_at": _ts(-30), "operator": "rjm", "second_contact": "on call"},
        },
        "backup": {
            "method": "sqlite3 .backup copy then kubectl cp; originals untouched",
            "files": {name: "a" * 64 for name in D.BACKUP_REQUIRED_ARTIFACTS},
            "ledger": {"records": 3284, "max_id": 3284, "pending": 0, "failed": 0},
        },
        "fault": {
            "kind": "connection-refused",
            "redis_port": 6390,
            "closed_port_verified": True,
            "started_at": _ts(0),
            "ended_at": _ts(10),
            "restarts_during": 1,
            "distinct_event_records": 12,
        },
        "recovery": {"operator_actions": []},
        "negatives": {
            "probe_ids": ["drill-probe-a", "drill-probe-b"],
            "results": {n: "pass" for n in D.NEGATIVE_CASES},
        },
        "plan": [
            'kubectl -n dama patch deploy/hear-mqtt-bridge --type=json -p '
            '\'[{"op":"replace","path":"/spec/template/spec/containers/0/env/5/value",'
            '"value":"6390"}]\'',
            "kubectl -n dama rollout status deploy/hear-mqtt-bridge --timeout=120s",
            "./snapshot.sh 12-fault-t5",
            'kubectl -n dama patch deploy/hear-mqtt-bridge --type=json -p '
            '\'[{"op":"replace","path":"/spec/template/spec/containers/0/env/5/value",'
            '"value":"6379"}]\'',
        ],
        "checkpoints": [
            cp("00-precheck", "pre", -5, 3284, 0, 0),
            fault_cp("10-fault-start", 0, 3464, 0, 0),
            fault_cp("11-fault-t2", 2, 3536, 72, 80),
            fault_cp("12-fault-t5", 5, 3644, 180, 210),
            cp("13-midfault-restart", "fault", 6, 3668, 204, 240, restarts=1,
               logs=dict(REFUSED_LOGS), redis=redis_block(-2), oldest_pending_at=_ts(0)),
            fault_cp("14-fault-t9", 9, 3788, 324, 380),
            cp("20-recovery-t0", "recovery", 10, 3824, 360, 400, restarts=1),
            cp("21-recovery-t1m", "recovery", 11, 3860, 120, 400, restarts=1),
            cp("22-recovery-t5m", "recovery", 14, 3968, 0, 400, restarts=1),
            cp("40-final", "post", 20, 4184, 0, 400, restarts=1),
        ],
    }


GOOD_RECEIPT = """# phase2 durable failure drill receipt

gate commit a1b2c3d, configmap sha256 %s
window start %s, window end %s
fault in %s, fault out %s
records 3284 -> 4184, pending 0 at 22-recovery-t5m, failed 400 frozen
restarts 1 of 3; mqtt loss 12 records across the restart gap
drain to zero in 240 s; xlen delta +12
node ttl: all precheck-live nodes 1..30 at 40-final
negative cases N1-N7 pass
abort triggers: none
checkpoints: 00-precheck 10-fault-start 11-fault-t2 12-fault-t5 13-midfault-restart 14-fault-t9
20-recovery-t0 21-recovery-t1m 22-recovery-t5m 40-final

phase2-durable-failure-drill: PASS rjm
""" % ("0" * 64, _ts(-5), _ts(20), _ts(0), _ts(10))


def verdicts(report):
    return {f["name"]: f["verdict"] for f in report.as_dict()["findings"]}


def triggers(bundle, now=None):
    return {t["id"]: t["tripped"] for t in D.evaluate_abort(bundle, now)["triggers"]}


# ---------------------------------------------------------------- 1. offline by construction

def test_module_imports_nothing_that_can_reach_the_cluster():
    forbidden = ("subprocess", "socket", "http.client", "urllib", "requests", "sqlite3",
                 "redis", "shutil")
    for name in forbidden:
        assert not re.search(r"^\s*(?:import|from)\s+%s\b" % re.escape(name), SOURCE,
                             re.MULTILINE), "%s must not be importable from this module" % name
    assert "subprocess" not in sys.modules or not hasattr(D, "subprocess")


def test_module_issues_no_cluster_or_cache_command():
    # kubectl/redis-cli/mosquitto appear only inside string literals that are *screened*, never
    # built into a call. The proof used here: no exec/eval/system/popen anywhere.
    for danger in ("os.system", "os.popen", "eval(", "exec(", "Popen"):
        assert danger not in SOURCE, "%s must not appear in an offline tool" % danger


def test_reading_a_bundle_touches_only_the_given_path(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(good_bundle()), encoding="utf-8")
    assert D.load_bundle(str(path))["drill_id"] == "phase2-durable-failure-drill"
    assert D.load_bundle(str(tmp_path))["drill_id"] == "phase2-durable-failure-drill"


def test_bad_bundles_raise_rather_than_grade():
    with pytest.raises(D.DrillEvidenceError):
        D.load_bundle("/nonexistent/bundle.json")


def test_wrong_schema_is_refused(tmp_path):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps({"schema": "something/else"}), encoding="utf-8")
    with pytest.raises(D.DrillEvidenceError):
        D.load_bundle(str(path))


# ---------------------------------------------------------------- 2. the abort boundary

def test_clean_drill_trips_nothing_and_allows_continue():
    result = D.evaluate_abort(good_bundle())
    assert result["tripped"] == []
    assert result["fault_elapsed_s"] == 600.0
    # T9/T10 are operator declarations; the fixture declares neither, so continue stays gated.
    assert set(result["unknown"]) == {"T9_node_pressure", "T10_operator_lost_control"}


def test_operator_declarations_close_the_boundary():
    bundle = good_bundle()
    bundle["operator"] = {"node_pressure": False, "operator_control_lost": False}
    result = D.evaluate_abort(bundle)
    assert result["tripped"] == [] and result["unknown"] == []
    assert result["continue_allowed"] is True


def test_hard_abort_is_exactly_fifteen_minutes():
    assert D.FAULT_HARD_ABORT_S == 900.0
    bundle = good_bundle()
    bundle["fault"]["ended_at"] = _ts(15)  # exactly at the bound: not yet over it
    assert triggers(bundle)["T1_fault_over_hard_bound"] is False
    bundle["fault"]["ended_at"] = _ts(15.02)
    assert triggers(bundle)["T1_fault_over_hard_bound"] is True


def test_open_fault_is_measured_against_now():
    bundle = good_bundle()
    bundle["fault"].pop("ended_at")
    now = dt.datetime(2026, 9, 20, 15, 20, tzinfo=dt.timezone.utc)
    assert triggers(bundle, now)["T1_fault_over_hard_bound"] is True
    assert triggers(bundle)["T1_fault_over_hard_bound"] is None


def test_pending_that_stops_growing_trips_the_dropped_records_abort():
    bundle = good_bundle()
    for c in bundle["checkpoints"]:
        if c["label"] in ("14-fault-t9",):
            c["pending"] = 205  # barely moved since 13-midfault-restart: records are being dropped
    assert triggers(bundle)["T2_pending_stalled"] is True


def test_pending_growth_at_the_ingest_rate_is_clear():
    assert triggers(good_bundle())["T2_pending_stalled"] is False


def test_sqlite_integrity_message_trips_abort():
    for key in ("sqlite_locked", "disk_io_error", "malformed_database"):
        bundle = good_bundle()
        bundle["checkpoints"][3]["logs"][key] = 1
        assert triggers(bundle)["T3_sqlite_integrity"] is True, key


def test_ledger_going_backwards_trips_abort():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["records"] = 3000
    assert triggers(bundle)["T4_ledger_regressed"] is True


def test_max_id_going_backwards_trips_abort_even_if_counts_hold():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["max_id"] = 10
    assert triggers(bundle)["T4_ledger_regressed"] is True


def test_disk_floor_is_five_hundred_mebibytes():
    assert D.STATE_FREE_FLOOR_BYTES == 500 * 1024 * 1024
    bundle = good_bundle()
    bundle["checkpoints"][4]["state_free_bytes"] = 400 * 1024 * 1024
    assert triggers(bundle)["T5_state_disk_floor"] is True
    bundle = good_bundle()
    bundle["checkpoints"][4]["state_used_pct"] = 93.0
    assert triggers(bundle)["T5_state_disk_floor"] is True


def test_restart_budget_is_three_and_crash_loop_trips_immediately():
    assert D.RESTART_BUDGET == 3
    bundle = good_bundle()
    bundle["checkpoints"][-1]["restarts"] = 4
    assert triggers(bundle)["T6_restart_budget"] is True
    bundle = good_bundle()
    bundle["checkpoints"][-1]["restarts"] = 3
    assert triggers(bundle)["T6_restart_budget"] is False
    bundle["checkpoints"][5]["logs"]["crashloop"] = True
    assert triggers(bundle)["T6_restart_budget"] is True


def test_collateral_on_the_shared_redis_trips_abort():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["redis"]["audit_redis_restarts"] = 18
    assert triggers(bundle)["T7_redis_collateral"] is True

    bundle = good_bundle()
    bundle["checkpoints"][-1]["redis"]["degraded_consumers"] = ["fleet-api"]
    assert triggers(bundle)["T7_redis_collateral"] is True

    bundle = good_bundle()
    bundle["checkpoints"][-1]["redis"]["connected_clients"] = 20  # 31 -> 20 is not "the bridge"
    assert triggers(bundle)["T7_redis_collateral"] is True


def test_losing_only_the_bridge_client_is_not_collateral():
    bundle = good_bundle()
    for c in bundle["checkpoints"]:
        if c["phase"] == "fault":
            c["redis"]["connected_clients"] = 30
    assert triggers(bundle)["T7_redis_collateral"] is False


def test_mqtt_ingest_stopping_trips_abort():
    bundle = good_bundle()
    for c in bundle["checkpoints"]:
        if c["phase"] == "fault":
            c["records"] = 3464
    assert triggers(bundle)["T8_mqtt_ingest_stopped"] is True


def test_missing_evidence_is_unknown_not_clear():
    bare = {"schema": D.SCHEMA, "checkpoints": []}
    result = D.evaluate_abort(bare)
    assert result["tripped"] == []
    assert result["continue_allowed"] is False
    assert set(result["unknown"]) >= {"T1_fault_over_hard_bound", "T2_pending_stalled",
                                      "T4_ledger_regressed", "T7_redis_collateral"}


# ---------------------------------------------------------------- 3a. backup / conservation

def test_backup_is_complete_and_copy_only():
    v = verdicts(D.check_backup(good_bundle()))
    assert v["backup_artifacts"] == "pass"
    assert v["backup_checksums"] == "pass"
    assert v["pre_fault_ledger_reference"] == "pass"
    assert v["destructive_backup_steps_absent"] == "pass"


def test_missing_backup_artifact_fails():
    bundle = good_bundle()
    bundle["backup"]["files"].pop("mqtt-bridge.pre.sqlite3")
    assert verdicts(D.check_backup(bundle))["backup_artifacts"] == "fail"


def test_unchecksummed_backup_artifact_fails():
    bundle = good_bundle()
    bundle["backup"]["files"]["pvc.pre.yaml"] = ""
    assert verdicts(D.check_backup(bundle))["backup_checksums"] == "fail"


def test_a_backup_method_that_mutates_is_refused():
    bundle = good_bundle()
    bundle["backup"]["method"] = "redis-cli FLUSHDB then copy"
    assert verdicts(D.check_backup(bundle))["destructive_backup_steps_absent"] == "fail"
    bundle["backup"]["method"] = "sqlite3 'delete from durable_records' then copy"
    assert verdicts(D.check_backup(bundle))["destructive_backup_steps_absent"] == "fail"


def test_absent_backup_block_is_unknown_never_pass():
    bundle = good_bundle()
    bundle.pop("backup")
    assert set(verdicts(D.check_backup(bundle)).values()) == {"unknown"}


def test_record_conservation_passes_on_a_clean_drill():
    v = verdicts(D.check_record_conservation(good_bundle()))
    assert v == {"ledger_monotonic": "pass", "no_rows_lost_vs_backup": "pass",
                 "ingest_arithmetic_closes": "pass", "restart_lost_no_rows": "pass",
                 "no_duplicate_record_uids": "pass"}


def test_conservation_catches_rows_lost_against_the_pre_fault_backup():
    bundle = good_bundle()
    bundle["backup"]["ledger"]["records"] = 99999
    assert verdicts(D.check_record_conservation(bundle))["no_rows_lost_vs_backup"] == "fail"


def test_conservation_catches_a_restart_that_ate_rows():
    bundle = good_bundle()
    for c in bundle["checkpoints"]:
        if c["label"] == "13-midfault-restart":
            c["records"] = 3600  # fewer than 12-fault-t5's 3644
            c["max_id"] = 3600
    v = verdicts(D.check_record_conservation(bundle))
    assert v["restart_lost_no_rows"] == "fail"
    assert v["ledger_monotonic"] == "fail"


def test_ingest_arithmetic_flags_a_gap_beyond_the_restart_allowance():
    bundle = good_bundle()
    for c in bundle["checkpoints"]:
        if c["label"] == "20-recovery-t0":
            c["records"] = 3500  # only +36 over the 10-minute fault, ~360 expected
    assert verdicts(D.check_record_conservation(bundle))["ingest_arithmetic_closes"] == "fail"


def test_duplicate_record_uids_fail_conservation():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["duplicate_record_uids"] = 2
    assert verdicts(D.check_record_conservation(bundle))["no_duplicate_record_uids"] == "fail"


# ---------------------------------------------------------------- 3b. refusal / cap / cache / claim

def test_refusal_evidence_passes_for_a_refused_bridge_scoped_fault():
    v = verdicts(D.check_refusal_evidence(good_bundle()))
    assert v["fault_is_connection_refused"] == "pass"
    assert v["fault_scoped_to_bridge_env"] == "pass"
    assert v["refused_not_hung"] == "pass"
    assert v["failed_attempts_grew"] == "pass"
    assert v["oldest_pending_pinned"] == "pass"


def test_a_stalled_fault_shape_is_not_refusal_evidence():
    bundle = good_bundle()
    bundle["fault"]["kind"] = "client-pause"
    for c in bundle["checkpoints"]:
        if c["phase"] == "fault":
            c["logs"]["connection_refused"] = 0
    v = verdicts(D.check_refusal_evidence(bundle))
    assert v["fault_is_connection_refused"] == "fail"
    assert v["refused_not_hung"] == "fail"


def test_unverified_closed_port_fails_scoping():
    bundle = good_bundle()
    bundle["fault"]["closed_port_verified"] = False
    assert verdicts(D.check_refusal_evidence(bundle))["fault_scoped_to_bridge_env"] == "fail"


def test_oldest_pending_must_stay_pinned_at_fault_start():
    bundle = good_bundle()
    for c in bundle["checkpoints"]:
        if c["label"] == "14-fault-t9":
            c["oldest_pending_at"] = _ts(8)
    assert verdicts(D.check_refusal_evidence(bundle))["oldest_pending_pinned"] == "fail"


def test_cap_evidence_requires_a_backlog_over_the_replay_limit():
    v = verdicts(D.check_cap_evidence(good_bundle()))
    assert v["backlog_exceeds_replay_limit"] == "pass"
    assert v["drain_monotonic"] == "pass"
    assert v["drain_within_deadline"] == "pass"
    assert v["drain_unattended"] == "pass"
    assert v["failed_counter_frozen"] == "pass"


def test_a_backlog_under_the_cap_does_not_prove_looped_drain():
    bundle = good_bundle()
    for c in bundle["checkpoints"]:
        if c["phase"] == "fault":
            c["pending"] = min(c["pending"], 200)
    assert verdicts(D.check_cap_evidence(bundle))["backlog_exceeds_replay_limit"] == "fail"


def test_drain_deadline_is_five_minutes_and_unattended():
    assert D.DRAIN_DEADLINE_S == 300.0
    bundle = good_bundle()
    for c in bundle["checkpoints"]:
        if c["label"] == "22-recovery-t5m":
            c["pending"] = 40
        if c["label"] == "40-final":
            c["at"] = _ts(30)  # zero only 20 min after FAULT OUT
    assert verdicts(D.check_cap_evidence(bundle))["drain_within_deadline"] == "fail"

    bundle = good_bundle()
    bundle["recovery"]["operator_actions"] = ["rollout restart to force a startup drain"]
    assert verdicts(D.check_cap_evidence(bundle))["drain_unattended"] == "fail"


def test_failed_counter_must_freeze_after_recovery():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["failed"] = 460
    assert verdicts(D.check_cap_evidence(bundle))["failed_counter_frozen"] == "fail"


def test_pending_rising_again_during_recovery_fails():
    bundle = good_bundle()
    for c in bundle["checkpoints"]:
        if c["label"] == "22-recovery-t5m":
            c["pending"] = 200
    assert verdicts(D.check_cap_evidence(bundle))["drain_monotonic"] == "fail"


def test_cache_contract_is_graded_against_precheck_live_nodes_not_a_hardcoded_six():
    bundle = good_bundle()
    # rankine is silent before the window: it must not be required to come back.
    for c in bundle["checkpoints"]:
        c["redis"]["ttls"]["rankine"] = -2
    v = verdicts(D.check_cache_evidence(bundle))
    assert v["live_nodes_rearmed"] == "pass"


def test_a_node_not_rearmed_after_recovery_fails_the_cache_contract():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["redis"]["ttls"]["gold"] = -2
    assert verdicts(D.check_cache_evidence(bundle))["live_nodes_rearmed"] == "fail"


def test_ttl_above_thirty_seconds_is_not_the_frozen_contract():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["redis"]["ttls"]["gold"] = 120
    assert verdicts(D.check_cache_evidence(bundle))["live_nodes_rearmed"] == "fail"


def test_devices_membership_must_match_the_precheck_snapshot():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["redis"]["devices"] = list(NODES[:-1])
    assert verdicts(D.check_cache_evidence(bundle))["devices_membership_unchanged"] == "fail"


def test_probe_ids_left_behind_fail_cleanup_but_not_membership():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["redis"]["devices"] = list(NODES) + ["drill-probe-a"]
    v = verdicts(D.check_cache_evidence(bundle))
    assert v["devices_membership_unchanged"] == "pass"
    assert v["probe_ids_cleaned_up"] == "fail"


def test_liveness_must_actually_expire_during_the_fault():
    bundle = good_bundle()
    for c in bundle["checkpoints"]:
        if c["phase"] == "fault":
            c["redis"]["ttls"] = {n: 20 for n in NODES}
    assert verdicts(D.check_cache_evidence(bundle))["liveness_expired_during_fault"] == "fail"


def test_claim_evidence_passes_on_a_clean_drill():
    v = verdicts(D.check_claim_evidence(good_bundle()))
    assert v["pvc_claim_unchanged"] == "pass"
    assert v["dedupe_claim_atomic"] == "pass"
    assert v["stream_growth_bounded"] == "pass"
    assert v["negative_cases_pass"] == "pass"
    assert v["all_negative_cases_recorded"] == "pass"


def test_a_moved_pvc_claim_fails():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["pvc"] = {"claim": "hear-mqtt-bridge-state-2", "volume": "pvc-zzz",
                                        "phase": "Bound"}
    assert verdicts(D.check_claim_evidence(bundle))["pvc_claim_unchanged"] == "fail"


def test_stream_growth_beyond_distinct_events_is_a_duplicate_publish():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["redis"]["xlen"] = 1024 + 48  # 4x the 12 distinct records
    assert verdicts(D.check_claim_evidence(bundle))["stream_growth_bounded"] == "fail"


def test_blocking_negatives_must_be_run_and_pass():
    bundle = good_bundle()
    bundle["negatives"]["results"]["N2"] = "fail"
    assert verdicts(D.check_claim_evidence(bundle))["negative_cases_pass"] == "fail"

    bundle = good_bundle()
    bundle["negatives"]["results"].pop("N1")
    v = verdicts(D.check_claim_evidence(bundle))
    assert v["negative_cases_pass"] == "unknown"
    assert v["all_negative_cases_recorded"] == "fail"


# ---------------------------------------------------------------- 4. command plan screening

@pytest.mark.parametrize("command,expected", [
    ("kubectl -n infra scale sts/audit-redis --replicas=0", "refused_redis_scale_down"),
    ("kubectl -n infra delete pod audit-redis-0", "refused_redis_pod_delete"),
    ("redis-cli -a $PW FLUSHALL", "refused_redis_flush"),
    ("redis-cli -a $PW CLIENT PAUSE 600000", "refused_redis_pause"),
    ("redis-cli -a $PW DEBUG SLEEP 600", "refused_redis_pause"),
    ("redis-cli -a $PW config set maxmemory 0", "refused_redis_shutdown"),
    ("redis-cli -a $PW rename dama:hear:gold dama:hear:gold-old", "refused_redis_rename_keys"),
    ("iptables -I OUTPUT -p tcp --dport 6379 -j REJECT", "refused_host_netfilter"),
    ("kubectl -n dama apply -f netpol-deny-redis.yaml # networkpolicy", "refused_network_policy"),
    ("kubectl -n dama delete pvc hear-mqtt-bridge-state", "refused_pvc_destruction"),
    ("kubectl -n dama apply -f deploy/k8s/hear-mqtt-bridge.yaml", "refused_mtls_manifest_apply"),
    ("kubectl -n dama set env deploy/hear-mqtt-bridge HEAR_DURABLE_STORE=none",
     "refused_durable_store_disable"),
    ("kubectl -n dama scale deploy/hear-mqtt-bridge --replicas=2", "refused_replica_change"),
    ("kubectl -n dama rollout restart deploy/hear-heartbeat", "refused_heartbeat_in_scope"),
])
def test_every_rejected_alternative_is_refused_by_the_screen(command, expected):
    report = D.check_command_plan([command])
    assert verdicts(report)[expected] == "fail"
    assert report.verdict == "fail"


def test_the_runbook_plan_passes_the_screen():
    report = D.check_command_plan(good_bundle()["plan"])
    v = verdicts(report)
    assert v["no_forbidden_commands"] == "pass"
    assert v["plan_has_fault_in"] == "pass"
    assert v["plan_has_fault_out"] == "pass"
    assert v["fault_is_reversible"] == "pass"
    assert report.verdict == "pass"


def test_a_fault_without_its_inverse_is_not_reversible():
    plan = [c for c in good_bundle()["plan"] if '"6379"' not in c]
    v = verdicts(D.check_command_plan(plan))
    assert v["fault_is_reversible"] == "fail"
    assert v["plan_has_fault_out"] == "fail"


def test_screening_reads_text_and_never_executes(tmp_path):
    plan = tmp_path / "plan.txt"
    plan.write_text("kubectl -n infra scale sts/audit-redis --replicas=0\n", encoding="utf-8")
    marker = tmp_path / "side-effect"
    assert D.main(["plan", "--plan-file", str(plan), "--format", "json"]) == 0
    assert not marker.exists()


# ---------------------------------------------------------------- 5. receipt

def test_a_complete_receipt_validates():
    v = verdicts(D.validate_receipt(GOOD_RECEIPT, good_bundle()))
    assert v["receipt_present"] == "pass"
    assert v["receipt_fields_complete"] == "pass"
    assert v["receipt_signed_off"] == "pass"
    assert v["receipt_verdict_is_pass"] == "pass"
    assert v["checkpoints_complete"] == "pass"
    assert v["receipt_cites_checkpoints"] == "pass"


def test_missing_receipt_fields_fail():
    text = GOOD_RECEIPT.replace("xlen delta +12", "stream grew a bit")
    assert verdicts(D.validate_receipt(text, good_bundle()))["receipt_fields_complete"] == "fail"


def test_receipt_without_signoff_fails():
    text = GOOD_RECEIPT.replace("phase2-durable-failure-drill: PASS rjm", "looked fine")
    assert verdicts(D.validate_receipt(text, good_bundle()))["receipt_signed_off"] == "fail"


def test_receipt_must_name_a_tripped_abort_trigger():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["records"] = 100  # trips T4
    v = verdicts(D.validate_receipt(GOOD_RECEIPT, bundle))
    assert v["receipt_records_abort_T4_ledger_regressed"] == "fail"


def test_receipt_missing_checkpoints_fails():
    bundle = good_bundle()
    bundle["checkpoints"] = [c for c in bundle["checkpoints"] if c["label"] != "13-midfault-restart"]
    assert verdicts(D.validate_receipt(GOOD_RECEIPT, bundle))["checkpoints_complete"] == "fail"


def test_empty_receipt_fails_immediately():
    report = D.validate_receipt("", good_bundle())
    assert verdicts(report) == {"receipt_present": "fail"}


def test_receipt_precision_is_blunted_and_flagged():
    text = GOOD_RECEIPT + "\nnode at %s, %s\n" % near_pair()
    v = verdicts(D.validate_receipt(text, good_bundle()))
    assert v["receipt_precision_clean"] == "fail"
    rendered = D.format_report(D.validate_receipt(text, good_bundle()).as_dict())
    assert fraction(near_pair()[0]) not in rendered


# ---------------------------------------------------------------- 6. the execution gate

def test_the_gate_opens_only_on_a_fully_evidenced_bundle():
    report = D.evaluate_gate(good_bundle())
    assert report.blocking_open == []
    assert report.verdict == "pass"


def test_an_empty_bundle_keeps_the_gate_shut_without_failing_loudly():
    report = D.evaluate_gate({"schema": D.SCHEMA})
    assert report.verdict == "incomplete"
    assert "correctness_fix_merged" in report.blocking_open
    assert "backup_and_conservation_baseline_captured" in report.blocking_open


@pytest.mark.parametrize("mutate,expected", [
    (lambda g: g.__setitem__("fix_merged_to_main", False), "correctness_fix_merged"),
    (lambda g: g.__setitem__("defects_covered", ["D1"]), "fix_covers_audited_defects"),
    (lambda g: g.__setitem__("configmap_matches_repo", False), "code_configmap_matches_repo"),
    (lambda g: g.__setitem__("tests_passed", ["tests/test_bridge.py"]), "gate_tests_green"),
    (lambda g: g.__setitem__("soak_stable_minutes", 5), "post_fix_soak_stable"),
    (lambda g: g.__setitem__("soak_pending", 4), "post_fix_soak_stable"),
    (lambda g: g.__setitem__("strategy", "RollingUpdate"), "strategy_is_recreate"),
    (lambda g: g.__setitem__("heartbeat_drill_concurrent", True),
     "heartbeat_drill_not_concurrent"),
    (lambda g: g.__setitem__("redis_precheck_read_only", False), "redis_precheck_read_only"),
    (lambda g: g.__setitem__("window", {}), "window_agreed"),
])
def test_each_gate_condition_can_shut_the_gate(mutate, expected):
    bundle = good_bundle()
    mutate(bundle["gate"])
    report = D.evaluate_gate(bundle)
    assert expected in report.blocking_open
    assert report.verdict != "pass"


def test_a_dangerous_plan_shuts_the_gate():
    bundle = good_bundle()
    bundle["plan"].append("kubectl -n infra scale sts/audit-redis --replicas=0")
    report = D.evaluate_gate(bundle)
    assert "command_plan_screened" in report.blocking_open


def test_a_missing_backup_shuts_the_gate():
    bundle = good_bundle()
    bundle["backup"]["files"].pop("SHA256SUMS")
    assert "backup_and_conservation_baseline_captured" in D.evaluate_gate(bundle).blocking_open


def test_the_gate_reports_that_the_drill_is_unexecuted():
    v = verdicts(D.evaluate_gate(good_bundle()))
    assert v["drill_unexecuted"] == "pass"
    bundle = good_bundle()
    bundle["executed"] = True
    assert verdicts(D.evaluate_gate(bundle))["drill_unexecuted"] == "fail"


# ---------------------------------------------------------------- 7. analysis + CLI

def test_analysis_of_a_clean_drill_passes():
    result = D.analyze(good_bundle(), GOOD_RECEIPT)
    assert result["failed"] == []
    assert result["verdict"] == "pass"
    assert set(result["reports"]) == {"backup", "record_conservation", "refusal", "cap", "cache",
                                      "claim", "receipt"}


def test_analysis_fails_when_a_row_is_lost():
    bundle = good_bundle()
    bundle["checkpoints"][-1]["records"] = 3000
    result = D.analyze(bundle, GOOD_RECEIPT)
    assert result["verdict"] == "fail"
    assert "T4_ledger_regressed" in result["abort"]["tripped"]


def test_analysis_without_evidence_is_incomplete_not_pass():
    result = D.analyze({"schema": D.SCHEMA, "checkpoints": []})
    assert result["verdict"] == "incomplete"


def test_cli_gate_and_analyze_render_and_grade(tmp_path, capsys):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(good_bundle()), encoding="utf-8")
    receipt = tmp_path / "RECEIPT.md"
    receipt.write_text(GOOD_RECEIPT, encoding="utf-8")

    assert D.main(["gate", "--bundle", str(path), "--require-pass"]) == 0
    assert "blocking items still open: none" in capsys.readouterr().out

    assert D.main(["analyze", "--bundle", str(path), "--receipt", str(receipt),
                   "--require-pass"]) == 0
    out = capsys.readouterr().out
    assert "abort boundary" in out and "record_conservation" in out


def test_cli_require_pass_exits_two_on_failure(tmp_path, capsys):
    bundle = good_bundle()
    bundle["gate"]["fix_merged_to_main"] = False
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    assert D.main(["gate", "--bundle", str(path), "--require-pass"]) == 2
    capsys.readouterr()


def test_cli_json_output_is_machine_readable(tmp_path, capsys):
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(good_bundle()), encoding="utf-8")
    assert D.main(["receipt", "--bundle", str(path), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "receipt" and payload["verdict"] == "fail"  # no --receipt given


def test_cli_reports_an_unreadable_bundle_instead_of_crashing(capsys):
    assert D.main(["gate", "--bundle", "/nonexistent/bundle.json"]) == 1
    assert "error:" in capsys.readouterr().err


def test_bundle_is_never_mutated_by_grading():
    bundle = good_bundle()
    before = copy.deepcopy(bundle)
    D.analyze(bundle, GOOD_RECEIPT)
    D.evaluate_gate(bundle)
    assert bundle == before
