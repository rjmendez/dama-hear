"""tools/hear_redis_census.py -- the Redis census must be dry-run, prefix-confined and redacted.

The census tool of docs/phase7-redis-lifecycle-evidence.md runs against a *shared* instance where
hear owns 10 keys of 43 260. Every test here is one of the twelve the design doc's §10 demands,
and they all run against an in-memory fake: no Redis, no cluster, no network, no clock.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import hear_redis_census as C  # noqa: E402


# ---------------------------------------------------------------- the fake instance

class FakeRedis:
    """An in-memory stand-in for `infra/audit-redis`, shared-tenant shape included.

    It answers exactly the commands the census is allowed to issue. Anything else raises, so a
    test that smuggles a write past the guard fails here too rather than silently passing.
    """

    def __init__(self, keys=None, *, policy="allkeys-lru", evicted=0, clients=(), dbsize=43260):
        self.keys = dict(keys or {})
        self.policy = policy
        self.evicted = evicted
        self.clients = list(clients)
        self.dbsize = dbsize
        self.calls = []

    def execute_command(self, *argv):
        argv = [str(a) for a in argv]
        self.calls.append(tuple(argv))
        name = argv[0].upper()
        if name == "PING":
            return "PONG"
        if name == "DBSIZE":
            return self.dbsize
        if name == "INFO":
            section = (argv[1] if len(argv) > 1 else "").lower()
            if section == "memory":
                return "# Memory\r\nmaxmemory_policy:%s\r\nused_memory:1048576\r\n" % self.policy
            return ("# Stats\r\nevicted_keys:%d\r\nkeyspace_hits:1234\r\nkeyspace_misses:56\r\n"
                    % self.evicted)
        if name == "SCAN":
            pattern = argv[argv.index("MATCH") + 1] if "MATCH" in argv else "*"
            prefix = pattern[:-1] if pattern.endswith("*") else pattern
            matched = sorted(k for k in self.keys if k.startswith(prefix))
            return ["0", matched]
        if name == "TYPE":
            return self.keys[argv[1]]["type"]
        if name == "TTL":
            return self.keys.get(argv[1], {}).get("ttl", -2)
        if name == "OBJECT":
            return self.keys.get(argv[2], {}).get("idle", 0)
        if name == "SCARD":
            return self.keys[argv[1]]["cardinality"]
        if name == "STRLEN":
            return self.keys[argv[1]]["size"]
        if name == "XLEN":
            return self.keys[argv[1]]["len"]
        if name == "CLIENT" and argv[1].upper() == "LIST":
            return "\n".join(self.clients)
        raise AssertionError("fake refuses unexpected command %r" % (argv,))


def sample_keyspace():
    return {
        "dama:hear:devices": {"type": "set", "ttl": -1, "cardinality": 7, "idle": 4},
        "dama:hear:latest": {"type": "string", "ttl": -1, "size": 320, "idle": 2},
        "dama:hear:events": {"type": "stream", "ttl": -1, "len": 305, "idle": 1},
        "dama:hear:event:gold": {"type": "string", "ttl": -1, "size": 512, "idle": 900},
        "dama:hear:gold": {"type": "string", "ttl": 21, "size": 256, "idle": 0},
    }


def consent(**over):
    kwargs = dict(approver="infra-oncall-role", ticket="OPS-7", acknowledged_shared_instance=True)
    kwargs.update(over)
    return C.Consent(**kwargs)


# ------------------------------------------------- 1. the allow-list refuses by command name

@pytest.mark.parametrize("command", [
    ["MONITOR"], ["CONFIG", "SET", "maxmemory-policy", "allkeys-lfu"], ["CONFIG", "GET", "maxmemory"],
    ["KEYS", "dama:hear:*"], ["SUBSCRIBE", "__keyevent@0__:expired"], ["PSUBSCRIBE", "dama:hear:*"],
    ["DEL", "dama:hear:devices"], ["UNLINK", "dama:hear:devices"], ["EXPIRE", "dama:hear:latest", "60"],
    ["PERSIST", "dama:hear:gold"], ["FLUSHDB"], ["FLUSHALL"], ["SWAPDB", "0", "1"],
    ["RENAME", "dama:hear:latest", "dama:hear:old"], ["MIGRATE", "h", "6379", "dama:hear:latest"],
    ["XDEL", "dama:hear:events", "1-1"], ["XTRIM", "dama:hear:events", "MAXLEN", "10"],
    ["SREM", "dama:hear:devices", "test-verify"], ["DEBUG", "SLEEP", "0"], ["ACL", "SETUSER", "hear"],
    ["SHUTDOWN"], ["EVAL", "return 1", "0"], ["SCRIPT", "LOAD", "x"], ["SLAVEOF", "NO", "ONE"],
    ["SET", "dama:hear:latest", "x"], ["XADD", "dama:hear:events", "*", "a", "b"],
    ["SELECT", "1"], ["LATENCY", "RESET"], ["SLOWLOG", "GET"],
])
def test_allow_list_refuses_every_forbidden_command(command):
    with pytest.raises(C.CensusRefusal):
        C.assert_redis_command(command, consent=True)


@pytest.mark.parametrize("command", [
    ["GET", "dama:hear:latest"], ["MGET", "dama:hear:latest"], ["SMEMBERS", "dama:hear:devices"],
    ["XRANGE", "dama:hear:events", "-", "+"], ["HGETALL", "dama:hear:devices"],
    ["DUMP", "dama:hear:latest"], ["LRANGE", "dama:hear:events", "0", "1"],
])
def test_value_reads_are_refused_because_bodies_are_never_captured(command):
    with pytest.raises(C.CensusRefusal) as err:
        C.assert_redis_command(command, consent=True)
    assert "value" in str(err.value)


@pytest.mark.parametrize("command", [
    ["PING"], ["DBSIZE"], ["INFO", "memory"], ["TYPE", "dama:hear:devices"],
    ["TTL", "dama:hear:gold"], ["SCARD", "dama:hear:devices"], ["STRLEN", "dama:hear:latest"],
    ["XLEN", "dama:hear:events"], ["EXISTS", "dama:hear:latest"],
    ["OBJECT", "IDLETIME", "dama:hear:devices"], ["SCAN", "0", "MATCH", "dama:hear:*"],
])
def test_allow_list_permits_the_read_set(command):
    C.assert_redis_command(command)


def test_client_list_is_consent_only_and_never_unattended():
    with pytest.raises(C.ConsentMissing):
        C.assert_redis_command(["CLIENT", "LIST"])
    C.assert_redis_command(["CLIENT", "LIST"], consent=True)
    with pytest.raises(C.CensusRefusal):
        C.assert_redis_command(["CLIENT", "KILL", "ID", "4"], consent=True)


def test_object_freq_is_allowed_as_a_command_but_unusable_under_lru():
    C.assert_redis_command(["OBJECT", "FREQ", "dama:hear:devices"])
    server = C.CensusReader(FakeRedis(sample_keyspace())).server_facts()
    assert server["maxmemory_policy"] == "allkeys-lru"
    assert server["object_freq_collectable"] is False
    findings = C.grade_eviction_exposure(server)
    assert any(f.rule == "access-frequency" and f.verdict == "unknown" for f in findings)
    assert any("LFU" in f.detail for f in findings)


def test_object_freq_is_collectable_only_on_an_lfu_instance():
    server = C.CensusReader(FakeRedis(sample_keyspace(), policy="allkeys-lfu")).server_facts()
    assert server["object_freq_collectable"] is True


# ------------------------------------------------- 2. prefix confinement

@pytest.mark.parametrize("value", [
    "dama:consensus:leader", "dama:*", "*", "", "dama:hear", "hear:dama:x",
])
def test_prefix_confinement_rejects_foreign_patterns(value):
    with pytest.raises(C.CensusRefusal):
        C.assert_prefix(value)


def test_scan_with_a_foreign_match_is_refused():
    with pytest.raises(C.CensusRefusal):
        C.assert_redis_command(["SCAN", "0", "MATCH", "dama:consensus:*"])
    with pytest.raises(C.CensusRefusal):
        C.assert_redis_command(["SCAN", "0", "COUNT", "100"])


def test_a_foreign_key_returned_by_scan_can_never_reach_a_receipt():
    fake = FakeRedis(sample_keyspace())
    fake.keys["dama:consensus:leader"] = {"type": "string", "ttl": -1, "size": 8, "idle": 0}

    class LeakyScan(FakeRedis):
        def execute_command(self, *argv):
            if str(argv[0]).upper() == "SCAN":
                return ["0", ["dama:hear:devices", "dama:consensus:leader"]]
            return super().execute_command(*argv)

    with pytest.raises(C.CensusRefusal):
        C.CensusReader(LeakyScan(fake.keys)).scan_keys()


def test_manifest_rows_may_not_name_foreign_keys():
    with pytest.raises(C.CensusRefusal):
        C.validate_manifest({"consumers": [dict(consumer_id="x", owner="o", owner_contact_role="r",
                                                discovery_method="A", source_ref="abc",
                                                keys_touched=["dama:consensus:*"],
                                                access_kind="read", status="retired")]})


# ------------------------------------------------- 3. kubectl read-verb confinement

@pytest.mark.parametrize("args", [
    ["apply", "-f", "x.yaml"], ["patch", "sts/audit-redis", "-p", "{}"], ["delete", "pod", "p"],
    ["scale", "deploy/hear-mqtt-bridge", "--replicas", "0"], ["rollout", "restart", "deploy/x"],
    ["cp", "pod:/x", "./x"], ["edit", "cm/hear"],
])
def test_kubectl_write_verbs_are_refused(args):
    with pytest.raises(C.UnsafeCommand):
        C.assert_kubectl_read_only(args)


def test_kubectl_read_verbs_are_allowed():
    C.assert_kubectl_read_only(["-n", "infra", "get", "sts", "audit-redis", "-o", "json"])


# ------------------------------------------------- 4. redaction

def client_line(addr="10.42.0.31:41522", name="hear-heartbeat", extra=""):
    return ("id=41 addr=%s laddr=10.43.0.9:6379 fd=12 name=%s age=908 idle=3 flags=N db=0 "
            "sub=0 psub=0 multi=-1 qbuf=26 argv-mem=10 cmd=ttl user=default lib-name=redis-py "
            "lib-ver=7.4.0 resp=2 %s" % (addr, name, extra)).strip()


def test_client_row_keeps_attribution_and_drops_everything_else():
    row = C.redact_client_row(client_line(), salt="campaign-salt")
    assert row["name"] == "hear-heartbeat"
    assert row["lib_name"] == "redis-py"
    assert row["user"] == "default"
    assert row["cmd"] == "ttl"
    assert row["network_class"] == "rfc1918-10"
    blob = json.dumps(row)
    assert "10.42.0.31" not in blob and "41522" not in blob
    assert "campaign-salt" not in blob
    assert "laddr" not in blob and "qbuf" not in blob and "flags" not in blob


def test_pseudonym_is_stable_per_salt_and_changes_with_the_campaign():
    a = C.redact_client_row(client_line(), salt="campaign-salt-one")["client_pseudonym"]
    b = C.redact_client_row(client_line(), salt="campaign-salt-one")["client_pseudonym"]
    c = C.redact_client_row(client_line(), salt="campaign-salt-two")["client_pseudonym"]
    assert a == b != c
    with pytest.raises(C.CensusRefusal):
        C.pseudonym("", "10.42.0.31:41522")
    with pytest.raises(C.CensusRefusal):
        C.pseudonym("short", "10.42.0.31:41522")


def test_a_secret_named_or_high_precision_field_never_survives_capture():
    lat = "%.6f" % (json.loads(open(os.path.join(ROOT, "survey.json"),
                                   encoding="utf-8").read())["origin"]["lat_deg"] + 0.00002)
    row = C.redact_client_row(client_line(name="node-" + lat), salt="campaign-salt")
    assert lat not in json.dumps(row)
    secret = C.redact_client_row(client_line(name="AUTH_TOKEN=hunter2"), salt="campaign-salt")
    assert "hunter2" not in json.dumps(secret)


@pytest.mark.parametrize("addr,expected", [
    ("10.42.0.31:41522", "rfc1918-10"), ("192.168.1.9:6379", "rfc1918-192.168"),
    ("172.20.4.4:6379", "rfc1918-172.16/12"), ("127.0.0.1:6379", "loopback"),
    ("100.64.1.2:6379", "cgnat-100.64/10"), ("8.8.8.8:6379", "public-or-unclassified"),
])
def test_network_class_is_the_only_address_detail_kept(addr, expected):
    assert C.network_class(addr) == expected


def test_an_address_that_reached_a_row_is_refused_not_sanitized():
    with pytest.raises(C.CensusRefusal):
        C.assert_no_leak({"note": "seen from 10.42.0.31"}, salt="s", raw_line="")


# ------------------------------------------------- 5. receipt completeness

def valid_receipt(**over):
    base = dict(run_id="r1", window_start="2026-09-15T00:00:00+00:00",
                window_end="2026-09-29T00:00:00+00:00", method="A+C", sample_count=20160,
                samples_lost=3, longest_gap_s=120.0, allow_list_version=C.ALLOW_LIST_VERSION,
                redaction_profile_version=C.REDACTION_PROFILE_VERSION, approver="infra-oncall-role",
                tool_build=C.TOOL_BUILD)
    base.update(over)
    return base


@pytest.mark.parametrize("field", C.RECEIPT_REQUIRED)
def test_a_receipt_missing_any_required_field_is_void_not_clean(field):
    status, missing = C.receipt_status(valid_receipt(**{field: None}))
    assert status == "void"
    assert field in missing


def test_a_receipt_from_a_different_allow_list_version_is_void():
    assert C.receipt_status(valid_receipt(allow_list_version="other"))[0] == "void"
    assert C.receipt_status(valid_receipt(redaction_profile_version="other"))[0] == "void"
    assert C.receipt_status(valid_receipt())[0] == "valid"


# ------------------------------------------------- 6. coverage arithmetic

def test_a_gap_longer_than_the_claimed_cadence_is_not_coverage():
    hourly = C.coverage_verdict(longest_gap_s=4 * 3600, claimed_cadence_s=3600)
    assert hourly["covered"] is False
    assert "NOT covered" in hourly["detail"]
    assert C.coverage_verdict(longest_gap_s=120, claimed_cadence_s=3600)["covered"] is True


# ------------------------------------------------- 7. unattributed is blocking

def manifest(*rows):
    return {"manifest_kind": C.MANIFEST_KIND, "consumers": list(rows)}


def row(**over):
    base = dict(consumer_id="hear-heartbeat", owner="dama-hear", owner_contact_role="hear-oncall",
                discovery_method="A", source_ref="62641da", keys_touched=["dama:hear:devices"],
                access_kind="both", status="attributed", replacement_answer="canonical registry")
    base.update(over)
    return base


def test_an_unattributed_row_blocks_the_gate_and_is_named():
    result = C.evaluate_gate("census", manifest=manifest(row(), row(consumer_id="client-9f2",
                                                                    status="unattributed")),
                             evidence={})
    assert result.passed is False
    assert any("client-9f2" in r for r in result.reasons)


def test_a_clean_manifest_passes_the_first_stage_gate():
    assert C.evaluate_gate("census", manifest=manifest(row()), evidence={}).passed is True


def test_an_empty_manifest_is_absence_of_evidence():
    assert C.evaluate_gate("census", manifest=manifest(), evidence={}).passed is False


def test_an_attributed_reader_without_a_replacement_answer_fails():
    findings = C.validate_manifest(manifest(row(replacement_answer="")))
    assert any("replacement_answer" in f.detail for f in findings)


# ------------------------------------------------- 8. classification completeness

def test_every_frozen_redis_pattern_has_a_class():
    assert C.classification_gaps() == []
    assert set(C.frozen_key_patterns()) == {r.pattern for r in C.KEY_RULES}


def test_class_u_is_the_answer_for_an_unknown_key_and_it_blocks(tmp_path):
    assert C.classify_key("dama:hear:gold") == C.CLASS_AUTHORITATIVE
    baseline = tmp_path / "frozen.json"
    baseline.write_text(json.dumps({"redis_keys": {"keys": [{"pattern": "dama:hear:brand-new"}]}}))
    assert C.classification_gaps(str(baseline)) == ["dama:hear:brand-new"]
    findings = C.grade_ttl_policy([{"key": "dama:hear:nested:deep:key", "ttl_s": -1}])
    assert [f.verdict for f in findings] == ["fail"]


def test_key_patterns_resolve_to_the_most_specific_rule():
    assert C.rule_for_key("dama:hear:event:gold").pattern == "dama:hear:event:{device_id}"
    assert C.rule_for_key("dama:hear:gold").pattern == "dama:hear:{device_id}"
    assert C.rule_for_key("dama:hear:devices").pattern == "dama:hear:devices"


# ------------------------------------------------- 9. boundedness

def test_boundedness_fails_when_the_device_set_grows_past_the_enrolled_fleet():
    shapes = [{"key": "dama:hear:devices", "cardinality": 7},
              {"key": "dama:hear:latest", "size_bytes": 320}]
    ok = C.grade_boundedness(shapes, enrolled_devices=7, max_envelope_bytes=4096)
    assert all(f.verdict == "pass" for f in ok)
    grown = [{"key": "dama:hear:devices", "cardinality": 8}]
    assert C.grade_boundedness(grown, enrolled_devices=7, max_envelope_bytes=4096)[0].verdict == "fail"
    big = [{"key": "dama:hear:latest", "size_bytes": 9000}]
    assert C.grade_boundedness(big, enrolled_devices=7, max_envelope_bytes=4096)[0].verdict == "fail"


# ------------------------------------------------- 10. no count from the lossy stream

def test_no_count_may_be_derived_from_the_capped_stream():
    shapes = [{"key": "dama:hear:events", "stream_len": 305, "lossy": True},
              {"key": "dama:hear:devices", "cardinality": 7}]
    with pytest.raises(C.LossyEvidence):
        C.count_from(shapes, "dama:hear:events")
    assert C.count_from(shapes, "dama:hear:devices") == 7
    with pytest.raises(C.LossyEvidence):
        C.assert_not_lossy("dama:hear:events", "a conservation term")


def test_the_stream_shape_is_recorded_and_labelled_lossy():
    shape = C.CensusReader(FakeRedis(sample_keyspace())).key_shape("dama:hear:events")
    assert shape["stream_len"] == 305 and shape["lossy"] is True


# ------------------------------------------------- 11. receipts are staged, never frozen

def test_a_receipt_is_staged_and_refuses_the_frozen_baseline(tmp_path):
    receipt = C.run_census(FakeRedis(sample_keyspace()), consent=consent(), run_id="r1")
    assert receipt["staged"] is True and receipt["frozen"] is False
    path = C.write_receipt(receipt, str(tmp_path / "evidence"))
    assert os.path.exists(path)
    assert "docs/data" not in path
    with pytest.raises(C.CensusRefusal):
        C.write_receipt(receipt, os.path.join(ROOT, "docs", "data", "phase0-freeze-contracts.v1.json"))


def test_the_tool_never_writes_into_the_repository_contract_tree():
    baseline = os.path.join(ROOT, C.FREEZE_BASELINE)
    before = os.stat(baseline).st_mtime
    C.run_census(FakeRedis(sample_keyspace()), consent=consent(), run_id="r2")
    assert os.stat(baseline).st_mtime == before


# ------------------------------------------------- 12. gate ordering

def test_a_later_stage_refuses_to_pass_without_the_earlier_evidence():
    clean = manifest(row())
    result = C.evaluate_gate("dual_read", manifest=clean, evidence={})
    assert result.passed is False
    assert any("'census'" in r for r in result.reasons)
    assert any("'compare'" in r for r in result.reasons)


def test_void_or_short_earlier_evidence_still_blocks():
    clean = manifest(row())
    evidence = {"census": {"status": "void", "window_days": 14},
                "compare": {"status": "valid", "window_days": 14}}
    assert C.evaluate_gate("dual_read", manifest=clean, evidence=evidence).passed is False
    short = {"census": {"status": "valid", "window_days": 2},
             "compare": {"status": "valid", "window_days": 14}}
    result = C.evaluate_gate("dual_read", manifest=clean, evidence=short)
    assert result.passed is False
    assert any("shorter than the declared" in r for r in result.reasons)


def test_a_fully_evidenced_ordering_passes():
    evidence = {s: {"status": "valid", "window_days": 14} for s in ("census", "compare")}
    assert C.evaluate_gate("dual_read", manifest=manifest(row()), evidence=evidence).passed is True


def test_an_unknown_stage_is_refused():
    with pytest.raises(C.CensusRefusal):
        C.evaluate_gate("delete-everything", manifest=manifest(row()), evidence={})


# ------------------------------------------------- dry run and consent gating

def test_the_plan_connects_to_nothing_and_is_self_validating():
    plan = C.census_plan(device_ids=["gold", "nyquist"], include_client_census=True)
    for command in plan:
        assert command.kind == "redis"
        C.assert_redis_command(command.argv, consent=command.consent_required)
    assert any(c.argv[0] == "CLIENT" and c.consent_required for c in plan)
    text = C.render_plan(plan)
    assert "connects to nothing" in text
    assert not any(c.argv[0] == "KEYS" for c in plan)


def test_cli_defaults_to_a_dry_run(capsys):
    assert C.main([]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "SCAN 0 MATCH dama:hear:*" in out


def test_cli_json_plan_declares_it_did_not_execute(capsys):
    assert C.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["executed"] is False
    assert payload["allow_list_version"] == C.ALLOW_LIST_VERSION


def test_cli_execute_is_refused_without_recorded_consent(capsys):
    assert C.main(["--execute"]) == 2
    assert "missing recorded consent" in capsys.readouterr().err


def test_cli_execute_has_no_live_client_factory_in_this_build(capsys):
    code = C.main(["--execute", "--approver", "infra-oncall-role", "--ticket", "OPS-7",
                   "--i-understand-shared-instance"])
    assert code == 2
    assert "separately authorized operator task" in capsys.readouterr().err


def test_cli_gate_exits_nonzero_when_blocked(tmp_path, capsys):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest(row(status="unattributed"))))
    assert C.main(["--gate", str(path)]) == 1
    assert "BLOCKED" in capsys.readouterr().out


def test_cli_classify_lists_every_frozen_pattern(capsys):
    assert C.main(["--classify"]) == 0
    out = capsys.readouterr().out
    for pattern in C.frozen_key_patterns():
        assert pattern in out
    assert "none" in out.splitlines()[-1]


def test_a_live_run_needs_every_consent_field():
    for missing in ({"approver": ""}, {"ticket": ""}, {"acknowledged_shared_instance": False}):
        with pytest.raises(C.ConsentMissing):
            C.run_census(FakeRedis(sample_keyspace()), consent=consent(**missing))
    with pytest.raises(C.ConsentMissing):
        C.run_census(FakeRedis(sample_keyspace()), consent=consent(client_census=True, salt=""))


# ------------------------------------------------- the census itself, on the fake instance

def test_a_census_run_grades_the_measured_keyspace():
    fake = FakeRedis(sample_keyspace())
    receipt = C.run_census(fake, consent=consent(), run_id="r3", enrolled_devices=6)
    assert receipt["status"] == "valid"
    assert {k["key"] for k in receipt["keys"]} == set(sample_keyspace())
    by_subject = {(f["subject"], f["rule"]): f for f in receipt["findings"]}
    assert by_subject[("dama:hear:devices", "ttl-policy")]["verdict"] == "fail"
    assert by_subject[("dama:hear:event:gold", "ttl-policy")]["verdict"] == "fail"
    assert by_subject[("dama:hear:latest", "ttl-policy")]["verdict"] == "fail"
    assert by_subject[("dama:hear:gold", "ttl-policy")]["verdict"] == "pass"
    assert by_subject[("dama:hear:events", "ttl-policy")]["verdict"] == "pass"
    assert by_subject[("dama:hear:devices", "boundedness")]["verdict"] == "fail"
    assert receipt["server"]["dbsize"] == 43260
    assert any(c[0] == "SCAN" for c in fake.calls)
    assert not any(c[0] in ("KEYS", "CONFIG", "MONITOR", "GET") for c in fake.calls)


def test_a_ttl_above_the_frozen_contract_value_fails():
    keys = sample_keyspace()
    keys["dama:hear:gold"]["ttl"] = 90
    findings = C.grade_ttl_policy([C.CensusReader(FakeRedis(keys)).key_shape("dama:hear:gold")])
    assert findings[0].verdict == "fail" and "frozen contract value" in findings[0].detail


def test_an_absent_key_is_a_value_not_a_failure():
    findings = C.grade_ttl_policy([{"key": "dama:hear:mach", "ttl_s": -2}])
    assert findings[0].verdict == "unknown" and "absence is a value" in findings[0].detail


def test_eviction_history_is_a_dated_fact_in_every_receipt():
    quiet = C.grade_eviction_exposure(C.CensusReader(FakeRedis(sample_keyspace())).server_facts())
    assert any(f.rule == "eviction-history" and f.verdict == "pass" for f in quiet)
    evicting = C.CensusReader(FakeRedis(sample_keyspace(), evicted=12)).server_facts()
    assert any(f.rule == "eviction-history" and f.verdict == "fail"
               for f in C.grade_eviction_exposure(evicting))


def test_a_consented_client_census_is_redacted_in_the_receipt():
    fake = FakeRedis(sample_keyspace(), clients=[client_line(),
                                                 client_line(addr="192.168.4.7:5001", name="")])
    receipt = C.run_census(fake, consent=consent(client_census=True, salt="campaign-salt"),
                           run_id="r4")
    assert len(receipt["clients"]) == 2
    assert receipt["unattributed_count"] == 1
    blob = json.dumps(receipt)
    assert "10.42.0.31" not in blob and "192.168.4.7" not in blob and "campaign-salt" not in blob


def test_the_reader_records_every_command_it_issued():
    reader = C.CensusReader(FakeRedis(sample_keyspace()))
    reader.scan_keys()
    assert reader.issued and all(c[0] in C.REDIS_READ_COMMANDS for c in reader.issued)
    with pytest.raises(C.CensusRefusal):
        reader.execute("DEL", "dama:hear:devices")
    assert ("DEL", "dama:hear:devices") not in reader.issued
