"""The G0 gate of `docs/pool-backup-restore.md`, run against synthetic fixtures only.

⚠️EVERY TEST HERE BUILDS ITS OWN POOL OUT OF `tmp_path`. Nothing in this module reads `/pool`,
mounts a volume, talks to the cluster, or writes outside pytest's temporary directory -- and
`test_g0_12_the_live_pool_is_refused` is the test that keeps it that way: the tool refuses a
`--pool` under `/pool` before it stats anything, so a future edit that "just points it at the
real corpus to see" fails here rather than in production.

⚠️NO REAL PASSPHRASE EXISTS ANYWHERE IN THIS SUITE. The encrypted paths are exercised with a
throwaway file created inside `tmp_path` and destroyed with it; the plaintext path is reachable
only through `--cipher none --insecure-plaintext` AND a sentinel marked `synthetic`, which the
real volume's sentinel will never be. `test_g0_09_key_custody_is_external_only` asserts both
refusals. Custody of the live passphrase is an open policy question (plan §14.2) and this suite
deliberately cannot answer it.

The numbering maps one-to-one onto the plan's acceptance table, so a red test names the gate it
just failed.
"""
import gzip
import importlib.util
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "hear_pool_backup.py"
MANIFEST = ROOT / "deploy" / "k8s" / "hear-pool-backup.yaml"
SENTINEL_UUID = "5f0f0b1a-0000-4000-8000-0000000000aa"


def _load():
    spec = importlib.util.spec_from_file_location("hear_pool_backup", TOOL)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves annotations through sys.modules, and a module
    # that is not there yet raises instead.
    sys.modules["hear_pool_backup"] = module
    spec.loader.exec_module(module)
    return module


hpb = _load()


# --------------------------------------------------------------------------- fixtures


def make_pool(root):
    """A miniature of the real pool: one of each class the plan treats differently."""
    pool = root / "pool"
    (pool / "corpus" / "raw" / "node-a").mkdir(parents=True)
    (pool / "corpus" / "clips").mkdir(parents=True)
    (pool / "corpus" / "scene" / "20260915").mkdir(parents=True)
    (pool / "corpus" / "records").mkdir(parents=True)
    (pool / "corpus" / "state").mkdir(parents=True)
    (pool / "models").mkdir(parents=True)
    (pool / "sketch_corpus").mkdir(parents=True)
    # Excluded: vendored site-packages, and the scratch files the clip lane sweeps.
    (pool / "pylib" / "numpy").mkdir(parents=True)
    (pool / "pylib" / "numpy" / "big.so").write_bytes(b"x" * 4096)
    (pool / "corpus" / "clips" / "half.wav.tmp").write_bytes(b"torn")

    (pool / "corpus" / "raw" / "node-a" / "20260915-dets.csv").write_text("t,f\n1,2\n")
    (pool / "corpus" / "raw" / "node-a" / "20260915-health.csv").write_text("t,v\n1,9\n")
    (pool / "corpus" / "clips" / "index.jsonl").write_text('{"clip":"a"}\n{"clip":"b"}\n')
    (pool / "corpus" / "clips" / "a.wav").write_bytes(b"RIFF" + b"\0" * 64)
    (pool / "corpus" / "ledger.jsonl").write_text('{"src":"a"}\n')
    (pool / "corpus" / "records" / "day.jsonl").write_text('{"r":1}\n')
    (pool / "corpus" / "state" / "watermarks.json").write_text('{"w":1}\n')
    (pool / "models" / "m.json").write_text('{"model":1}\n')
    (pool / "sketch_corpus" / "s.jsonl").write_text('{"s":1}\n')
    with gzip.open(pool / "corpus" / "scene" / "20260915" / "node-a.jsonl.gz", "at") as handle:
        handle.write('{"scene":1}\n')
    with gzip.open(pool / "corpus" / "scene" / "20260915" / "node-a.jsonl.gz", "at") as handle:
        handle.write('{"scene":2}\n')

    conn = sqlite3.connect(pool / "corpus" / "annotations.sqlite3")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE annotation (id INTEGER PRIMARY KEY, label TEXT)")
    conn.execute("INSERT INTO annotation (label) VALUES ('shot')")
    conn.commit()
    conn.close()
    return pool


def make_target(root, name="target", synthetic=True, uuid=SENTINEL_UUID):
    target = root / name
    target.mkdir(parents=True, exist_ok=True)
    (target / hpb.SENTINEL_NAME).write_text(
        json.dumps({"uuid": uuid, "volume": "SYNTHETIC", "synthetic": synthetic})
    )
    return target


def run(argv):
    """Invoke the CLI the way the CronJob does, in-process, returning the exit code."""
    return hpb.main([str(a) for a in argv])


def backup(pool, target, level="l0", extra=()):
    return run(
        [
            "backup", "--pool", pool, "--target", target,
            "--sentinel-uuid", SENTINEL_UUID,
            "--cipher", "none", "--insecure-plaintext",
            "--level", level, "--execute", *extra,
        ]
    )


def latest(target):
    return hpb.generations(str(target))[-1]


# --------------------------------------------------------------------------- G0.1


def test_g0_01a_target_independence_needs_an_explicit_sentinel_uuid(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    assert run(["backup", "--pool", pool, "--target", target, "--sentinel-uuid",
                "00000000-0000-0000-0000-000000000000", "--cipher", "none",
                "--insecure-plaintext", "--execute"]) == hpb.EXIT_REFUSED
    assert not (target / "generations").exists()
    assert not (target / hpb.INDEX_NAME).exists()


def test_g0_01b_sentinel_enforcement_writes_nothing(tmp_path):
    """The load-bearing guard: /mnt/f exists as empty ext4 when F: is not mounted."""
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    (target / hpb.SENTINEL_NAME).unlink()
    assert backup(pool, target) == hpb.EXIT_REFUSED
    assert sorted(os.listdir(target)) == []


def test_g0_01c_the_target_must_sit_under_the_expected_mount(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    ok = backup(pool, target, extra=["--expect-mount", str(tmp_path)])
    wrong = backup(pool, target, extra=["--expect-mount", str(tmp_path / "elsewhere")])
    assert ok == hpb.EXIT_OK
    assert wrong == hpb.EXIT_REFUSED


# --------------------------------------------------------------------------- G0.2


def test_g0_02_a_level0_completes_and_selects_only_allowlisted_roots(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    assert backup(pool, target) == hpb.EXIT_OK
    receipt = latest(target)
    assert receipt["ok"] and receipt["level"] == "l0"
    assert receipt["files_unstable"] == 0 and receipt["files_vanished"] == 0

    rows = hpb.read_manifest_named(str(target), receipt, None, "none")
    paths = {row["path"] for row in rows}
    assert "corpus/raw/node-a/20260915-dets.csv" in paths
    assert "models/m.json" in paths and "sketch_corpus/s.jsonl" in paths
    # The exclusions that make this a backup rather than a copy of the whole volume.
    assert not any(p.startswith("pylib") for p in paths)
    assert not any(p.endswith(".tmp") for p in paths)
    # The live WAL database is never read as a file; only the backup-API snapshot ships.
    assert sum(1 for p in paths if p == hpb.SQLITE_REL) == 1
    assert [r for r in rows if r["path"] == hpb.SQLITE_REL][0]["snapshot"] == "sqlite-backup-api"


def test_g0_02b_dry_run_is_the_default_and_writes_nothing(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    assert run(["backup", "--pool", pool, "--target", target, "--sentinel-uuid", SENTINEL_UUID,
                "--cipher", "none", "--insecure-plaintext"]) == hpb.EXIT_OK
    assert sorted(os.listdir(target)) == [hpb.SENTINEL_NAME]


# --------------------------------------------------------------------------- G0.3


def test_g0_03_restore_isolation(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    backup(pool, target)
    gen = latest(target)["gen_id"]
    into = tmp_path / "restore"
    into.mkdir()

    # No confirmation: refused.
    assert run(["restore", "--target", target, "--gen", gen, "--into", into,
                "--cipher", "none", "--execute"]) == hpb.EXIT_REFUSED
    # Into the source tree: refused.
    assert run(["restore", "--target", target, "--gen", gen, "--into", pool / "corpus",
                "--pool", pool, "--cipher", "none", "--i-understand",
                "--execute"]) == hpb.EXIT_REFUSED
    # Non-empty destination: refused.
    (into / "leftover").write_text("x")
    assert run(["restore", "--target", target, "--gen", gen, "--into", into,
                "--cipher", "none", "--i-understand", "--execute"]) == hpb.EXIT_REFUSED
    (into / "leftover").unlink()
    # And the live pool path is refused outright, whatever else is passed.
    with pytest.raises(hpb.Refusal):
        hpb.refuse_unsafe_restore_target("/pool/corpus")

    assert run(["restore", "--target", target, "--gen", gen, "--into", into,
                "--cipher", "none", "--i-understand", "--execute"]) == hpb.EXIT_OK
    assert (into / "corpus" / "ledger.jsonl").is_file()


def test_g0_03b_the_restore_job_mounts_no_pool_volume(tmp_path):
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    job = [d for d in docs if d["kind"] == "Job"][0]
    volumes = job["spec"]["template"]["spec"]["volumes"]
    claims = [
        v.get("persistentVolumeClaim", {}).get("claimName")
        for v in volumes
        if "persistentVolumeClaim" in v
    ]
    assert "hear-pool" not in claims, "the restore drill can see the live pool"
    assert "hear-pool-restore" in claims
    pvc = [d for d in docs if d["kind"] == "PersistentVolumeClaim"][0]
    assert pvc["spec"]["storageClassName"] == "local-path-retain"


# --------------------------------------------------------------------------- G0.4 / G0.5


def test_g0_04_every_restored_object_matches_its_manifest_digest(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    backup(pool, target)
    gen = latest(target)["gen_id"]
    into = tmp_path / "restore"
    assert run(["restore", "--target", target, "--gen", gen, "--into", into, "--cipher", "none",
                "--i-understand", "--execute", "--test-id", "g0-04"]) == hpb.EXIT_OK
    report = json.loads((target / "restore-tests" / "g0-04" / "report.json").read_text())
    assert report["ok"] and report["v2_objects"]["mismatched"] == 0
    assert report["v2_objects"]["checked"] >= 9


def test_g0_05_restored_bytes_equal_the_source_bytes(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    backup(pool, target)
    into = tmp_path / "restore"
    run(["restore", "--target", target, "--gen", latest(target)["gen_id"], "--into", into,
         "--cipher", "none", "--i-understand", "--execute"])
    for rel in hpb.select(str(pool)):
        assert (into / rel).read_bytes() == (pool / rel).read_bytes(), rel


# --------------------------------------------------------------------------- G0.6


def test_g0_06_the_manifest_digest_is_canonical_and_stable(tmp_path):
    """The snapshot manifest is hashed the way tools/freeze_contracts.py hashes contracts."""
    rows = [{"b": 2, "a": 1}, {"a": 3, "b": 4}]
    once = hpb.manifest_bytes(rows)
    again = hpb.manifest_bytes([{"a": 1, "b": 2}, {"b": 4, "a": 3}])
    assert once == again
    assert hpb.sha256_hex(once) == hpb.sha256_hex(again)

    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    backup(pool, target)
    receipt = latest(target)
    manifest = (
        pathlib.Path(hpb.gen_dir(str(target), receipt["gen_id"])) / receipt["manifest_name"]
    ).read_bytes()
    assert hpb.sha256_hex(manifest) == receipt["manifest_sha256"]
    assert receipt["manifest_rows"] == len(manifest.decode().splitlines())


# --------------------------------------------------------------------------- G0.7


def test_g0_07_the_sqlite_snapshot_is_consistent_while_a_writer_holds_the_database(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    writer = sqlite3.connect(pool / "corpus" / "annotations.sqlite3")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO annotation (label) VALUES ('live')")
    writer.commit()
    try:
        assert backup(pool, target) == hpb.EXIT_OK
    finally:
        writer.close()
    receipt = latest(target)
    assert receipt["sqlite_table_rows"]["annotation"] == 2
    into = tmp_path / "restore"
    run(["restore", "--target", target, "--gen", receipt["gen_id"], "--into", into,
         "--cipher", "none", "--i-understand", "--execute", "--test-id", "g0-07"])
    report = json.loads((target / "restore-tests" / "g0-07" / "report.json").read_text())
    assert report["v4_structural"]["sqlite"] == "ok"
    restored = sqlite3.connect(into / hpb.SQLITE_REL)
    try:
        assert restored.execute("SELECT count(*) FROM annotation").fetchone()[0] == 2
    finally:
        restored.close()


# --------------------------------------------------------------------------- G0.8


def test_g0_08_append_only_streams_are_stored_as_a_prefix_and_repaired_on_restore(tmp_path):
    """A JSONL that grows a half-row under the reader must restore as whole rows only."""
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    ledger = pool / "corpus" / "ledger.jsonl"
    ledger.write_text('{"src":"a"}\n{"src":"b"}\n{"src":"par')  # torn tail, as on disk
    assert backup(pool, target) == hpb.EXIT_OK
    into = tmp_path / "restore"
    assert run(["restore", "--target", target, "--gen", latest(target)["gen_id"], "--into", into,
                "--cipher", "none", "--i-understand", "--execute",
                "--test-id", "g0-08"]) == hpb.EXIT_OK
    report = json.loads((target / "restore-tests" / "g0-08" / "report.json").read_text())
    restored = (into / "corpus" / "ledger.jsonl").read_text()
    assert restored == '{"src":"a"}\n{"src":"b"}\n'
    assert report["trimmed_bytes"]["ledger.jsonl"] == len('{"src":"par')
    for line in restored.splitlines():
        json.loads(line)
    assert gzip.decompress((into / "corpus/scene/20260915/node-a.jsonl.gz").read_bytes()) == (
        b'{"scene":1}\n{"scene":2}\n'
    )


# --------------------------------------------------------------------------- G0.9


def test_g0_09_key_custody_is_external_only_and_never_leaks(tmp_path):
    """No passphrase is created, derived, defaulted, logged or written. Ever."""
    pool = make_pool(tmp_path)
    real = make_target(tmp_path, "real", synthetic=False)

    # Plaintext is unreachable on a non-synthetic target, with or without the escape hatch.
    assert run(["backup", "--pool", pool, "--target", real, "--sentinel-uuid", SENTINEL_UUID,
                "--cipher", "none", "--insecure-plaintext", "--execute"]) == hpb.EXIT_REFUSED
    # And encryption without an externally supplied key is refused rather than improvised.
    assert run(["backup", "--pool", pool, "--target", real, "--sentinel-uuid", SENTINEL_UUID,
                "--execute"]) == hpb.EXIT_REFUSED
    assert not (real / "generations").exists()

    # A key file that anyone else on the host can read is refused too.
    key = tmp_path / "passphrase"
    key.write_text("throwaway-fixture-value")
    os.chmod(key, 0o644)
    with pytest.raises(hpb.Refusal):
        hpb.require_key(str(key), "ref", "gpg", {"synthetic": False}, False)
    os.chmod(key, 0o600)

    # Nothing the tool emits may carry a passphrase, in any shape.
    assert "<redacted>" in hpb.redact("passphrase=hunter2")
    assert "hunter2" not in hpb.redact({"a": "secret: hunter2"})["a"]
    target = make_target(tmp_path)
    backup(pool, target)
    receipt_path = (
        pathlib.Path(hpb.gen_dir(str(target), latest(target)["gen_id"]))
        / f"receipt-{latest(target)['gen_id']}.json"
    )
    blob = receipt_path.read_text()
    assert "throwaway-fixture-value" not in blob
    # The cleartext receipt carries counts and digests, never a pool path (plan §4.4).
    assert "corpus/" not in blob and "node-a" not in blob


@pytest.mark.skipif(shutil.which("gpg") is None, reason="gpg is not installed")
def test_g0_09b_an_encrypted_chain_round_trips_with_an_external_key_file(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    key = tmp_path / "passphrase"
    key.write_text("fixture-only-never-a-real-custody-value")
    os.chmod(key, 0o600)
    env_home = tmp_path / "gnupg"
    env_home.mkdir(mode=0o700)
    os.environ["GNUPGHOME"] = str(env_home)
    try:
        assert run(["backup", "--pool", pool, "--target", target, "--sentinel-uuid", SENTINEL_UUID,
                    "--passphrase-file", key, "--key-ref", "fixture-2026Q3",
                    "--execute"]) == hpb.EXIT_OK
        gen = latest(target)
        assert gen["archive_name"].endswith(".gpg") and gen["cipher"] == "gpg"
        archive = pathlib.Path(hpb.gen_dir(str(target), gen["gen_id"])) / gen["archive_name"]
        assert b"ledger.jsonl" not in archive.read_bytes(), "the archive is not encrypted"
        into = tmp_path / "restore"
        assert run(["restore", "--target", target, "--gen", gen["gen_id"], "--into", into,
                    "--passphrase-file", key, "--i-understand", "--execute"]) == hpb.EXIT_OK
        assert (into / "corpus" / "ledger.jsonl").read_text() == '{"src":"a"}\n'
        # G0.11b, the same fact from the other side: no key, no restore.
        empty = tmp_path / "restore2"
        assert run(["restore", "--target", target, "--gen", gen["gen_id"], "--into", empty,
                    "--i-understand", "--execute"]) == hpb.EXIT_REFUSED
    finally:
        os.environ.pop("GNUPGHOME", None)


# --------------------------------------------------------------------------- G0.10


def test_g0_10_receipts_and_reports_carry_the_evidence_the_gate_asks_for(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    backup(pool, target)
    receipt = latest(target)
    for field in (
        "gen_id", "level", "started_at", "ended_at", "files_stored", "files_unchanged",
        "files_vanished", "files_unstable", "bytes_in", "bytes_out", "archive_sha256",
        "manifest_sha256", "manifest_rows", "tool_version", "sentinel_uuid",
        "target_free_bytes",
    ):
        assert field in receipt, field
    into = tmp_path / "restore"
    run(["restore", "--target", target, "--gen", receipt["gen_id"], "--into", into,
         "--cipher", "none", "--i-understand", "--execute", "--test-id", "g0-10"])
    report = json.loads((target / "restore-tests" / "g0-10" / "report.json").read_text())
    assert report["rto_seconds"] >= 0 and report["chain"] == [receipt["gen_id"]]
    assert report["v1_archive"] and report["isolated_from_pool"] is True


# --------------------------------------------------------------------------- G0.11


def test_g0_11a_one_flipped_byte_fails_v1_and_produces_no_partial_tree(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    backup(pool, target)
    receipt = latest(target)
    archive = pathlib.Path(hpb.gen_dir(str(target), receipt["gen_id"])) / receipt["archive_name"]
    body = bytearray(archive.read_bytes())
    body[len(body) // 2] ^= 0xFF
    archive.write_bytes(bytes(body))

    assert run(["verify", "--target", target, "--gen", receipt["gen_id"]]) == hpb.EXIT_REFUSED
    into = tmp_path / "restore"
    assert run(["restore", "--target", target, "--gen", receipt["gen_id"], "--into", into,
                "--cipher", "none", "--i-understand", "--execute"]) == hpb.EXIT_REFUSED
    assert list(into.iterdir()) == [], "a failed restore left a partial tree behind"


def test_g0_11c_a_file_that_vanishes_mid_run_is_recorded_not_fatal(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    clip = "corpus/clips/a.wav"
    rels = hpb.select(str(pool))
    (pool / clip).unlink()  # the clip pruner, reproduced on a synthetic tree
    payload, stored = hpb.build_payload(str(pool), rels, "l0-test", [], "l0", 0)
    assert stored.vanished == 1
    row = [r for r in stored.rows if r["path"] == clip][0]
    assert row["state"] == "vanished"
    assert stored.stored == len(rels) - 1


def test_g0_11d_a_failed_run_leaves_no_generation_and_no_index_row(tmp_path, monkeypatch):
    """Atomic output: a generation directory appears whole or not at all."""
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    backup(pool, target)
    before = (target / hpb.INDEX_NAME).read_text()

    def boom(*_a, **_k):
        raise RuntimeError("target went away mid-write")

    monkeypatch.setattr(hpb, "manifest_bytes", boom)
    with pytest.raises(RuntimeError):
        backup(pool, target, level="l1")
    assert (target / hpb.INDEX_NAME).read_text() == before
    staged = [p for p in (target / "generations").iterdir() if p.name.endswith(".part")]
    assert staged == [], "a half-written generation survived the failure"
    assert len(hpb.generations(str(target))) == 1


# --------------------------------------------------------------------------- G0.12


def test_g0_12_the_source_is_never_written_and_the_live_pool_is_refused(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)

    def fingerprint():
        out = {}
        for dirpath, _dirs, names in os.walk(pool):
            for name in names:
                full = pathlib.Path(dirpath) / name
                stat = full.stat()
                out[str(full)] = (stat.st_size, stat.st_mtime_ns)
        return out

    before = fingerprint()
    assert backup(pool, target) == hpb.EXIT_OK
    into = tmp_path / "restore"
    run(["restore", "--target", target, "--gen", latest(target)["gen_id"], "--into", into,
         "--cipher", "none", "--i-understand", "--execute"])
    assert fingerprint() == before, "the backup modified its own source"

    # The live pool is out of scope for this scaffolding, whatever the caller passes.
    with pytest.raises(hpb.Refusal):
        hpb.refuse_live_pool("/pool")
    with pytest.raises(hpb.Refusal):
        hpb.refuse_live_pool("/pool/corpus")
    assert run(["backup", "--pool", "/pool", "--target", target, "--sentinel-uuid", SENTINEL_UUID,
                "--cipher", "none", "--insecure-plaintext", "--execute"]) == hpb.EXIT_REFUSED

    # ...and the manifest says the same thing about the pod, on both the volume and the mount.
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    crons = [d for d in docs if d["kind"] == "CronJob"]
    assert crons, "no CronJob in the manifest"
    for cron in crons:
        spec = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        assert cron["spec"]["suspend"] is True, "%s ships unsuspended" % cron["metadata"]["name"]
        for volume in spec["volumes"]:
            if volume.get("persistentVolumeClaim", {}).get("claimName") == "hear-pool":
                assert volume["persistentVolumeClaim"].get("readOnly") is True
                for container in spec["containers"]:
                    for mount in container["volumeMounts"]:
                        if mount["name"] == volume["name"]:
                            assert mount.get("readOnly") is True
            if "hostPath" in volume:
                # DirectoryOrCreate would create /mnt/f/hear-backup on ext4 when F: is absent.
                assert volume["hostPath"]["type"] == "Directory"


def test_g0_12b_no_passphrase_value_appears_in_any_shipped_manifest():
    for path in (MANIFEST, ROOT / "deploy" / "k8s" / "hear-pool-backup-code.yaml"):
        text = path.read_text()
        for document in yaml.safe_load_all(text):
            if not document or document.get("kind") != "CronJob":
                continue
            spec = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
            for container in spec["containers"]:
                for env in container.get("env", []):
                    name = env["name"].lower()
                    if "passphrase" in name or "secret" in name or "password" in name:
                        assert env.get("value", "").startswith("/"), (
                            "%s carries a value, not a path" % env["name"]
                        )
        assert "secretKeyRef" not in text, (
            "a Secret in this namespace is dumped in cleartext nightly; see plan section 6"
        )


# --------------------------------------------------------------------------- G0.13


def test_g0_13_two_chained_generations_restore_independently(tmp_path):
    pool = make_pool(tmp_path)
    target = make_target(tmp_path)
    assert backup(pool, target) == hpb.EXIT_OK
    parent = latest(target)["gen_id"]

    # New body, an appended stream, and an untouched file that must NOT be re-stored.
    time.sleep(0.01)
    (pool / "corpus" / "raw" / "node-a" / "20260916-dets.csv").write_text("t,f\n3,4\n")
    with open(pool / "corpus" / "ledger.jsonl", "a") as handle:
        handle.write('{"src":"b"}\n')
    assert backup(pool, target, level="l1", extra=["--min-age-seconds", "0"]) == hpb.EXIT_OK
    child = latest(target)
    assert child["parent_gen_id"] == parent
    rows = {r["path"]: r for r in hpb.read_manifest_named(str(target), child, None, "none")}
    assert rows["corpus/raw/node-a/20260916-dets.csv"]["state"] == "stored"
    assert rows["corpus/raw/node-a/20260915-dets.csv"]["state"] == "unchanged"
    assert rows["corpus/ledger.jsonl"]["state"] == "stored"  # append-only: always re-stored whole

    for gen_id, expect_ledger in ((parent, '{"src":"a"}\n'),
                                  (child["gen_id"], '{"src":"a"}\n{"src":"b"}\n')):
        into = tmp_path / ("restore-" + gen_id)
        assert run(["restore", "--target", target, "--gen", gen_id, "--into", into,
                    "--cipher", "none", "--i-understand", "--execute",
                    "--test-id", "g0-13-" + gen_id]) == hpb.EXIT_OK
        assert (into / "corpus" / "ledger.jsonl").read_text() == expect_ledger
        assert (into / "corpus" / "raw" / "node-a" / "20260915-dets.csv").is_file()
    assert (tmp_path / ("restore-" + child["gen_id"]) / "corpus/raw/node-a/20260916-dets.csv").is_file()
    assert not (tmp_path / ("restore-" + parent) / "corpus/raw/node-a/20260916-dets.csv").exists()

    # A broken chain is refused rather than half-restored.
    with pytest.raises(hpb.Refusal):
        hpb.chain_for([{"gen_id": "l1-x", "parent_gen_id": "l0-missing"}], "l1-x")


# --------------------------------------------------------------------------- CLI surface


def test_the_cli_exits_non_zero_on_every_refusal_path(tmp_path):
    """A refusal must be an exit code, not a stack trace: a failed Job is the alert."""
    proc = subprocess.run(
        [sys.executable, str(TOOL), "backup", "--pool", str(tmp_path),
         "--target", str(tmp_path), "--sentinel-uuid", SENTINEL_UUID, "--execute"],
        capture_output=True, text=True,
    )
    assert proc.returncode == hpb.EXIT_REFUSED
    assert "Traceback" not in proc.stderr
    assert "SENTINEL_MISSING" in proc.stderr
