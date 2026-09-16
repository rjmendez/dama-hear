"""The cutover verifier must fail on the failures, not only pass on the happy path.

`tools/verify_heartbeat_cutover.py` is the instrument gate 1 closes on
(`deploy/images/service/README.md`), so a bug in it is indistinguishable from evidence. Every
test here therefore comes in pairs: the state the runbook expects, and the state that must stop
the cutover -- an unpublished digest, an optimistic manifest, a manifest that changed something
other than packaging, a ledger that lost rows or a device, a restart that swallowed a heartbeat
interval, a cache that started failing, a `/healthz` that quietly moved its Redis target, a
rollback that was written down rather than performed.

⚠️OFFLINE, AND IT MUTATES NOTHING. Fixtures are built in `tmp_path`: real SQLite ledgers with
the receiver's real schema, and copies of the committed manifests with one field edited. Nothing
here reaches a registry, a cluster or the network, and the verifier itself only ever opens a
database `mode=ro`.
"""
import copy
import json
import pathlib
import shutil
import sqlite3
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_heartbeat_cutover as V  # noqa: E402

APPLIED = ROOT / "deploy" / "k8s" / "hear-heartbeat.yaml"
PROPOSED = ROOT / "deploy" / "k8s" / "hear-heartbeat.proposed.yaml"

REAL_DIGEST = "sha256:" + "ab" * 32
DEVICES = ("nyquist", "shannon", "hartley", "fourier", "nikola", "bode")


# --------------------------------------------------------------------------- fixtures

def _write_yaml(path, docs):
    path.write_text("---\n" + "---\n".join(yaml.safe_dump(d, sort_keys=False) for d in docs))


def _docs(path):
    return [d for d in yaml.safe_load_all(pathlib.Path(path).read_text()) if d]


@pytest.fixture
def repo(tmp_path):
    """A minimal checkout: the two manifests, the bundle, the digest record, the lock, the file."""
    dst = tmp_path / "repo"
    (dst / "deploy" / "k8s").mkdir(parents=True)
    (dst / "deploy" / "images" / "service").mkdir(parents=True)
    (dst / "requirements" / "lock").mkdir(parents=True)
    for name in ("hear-heartbeat.yaml", "hear-heartbeat.proposed.yaml",
                 "hear-heartbeat-code.yaml"):
        shutil.copy(ROOT / "deploy" / "k8s" / name, dst / "deploy" / "k8s" / name)
    for name in ("digests.txt", "Dockerfile.hear-heartbeat"):
        shutil.copy(ROOT / "deploy" / "images" / "service" / name,
                    dst / "deploy" / "images" / "service" / name)
    shutil.copy(ROOT / V.LOCK, dst / V.LOCK)
    return dst


def _publish(repo, digest=REAL_DIGEST, tag="0ea3ae1"):
    """Simulate step 1 of the cutover: the digest is published and recorded in both places."""
    path = repo / "deploy" / "images" / "service" / "digests.txt"
    path.write_text(path.read_text().replace(
        "pending       pending", "%s       %s" % (tag, digest)))
    proposed = repo / "deploy" / "k8s" / "hear-heartbeat.proposed.yaml"
    proposed.write_text(proposed.read_text().replace(V.ZERO_DIGEST, digest))
    return digest


def _preflight(repo, **kw):
    args = V.build_parser().parse_args(["--repo", str(repo), "preflight"])
    for key, value in kw.items():
        setattr(args, key, value)
    args.applied = args.applied or repo / "deploy" / "k8s" / "hear-heartbeat.yaml"
    args.proposed = args.proposed or repo / "deploy" / "k8s" / "hear-heartbeat.proposed.yaml"
    report = V.Report("t")
    digest = V._check_provenance(report, repo, args.proposed)
    V._check_lock_and_source(report, repo)
    violations, _notes = V.diff_manifests(args.applied, args.proposed)
    report.add("the proposed manifest changes only packaging", not violations,
               "; ".join(violations))
    V._check_invariants(report, V._kind(V._read_yaml_docs(args.proposed), "Deployment"),
                        "proposed")
    V._check_registry_readiness(report, digest, args)
    V._check_live_drift(report, args)
    return report


def _failed(report):
    return {c["check"] for c in report.failed}


def make_ledger(path, devices=DEVICES, per_device=3, start="2026-09-15T12:00:00+00:00",
                succeeded=True):
    """A real ledger with the receiver's real schema, so the snapshot reads real SQL."""
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS durable_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, record_uid TEXT NOT NULL UNIQUE,
            telemetry_path TEXT NOT NULL, device_id TEXT NOT NULL, idempotency_key TEXT,
            payload_json TEXT NOT NULL, received_at TEXT NOT NULL,
            receiver_schema_version INTEGER NOT NULL, created_at TEXT NOT NULL,
            durable_schema_version INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS cache_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, record_uid TEXT NOT NULL,
            cache_target TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
            error_text TEXT, created_at TEXT NOT NULL,
            FOREIGN KEY(record_uid) REFERENCES durable_records(record_uid));
        CREATE TABLE IF NOT EXISTS cache_claims (
            record_uid TEXT PRIMARY KEY, claimed_at TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS durable_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS refused_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, reason TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
    con.close()
    return extend_ledger(path, devices=devices, per_device=per_device, start=start,
                         succeeded=succeeded)


def extend_ledger(path, devices=DEVICES, per_device=3, start="2026-09-15T12:00:00+00:00",
                  succeeded=True):
    """Append the next window of heartbeats to an existing ledger -- the same file, continuing.

    A cutover does not hand the pod a new database; it restarts the process on the one that is
    already there. Fixtures that build two unrelated files cannot see a lost row, so the
    post-cutover ledger here is always the pre-cutover one, extended.
    """
    import datetime as dt
    t0 = dt.datetime.fromisoformat(start)
    con = sqlite3.connect(str(path))
    seq = con.execute("SELECT COALESCE(MAX(id), 0) FROM durable_records").fetchone()[0]
    for device in devices:
        for i in range(per_device):
            seq += 1
            ts = (t0 + dt.timedelta(seconds=30 * i)).isoformat()
            ruid = "%s-%d" % (device, seq)
            con.execute(
                "INSERT INTO durable_records (record_uid, telemetry_path, device_id, "
                "payload_json, received_at, receiver_schema_version, created_at, "
                "durable_schema_version) VALUES (?,?,?,?,?,?,?,?)",
                (ruid, "hear/heartbeat", device, "{}", ts, 1, ts, 1))
            if succeeded:
                con.execute(
                    "INSERT INTO cache_attempts (record_uid, cache_target, outcome, created_at) "
                    "VALUES (?,?,?,?)", (ruid, "redis", "succeeded", ts))
    con.commit()
    con.close()
    return pathlib.Path(path)


def _continued(tmp_path, before_db, name, **kw):
    """The same ledger, after the cutover: a copy of the file plus the rows the new pod wrote."""
    dst = pathlib.Path(shutil.copy(before_db, tmp_path / name))
    return extend_ledger(dst, **kw)


def _health(redis_target="audit-redis.infra.svc.cluster.local:6379", path="/state/x.sqlite3",
            successes=10, failures=0, pending=0):
    return {
        "status": "ok",
        "service": "hear-heartbeat-receiver",
        "redis_target": redis_target,
        "durable_store": {"backend": "sqlite", "enabled": True, "path": path,
                          "pending_records": pending, "cache_successes": successes,
                          "cache_failures": failures, "last_cache_failure_at": None},
        "refusals": {"refused_total": 0, "suppressed_refusals": 0},
    }


def _pod(image, image_id=None, ready=True, volumes=("state",), count=1):
    items = []
    for i in range(count):
        items.append({
            "kind": "Pod",
            "metadata": {"name": "hear-heartbeat-%d" % i},
            "spec": {"volumes": [{"name": v} for v in volumes]},
            "status": {
                "phase": "Running",
                "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                "containerStatuses": [{"name": "receiver", "image": image,
                                       "imageID": image_id or image}],
            },
        })
    return {"items": items}


# --------------------------------------------------------------------------- provenance

def test_a_pending_digest_blocks_the_cutover_and_says_so(repo):
    """⚠️`pending` is a blocker, not a default: nothing was published, so nothing can be applied."""
    report = _preflight(repo)
    failed = _failed(report)
    assert "digest published by a `main` build and recorded" in failed
    assert not report.ok
    detail = next(c["detail"] for c in report.checks
                  if c["check"] == "digest published by a `main` build and recorded")
    assert "pull request publishes nothing" in detail


def test_the_sentinel_is_required_while_the_digest_is_pending(repo):
    """An optimistic manifest is the one thing the all-zero sentinel exists to prevent."""
    proposed = repo / "deploy" / "k8s" / "hear-heartbeat.proposed.yaml"
    proposed.write_text(proposed.read_text().replace(V.ZERO_DIGEST, REAL_DIGEST))
    assert ("unpublished manifest carries the all-zero sentinel and stays unappliable"
            in _failed(_preflight(repo)))


def test_a_published_and_recorded_digest_passes_provenance(repo):
    _publish(repo)
    report = _preflight(repo)
    assert report.ok, sorted(_failed(report))


def test_a_manifest_that_disagrees_with_the_record_fails(repo):
    _publish(repo)
    proposed = repo / "deploy" / "k8s" / "hear-heartbeat.proposed.yaml"
    proposed.write_text(proposed.read_text().replace(REAL_DIGEST, "sha256:" + "cd" * 32))
    assert "manifest digest equals the recorded digest" in _failed(_preflight(repo))


def test_a_tag_reference_is_never_acceptable(repo):
    _publish(repo)
    proposed = repo / "deploy" / "k8s" / "hear-heartbeat.proposed.yaml"
    proposed.write_text(proposed.read_text().replace(
        "hear-heartbeat@" + REAL_DIGEST, "hear-heartbeat:latest"))
    failed = _failed(_preflight(repo))
    assert "proposed manifest references a digest, never a tag" in failed


def test_the_zero_digest_is_never_accepted_as_a_recorded_digest(repo):
    _publish(repo, digest=V.ZERO_DIGEST)
    assert "digest is not the sentinel" in _failed(_preflight(repo))


# --------------------------------------------------------------------------- allowlist diff

def test_the_committed_cutover_diff_is_inside_the_allowlist():
    violations, _notes = V.diff_manifests(APPLIED, PROPOSED)
    assert violations == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda c, s, d: c["env"].pop(), "env"),
    (lambda c, s, d: c.pop("livenessProbe"), "livenessProbe"),
    (lambda c, s, d: c["ports"][0].update(hostPort=5052), "hostPort"),
    (lambda c, s, d: c["resources"]["limits"].update(memory="512Mi"), "resources"),
    (lambda c, s, d: d["spec"]["strategy"].update(type="RollingUpdate"), "strategy"),
    (lambda c, s, d: d["spec"].update(replicas=2), "replicas"),
    (lambda c, s, d: s.update(hostNetwork=False), "hostNetwork"),
])
def test_anything_beyond_packaging_is_a_violation(tmp_path, mutate, expect):
    """⚠️The whole review, mechanised: "I only meant to change the packaging" is unverifiable."""
    docs = _docs(PROPOSED)
    dep = next(d for d in docs if d["kind"] == "Deployment")
    mutate(V._container(dep), V._pod_spec(dep), dep)
    bad = tmp_path / "bad.yaml"
    _write_yaml(bad, docs)
    violations, _notes = V.diff_manifests(APPLIED, bad)
    assert violations, "mutating %s produced no violation" % expect


def test_the_configmap_restart_annotation_goes_with_the_mount_it_restarts_for():
    """⚠️THE SIXTH ALLOWED CHANGE, AND ONLY BECAUSE THE MOUNT IS GONE.

    `checksum/hear-heartbeat-code` exists (deploy/k8s/gen_configmap.py) to force a restart when
    the subPath-mounted ConfigMap changes. The proposed pod mounts no ConfigMap, so the
    annotation has nothing to force and drops with `code`. The committed pair proves it.
    """
    applied = V._kind(V._read_yaml_docs(APPLIED), "Deployment")
    annotations = applied["spec"]["template"]["metadata"].get("annotations") or {}
    assert "checksum/%s" % V.BUNDLE in annotations, (
        "the applied manifest no longer carries the annotation this allowance is about")
    violations, _notes = V.diff_manifests(APPLIED, PROPOSED)
    assert violations == []


def test_a_kept_restart_annotation_must_still_agree(tmp_path):
    """The allowance is a deletion, not a blank cheque: a retained key is compared on its value."""
    docs = _docs(PROPOSED)
    dep = next(d for d in docs if d["kind"] == "Deployment")
    dep["spec"]["template"].setdefault("metadata", {})["annotations"] = {
        "checksum/%s" % V.BUNDLE: "0" * 64}
    bad = tmp_path / "bad.yaml"
    _write_yaml(bad, docs)
    violations, _notes = V.diff_manifests(APPLIED, bad)
    assert any("differs by more than the image" in v for v in violations)


def test_an_unrelated_pod_annotation_is_not_covered_by_the_allowance(tmp_path):
    """Only the bundle's own checksum key is allowed to go; anything else is a real change."""
    docs = _docs(APPLIED)
    dep = next(d for d in docs if d["kind"] == "Deployment")
    dep["spec"]["template"].setdefault("metadata", {}).setdefault(
        "annotations", {})["dama-hear/unrelated"] = "true"
    applied = tmp_path / "applied.yaml"
    _write_yaml(applied, docs)
    violations, _notes = V.diff_manifests(applied, PROPOSED)
    assert any("differs by more than the image" in v for v in violations)


def test_the_state_volume_may_not_be_dropped(tmp_path):
    docs = _docs(PROPOSED)
    dep = next(d for d in docs if d["kind"] == "Deployment")
    spec = V._pod_spec(dep)
    spec["volumes"] = [v for v in spec["volumes"] if v["name"] != "state"]
    V._container(dep)["volumeMounts"] = []
    bad = tmp_path / "bad.yaml"
    _write_yaml(bad, docs)
    violations, _notes = V.diff_manifests(APPLIED, bad)
    assert any("may remove only code and deps" in v for v in violations)


def test_a_restated_entrypoint_is_a_second_place_to_drift(tmp_path):
    docs = _docs(PROPOSED)
    dep = next(d for d in docs if d["kind"] == "Deployment")
    V._container(dep)["command"] = ["python", "/app/tools/hear_heartbeat_receiver.py"]
    bad = tmp_path / "bad.yaml"
    _write_yaml(bad, docs)
    violations, _notes = V.diff_manifests(APPLIED, bad)
    assert any("restates command/args" in v for v in violations)


def test_a_pip_install_in_the_cutover_manifest_is_a_violation(tmp_path):
    docs = _docs(PROPOSED)
    dep = next(d for d in docs if d["kind"] == "Deployment")
    V._container(dep)["lifecycle"] = {
        "postStart": {"exec": {"command": ["sh", "-c", "pip install redis"]}}}
    bad = tmp_path / "bad.yaml"
    _write_yaml(bad, docs)
    violations, _notes = V.diff_manifests(APPLIED, bad)
    assert any("installs a package at pod start" in v for v in violations)


def test_the_service_document_may_not_move(tmp_path):
    docs = _docs(PROPOSED)
    svc = next(d for d in docs if d["kind"] == "Service")
    svc["spec"]["ports"][0]["port"] = 5052
    bad = tmp_path / "bad.yaml"
    _write_yaml(bad, docs)
    violations, _notes = V.diff_manifests(APPLIED, bad)
    assert any("touches the Deployment and nothing else" in v for v in violations)


def test_a_nonroot_uid_in_the_packaging_change_is_caught():
    """The identity boundary: 65532 on a root-owned local-path PVC is a write failure after apply."""
    dep = copy.deepcopy(V._kind(V._read_yaml_docs(PROPOSED), "Deployment"))
    V._container(dep)["securityContext"] = {"runAsUser": 65532, "runAsNonRoot": True}
    report = V.Report("t")
    V._check_invariants(report, dep, "proposed")
    assert "proposed: the uid is not changed by the packaging cutover" in _failed(report)


# --------------------------------------------------------------------------- registry / pre-pull

def test_a_warm_node_is_provable_and_a_cold_one_is_not(repo, tmp_path):
    digest = _publish(repo)
    warm = tmp_path / "warm.txt"
    warm.write_text("ghcr.io/rjmendez/dama-hear/hear-heartbeat@%s  application/vnd.oci...\n"
                    % digest)
    assert _preflight(repo, node_images=str(warm)).ok
    cold = tmp_path / "cold.txt"
    cold.write_text("docker.io/library/python:3.13-slim\n")
    assert "the digest is already resident on the node" in _failed(
        _preflight(repo, node_images=str(cold)))


def test_an_always_pull_policy_makes_the_recreate_gap_a_registry_round_trip(repo, tmp_path):
    _publish(repo)
    docs = _docs(repo / "deploy" / "k8s" / "hear-heartbeat.proposed.yaml")
    dep = next(d for d in docs if d["kind"] == "Deployment")
    V._container(dep)["imagePullPolicy"] = "Always"
    path = tmp_path / "always.yaml"
    _write_yaml(path, docs)
    assert "imagePullPolicy is left at the digest default" in _failed(
        _preflight(repo, proposed=path))


# --------------------------------------------------------------------------- live drift

def test_live_drift_against_the_committed_manifest_is_reported(repo, tmp_path):
    _publish(repo)
    live = copy.deepcopy(V._kind(V._read_yaml_docs(APPLIED), "Deployment"))
    live["kind"] = "Deployment"
    V._container(live)["env"] = [e for e in V._container(live)["env"]
                                 if e["name"] != "HEAR_DURABLE_REPLAY_LIMIT"]
    path = tmp_path / "live.json"
    path.write_text(json.dumps(live))
    failed = _failed(_preflight(repo, live_deployment=str(path)))
    assert "live: all nine env vars present" in failed
    assert "live env equals the committed env" in failed


def test_a_live_object_matching_the_repo_passes_drift(repo, tmp_path):
    _publish(repo)
    live = copy.deepcopy(V._kind(V._read_yaml_docs(APPLIED), "Deployment"))
    path = tmp_path / "live.json"
    path.write_text(json.dumps(live))
    report = _preflight(repo, live_deployment=str(path))
    assert report.ok, sorted(_failed(report))


# --------------------------------------------------------------------------- snapshot

def test_a_snapshot_reads_the_ledger_without_writing_it(tmp_path):
    db = make_ledger(tmp_path / "ledger.sqlite3")
    before = db.stat().st_mtime_ns
    snap = V.snapshot(db)
    assert snap["records_total"] == len(DEVICES) * 3
    assert sorted(snap["devices"]) == sorted(DEVICES)
    assert snap["integrity"] == "ok"
    assert snap["missing_tables"] == []
    assert snap["cache_attempts"]["succeeded"] == len(DEVICES) * 3
    assert db.stat().st_mtime_ns == before, "the snapshot modified the database"
    assert not (tmp_path / "ledger.sqlite3-wal").exists()


def test_a_hot_wal_database_is_refused_unless_explicitly_allowed(tmp_path):
    db = make_ledger(tmp_path / "hot.sqlite3")
    (tmp_path / "hot.sqlite3-wal").write_bytes(b"\x00" * 64)
    with pytest.raises(V.InputError, match="torn read"):
        V.snapshot(db)
    assert V.snapshot(db, allow_hot=True)["integrity"] == "ok"


def test_a_database_that_is_not_the_ledger_is_an_input_error_not_a_pass(tmp_path):
    other = tmp_path / "other.sqlite3"
    con = sqlite3.connect(str(other))
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    snap = V.snapshot(other)
    assert snap["missing_tables"] == list(V.LEDGER_TABLES)


# --------------------------------------------------------------------------- compare

def _cutover(tmp_path, after_start="2026-09-15T12:02:40+00:00", after_rows=5,
             devices=DEVICES, succeeded=True, before_rows=3):
    """before/after snapshots of one ledger that a Recreate cycle restarted in the middle of."""
    db = make_ledger(tmp_path / "ledger.sqlite3", per_device=before_rows)
    before = V.snapshot(db, label="before")
    after_db = _continued(tmp_path, db, "after.sqlite3", per_device=after_rows,
                          start=after_start, devices=devices, succeeded=succeeded)
    return before, V.snapshot(after_db, label="after", since_id=before["max_record_id"]), after_db


def test_a_clean_cutover_conserves_every_record_and_every_device(tmp_path):
    before, after, _db = _cutover(tmp_path)
    report = V.compare_snapshots(before, after)
    assert report.ok, sorted(_failed(report))


def test_a_lost_record_fails_conservation(tmp_path):
    """⚠️A `Recreate` that dropped rows is the failure this whole gate exists to detect."""
    before, _after, db = _cutover(tmp_path)
    con = sqlite3.connect(str(db))
    con.execute("DELETE FROM cache_attempts")
    con.execute("DELETE FROM durable_records WHERE id <= 4")
    con.commit()
    con.close()
    after = V.snapshot(db, since_id=before["max_record_id"])
    assert ("every record written before the cutover is still in the ledger"
            in _failed(V.compare_snapshots(before, after)))


def test_a_device_that_stops_reporting_is_caught(tmp_path):
    before, after, db = _cutover(tmp_path, devices=DEVICES[:-1])
    con = sqlite3.connect(str(db))
    con.execute("DELETE FROM cache_attempts WHERE record_uid LIKE ?", (DEVICES[-1] + "-%",))
    con.execute("DELETE FROM durable_records WHERE device_id = ?", (DEVICES[-1],))
    con.commit()
    con.close()
    after = V.snapshot(db, since_id=before["max_record_id"])
    failed = _failed(V.compare_snapshots(before, after))
    assert "every device that was in the ledger is still in it" in failed
    assert "the expected device count is present after the cutover" in failed


def test_a_device_present_but_silent_after_the_cutover_is_caught(tmp_path):
    """It still has rows, from *before*. Nothing it sent after the restart was recorded."""
    before, after, db = _cutover(tmp_path, devices=DEVICES[:-1])
    failed = _failed(V.compare_snapshots(before, after))
    assert "every device reported again after the cutover" in failed


def test_a_swallowed_heartbeat_interval_is_a_continuity_failure(tmp_path):
    before, after, _db = _cutover(tmp_path, after_start="2026-09-15T13:30:00+00:00")
    failed = _failed(V.compare_snapshots(before, after, interval_s=30, gap_s=180))
    assert "received_at is continuous across the cutover, per device" in failed


def test_a_recreate_gap_inside_the_budget_is_not_a_failure(tmp_path):
    """One pod cycle is a gap the gate expects, measured to the first row after, not the last."""
    before, after, _db = _cutover(tmp_path, after_start="2026-09-15T12:02:40+00:00",
                                  after_rows=20)
    report = V.compare_snapshots(before, after, interval_s=30, gap_s=180)
    assert "received_at is continuous across the cutover, per device" not in _failed(report)


def test_continuity_measured_without_the_watermark_says_so(tmp_path):
    """⚠️Comparing the two maxima only proves the device is alive now, and the tool admits it."""
    before, _after, db = _cutover(tmp_path)
    after = V.snapshot(db, label="after")  # no --since-id
    report = V.compare_snapshots(before, after)
    assert any("continuity measured from the wrong end" in c["check"] for c in report.checks)
    assert any("re-take the after snapshot" in str(c["detail"]) for c in report.checks)


def test_new_failed_cache_attempts_after_cutover_fail_the_gate(tmp_path):
    before, _after, db = _cutover(tmp_path)
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO cache_attempts (record_uid, cache_target, outcome, created_at) "
                "VALUES ((SELECT record_uid FROM durable_records LIMIT 1), 'redis', 'failed', ?)",
                ("2026-09-15T12:05:00+00:00",))
    con.commit()
    con.close()
    after = V.snapshot(db, since_id=before["max_record_id"])
    assert "zero failed cache attempts after the cutover" in _failed(
        V.compare_snapshots(before, after))


def test_a_ready_pod_that_caches_nothing_is_not_a_working_cutover(tmp_path):
    """Durable-first is the contract, but a Redis that is never written is a silent outage."""
    before, after, _db = _cutover(tmp_path, succeeded=False)
    assert "the cache path is alive after the cutover" in _failed(
        V.compare_snapshots(before, after))


def test_a_ledger_that_did_not_advance_is_not_evidence(tmp_path):
    db = make_ledger(tmp_path / "ledger.sqlite3")
    before = V.snapshot(db, label="before")
    after = V.snapshot(db, label="after", since_id=before["max_record_id"])
    failed = _failed(V.compare_snapshots(before, after))
    assert "new traffic was accepted after the cutover" in failed


def test_a_reformatted_schema_is_out_of_bounds_for_a_packaging_change(tmp_path):
    before, _after, db = _cutover(tmp_path)
    con = sqlite3.connect(str(db))
    con.execute("ALTER TABLE durable_records ADD COLUMN site TEXT")
    con.commit()
    con.close()
    after = V.snapshot(db, since_id=before["max_record_id"])
    assert "the schema is byte-identical across the cutover" in _failed(
        V.compare_snapshots(before, after))


def test_retention_pruning_explains_a_smaller_ledger(tmp_path):
    """Pruning only ever removes an already-cached record; it must not read as data loss."""
    before, _after, db = _cutover(tmp_path)
    con = sqlite3.connect(str(db))
    con.execute("DELETE FROM cache_attempts WHERE record_uid IN "
                "(SELECT record_uid FROM durable_records WHERE id <= 12)")
    con.execute("DELETE FROM durable_records WHERE id <= 12")
    con.commit()
    con.close()
    after = V.snapshot(db, since_id=before["max_record_id"])
    strict = V.compare_snapshots(before, after)
    assert ("every record written before the cutover is still in the ledger"
            in _failed(strict)), "the default tolerance for losing pre-cutover rows is zero"
    declared = V.compare_snapshots(before, after, allow_pruned=12)
    assert declared.ok, sorted(_failed(declared))
    assert any("retention pruning" in c["check"] for c in declared.checks)


def test_the_same_comparison_is_applied_to_the_rollback(tmp_path):
    """The ledger must be continuous across *both* transitions, not only the interesting one."""
    before, after, db = _cutover(tmp_path)
    rb_db = _continued(tmp_path, db, "rollback.sqlite3", per_device=4,
                       start="2026-09-15T12:07:00+00:00")
    rollback = V.snapshot(rb_db, label="rollback", since_id=after["max_record_id"])
    report = V.compare_snapshots(after, rollback, transition="rollback")
    assert report.ok, sorted(_failed(report))
    assert any("rollback" in c["check"] for c in report.checks)


# --------------------------------------------------------------------------- health

def test_an_unchanged_healthz_passes():
    report = V.compare_health(_health(), _health(successes=42))
    assert report.ok, sorted(_failed(report))


def test_a_moved_redis_target_is_a_configuration_change_in_a_packaging_cutover():
    assert "redis_target is unchanged" in _failed(
        V.compare_health(_health(), _health(redis_target="other:6379")))


def test_a_moved_durable_path_is_caught():
    assert "durable_store.path is unchanged" in _failed(
        V.compare_health(_health(), _health(path="/state/other.sqlite3")))


def test_new_cache_failures_reported_by_the_pod_are_caught():
    assert "no new cache failures are reported by the pod" in _failed(
        V.compare_health(_health(), _health(failures=3)))


def test_a_missing_healthz_key_is_a_shape_change():
    after = _health()
    after.pop("refusals")
    assert "/healthz returns the same shape as before" in _failed(
        V.compare_health(_health(), after))


def test_durability_silently_disabled_is_caught():
    after = _health()
    after["durable_store"]["enabled"] = False
    after["durable_store"]["backend"] = "none"
    failed = _failed(V.compare_health(_health(), after))
    assert "durability is still enabled" in failed
    assert "durable_store.backend is unchanged" in failed


# --------------------------------------------------------------------------- receipt

def _receipt_args(repo, **kw):
    args = V.build_parser().parse_args(["--repo", str(repo), "receipt"])
    for key, value in kw.items():
        setattr(args, key, value)
    return args


def _full_evidence(repo, tmp_path, digest):
    """Everything an operator would have captured after a clean cutover and a real rollback."""
    before, after, db = _cutover(tmp_path)
    rb_db = _continued(tmp_path, db, "rollback.sqlite3", per_device=4,
                       start="2026-09-15T12:07:00+00:00")
    rollback = V.snapshot(rb_db, label="rollback", since_id=after["max_record_id"])
    paths = {}
    for name, obj in (("snap_before", before), ("snap_after", after), ("snap_rb", rollback),
                      ("health_before", _health()), ("health_after", _health(successes=42)),
                      ("pod", _pod("ghcr.io/rjmendez/dama-hear/hear-heartbeat@" + digest,
                                   image_id="ghcr.io/rjmendez/dama-hear/hear-heartbeat@" + digest)),
                      ("pod_rb", _pod("python:3.13-slim",
                                      volumes=("code", "deps", "state")))):
        p = tmp_path / ("%s.json" % name)
        p.write_text(json.dumps(obj))
        paths[name] = str(p)
    logs = tmp_path / "logs.txt"
    logs.write_text("hear-heartbeat receiver listening on 0.0.0.0:5051\n")
    redis = tmp_path / "redis.txt"
    redis.write_text("dama:hear:heartbeat:nyquist\ndama:hear:heartbeat:shannon\n(integer) 27\n")
    cm = tmp_path / "cm.json"
    cm.write_text(json.dumps({"kind": "ConfigMap", "metadata": {"name": V.BUNDLE},
                              "data": {"tools_hear_heartbeat_receiver.py": "x"}}))
    return _receipt_args(
        repo, pod=paths["pod"], logs=str(logs),
        health_before=paths["health_before"], health_after=paths["health_after"],
        snapshot_before=paths["snap_before"], snapshot_after=paths["snap_after"],
        snapshot_rollback=paths["snap_rb"], rollback_pod=paths["pod_rb"],
        redis_keys=str(redis), live_configmap=str(cm)), paths


def test_a_complete_evidence_set_closes_all_ten_rows(repo, tmp_path):
    digest = _publish(repo)
    args, _paths = _full_evidence(repo, tmp_path, digest)
    rows = V.build_receipt(args)
    assert len(rows) == 10
    assert all(r["state"] == "PASS" for r in rows.values()), \
        {n: r for n, r in rows.items() if r["state"] != "PASS"}


def test_a_lost_record_fails_the_receipt_on_row_four(repo, tmp_path):
    """⚠️An unattributed ledger failure must never vanish between the compare and the receipt."""
    digest = _publish(repo)
    args, paths = _full_evidence(repo, tmp_path, digest)
    after = json.loads(pathlib.Path(paths["snap_after"]).read_text())
    after["retained_below_watermark"] -= 3
    pathlib.Path(paths["snap_after"]).write_text(json.dumps(after))
    rows = V.build_receipt(args)
    assert rows[4]["state"] == "FAIL"
    assert "still in the ledger" in rows[4]["detail"]


def test_a_receipt_with_no_artifacts_is_missing_not_passing(repo):
    args = _receipt_args(repo)
    rows = V.build_receipt(args)
    assert rows[1]["state"] == V.MISSING
    assert rows[10]["state"] == "FAIL", "a pending digest cannot pass row 10"


def test_a_pod_running_a_tag_fails_row_one(repo, tmp_path):
    digest = _publish(repo)
    args, _paths = _full_evidence(repo, tmp_path, digest)
    pod = tmp_path / "tagpod.json"
    pod.write_text(json.dumps(_pod("ghcr.io/rjmendez/dama-hear/hear-heartbeat:0ea3ae1",
                                   image_id="ghcr.io/rjmendez/dama-hear/hear-heartbeat"
                                            "@sha256:" + "ee" * 32)))
    args.pod = str(pod)
    assert V.build_receipt(args)[1]["state"] == "FAIL"


def test_two_pods_on_one_rwo_pvc_fail_row_one(repo, tmp_path):
    """⚠️Two receivers on one SQLite ledger is what `Recreate` exists to make impossible."""
    digest = _publish(repo)
    args, _paths = _full_evidence(repo, tmp_path, digest)
    pod = tmp_path / "twopods.json"
    pod.write_text(json.dumps(_pod("ghcr.io/rjmendez/dama-hear/hear-heartbeat@" + digest,
                                   count=2)))
    args.pod = str(pod)
    row = V.build_receipt(args)[1]
    assert row["state"] == "FAIL"
    assert "Recreate" in row["detail"]


def test_a_pip_install_in_the_pod_log_fails_row_seven(repo, tmp_path):
    digest = _publish(repo)
    args, _paths = _full_evidence(repo, tmp_path, digest)
    logs = tmp_path / "badlogs.txt"
    logs.write_text("installing redis==7.4.0 into /deps\nreceiver listening\n")
    args.logs = str(logs)
    assert V.build_receipt(args)[7]["state"] == "FAIL"


def test_a_pod_that_still_mounts_the_configmap_fails_row_eight(repo, tmp_path):
    digest = _publish(repo)
    args, _paths = _full_evidence(repo, tmp_path, digest)
    pod = tmp_path / "cmpod.json"
    pod.write_text(json.dumps(_pod("ghcr.io/rjmendez/dama-hear/hear-heartbeat@" + digest,
                                   volumes=("code", "deps", "state"))))
    args.pod = str(pod)
    assert V.build_receipt(args)[8]["state"] == "FAIL"


def test_disarmed_redis_keys_fail_row_six(repo, tmp_path):
    digest = _publish(repo)
    args, _paths = _full_evidence(repo, tmp_path, digest)
    redis = tmp_path / "noredis.txt"
    redis.write_text("")
    args.redis_keys = str(redis)
    assert V.build_receipt(args)[6]["state"] == "FAIL"


def test_a_rollback_that_was_only_written_down_does_not_close_row_nine(repo, tmp_path):
    """⚠️Rollback is part of the gate, not a contingency."""
    digest = _publish(repo)
    args, _paths = _full_evidence(repo, tmp_path, digest)
    args.rollback_pod = None
    assert V.build_receipt(args)[9]["state"] == V.MISSING


def test_a_rollback_onto_the_wrong_image_fails_row_nine(repo, tmp_path):
    digest = _publish(repo)
    args, _paths = _full_evidence(repo, tmp_path, digest)
    pod = tmp_path / "wrongrb.json"
    pod.write_text(json.dumps(_pod("ghcr.io/rjmendez/dama-hear/hear-heartbeat@" + digest)))
    args.rollback_pod = str(pod)
    assert V.build_receipt(args)[9]["state"] == "FAIL"


def test_a_rollback_that_loses_records_fails_row_nine(repo, tmp_path):
    digest = _publish(repo)
    args, _paths = _full_evidence(repo, tmp_path, digest)
    truncated = V.snapshot(make_ledger(tmp_path / "truncated.sqlite3", per_device=1),
                           label="rollback", since_id=10_000)
    path = tmp_path / "truncated.json"
    path.write_text(json.dumps(truncated))
    args.snapshot_rollback = str(path)
    assert V.build_receipt(args)[9]["state"] == "FAIL"


# --------------------------------------------------------------------------- CLI surface

def test_plan_refuses_to_print_an_apply_procedure_for_an_unpublished_digest(repo, capsys):
    rc = V.main(["--repo", str(repo), "plan"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "CUTOVER BLOCKED AT STEP 1" in out
    assert "kubectl" not in out.split("To unblock")[0]
    assert "apply -f" not in out


def test_plan_prints_the_procedure_once_the_digest_is_recorded(repo, capsys):
    digest = _publish(repo)
    rc = V.main(["--repo", str(repo), "plan"])
    out = capsys.readouterr().out
    assert rc == 0
    assert digest in out
    for expected in ("imagetools inspect", "ctr images pull", "apply -f deploy/k8s/"
                     "hear-heartbeat.proposed.yaml", "rollout status", "rollout undo",
                     "backup", "ROLLBACK"):
        assert expected in out, expected


def test_the_rollback_procedure_is_printable_on_its_own(repo, capsys):
    rc = V.main(["--repo", str(repo), "plan", "--rollback"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "deploy/k8s/hear-heartbeat.yaml" in out and "hear-heartbeat-code.yaml" in out
    assert "No state is reversed in either direction" in out


def test_the_tool_never_offers_a_way_to_mutate_anything():
    """⚠️An evidence tool that can mutate the thing it measures is not evidence."""
    import ast
    source = (ROOT / "tools" / "verify_heartbeat_cutover.py").read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("subprocess", "urllib", "http", "requests", "socket", "docker",
                      "kubernetes", "redis", "shutil"):
        assert forbidden not in imported, (
            "%s is imported by the verifier: it reads files, and nothing else" % forbidden)

    connects = [n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "connect"]
    assert len(connects) == 1, "every sqlite connection must go through the one mode=ro helper"
    assert "mode=ro" in source

    writes = [n for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr in ("write_text", "write_bytes", "unlink", "mkdir", "rename")]
    assert len(writes) == 1, (
        "the verifier writes %d times; the only permitted write is the snapshot --out file"
        % len(writes))


def test_the_runbook_exists_and_is_wired_into_the_documentation_index():
    """A procedure nobody can find is not a procedure."""
    runbook = ROOT / "docs" / "hear-heartbeat-cutover-runbook.md"
    assert runbook.exists(), "docs/hear-heartbeat-cutover-runbook.md is missing"
    text = runbook.read_text()
    for token in ("tools/verify_heartbeat_cutover.py",
                  "deploy/k8s/hear-heartbeat.proposed.yaml",
                  "deploy/k8s/hear-heartbeat.yaml",
                  "deploy/k8s/hear-heartbeat-code.yaml",
                  "deploy/images/service/digests.txt",
                  "rollout undo", "Recreate", "sqlite3", "backup", "--since-id",
                  "imagetools inspect", "ctr images pull", "attestation"):
        assert token in text, "the runbook never mentions %s" % token
    index = (ROOT / "docs" / "README.md").read_text()
    assert "hear-heartbeat-cutover-runbook.md" in index, (
        "the runbook is not listed in docs/README.md")


def test_the_runbook_does_not_promise_an_appliable_manifest_today():
    """⚠️While the digest is `pending`, the runbook must say so above the apply step."""
    text = (ROOT / "docs" / "hear-heartbeat-cutover-runbook.md").read_text()
    blocked = text.index("BLOCKED")
    apply_step = text.index("kubectl -n $NS apply -f deploy/k8s/hear-heartbeat.proposed.yaml")
    assert blocked < apply_step, (
        "the blocked-by-construction warning must precede the apply command")


def test_main_returns_two_on_unusable_input(repo, tmp_path, capsys):
    missing = tmp_path / "nope.json"
    assert V.main(["--repo", str(repo), "compare", "--before", str(missing),
                   "--after", str(missing)]) == 2


def test_preflight_on_this_checkout_is_blocked_only_by_the_pending_digest(capsys):
    """The repository as merged: everything holds except the live step that CI cannot perform."""
    rc = V.main(["preflight"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "1 blocking check(s) failed" in out
    assert "digests.txt row is `pending`" in out
