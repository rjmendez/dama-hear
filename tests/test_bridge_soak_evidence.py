"""tools/bridge_soak_evidence.py -- the soak snapshot must be read-only, redacted and graded.

Three things this tool has to get right, tested in that order:
  1. it can never mutate the cluster or the outbox (every kubectl call goes through one guard)
  2. it never emits a secret or a high-precision coordinate into a snapshot that gets pasted around
  3. it turns two snapshots into a Phase-2 T+24h/7d/14d verdict that fails when the soak fails

Everything here runs off fixtures and a fake kubectl runner: no cluster, no network, no clock.
"""
import copy
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import bridge_soak_evidence as B  # noqa: E402

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

def deploy_doc(**overrides):
    env = [
        {"name": "MQTT_HOST", "value": "127.0.0.1"},
        {"name": "MQTT_PORT", "value": "31883"},
        {"name": "MQTT_TOPIC", "value": "dama/+/telemetry"},
        {"name": "REDIS_PASS", "value": "hunter2-not-a-real-password"},
        {"name": "HEAR_HEARTBEAT_TOKEN", "valueFrom": {"secretKeyRef": {"name": "hb"}}},
        {"name": "HEAR_DURABLE_STORE", "value": "sqlite"},
        {"name": "HEAR_DURABLE_DB", "value": "/state/mqtt-bridge.sqlite3"},
        {"name": "HEAR_DURABLE_REPLAY_LIMIT", "value": "256"},
    ]
    env += overrides.pop("extra_env", [])
    doc = {
        "metadata": {"name": "hear-mqtt-bridge", "namespace": "dama", "generation": 6,
                     "annotations": {"dama-hear/what": "mqtt bridge"}},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "template": {"spec": {
                "hostNetwork": True,
                "volumes": [{"name": "state",
                             "persistentVolumeClaim": {"claimName": "hear-mqtt-bridge-state"}}],
                "containers": [{
                    "name": "bridge",
                    "image": "python:3.13-slim",
                    "env": env,
                    "volumeMounts": [{"name": "state", "mountPath": "/state"}],
                }],
            }},
        },
        "status": {"observedGeneration": 6, "readyReplicas": 1, "updatedReplicas": 1},
    }
    for key, value in overrides.items():
        doc[key] = value
    return doc


def pods_doc(restarts=0, phase="Running"):
    return {"items": [{
        "metadata": {"name": "hear-mqtt-bridge-866997c48b-4dw7n"},
        "status": {"phase": phase, "startTime": "2026-09-15T13:12:40Z",
                   "containerStatuses": [{"name": "bridge", "ready": phase == "Running",
                                          "restartCount": restarts,
                                          "state": {"running": {"startedAt":
                                                                "2026-09-15T13:12:44Z"}}}]},
    }]}


def pvc_doc(phase="Bound"):
    return {"metadata": {"name": "hear-mqtt-bridge-state"},
            "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "local-path",
                     "volumeName": "pvc-94a98467", "resources": {"requests": {"storage": "5Gi"}}},
            "status": {"phase": phase, "capacity": {"storage": "5Gi"},
                       "accessModes": ["ReadWriteOnce"]}}


def cm_doc(body="print('bridge')\n"):
    return {"metadata": {"name": "hear-mqtt-bridge-code",
                         "annotations": {"dama-hear/commit": "eccd7e9",
                                         "dama-hear/source-sha256": "037388c8",
                                         "unrelated": "kept out"}},
            "data": {"tools_hear_mqtt_bridge.py": body}}


def outbox_sample(records=2810, pending=0, failed=0, newest="2026-09-15T14:30:44Z",
                  oldest="2026-09-15T13:12:59Z", devices=None):
    devices = devices if devices is not None else [
        ["gold", 592], ["ageev", 589], ["kasami", 587],
        ["nyquist", 548], ["mach", 494]]
    return {
        "path": "/state/mqtt-bridge.sqlite3",
        "tables": ["cache_attempts", "durable_records"],
        "records": records,
        "window": [oldest, newest],
        "by_path": [["hear/event", 103], ["hear/heartbeat", records - 103]],
        "by_device": devices,
        "pending": pending,
        "succeeded": records + 11,
        "failed": failed,
        "last_failure_at": None,
        "failed_by_target": [],
        "schema_versions": [[1]],
        "page_count": 1022,
        "page_size": 4096,
        "journal_mode": "wal",
        "sampled_at": "2026-09-15T14:30:45Z",
    }


HEALTHZ = {"status": "ok", "durable_store": {"backend": "sqlite", "enabled": True,
                                             "pending_records": 0, "cache_successes": 9,
                                             "cache_failures": 0,
                                             "last_cache_failure_at": None},
           "refusals": {"backend": "sqlite", "enabled": True, "refused_messages": 3,
                        "last_refusal_at": "2026-09-15T18:40:02Z", "max_rows": 5000,
                        "max_bytes": 16 * 1024 * 1024, "evicted_refusals": 0,
                        "suppressed_refusals": 0}}

STATE_LS = ("total 5540\n"
            "-rw-r--r-- 1 root root 4186112 Sep 15 14:30 mqtt-bridge.sqlite3\n"
            "-rw-r--r-- 1 root root 1561512 Sep 15 14:30 mqtt-bridge.sqlite3-wal\n"
            "5632\t/state\n")


class FakeKubectl:
    """Answers the exact reads the tool makes, and records every call for assertion."""

    def __init__(self, **overrides):
        self.calls = []
        self.deploy = overrides.get("deploy", deploy_doc())
        self.pods = overrides.get("pods", pods_doc())
        self.pvc = overrides.get("pvc", pvc_doc())
        self.cm = overrides.get("cm", cm_doc())
        self.outbox = overrides.get("outbox", outbox_sample())
        self.healthz = overrides.get("healthz", HEALTHZ)
        self.logs = overrides.get("logs", "connected; subscribing\ndurable backend=sqlite\n")
        self.state_ls = overrides.get("state_ls", STATE_LS)
        self.redis = overrides.get("redis", "41\n")
        self.redis_ttl = overrides.get("redis_ttl", "22\n")

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        B.assert_read_only(args)  # the fake refuses anything the real runner would refuse
        text = " ".join(args)
        if "exec" in args:
            remote = args[args.index("--") + 1:]
            if remote[0] == "redis-cli":
                out = self.redis_ttl if "TTL" in remote else self.redis
            elif "sqlite3" in " ".join(remote):
                out = json.dumps(self.outbox)
            elif remote[0] == "sh":
                out = self.state_ls
            else:
                out = json.dumps(self.healthz)
        elif "logs" in args:
            out = self.logs
        elif "cm" in args:
            out = json.dumps(self.cm)
        elif "pvc" in args:
            out = json.dumps(self.pvc)
        elif "pods" in args:
            out = json.dumps(self.pods)
        elif "deploy" in args:
            out = json.dumps(self.deploy)
        else:
            return subprocess.CompletedProcess(args, 1, "", "unexpected read: %s" % text)
        return subprocess.CompletedProcess(args, 0, out, "")


def run(argv, **overrides):
    # the CLI grades against the real clock, so the default ledger has to look current
    overrides.setdefault("outbox", outbox_sample(newest=B.utc_now_iso()))
    fake = FakeKubectl(**overrides)
    rc = B.main(argv, runner=fake)
    return rc, fake


# ---------------------------------------------------------------- 1. read-only enforcement

class TestReadOnlyGuard:
    @pytest.mark.parametrize("verb", ["delete", "apply", "patch", "scale", "rollout", "edit",
                                      "create", "replace", "cp", "drain", "annotate", "label",
                                      "taint", "attach", "port-forward", "debug"])
    def test_a_mutating_verb_is_refused(self, verb):
        with pytest.raises(B.UnsafeCommand):
            B.assert_read_only(["-n", "dama", verb, "deploy/hear-mqtt-bridge"])

    @pytest.mark.parametrize("args", [
        ["-n", "dama", "get", "deploy", "hear-mqtt-bridge", "-o", "json"],
        ["-n", "dama", "logs", "deploy/hear-mqtt-bridge", "--tail", "500"],
        ["-n", "dama", "get", "pods", "-l", "app=hear-mqtt-bridge", "-o", "json"],
    ])
    def test_a_read_verb_passes(self, args):
        B.assert_read_only(args)

    def test_a_namespace_value_is_not_mistaken_for_a_verb(self):
        # `-n delete` would otherwise read as the verb `delete` and pass or fail for the wrong
        # reason; the option's value must be skipped.
        B.assert_read_only(["-n", "delete", "get", "pods"])

    def test_exec_without_a_separator_is_refused(self):
        with pytest.raises(B.UnsafeCommand):
            B.assert_read_only(["-n", "dama", "exec", "pod/x", "rm", "-rf", "/state"])

    def test_exec_of_an_arbitrary_binary_is_refused(self):
        with pytest.raises(B.UnsafeCommand):
            B.assert_read_only(["-n", "dama", "exec", "pod/x", "--", "rm", "-rf", "/state"])

    def test_the_outbox_command_is_accepted(self):
        B.assert_read_only(["-n", "dama", "exec", "pod/x", "--",
                            *B.outbox_command("/state/mqtt-bridge.sqlite3")])

    @pytest.mark.parametrize("snippet", [
        "import sqlite3; sqlite3.connect('/state/x.sqlite3').execute('delete from durable_records')",
        "import sqlite3; sqlite3.connect('/state/x.sqlite3')",              # writable connection
        "import sqlite3; c=sqlite3.connect('file:/state/x?mode=ro',uri=True); c.backup(d)",
        "import os; os.remove('/state/mqtt-bridge.sqlite3')",
        "import shutil; shutil.rmtree('/state')",
        "import subprocess; subprocess.run(['kill','1'])",
        "open('/state/x','w').write('boom')",
    ])
    def test_a_writing_python_snippet_is_refused(self, snippet):
        with pytest.raises(B.UnsafeCommand):
            B.assert_read_only(["-n", "dama", "exec", "pod/x", "--", "python3", "-c", snippet])

    @pytest.mark.parametrize("script", ["rm -rf /state", "ls /state && rm x", "du -sk /state; rm x",
                                        "cat /state/db > /dev/null"])
    def test_a_writing_shell_fragment_is_refused(self, script):
        with pytest.raises(B.UnsafeCommand):
            B.assert_read_only(["-n", "dama", "exec", "pod/x", "--", "sh", "-lc", script])

    def test_the_state_listing_shell_command_is_accepted(self):
        B.assert_read_only(["-n", "dama", "exec", "pod/x", "--", "sh", "-lc",
                            "ls -l /state; du -sk /state"])

    @pytest.mark.parametrize("cmd", [["FLUSHALL"], ["DEL", "k"], ["XADD", "s", "*", "a", "b"],
                                     ["CONFIG", "SET", "appendonly", "no"], ["SHUTDOWN"]])
    def test_a_writing_redis_command_is_refused(self, cmd):
        with pytest.raises(B.UnsafeCommand):
            B.assert_read_only(["-n", "infra", "exec", "sts/audit-redis", "--", "redis-cli", *cmd])

    def test_reading_redis_commands_are_accepted(self):
        for cmd in (["DBSIZE"], ["XLEN", "dama:hear:events"], ["INFO", "keyspace"]):
            B.assert_read_only(["-n", "infra", "exec", "sts/audit-redis", "--", "redis-cli", *cmd])

    def test_the_outbox_snippet_opens_the_ledger_read_only_and_never_writes(self):
        snippet = B.OUTBOX_SNIPPET
        assert "mode=ro" in snippet and "query_only=ON" in snippet
        assert not B.SQL_WRITE.search(snippet), "the sample must not contain a writing statement"
        assert "prune" not in snippet.lower()

    def test_every_call_a_full_run_makes_is_a_read(self):
        rc, fake = run(["--milestone", "T0", "--no-write"])
        assert rc == 0
        assert fake.calls, "the run made no cluster calls at all"
        for call in fake.calls:
            B.assert_read_only(call)          # raises if any single call could mutate

    def test_the_real_runner_refuses_before_spawning_kubectl(self, monkeypatch):
        spawned = []
        monkeypatch.setattr(B.subprocess, "run", lambda *a, **k: spawned.append(a))
        with pytest.raises(B.UnsafeCommand):
            B.run_kubectl(["-n", "dama", "delete", "pod", "x"])
        assert spawned == []


# ---------------------------------------------------------------- 2. redaction

class TestRedaction:
    def test_a_credential_env_value_never_appears(self):
        env = B.deployment_env(deploy_doc())
        assert env["REDIS_PASS"] == B.REDACTED
        assert env["MQTT_HOST"] == "127.0.0.1"

    def test_a_secret_backed_env_is_reported_as_its_source_not_its_value(self):
        assert B.deployment_env(deploy_doc())["HEAR_HEARTBEAT_TOKEN"] == "<from:secretKeyRef>"

    def test_an_empty_credential_stays_empty_rather_than_looking_set(self):
        assert B.redact_env_value("REDIS_PASS", "") == ""

    def test_a_high_precision_decimal_is_blunted(self):
        # a position travels as a decimal with four or more places; snapshots are pasted into
        # issues, where tools/coord_guard.py is not watching.
        lat, lon = near_pair()
        out = B.redact_text("rejected hear/event: lat=%s lon=%s" % (lat, lon))
        assert fraction(lat) not in out and fraction(lon) not in out
        assert "precision-redacted" in out

    def test_redaction_is_idempotent(self):
        once = B.redact_text("value %s" % near_pair()[0])
        assert B.redact_text(once) == once

    def test_a_coarse_number_survives(self):
        assert B.redact_text("db 4.6 MB in 77.75 min, 36.1 rec/min") == \
            "db 4.6 MB in 77.75 min, 36.1 rec/min"

    def test_a_nested_structure_is_redacted_whole(self):
        lat = near_pair()[0]
        tree = {"env": {"REDIS_PASS": "swordfish"}, "lines": ["at %s N" % lat], "n": 3}
        out = B.redact_tree(tree)
        assert out["env"]["REDIS_PASS"] == B.REDACTED
        assert fraction(lat) not in json.dumps(out)
        assert out["n"] == 3

    def test_a_snapshot_carries_no_secret_and_no_precise_decimal(self):
        lat = near_pair()[0]
        fake = FakeKubectl(
            logs="rejected hear/event on dama/mach/telemetry: position %s\n" % lat)
        args = B.build_parser().parse_args(["--no-write"])
        args.expected_nodes = list(B.EXPECTED_NODES)
        snapshot = B.collect(B.Cluster(runner=fake), args)
        blob = json.dumps(snapshot)
        assert "hunter2" not in blob
        assert fraction(lat) not in blob
        assert B.REDACTED in blob

    def test_a_warning_is_redacted_before_it_is_kept(self):
        lat = near_pair()[0]
        sink = B.WarningSink()
        sink.add("kubectl failed at %s" % lat)
        assert fraction(lat) not in sink.items[0]


# ---------------------------------------------------------------- 3. object summaries

class TestSummaries:
    def test_the_deployment_summary_records_generation_image_and_strategy(self):
        d = B.summarize_deployment(deploy_doc())
        assert (d["generation"], d["observed_generation"], d["rolled_out"]) == (6, 6, True)
        assert d["image"] == "python:3.13-slim"
        assert d["strategy"] == "Recreate"
        assert d["volume_claims"] == {"state": "hear-mqtt-bridge-state"}

    def test_an_unsettled_rollout_is_visible(self):
        doc = deploy_doc()
        doc["status"]["observedGeneration"] = 5
        assert B.summarize_deployment(doc)["rolled_out"] is False

    def test_the_durable_env_defaults_are_filled_in_and_marked_as_defaults(self):
        d = B.summarize_deployment(deploy_doc())
        assert d["durable_env"]["HEAR_DURABLE_RETENTION_DAYS"] == "30"
        assert d["durable_env_explicit"]["HEAR_DURABLE_RETENTION_DAYS"] is False
        assert d["durable_env_explicit"]["HEAR_DURABLE_STORE"] is True

    def test_a_tls_cutover_shows_up_as_network_drift(self):
        # the repo also carries an mTLS/8883 variant of this Deployment; live must stay plaintext
        # 31883, so a port change has to be reported rather than silently accepted.
        doc = deploy_doc()
        for item in doc["spec"]["template"]["spec"]["containers"][0]["env"]:
            if item["name"] == "MQTT_PORT":
                item["value"] = "8883"
        drift = B.summarize_deployment(doc)["network_drift"]
        assert drift == {"MQTT_PORT": {"want": "31883", "have": "8883"}}

    def test_the_configmap_checksum_is_stable_and_content_sensitive(self):
        first = B.configmap_digest(cm_doc())["checksum_sha256"]
        assert first == B.configmap_digest(cm_doc())["checksum_sha256"]
        assert first != B.configmap_digest(cm_doc("print('other')\n"))["checksum_sha256"]

    def test_the_configmap_keeps_only_provenance_annotations(self):
        ann = B.configmap_digest(cm_doc())["annotations"]
        assert ann["dama-hear/commit"] == "eccd7e9"
        assert "unrelated" not in ann

    def test_a_pending_pvc_is_not_bound(self):
        assert B.summarize_pvc(pvc_doc("Pending"))["bound"] is False
        assert B.summarize_pvc(pvc_doc())["capacity"] == "5Gi"

    def test_pod_restarts_are_summed_across_containers(self):
        assert B.pick_pod(pods_doc(restarts=3))["restarts"] == 3

    def test_a_missing_object_degrades_instead_of_raising(self):
        for fn in (B.summarize_deployment, B.summarize_pvc, B.configmap_digest, B.summarize_pod):
            assert fn(None)["available"] is False

    def test_the_state_listing_yields_file_sizes_and_a_total(self):
        state = B.parse_state_listing(STATE_LS)
        assert state["files"]["mqtt-bridge.sqlite3"] == 4186112
        assert state["total_bytes"] == 5632 * 1024

    def test_the_outbox_sample_is_normalized_into_counts_and_a_window(self):
        o = B.normalize_outbox(outbox_sample())
        assert o["records"] == 2810 and o["pending"] == 0 and o["failed"] == 0
        assert o["oldest_record_at"] == "2026-09-15T13:12:59Z"
        assert o["db_bytes"] == 1022 * 4096
        assert o["by_device"]["gold"] == 592

    def test_an_unavailable_outbox_is_marked_rather_than_guessed(self):
        assert B.normalize_outbox(None) == {"available": False}

    def test_node_coverage_names_what_is_missing(self):
        o = B.normalize_outbox(outbox_sample(devices=[["gold", 10], ["mach", 4]]))
        cov = B.node_coverage(o, B.EXPECTED_NODES)
        assert cov["missing"] == ["ageev", "kasami", "nyquist"]
        assert cov["complete"] is False

    def test_an_excluded_node_is_documented_rather_than_missing(self):
        # Rankine is out of the soak pending physical recovery. It must never show up as one of
        # the expected five, and its absence must carry a reason.
        cov = B.node_coverage(B.normalize_outbox(outbox_sample()), B.EXPECTED_NODES)
        assert "rankine" not in cov["expected"]
        assert "rankine" not in cov["missing"]
        assert cov["excluded"]["rankine"]
        assert cov["excluded_present"] == []
        assert cov["complete"] is True

    def test_an_excluded_node_that_is_still_writing_is_reported(self):
        o = B.normalize_outbox(outbox_sample(
            devices=[[n, 100] for n in B.EXPECTED_NODES] + [["rankine", 4]]))
        cov = B.node_coverage(o, B.EXPECTED_NODES)
        assert cov["excluded_present"] == ["rankine"]
        assert cov["unexpected"] == [], "a documented exclusion is not an unknown device"

    def test_full_coverage_is_complete(self):
        cov = B.node_coverage(B.normalize_outbox(outbox_sample()), B.EXPECTED_NODES)
        assert cov["complete"] is True and cov["missing"] == []

    def test_log_scanning_counts_the_kinds_that_grade_a_soak(self):
        logs = B.scan_logs("ok\nsqlite3.OperationalError: database is locked\n"
                           "rejected hear/event message on dama/mach/telemetry\n")
        assert logs["counts"]["sqlite_locked"] == 1
        assert logs["counts"]["rejected"] == 1
        assert logs["graded"] == {"sqlite_locked": 1, "durable_error": 0, "traceback": 0}

    def test_a_rejected_payload_is_counted_but_not_graded(self):
        # validation drops a malformed payload *before* the outbox commit: an upstream schema
        # defect, not a durability defect.
        assert "rejected" not in B.GRADED_LOG_KINDS

    def test_redis_noauth_is_recorded_as_a_limitation_not_a_crash(self):
        value, error = B.redis_value("NOAUTH Authentication required.")
        assert value is None and "NOAUTH" in error

    def test_a_redis_integer_reply_parses(self):
        assert B.redis_value("41\n") == (41, None)

    def test_the_receiver_health_block_is_flattened(self):
        r = B.summarize_receiver(HEALTHZ)
        assert r["backend"] == "sqlite" and r["pending_records"] == 0 and r["available"] is True

    def test_the_refusal_surface_is_kept_separate_from_durable_health(self):
        # refused input never reaches the outbox, so its counters must not be folded into the
        # durable_store block the durability verdict reads.
        r = B.summarize_refusals(HEALTHZ)
        assert r["refused_messages"] == 3 and r["max_rows"] == 5000
        assert r["within_caps"] is True
        assert r["separate_from_durable_health"] is True
        assert "pending_records" not in r

    def test_refusals_beyond_the_row_cap_are_reported_as_a_cap_breach(self):
        health = copy.deepcopy(HEALTHZ)
        health["refusals"]["refused_messages"] = 500000
        assert B.summarize_refusals(health)["within_caps"] is False

    def test_a_receiver_without_a_refusal_surface_is_unavailable_not_empty(self):
        assert B.summarize_refusals({"durable_store": {}})["available"] is False

    def test_cache_ttls_are_split_into_volatile_persistent_and_absent(self):
        cache = B.summarize_cache({"reachable": True, "authenticated": True, "dbsize": 41,
                                   "key_prefix": "dama:hear:", "ttl_limit_s": 30,
                                   "ttl_seconds": {"gold": 22, "mach": -1, "ageev": -2,
                                                   "kasami": 900, "nyquist": 7}})
        assert cache["volatile"] == ["gold", "kasami", "nyquist"]
        assert cache["persistent"] == ["mach"], "a key that lost its expiry is an unbounded cache"
        assert cache["absent"] == ["ageev"]
        assert cache["over_limit"] == ["kasami"]


# ---------------------------------------------------------------- 4. Phase-2 grading

def snapshot_from(runner_kwargs=None, argv=("--no-write",)):
    fake = FakeKubectl(**(runner_kwargs or {}))
    args = B.build_parser().parse_args(list(argv))
    args.expected_nodes = list(B.EXPECTED_NODES)
    return B.collect(B.Cluster(runner=fake), args)


def at(snapshot, captured_at):
    out = copy.deepcopy(snapshot)
    out["captured_at"] = captured_at
    out["window"] = {"started_at": captured_at, "finished_at": captured_at}
    return out


def verdicts(grading):
    return {c["name"]: c["verdict"] for c in grading["checks"]}


class TestGrading:
    def test_a_healthy_t0_passes_every_criterion(self):
        snap = at(snapshot_from(), "2026-09-15T14:30:45Z")
        grading = B.evaluate(snap, None, "T0")
        assert grading["verdict"] == "pass", grading["failed"] + grading["unknown"]

    def test_a_pending_backlog_fails(self):
        snap = at(snapshot_from({"outbox": outbox_sample(pending=17)}), "2026-09-15T14:30:45Z")
        grading = B.evaluate(snap, None, "T0")
        assert verdicts(grading)["pending_drained"] == "fail"
        assert grading["verdict"] == "fail"

    def test_a_missing_node_fails_coverage(self):
        snap = at(snapshot_from({"outbox": outbox_sample(devices=[["gold", 10]])}),
                  "2026-09-15T14:30:45Z")
        assert verdicts(B.evaluate(snap, None, "T0"))["all_nodes_covered"] == "fail"

    def test_the_excluded_node_is_graded_as_documented_not_missing(self):
        snap = at(snapshot_from(), "2026-09-15T14:30:45Z")
        grading = B.evaluate(snap, None, "T0")
        assert verdicts(grading)["excluded_nodes_documented"] == "pass"
        assert verdicts(grading)["all_nodes_covered"] == "pass"
        assert snap["coverage"]["excluded"]["rankine"]

    def test_an_exclusion_without_a_reason_fails(self):
        snap = at(snapshot_from(), "2026-09-15T14:30:45Z")
        snap["coverage"]["excluded"] = {"rankine": ""}
        assert verdicts(B.evaluate(snap, None, "T0"))["excluded_nodes_documented"] == "fail"

    def test_a_node_cannot_be_expected_and_excluded_at_once(self):
        snap = at(snapshot_from(), "2026-09-15T14:30:45Z")
        snap["coverage"] = B.node_coverage(B.normalize_outbox(outbox_sample()),
                                           B.EXPECTED_NODES, {"gold": "on the bench"})
        grading = B.evaluate(snap, None, "T0")
        assert verdicts(grading)["excluded_nodes_documented"] == "fail"
        assert verdicts(grading)["all_nodes_covered"] == "fail"

    def test_the_snapshot_declares_the_authoritative_soak_baseline(self):
        snap = at(snapshot_from(), "2026-09-15T18:30:00Z")
        assert snap["soak_baseline"] == "2026-09-15T18:26:22Z"
        assert verdicts(B.evaluate(snap, None, "T0"))["soak_baseline_declared"] == "pass"

    def test_a_snapshot_from_another_baseline_fails(self):
        snap = at(snapshot_from(), "2026-09-15T18:30:00Z")
        snap["soak_baseline"] = "2026-09-15T13:12:44Z"
        assert verdicts(B.evaluate(snap, None, "T0"))["soak_baseline_declared"] == "fail"

    def test_refusals_are_graded_on_their_own_line(self):
        snap = at(snapshot_from(), "2026-09-15T14:30:45Z")
        grading = B.evaluate(snap, None, "T0")
        assert verdicts(grading)["refusal_caps_hold"] == "pass"
        snap["refusals"]["within_caps"] = False
        broken = B.evaluate(snap, None, "T0")
        assert verdicts(broken)["refusal_caps_hold"] == "fail"
        assert verdicts(broken)["receiver_ledger_drained"] == "pass", \
            "a refusal cap breach is not a durability defect"

    def test_a_heartbeat_key_that_lost_its_expiry_fails(self):
        snap = at(snapshot_from({"redis_ttl": "-1\n"}), "2026-09-15T14:30:45Z")
        assert verdicts(B.evaluate(snap, None, "T0"))["cache_ttl_armed"] == "fail"

    def test_a_missing_heartbeat_key_for_a_writing_node_fails(self):
        snap = at(snapshot_from({"redis_ttl": "-2\n"}), "2026-09-15T14:30:45Z")
        assert verdicts(B.evaluate(snap, None, "T0"))["cache_ttl_armed"] == "fail"

    def test_unreadable_redis_leaves_ttl_unknown_rather_than_failing_the_soak(self):
        snap = at(snapshot_from({"redis": "NOAUTH Authentication required.\n"}),
                  "2026-09-15T14:30:45Z")
        assert verdicts(B.evaluate(snap, None, "T0"))["cache_ttl_armed"] == "unknown"

    def test_ttl_evidence_never_enumerates_keys(self):
        fake = FakeKubectl()
        args = B.build_parser().parse_args(["--no-write"])
        args.expected_nodes = list(B.EXPECTED_NODES)
        B.collect(B.Cluster(runner=fake), args)
        redis_calls = [c for c in fake.calls if "redis-cli" in c]
        assert redis_calls, "the collector must ask Redis for per-node TTLs"
        assert all("KEYS" not in c and "SCAN" not in c for c in redis_calls)
        assert any(["TTL", "dama:hear:gold"] == c[-2:] for c in redis_calls)

    def test_a_restarted_pod_fails(self):
        snap = at(snapshot_from({"pods": pods_doc(restarts=2)}), "2026-09-15T14:30:45Z")
        assert verdicts(B.evaluate(snap, None, "T0"))["pod_restarts_zero"] == "fail"

    def test_an_unbound_pvc_fails(self):
        snap = at(snapshot_from({"pvc": pvc_doc("Pending")}), "2026-09-15T14:30:45Z")
        assert verdicts(B.evaluate(snap, None, "T0"))["pvc_bound"] == "fail"

    def test_a_locked_database_fails_the_log_check(self):
        snap = at(snapshot_from({"logs": "sqlite3.OperationalError: database is locked\n"}),
                  "2026-09-15T14:30:45Z")
        assert verdicts(B.evaluate(snap, None, "T0"))["logs_clean"] == "fail"

    def test_a_stale_ledger_fails_freshness(self):
        snap = at(snapshot_from({"outbox": outbox_sample(newest="2026-09-15T12:00:00Z")}),
                  "2026-09-15T14:30:45Z")
        assert verdicts(B.evaluate(snap, None, "T0"))["ledger_fresh"] == "fail"

    def test_a_tls_port_change_fails_the_plaintext_criterion(self):
        doc = deploy_doc()
        for item in doc["spec"]["template"]["spec"]["containers"][0]["env"]:
            if item["name"] == "MQTT_PORT":
                item["value"] = "8883"
        snap = at(snapshot_from({"deploy": doc}), "2026-09-15T14:30:45Z")
        assert verdicts(B.evaluate(snap, None, "T0"))["plaintext_mqtt_preserved"] == "fail"

    # -- milestones that only a baseline can answer

    def _pair(self, hours, records):
        base = at(snapshot_from(), "2026-09-15T14:30:45Z")
        later = at(snapshot_from({"outbox": outbox_sample(
            records=records, newest="2026-09-16T14:30:45Z",
            devices=[[n, records // 5] for n in B.EXPECTED_NODES])}),
            "2026-09-16T14:30:45Z" if hours == 24 else "2026-09-22T14:30:45Z")
        return base, later

    def test_t24h_passes_when_growth_matches_the_measured_rate(self):
        base, later = self._pair(24, 2810 + 43300)
        grading = B.evaluate(later, base, "T+24h")
        assert verdicts(grading)["record_growth"] == "pass"
        assert verdicts(grading)["window_matches_milestone"] == "pass"

    def test_t24h_fails_when_the_fleet_stopped_writing(self):
        base, later = self._pair(24, 2810 + 400)
        assert verdicts(B.evaluate(later, base, "T+24h"))["record_growth"] == "fail"

    def test_t24h_fails_when_records_grow_far_beyond_the_expected_rate(self):
        base, later = self._pair(24, 2810 + 200000)
        assert verdicts(B.evaluate(later, base, "T+24h"))["record_growth"] == "fail"

    def test_a_milestone_without_a_baseline_cannot_pass(self):
        snap = at(snapshot_from(), "2026-09-16T14:30:45Z")
        grading = B.evaluate(snap, None, "T+24h")
        assert verdicts(grading)["baseline_supplied"] == "fail"
        assert grading["verdict"] == "fail"

    def test_a_sample_taken_at_the_wrong_time_is_not_graded_as_that_milestone(self):
        base, later = self._pair(24, 2810 + 43300)
        assert verdicts(B.evaluate(later, base, "T+7d"))["window_matches_milestone"] == "fail"

    def test_coverage_regression_against_the_baseline_fails(self):
        base = at(snapshot_from(), "2026-09-15T14:30:45Z")
        later = at(snapshot_from({"outbox": outbox_sample(
            records=2810 + 43300, newest="2026-09-16T14:30:45Z",
            devices=[["gold", 9000], ["mach", 9000]])}), "2026-09-16T14:30:45Z")
        grading = B.evaluate(later, base, "T+24h")
        assert verdicts(grading)["coverage_not_regressed"] == "fail"

    def test_retention_is_graded_at_the_long_milestones(self):
        base = at(snapshot_from(), "2026-09-15T14:30:45Z")
        later = at(snapshot_from({"outbox": outbox_sample(
            records=2810 + 43300 * 14, oldest="2026-07-01T00:00:00Z",
            newest="2026-09-29T14:30:45Z",
            devices=[[n, 100000] for n in B.EXPECTED_NODES])}), "2026-09-29T14:30:45Z")
        grading = B.evaluate(later, base, "T+14d")
        assert verdicts(grading)["prune_within_retention"] == "fail", \
            "records older than HEAR_DURABLE_RETENTION_DAYS mean the prune worker is not running"

    def test_a_historic_redis_outage_does_not_fail_a_later_sample(self):
        # failures that predate the baseline and have not grown are evidence of a survived outage,
        # which is what the outbox is for.
        base = at(snapshot_from({"outbox": outbox_sample(failed=12)}), "2026-09-15T14:30:45Z")
        later = at(snapshot_from({"outbox": outbox_sample(
            records=2810 + 43300, failed=12, newest="2026-09-16T14:30:45Z",
            devices=[[n, 9000] for n in B.EXPECTED_NODES])}), "2026-09-16T14:30:45Z")
        assert verdicts(B.evaluate(later, base, "T+24h"))["no_cache_failures"] == "pass"

    def test_growing_cache_failures_fail(self):
        base = at(snapshot_from({"outbox": outbox_sample(failed=12)}), "2026-09-15T14:30:45Z")
        later = at(snapshot_from({"outbox": outbox_sample(
            records=2810 + 43300, failed=900, newest="2026-09-16T14:30:45Z",
            devices=[[n, 9000] for n in B.EXPECTED_NODES])}), "2026-09-16T14:30:45Z")
        assert verdicts(B.evaluate(later, base, "T+24h"))["no_cache_failures"] == "fail"

    def test_an_oversized_state_directory_fails_its_milestone_budget(self):
        snap = at(snapshot_from(), "2026-09-15T14:30:45Z")
        snap["state_dir"] = {"available": True, "files": {},
                             "total_bytes": int(4.5 * 1024 ** 3)}
        assert verdicts(B.evaluate(snap, None, "T0"))["state_within_budget"] == "fail"

    def test_missing_evidence_is_unknown_rather_than_a_pass(self):
        snap = at(snapshot_from(), "2026-09-15T14:30:45Z")
        snap["outbox"] = {"available": False}
        snap["coverage"] = {"counts": {}}
        grading = B.evaluate(snap, None, "T0")
        assert "ledger_has_records" in grading["unknown"]
        assert grading["verdict"] == "incomplete"


# ---------------------------------------------------------------- 5. CLI and artifacts

class TestCli:
    def test_a_dated_snapshot_directory_is_written(self, tmp_path, capsys):
        rc, _ = run(["--milestone", "T0", "--out-dir", str(tmp_path)])
        assert rc == 0
        dirs = sorted(p for p in os.listdir(tmp_path))
        assert len(dirs) == 1 and dirs[0].startswith("T0-") and dirs[0].endswith("Z")
        written = tmp_path / dirs[0]
        payload = json.loads((written / "snapshot.json").read_text())
        assert payload["schema"] == "dama-hear/bridge-soak-evidence/v1"
        assert payload["grading"]["milestone"] == "T0"
        assert payload["outbox"]["records"] == 2810
        assert (written / "report.md").read_text().startswith("# hear-mqtt-bridge durable soak")

    def test_two_runs_do_not_collide_or_overwrite(self, tmp_path):
        run(["--out-dir", str(tmp_path), "--milestone", "T0"])
        run(["--out-dir", str(tmp_path), "--milestone", "T+24h"])
        assert len(os.listdir(tmp_path)) == 2

    def test_no_write_leaves_the_filesystem_alone(self, tmp_path):
        rc, _ = run(["--no-write", "--out-dir", str(tmp_path)])
        assert rc == 0 and os.listdir(tmp_path) == []

    def test_json_output_carries_snapshot_and_grading(self, capsys):
        rc, _ = run(["--no-write", "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["grading"]["verdict"] == "pass"
        assert payload["snapshot"]["deployment"]["strategy"] == "Recreate"

    def test_the_default_run_only_reports(self):
        rc, _ = run(["--no-write"], outbox=outbox_sample(pending=99))
        assert rc == 0, "the default must report, the same rule tools/fleet.py follows"

    def test_require_pass_gates_on_a_failing_criterion(self):
        rc, _ = run(["--no-write", "--require-pass"], outbox=outbox_sample(pending=99))
        assert rc == 2

    def test_require_pass_passes_a_healthy_soak(self):
        rc, _ = run(["--no-write", "--require-pass"])
        assert rc == 0

    def test_require_pass_gates_on_incomplete_evidence(self):
        rc, _ = run(["--no-write", "--require-pass"], outbox={"tables": []})
        assert rc == 2, "an unreadable ledger must not be graded as a pass"

    def test_a_baseline_directory_is_accepted_in_place_of_its_file(self, tmp_path, capsys):
        run(["--out-dir", str(tmp_path), "--milestone", "T0"])
        baseline_dir = os.path.join(str(tmp_path), os.listdir(tmp_path)[0])
        capsys.readouterr()
        rc, _ = run(["--no-write", "--milestone", "T+24h", "--baseline", baseline_dir,
                     "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["grading"]["checks"][0]["verdict"] in ("pass", "fail", "unknown")
        assert "baseline_supplied" not in payload["grading"]["failed"]

    def test_an_existing_snapshot_can_be_regraded_without_touching_the_cluster(self, tmp_path,
                                                                              capsys):
        run(["--out-dir", str(tmp_path), "--milestone", "T0"])
        path = os.path.join(str(tmp_path), os.listdir(tmp_path)[0], "snapshot.json")
        capsys.readouterr()

        def forbidden(args, timeout):
            raise AssertionError("--from-snapshot must not read the cluster")

        rc = B.main(["--from-snapshot", path, "--format", "json", "--no-write"], runner=forbidden)
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["grading"]["verdict"] == "pass"

    def test_a_cluster_that_cannot_be_read_warns_and_does_not_claim_a_pass(self, capsys):
        def broken(args, timeout):
            return subprocess.CompletedProcess(args, 1, "", "Error from server (Forbidden)")

        rc = B.main(["--no-write", "--require-pass"], runner=broken)
        err = capsys.readouterr().err
        assert rc == 2
        assert "warning:" in err

    def test_expected_nodes_are_configurable(self, capsys):
        rc, _ = run(["--no-write", "--format", "json", "--expected-nodes", "gold,mach"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["snapshot"]["coverage"]["expected"] == ["gold", "mach"]
        assert payload["snapshot"]["coverage"]["complete"] is True

    def test_exclusions_are_configurable_and_always_carry_a_reason(self, capsys):
        rc, _ = run(["--no-write", "--format", "json", "--expected-nodes", "gold,mach",
                     "--excluded-node", "kasami=on the bench for mic rework"])
        payload = json.loads(capsys.readouterr().out)
        coverage = payload["snapshot"]["coverage"]
        assert coverage["excluded"] == {"kasami": "on the bench for mic rework"}
        assert "rankine" in coverage["unexpected"] or "rankine" not in coverage["present"]
        assert payload["grading"]["verdict"] in ("pass", "fail", "incomplete")

    def test_ttl_evidence_can_be_skipped_without_failing_the_run(self, capsys):
        rc, fake = run(["--no-write", "--format", "json", "--cache-key-prefix", ""])
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["snapshot"]["cache"]["ttl_seconds"] == {}
        assert all("TTL" not in call for call in fake.calls)
        names = {c["name"]: c["verdict"] for c in payload["grading"]["checks"]}
        assert names["cache_ttl_armed"] == "unknown"
