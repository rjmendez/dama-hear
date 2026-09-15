#!/usr/bin/env python3
"""The gate-1 evidence instrument for the `hear-heartbeat` ConfigMap-to-image cutover.

`tests/test_service_images.py` proves the *repository* is internally consistent, and
`.github/workflows/images.yml` proves the *built image* contains what the repository says.
Neither can answer the questions gate 1 actually closes on (deploy/images/service/README.md):
was the digest published and recorded, was the node warm, did the ledger survive a `Recreate`
with every record and every device intact, did anything but packaging change, and was the
rollback *performed*.

This module is that answer, mechanised, so the evidence is a reproducible computation over
captured artifacts instead of ten screenshots pasted into an issue.

⚠️IT CHANGES NOTHING, ANYWHERE. Every subcommand reads: files in the checkout, JSON/YAML the
operator captured with `kubectl get -o json`, and SQLite databases opened `mode=ro`. It does not
shell out to `kubectl`, `docker`, `ctr` or `redis-cli`, it does not reach a registry, it does not
apply, patch, restart, pull, publish or edit a digest sentinel, and it never opens a database for
writing. `plan` *prints* the commands an operator runs; it does not run them. That is deliberate:
an evidence tool that can mutate the thing it measures is not evidence.

⚠️SNAPSHOT THE BACKUP, NOT THE LIVE WAL. `snapshot` refuses a database with a hot `-wal` sidecar
unless `--allow-hot`, because a byte copy of a live WAL database is the classic way to produce a
"backup" that is a torn read. Take the copy with the SQLite backup API first -- the runbook
(docs/runbooks/hear-heartbeat-oci-cutover.md) gives the exact command -- and snapshot that.

Subcommands
    preflight   repo + optional live-state gates that must hold before an apply is even legal
    snapshot    a read-only fingerprint of the durable ledger (records, devices, attempts, schema)
    compare     two snapshots: record conservation, device coverage, continuity, cache failures
    health      two /healthz captures: same shape, same targets, no new cache failures
    receipt     the ten-row gate-1 evidence table, PASS/FAIL/MISSING per row
    plan        the ordered cutover and rollback commands, printed, refusing an unpublished digest

Exit codes: 0 every check passed, 1 a check failed, 2 the inputs were unusable.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import re
import sqlite3
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is in requirements/ci-pods.txt
    yaml = None

ROOT = pathlib.Path(__file__).resolve().parents[1]

WORKLOAD = "hear-heartbeat"
BUNDLE = "hear-heartbeat-code"
ZERO_DIGEST = "sha256:" + "0" * 64
PENDING = "pending"
LOCK = "requirements/lock/service-hear-heartbeat.txt"

#: The five things a packaging cutover may change (docs/worker-packaging.md, per-workload
#: procedure, step 2). Everything else is out of bounds and the diff is a violation, not a taste.
ALLOWED_CONTAINER_KEYS = ("image", "command", "args")
ALLOWED_DROPPED_VOLUMES = {"code", "deps"}

#: Invariants gate 1 names explicitly. Checked on the *proposed* manifest and, when a live dump
#: is supplied, on the live object -- a cutover from an unknown live spec converts an unknown
#: into an image.
EXPECTED_ENV_NAMES = (
    "HEAR_HEARTBEAT_TTL_S",
    "HEAR_HEARTBEAT_SOCKET_TIMEOUT_S",
    "HEAR_DURABLE_STORE",
    "HEAR_DURABLE_DB",
    "HEAR_DURABLE_REPLAY_LIMIT",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_PASS",
    "HEAR_HEARTBEAT_TOKEN",
)
EXPECTED_SECRET_ENV = ("REDIS_PASS", "HEAR_HEARTBEAT_TOKEN")
EXPECTED_STATE_MOUNT = "/state"
EXPECTED_PVC = "hear-heartbeat-state"
EXPECTED_HOST_PORT = 5051
EXPECTED_STRATEGY = "Recreate"

#: The ledger's tables, as tools/hear_heartbeat_receiver.py creates them. A packaging change may
#: not create, move, reformat or re-own durable state, so the schema fingerprint must be equal on
#: both sides of a cutover -- and equal again after a rollback.
LEDGER_TABLES = ("durable_records", "cache_attempts", "cache_claims", "durable_meta",
                 "refused_messages")

#: Default fleet shape. Gate 1 evidence row 3 is "all six devices appear after cutover".
DEFAULT_DEVICE_COUNT = 6
#: The fleet's heartbeat interval; a per-device gap larger than this plus the Recreate gap is a
#: lost interval, not a restart.
DEFAULT_HEARTBEAT_INTERVAL_S = 30.0
#: How long one `Recreate` pod cycle is allowed to take before the gap stops being explainable.
DEFAULT_RECREATE_GAP_S = 180.0


class InputError(Exception):
    """The artifacts handed in cannot be read, which is not the same as a failed check."""


# --------------------------------------------------------------------------- small helpers

def _read_yaml_docs(path):
    if yaml is None:
        raise InputError("PyYAML is required to parse %s" % path)
    try:
        text = pathlib.Path(path).read_text()
    except OSError as exc:
        raise InputError("cannot read %s: %s" % (path, exc))
    try:
        return [d for d in yaml.safe_load_all(text) if d]
    except yaml.YAMLError as exc:
        raise InputError("%s is not valid YAML: %s" % (path, exc))


def _read_json(path):
    try:
        text = pathlib.Path(path).read_text()
    except OSError as exc:
        raise InputError("cannot read %s: %s" % (path, exc))
    try:
        return json.loads(text)
    except ValueError as exc:
        raise InputError("%s is not valid JSON: %s" % (path, exc))


def _kind(docs, kind):
    for d in docs:
        if d.get("kind") == kind:
            return d
    raise InputError("no %s document found" % kind)


def _container(deployment):
    try:
        return deployment["spec"]["template"]["spec"]["containers"][0]
    except (KeyError, IndexError, TypeError):
        raise InputError("deployment has no container[0]")


def _pod_spec(deployment):
    try:
        return deployment["spec"]["template"]["spec"]
    except (KeyError, TypeError):
        raise InputError("deployment has no pod spec")


def _parse_ts(value):
    """ISO-8601 (with or without `Z`) -> aware datetime, or None."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


class Report:
    """An ordered list of checks, each with a verdict and a reason a human can act on."""

    def __init__(self, title):
        self.title = title
        self.checks = []

    def add(self, name, ok, detail, blocking=True):
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail,
                            "blocking": bool(blocking)})
        return ok

    def record(self, name, detail):
        """A measured fact that is not a pass/fail gate."""
        self.checks.append({"check": name, "ok": None, "detail": detail, "blocking": False})

    @property
    def failed(self):
        return [c for c in self.checks if c["ok"] is False and c["blocking"]]

    @property
    def ok(self):
        return not self.failed

    def to_json(self):
        return {"title": self.title, "ok": self.ok, "checks": self.checks}

    def to_text(self):
        lines = ["== %s" % self.title]
        for c in self.checks:
            mark = "note" if c["ok"] is None else ("PASS" if c["ok"] else "FAIL")
            if c["ok"] is False and not c["blocking"]:
                mark = "WARN"
            lines.append("[%-4s] %s" % (mark, c["check"]))
            if c["detail"]:
                lines.append("         %s" % c["detail"])
        lines.append("-- %s" % ("all checks passed" if self.ok
                                else "%d blocking check(s) failed" % len(self.failed)))
        return "\n".join(lines)


def _emit(report, args):
    if getattr(args, "json", False):
        print(json.dumps(report.to_json(), indent=2, sort_keys=True))
    else:
        print(report.to_text())
    return 0 if report.ok else 1


# --------------------------------------------------------------------------- repository facts

def digest_rows(repo=ROOT):
    """workload -> (repository, tag, digest) from deploy/images/service/digests.txt."""
    path = pathlib.Path(repo) / "deploy" / "images" / "service" / "digests.txt"
    try:
        text = path.read_text()
    except OSError as exc:
        raise InputError("cannot read %s: %s" % (path, exc))
    rows = {}
    for line in text.splitlines():
        fields = line.split("#", 1)[0].split()
        if not fields:
            continue
        if len(fields) != 4:
            raise InputError("malformed digests.txt row: %r" % (fields,))
        rows[fields[0]] = tuple(fields[1:])
    return rows


def published_digest(repo=ROOT, workload=WORKLOAD):
    """(repository, tag, digest) or None while the row is still `pending`."""
    rows = digest_rows(repo)
    if workload not in rows:
        raise InputError("digests.txt has no row for %s" % workload)
    repository, tag, digest = rows[workload]
    if digest == PENDING or tag == PENDING:
        return None
    return repository, tag, digest


def manifest_image(path):
    return _container(_kind(_read_yaml_docs(path), "Deployment"))["image"]


# --------------------------------------------------------------------------- preflight

def _check_provenance(report, repo, proposed_path):
    """Gate-1 step 1: the digest is published, recorded, and the manifest points at *that*."""
    rows = digest_rows(repo)
    if WORKLOAD not in rows:
        report.add("digest recorded in digests.txt", False,
                   "no row for %s -- `what ran that day` is not answerable from git" % WORKLOAD)
        return None
    repository, tag, digest = rows[WORKLOAD]
    image = manifest_image(proposed_path)
    got_repo, _, got_digest = image.partition("@")

    report.add("proposed manifest references a digest, never a tag", "@sha256:" in image,
               "image: %s" % image)
    report.add("manifest repository matches the recorded repository", got_repo == repository,
               "manifest %s / digests.txt %s" % (got_repo, repository))

    if digest == PENDING:
        report.add("digest published by a `main` build and recorded", False,
                   "digests.txt row is `pending`: nothing has been published, because a pull "
                   "request publishes nothing. Cutover is BLOCKED at step 1 of "
                   "deploy/images/service/README.md.")
        report.add("unpublished manifest carries the all-zero sentinel and stays unappliable",
                   got_digest == ZERO_DIGEST,
                   "sentinel present -- an accidental apply fails on a digest that cannot exist"
                   if got_digest == ZERO_DIGEST else
                   "manifest carries %s while digests.txt says pending: the manifest is "
                   "optimistic, which is the one thing the sentinel exists to prevent" % got_digest)
        return None

    report.add("digest published by a `main` build and recorded", True,
               "%s:%s -> %s" % (repository, tag, digest))
    report.add("digest is not the sentinel", digest != ZERO_DIGEST, digest)
    report.add("digest is syntactically a sha256",
               bool(re.fullmatch(r"sha256:[0-9a-f]{64}", digest)), digest)
    report.add("manifest digest equals the recorded digest", got_digest == digest,
               "manifest %s / digests.txt %s" % (got_digest, digest))
    report.add("recorded tag is a commit the checkout contains",
               bool(re.fullmatch(r"[0-9a-f]{7,40}", tag)),
               "tag column %r should be the publishing commit sha" % tag)
    return digest if got_digest == digest else None


def _check_lock_and_source(report, repo):
    repo = pathlib.Path(repo)
    lock = repo / LOCK
    if not lock.exists():
        report.add("service lock exists", False, "%s is missing" % LOCK)
        return
    body = [ln for ln in lock.read_text().splitlines() if ln and not ln.startswith("#")]
    pins = [ln for ln in body if "==" in ln]
    hashes = [ln for ln in body if "--hash=sha256:" in ln]
    report.add("every locked requirement carries exactly one sha256",
               bool(pins) and len(pins) == len(hashes) and len(pins) * 2 == len(body),
               "%d pins / %d hashes / %d lines" % (len(pins), len(hashes), len(body)))
    dockerfile = repo / "deploy" / "images" / "service" / ("Dockerfile.%s" % WORKLOAD)
    text = dockerfile.read_text() if dockerfile.exists() else ""
    report.add("the build replays the lock and never resolves one",
               "--require-hashes" in text and "--no-deps" in text,
               "Dockerfile.%s pip install flags" % WORKLOAD)
    report.add("the image records the commit its code came from",
               "ARG SOURCE_COMMIT" in text,
               "org.opencontainers.image.revision is stamped from SOURCE_COMMIT")


def diff_manifests(applied_path, proposed_path):
    """The allowlist diff, parsed rather than textual. Returns (violations, notes)."""
    old_docs = _read_yaml_docs(applied_path)
    new_docs = _read_yaml_docs(proposed_path)
    violations, notes = [], []

    if [d.get("kind") for d in old_docs] != [d.get("kind") for d in new_docs]:
        violations.append("the proposed manifest declares different objects (%s) than the applied "
                          "one (%s)" % ([d.get("kind") for d in new_docs],
                                        [d.get("kind") for d in old_docs]))
        return violations, notes

    for old, new in zip(old_docs, new_docs):
        if old.get("kind") != "Deployment":
            if old != new:
                violations.append("the proposed manifest changes the %s; a packaging cutover "
                                  "touches the Deployment and nothing else" % old.get("kind"))
            continue

        new_c = _container(new)
        if "command" in new_c or "args" in new_c:
            violations.append("the proposed container restates command/args; the image ENTRYPOINT "
                              "is what runs and a restated argv is a second place to drift")
        dumped = yaml.safe_dump(new)
        if "pip install" in dumped:
            violations.append("the proposed manifest still installs a package at pod start")

        old_norm = yaml.safe_load(yaml.safe_dump(old))
        c = _container(old_norm)
        for key in ALLOWED_CONTAINER_KEYS:
            c.pop(key, None)
        c["volumeMounts"] = [m for m in c.get("volumeMounts") or []
                             if m.get("name") not in ALLOWED_DROPPED_VOLUMES]
        spec = _pod_spec(old_norm)
        spec["volumes"] = [v for v in spec.get("volumes") or []
                           if v.get("name") not in ALLOWED_DROPPED_VOLUMES]

        new_norm = yaml.safe_load(yaml.safe_dump(new))
        _container(new_norm).pop("image", None)

        if old_norm != new_norm:
            violations.append("the proposed Deployment differs by more than the image, the "
                              "entrypoint and the code/deps volumes: %s"
                              % "; ".join(_describe_difference(old_norm, new_norm)))

        old_vols = {v["name"] for v in _pod_spec(old).get("volumes") or []}
        new_vols = {v["name"] for v in _pod_spec(new).get("volumes") or []}
        dropped, added = old_vols - new_vols, new_vols - old_vols
        if dropped != ALLOWED_DROPPED_VOLUMES:
            violations.append("the cutover removes %s; it may remove only code and deps"
                              % sorted(dropped))
        if added:
            violations.append("the cutover adds volumes: %s" % sorted(added))
        notes.append("volumes dropped: %s" % sorted(dropped))

    return violations, notes


def _describe_difference(a, b, path="") -> list:
    """A short, human-usable list of where two parsed structures diverge."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append("%s.%s added" % (path, key))
            elif key not in b:
                out.append("%s.%s removed" % (path, key))
            else:
                out.extend(_describe_difference(a[key], b[key], "%s.%s" % (path, key)))
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append("%s length %d -> %d" % (path, len(a), len(b)))
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                out.extend(_describe_difference(x, y, "%s[%d]" % (path, i)))
    elif a != b:
        out.append("%s %r -> %r" % (path, a, b))
    return out[:12]


def _check_invariants(report, deployment, label):
    """The surface gate 1 names: it must be identical before and after, live or committed."""
    spec = _pod_spec(deployment)
    c = _container(deployment)

    strategy = (deployment.get("spec", {}).get("strategy") or {}).get("type")
    report.add("%s: strategy is Recreate" % label, strategy == EXPECTED_STRATEGY,
               "one pod holds hostPort %d and an RWO PVC; RollingUpdate either hangs Pending or "
               "puts two writers on one SQLite ledger (got %r)" % (EXPECTED_HOST_PORT, strategy))
    replicas = deployment.get("spec", {}).get("replicas", 1)
    report.add("%s: exactly one replica" % label, replicas == 1, "replicas=%r" % replicas)
    report.add("%s: hostNetwork with ClusterFirstWithHostNet" % label,
               spec.get("hostNetwork") is True
               and spec.get("dnsPolicy") == "ClusterFirstWithHostNet",
               "hostNetwork=%r dnsPolicy=%r" % (spec.get("hostNetwork"), spec.get("dnsPolicy")))

    ports = c.get("ports") or []
    report.add("%s: hostPort %d is bound" % (label, EXPECTED_HOST_PORT),
               any(p.get("hostPort") == EXPECTED_HOST_PORT for p in ports), "ports=%r" % ports)

    env = {e["name"]: e for e in c.get("env") or []}
    missing = [n for n in EXPECTED_ENV_NAMES if n not in env]
    report.add("%s: all nine env vars present" % label, not missing,
               "missing %s" % missing if missing else "%d present" % len(env))
    for name in EXPECTED_SECRET_ENV:
        entry = env.get(name) or {}
        report.add("%s: %s still arrives from a secretKeyRef" % (label, name),
                   "secretKeyRef" in (entry.get("valueFrom") or {}),
                   "%s=%r" % (name, entry.get("valueFrom")))

    report.add("%s: durability env is untouched" % label,
               (env.get("HEAR_DURABLE_STORE", {}).get("value") == "sqlite"
                and env.get("HEAR_DURABLE_DB", {}).get("value", "").startswith(
                    EXPECTED_STATE_MOUNT + "/")),
               "HEAR_DURABLE_STORE=%r HEAR_DURABLE_DB=%r"
               % (env.get("HEAR_DURABLE_STORE", {}).get("value"),
                  env.get("HEAR_DURABLE_DB", {}).get("value")))

    mounts = {m.get("mountPath"): m for m in c.get("volumeMounts") or []}
    report.add("%s: the durable state mount survives" % label, EXPECTED_STATE_MOUNT in mounts,
               "mountPaths=%s" % sorted(mounts))
    claims = {v["name"]: (v.get("persistentVolumeClaim") or {}).get("claimName")
              for v in spec.get("volumes") or []}
    report.add("%s: the state PVC claim is unchanged" % label,
               claims.get("state") == EXPECTED_PVC,
               "state -> %r (expected %s)" % (claims.get("state"), EXPECTED_PVC))

    probes = [p for p in ("readinessProbe", "livenessProbe") if p in c]
    report.add("%s: both probes present on /healthz" % label,
               len(probes) == 2
               and all((c[p].get("httpGet") or {}).get("path") == "/healthz" for p in probes),
               "probes=%s" % probes)

    sec = (c.get("securityContext") or {})
    pod_sec = (spec.get("securityContext") or {})
    runs_nonroot = sec.get("runAsNonRoot") or pod_sec.get("runAsNonRoot")
    uid = sec.get("runAsUser", pod_sec.get("runAsUser"))
    report.add("%s: the uid is not changed by the packaging cutover" % label,
               not runs_nonroot and uid in (None, 0),
               "the /state database, WAL and shm are root-owned and local-path gets no fsGroup "
               "management; re-owning /state is a separate change (got runAsUser=%r, "
               "runAsNonRoot=%r)" % (uid, runs_nonroot))


def _check_registry_readiness(report, digest, args):
    """Open question 1, made concrete for the pilot: is the node warm before `Recreate` runs."""
    c = None
    if args.proposed:
        try:
            c = _container(_kind(_read_yaml_docs(args.proposed), "Deployment"))
        except InputError:
            c = None
    policy = (c or {}).get("imagePullPolicy")
    report.add("imagePullPolicy is left at the digest default", policy in (None, "IfNotPresent"),
               "a digest reference defaults to IfNotPresent, so a pre-pulled node starts from "
               "cache and does not make the Recreate gap a registry round-trip (got %r)" % policy,
               blocking=policy not in (None, "IfNotPresent"))

    if not args.node_images:
        report.record("node pre-pull evidence",
                      "not supplied: pass --node-images with `sudo k3s ctr images ls` output to "
                      "prove the digest is resident on the node that holds hostPort 5051")
        return
    try:
        text = pathlib.Path(args.node_images).read_text()
    except OSError as exc:
        raise InputError("cannot read %s: %s" % (args.node_images, exc))
    if digest is None:
        report.add("node pre-pull evidence matches a published digest", False,
                   "no published digest to look for yet")
        return
    report.add("the digest is already resident on the node", digest in text,
               "%s %s in %s" % (digest, "found" if digest in text else "absent",
                                args.node_images))


def _check_live_drift(report, args):
    """R8: migrating a workload whose live spec is unknown converts an unknown into an image."""
    if not args.live_deployment:
        report.record("live-vs-repo drift",
                      "not supplied: capture `kubectl -n dama get deploy hear-heartbeat -o json` "
                      "and pass --live-deployment. A cutover from an unknown live spec is not a "
                      "packaging change.")
        return
    live = _read_json(args.live_deployment)
    if live.get("kind") != "Deployment":
        raise InputError("%s is not a Deployment object" % args.live_deployment)
    _check_invariants(report, live, "live")

    applied = _kind(_read_yaml_docs(args.applied), "Deployment")
    live_c, repo_c = _container(live), _container(applied)
    for field in ("image", "command", "args"):
        report.add("live %s equals the committed manifest" % field,
                   live_c.get(field) == repo_c.get(field),
                   "live=%r repo=%r" % (live_c.get(field), repo_c.get(field)))
    live_env = {e["name"]: e.get("value") for e in live_c.get("env") or []}
    repo_env = {e["name"]: e.get("value") for e in repo_c.get("env") or []}
    report.add("live env equals the committed env", live_env == repo_env,
               "differences: %s" % _describe_difference(repo_env, live_env))

    if args.live_configmap:
        cm = _read_json(args.live_configmap)
        keys = sorted((cm.get("data") or {}))
        report.add("the rollback ConfigMap is applied and has its code key",
                   cm.get("kind") == "ConfigMap"
                   and "tools_hear_heartbeat_receiver.py" in keys,
                   "keys=%s" % keys)
        committed = _kind(_read_yaml_docs(
            pathlib.Path(args.repo) / "deploy" / "k8s" / ("%s.yaml" % BUNDLE)), "ConfigMap")
        same = (cm.get("data") or {}) == (committed.get("data") or {})
        report.add("the live ConfigMap equals the committed bundle byte for byte", same,
                   "a rollback restores the committed bundle; if live differs, rollback is a "
                   "second, unreviewed code change")
    else:
        report.record("live ConfigMap drift",
                      "not supplied: pass --live-configmap with `kubectl -n dama get cm "
                      "hear-heartbeat-code -o json`")


def cmd_preflight(args):
    repo = pathlib.Path(args.repo)
    args.applied = args.applied or repo / "deploy" / "k8s" / ("%s.yaml" % WORKLOAD)
    args.proposed = args.proposed or repo / "deploy" / "k8s" / ("%s.proposed.yaml" % WORKLOAD)
    report = Report("hear-heartbeat cutover preflight")

    digest = _check_provenance(report, repo, args.proposed)
    _check_lock_and_source(report, repo)

    violations, notes = diff_manifests(args.applied, args.proposed)
    report.add("the proposed manifest changes only packaging", not violations,
               "; ".join(violations) if violations else "; ".join(notes) or "image, entrypoint "
               "and the code/deps volumes only")

    _check_invariants(report, _kind(_read_yaml_docs(args.proposed), "Deployment"), "proposed")

    applied_doc = _kind(_read_yaml_docs(args.applied), "Deployment")
    report.add("the applied manifest is still the pre-cutover rollback path",
               "@sha256:" not in _container(applied_doc).get("image", "")
               and "code" in {v["name"] for v in _pod_spec(applied_doc).get("volumes") or []},
               "deploy/k8s/%s.yaml keeps its ConfigMap volume and its tag" % WORKLOAD)
    report.add("the rollback ConfigMap manifest exists in the checkout",
               (repo / "deploy" / "k8s" / ("%s.yaml" % BUNDLE)).exists(),
               "deploy/k8s/%s.yaml" % BUNDLE)
    report.add("the proposed manifest no longer references the ConfigMap",
               "code" not in {v["name"] for v in _pod_spec(
                   _kind(_read_yaml_docs(args.proposed), "Deployment")).get("volumes") or []},
               "applied and unreferenced is the rollback state; deletion is retirement, step 9")

    _check_registry_readiness(report, digest, args)
    _check_live_drift(report, args)

    if args.state_backup:
        backup = pathlib.Path(args.state_backup)
        report.add("a pre-cutover state backup exists", backup.is_file(),
                   "%s (%s bytes)" % (backup, backup.stat().st_size if backup.is_file() else 0))
        if backup.is_file():
            try:
                integrity = _integrity(backup)
            except InputError as exc:
                integrity = str(exc)
            report.add("the backup passes PRAGMA quick_check", integrity == "ok",
                       "quick_check=%s" % integrity)
    else:
        report.record("pre-cutover state backup",
                      "not supplied: take one with the SQLite backup API (never a raw copy of a "
                      "live WAL) and pass --state-backup")

    return _emit(report, args)


# --------------------------------------------------------------------------- snapshot

def _open_ro(path):
    path = pathlib.Path(path)
    if not path.is_file():
        raise InputError("%s is not a file" % path)
    uri = "file:%s?mode=ro" % path.resolve().as_posix().replace("?", "%3f").replace("#", "%23")
    try:
        return sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        raise InputError("cannot open %s read-only: %s" % (path, exc))


def _integrity(path):
    con = _open_ro(path)
    try:
        return con.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise InputError("quick_check failed on %s: %s" % (path, exc))
    finally:
        con.close()


def snapshot(path, label=None, allow_hot=False, since_id=None):
    """A read-only fingerprint of the durable ledger. Opens `mode=ro`; writes nothing, ever.

    ``since_id`` is the *before* snapshot's ``max_record_id``. Given it, the snapshot also records
    the first row each device wrote **after** that watermark, which is the only continuity
    measurement that actually answers "did this device lose an interval across the restart" --
    comparing the two maxima only tells you the device is alive now.
    """
    path = pathlib.Path(path)
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0 and not allow_hot:
        raise InputError(
            "%s has a non-empty WAL sidecar: this looks like a live database, and a byte copy of "
            "one is a torn read. Take a copy with the SQLite backup API and snapshot that, or "
            "pass --allow-hot if you are reading the live file in place (which is safe here -- "
            "this tool only ever opens mode=ro)." % path)

    con = _open_ro(path)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        missing = [t for t in LEDGER_TABLES if t not in tables]
        schema = sorted(
            r[0] for r in con.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name").fetchall())
        out = {
            "label": label or path.name,
            "source": str(path),
            "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "missing_tables": missing,
            "schema_fingerprint": _fingerprint(schema),
            "integrity": con.execute("PRAGMA quick_check").fetchone()[0],
        }
        if missing:
            out.update({"records_total": 0, "max_record_id": 0, "devices": {},
                        "cache_attempts": {}, "max_cache_attempt_id": 0, "refused_total": 0})
            return out

        out["records_total"] = con.execute(
            "SELECT COUNT(*) FROM durable_records").fetchone()[0]
        out["max_record_id"] = con.execute(
            "SELECT COALESCE(MAX(id), 0) FROM durable_records").fetchone()[0]
        out["devices"] = {
            row[0]: {"count": row[1], "max_received_at": row[2], "max_id": row[3]}
            for row in con.execute(
                "SELECT device_id, COUNT(*), MAX(received_at), MAX(id) "
                "FROM durable_records GROUP BY device_id").fetchall()}
        out["telemetry_paths"] = {
            row[0]: row[1] for row in con.execute(
                "SELECT telemetry_path, COUNT(*) FROM durable_records GROUP BY 1").fetchall()}
        out["cache_attempts"] = {
            row[0]: row[1] for row in con.execute(
                "SELECT outcome, COUNT(*) FROM cache_attempts GROUP BY 1").fetchall()}
        out["max_cache_attempt_id"] = con.execute(
            "SELECT COALESCE(MAX(id), 0) FROM cache_attempts").fetchone()[0]
        out["pending_records"] = con.execute(
            "SELECT COUNT(*) FROM durable_records r WHERE NOT EXISTS ("
            "  SELECT 1 FROM cache_attempts a WHERE a.record_uid = r.record_uid"
            "    AND a.outcome = 'succeeded')").fetchone()[0]
        out["refused_total"] = con.execute(
            "SELECT COUNT(*) FROM refused_messages").fetchone()[0]
        # The oldest surviving row: retention prunes from the front, so a *before* row id that no
        # longer exists after is only conservative if pruning explains it.
        out["min_record_id"] = con.execute(
            "SELECT COALESCE(MIN(id), 0) FROM durable_records").fetchone()[0]
        if since_id is not None:
            out["since_id"] = int(since_id)
            # ⚠️THE CONSERVATION MEASUREMENT. Rows the *previous* pod wrote, counted in the ledger
            # the new one is using: if this is smaller than the before snapshot's total, records
            # were destroyed, and a growing `records_total` would have hidden it.
            out["retained_below_watermark"] = con.execute(
                "SELECT COUNT(*) FROM durable_records WHERE id <= ?", (int(since_id),)
            ).fetchone()[0]
            for device, first_id, first_at in con.execute(
                    "SELECT device_id, MIN(id), MIN(received_at) FROM durable_records "
                    "WHERE id > ? GROUP BY device_id", (int(since_id),)).fetchall():
                out["devices"].setdefault(device, {"count": 0, "max_received_at": None,
                                                   "max_id": None})
                out["devices"][device]["first_id_after"] = first_id
                out["devices"][device]["first_received_at_after"] = first_at
        return out
    except sqlite3.DatabaseError as exc:
        raise InputError("%s is not a readable heartbeat ledger: %s" % (path, exc))
    finally:
        con.close()


def _fingerprint(schema_statements):
    import hashlib
    h = hashlib.sha256()
    for stmt in schema_statements:
        h.update(re.sub(r"\s+", " ", stmt).strip().encode())
        h.update(b"\n")
    return h.hexdigest()


def cmd_snapshot(args):
    out = snapshot(args.sqlite, label=args.label, allow_hot=args.allow_hot,
                   since_id=args.since_id)
    text = json.dumps(out, indent=2, sort_keys=True)
    if args.out:
        pathlib.Path(args.out).write_text(text + "\n")
    print(text)
    return 0 if out["integrity"] == "ok" and not out["missing_tables"] else 1


# --------------------------------------------------------------------------- compare

def compare_snapshots(before, after, devices=DEFAULT_DEVICE_COUNT,
                      interval_s=DEFAULT_HEARTBEAT_INTERVAL_S,
                      gap_s=DEFAULT_RECREATE_GAP_S, transition="cutover", allow_pruned=0):
    report = Report("durable ledger across the %s" % transition)

    report.add("the ledger is intact after the %s" % transition, after.get("integrity") == "ok",
               "PRAGMA quick_check = %r" % after.get("integrity"))
    report.add("no table was lost", not after.get("missing_tables"),
               "missing: %s" % after.get("missing_tables"))
    report.add("the schema is byte-identical across the %s" % transition,
               before.get("schema_fingerprint") == after.get("schema_fingerprint"),
               "a packaging change may not create, move, reformat or re-own durable state "
               "(%s -> %s)" % (str(before.get("schema_fingerprint"))[:12],
                               str(after.get("schema_fingerprint"))[:12]))

    # ---- record conservation
    b_max, a_max = before.get("max_record_id", 0), after.get("max_record_id", 0)
    report.add("record ids never went backwards", a_max >= b_max,
               "max(durable_records.id) %s -> %s" % (b_max, a_max))

    b_total, a_total = before.get("records_total", 0), after.get("records_total", 0)
    b_min, a_min = before.get("min_record_id", 0), after.get("min_record_id", 0)
    retained = after.get("retained_below_watermark")
    if retained is None:
        # Without the watermark, `rows went up` is the only statement that can be made, and it is
        # not conservation: a ledger can gain a hundred rows and lose ten in the same window.
        report.add("no record was lost", a_total >= b_total,
                   "rows %d -> %d, measured only as a total: re-take the after snapshot with "
                   "`snapshot --since-id %d` to count the pre-%s rows that actually survived"
                   % (b_total, a_total, before.get("max_record_id", 0), transition))
    else:
        lost = b_total - retained
        allowed = max(int(allow_pruned), 0)
        report.add("every record written before the %s is still in the ledger" % transition,
                   lost <= allowed,
                   "%d of %d pre-%s rows survive (%d missing, %d tolerated as retention "
                   "pruning). Durable state is out of bounds for a packaging change, so the "
                   "default tolerance is zero." % (retained, b_total, transition, max(lost, 0),
                                                   allowed))
        if 0 < lost <= allowed:
            report.record("retention pruning ran across the window",
                          "%d pre-%s rows removed within the declared --allow-pruned budget; "
                          "pruning only ever removes a record that already has a successful "
                          "cache attempt (min id %d -> %d)" % (lost, transition, b_min, a_min))
        report.add("the ledger is the same ledger, not a new one", a_total >= retained,
                   "rows %d -> %d" % (b_total, a_total))

    b_devices = before.get("devices") or {}
    a_devices = after.get("devices") or {}
    disappeared = sorted(set(b_devices) - set(a_devices))
    report.add("every device that was in the ledger is still in it", not disappeared,
               "disappeared: %s" % disappeared if disappeared else "%d devices" % len(a_devices))
    shrank = sorted(d for d in b_devices if d in a_devices
                    and a_devices[d]["count"] < b_devices[d]["count"]
                    and b_total - (after.get("retained_below_watermark") or b_total) <= 0)
    report.add("no device's row count shrank", not shrank, "shrank: %s" % shrank)

    if "since_id" in after:
        silent = sorted(d for d in a_devices
                        if a_devices[d].get("first_id_after") is None)
        report.add("every device reported again after the %s" % transition, not silent,
                   "silent since the restart: %s -- rows from before the %s do not prove a "
                   "device came back" % (silent, transition) if silent else
                   "all %d devices wrote at least one row after the watermark" % len(a_devices))

    report.add("the expected device count is present after the %s" % transition,
               len(a_devices) >= devices,
               "%d of %d devices: %s" % (len(a_devices), devices, sorted(a_devices)))

    # ---- continuity: did any device lose an interval across the restart
    limit = float(interval_s) + float(gap_s)
    gaps, unparsed, weak = [], [], []
    for device, after_row in sorted(a_devices.items()):
        before_row = b_devices.get(device)
        if not before_row:
            continue
        t0 = _parse_ts(before_row.get("max_received_at"))
        # The first row this device wrote *after* the pre-cutover watermark is the measurement
        # that answers the question. Fall back to the post-cutover maximum when the after
        # snapshot was taken without --since-id, and say so rather than claiming more.
        first_after = after_row.get("first_received_at_after")
        if first_after is None:
            if "since_id" in after:
                continue  # already reported as a device that never spoke again
            weak.append(device)
            first_after = after_row.get("max_received_at")
        t1 = _parse_ts(first_after)
        if t0 is None or t1 is None:
            unparsed.append(device)
            continue
        delta = (t1 - t0).total_seconds()
        if delta > limit:
            gaps.append("%s: %.0fs > %.0fs" % (device, delta, limit))
    report.add("received_at is continuous across the %s, per device" % transition, not gaps,
               "gaps: %s" % "; ".join(gaps) if gaps else
               "every device resumed within %.0fs (interval %.0fs + Recreate gap %.0fs)"
               % (limit, interval_s, gap_s))
    if weak:
        report.record("continuity measured from the wrong end for %d device(s)" % len(weak),
                      "the after snapshot has no `first_received_at_after`: re-take it with "
                      "`snapshot --since-id %s` so the gap is measured against the first row "
                      "written after the cutover, not the newest row in the ledger"
                      % before.get("max_record_id", 0))
    if unparsed:
        report.record("devices whose received_at could not be parsed", ", ".join(unparsed))

    # ---- cache attempts after the cutover
    b_attempt_id = before.get("max_cache_attempt_id", 0)
    b_failed = (before.get("cache_attempts") or {}).get("failed", 0)
    a_failed = (after.get("cache_attempts") or {}).get("failed", 0)
    new_failures = a_failed - b_failed
    report.add("zero failed cache attempts after the %s" % transition, new_failures <= 0,
               "cache_attempts(failed) %d -> %d (+%d since id %d). Query the ids yourself: "
               "SELECT outcome, count(*) FROM cache_attempts WHERE id > %d GROUP BY 1;"
               % (b_failed, a_failed, max(new_failures, 0), b_attempt_id, b_attempt_id))
    b_succ = (before.get("cache_attempts") or {}).get("succeeded", 0)
    a_succ = (after.get("cache_attempts") or {}).get("succeeded", 0)
    report.add("the cache path is alive after the %s" % transition, a_succ > b_succ,
               "cache_attempts(succeeded) %d -> %d; no new success means the Redis "
               "compatibility keys stopped being written" % (b_succ, a_succ))

    a_pending = after.get("pending_records")
    if a_pending is not None:
        b_pending = before.get("pending_records", 0)
        report.add("the pending backlog did not grow", a_pending <= max(b_pending, 0),
                   "pending_records %s -> %s" % (b_pending, a_pending), blocking=False)

    report.add("new traffic was accepted after the %s" % transition, a_total > b_total,
               "rows %d -> %d: a Ready pod that records nothing is not evidence of a working "
               "cutover" % (b_total, a_total))
    return report


def cmd_compare(args):
    before = _read_json(args.before)
    after = _read_json(args.after)
    report = compare_snapshots(before, after, devices=args.devices,
                               interval_s=args.heartbeat_interval_s,
                               gap_s=args.recreate_gap_s, transition=args.transition,
                               allow_pruned=args.allow_pruned)
    return _emit(report, args)


# --------------------------------------------------------------------------- health

HEALTH_KEYS = ("status", "service", "redis_target", "durable_store")
DURABLE_KEYS = ("backend", "enabled", "path", "pending_records", "cache_successes",
                "cache_failures", "last_cache_failure_at")


def compare_health(before, after):
    report = Report("/healthz across the cutover")
    missing_before = [k for k in HEALTH_KEYS if k not in before]
    missing_after = [k for k in HEALTH_KEYS if k not in after]
    report.add("the pre-cutover capture has the documented shape", not missing_before,
               "missing %s" % missing_before)
    report.add("/healthz returns the same shape as before", not missing_after
               and set(before) == set(after),
               "missing %s; before=%s after=%s"
               % (missing_after, sorted(before), sorted(after)))
    report.add("status is ok", after.get("status") == "ok", "status=%r" % after.get("status"))
    report.add("it is the same service", after.get("service") == before.get("service"),
               "%r -> %r" % (before.get("service"), after.get("service")))
    report.add("redis_target is unchanged", after.get("redis_target") == before.get("redis_target"),
               "%r -> %r" % (before.get("redis_target"), after.get("redis_target")))

    b_store = before.get("durable_store") or {}
    a_store = after.get("durable_store") or {}
    report.add("durable_store keeps its documented keys",
               all(k in a_store for k in DURABLE_KEYS),
               "missing %s" % [k for k in DURABLE_KEYS if k not in a_store])
    report.add("durable_store.backend is unchanged",
               a_store.get("backend") == b_store.get("backend"),
               "%r -> %r" % (b_store.get("backend"), a_store.get("backend")))
    report.add("durable_store.path is unchanged", a_store.get("path") == b_store.get("path"),
               "the ledger the image writes must be the ledger the ConfigMap pod wrote "
               "(%r -> %r)" % (b_store.get("path"), a_store.get("path")))
    report.add("durability is still enabled", a_store.get("enabled") is True,
               "enabled=%r" % a_store.get("enabled"))
    b_fail = b_store.get("cache_failures", 0) or 0
    a_fail = a_store.get("cache_failures", 0) or 0
    report.add("no new cache failures are reported by the pod", a_fail <= b_fail,
               "cache_failures %s -> %s" % (b_fail, a_fail))
    b_succ = b_store.get("cache_successes", 0) or 0
    a_succ = a_store.get("cache_successes", 0) or 0
    report.add("cache successes advanced", a_succ >= b_succ,
               "cache_successes %s -> %s" % (b_succ, a_succ))
    return report


def cmd_health(args):
    return _emit(compare_health(_read_json(args.before), _read_json(args.after)), args)


# --------------------------------------------------------------------------- receipt

RECEIPT_ROWS = (
    (1, "The pod is Ready on the digest, not on a tag"),
    (2, "/healthz returns the same shape as before"),
    (3, "All devices appear in the durable ledger after cutover"),
    (4, "received_at is continuous across the restart"),
    (5, "Zero failed cache attempts after cutover"),
    (6, "The Redis compatibility keys are still armed"),
    (7, "No pip install ran"),
    (8, "The ConfigMap is applied and unreferenced"),
    (9, "Rollback performed, not assumed"),
    (10, "The digest, source commit and lock hash are in git"),
)

MISSING = "MISSING"


def _row(state, detail, source):
    return {"state": state, "detail": detail, "source": source}


def _pod_row(pod_json, expect_digest=None, expect_image_substring=None):
    """kubectl get pod -o json (an item list or a single pod) -> readiness + imageID."""
    items = pod_json.get("items", [pod_json]) if isinstance(pod_json, dict) else list(pod_json)
    items = [p for p in items if p.get("kind", "Pod") == "Pod"]
    if not items:
        return _row("FAIL", "no pod objects in the capture", "pod json")
    running = [p for p in items
               if (p.get("status") or {}).get("phase") == "Running"
               and any(c.get("type") == "Ready" and c.get("status") == "True"
                       for c in (p.get("status") or {}).get("conditions") or [])]
    if not running:
        return _row("FAIL", "no pod is Running and Ready", "pod json")
    if len(items) > 1:
        return _row("FAIL", "%d pods present: Recreate must leave exactly one writer on the "
                            "RWO PVC and hostPort 5051" % len(items), "pod json")
    statuses = (running[0].get("status") or {}).get("containerStatuses") or []
    image_ids = [c.get("imageID", "") for c in statuses]
    images = [c.get("image", "") for c in statuses]
    if expect_digest:
        ok = any(expect_digest in i for i in image_ids) or any(expect_digest in i for i in images)
        return _row("PASS" if ok else "FAIL",
                    "imageID=%s (expected %s)" % (image_ids, expect_digest), "pod json")
    if expect_image_substring:
        ok = any(expect_image_substring in i for i in images)
        return _row("PASS" if ok else "FAIL",
                    "image=%s (expected the pre-cutover %s)" % (images, expect_image_substring),
                    "pod json")
    return _row("PASS", "Ready; imageID=%s" % image_ids, "pod json")


def build_receipt(args):
    repo = pathlib.Path(args.repo)
    rows = {n: _row(MISSING, "no artifact supplied", "-") for n, _ in RECEIPT_ROWS}

    published = None
    try:
        published = published_digest(repo)
    except InputError as exc:
        rows[10] = _row("FAIL", str(exc), "digests.txt")
    digest = published[2] if published else None

    # 1 -- Ready on the digest
    if args.pod:
        rows[1] = _pod_row(_read_json(args.pod), expect_digest=digest)
        if digest is None:
            rows[1] = _row("FAIL", "no published digest recorded, so a Ready pod cannot be "
                                   "attributed to one", "digests.txt")

    # 2 -- /healthz parity
    if args.health_before and args.health_after:
        rep = compare_health(_read_json(args.health_before), _read_json(args.health_after))
        rows[2] = _row("PASS" if rep.ok else "FAIL",
                       "; ".join(c["check"] for c in rep.failed) or
                       "same shape, same redis_target, same durable_store backend and path",
                       "healthz captures")

    # 3, 4, 5 -- the ledger across the cutover
    if args.snapshot_before and args.snapshot_after:
        before = _read_json(args.snapshot_before)
        after = _read_json(args.snapshot_after)
        rep = compare_snapshots(before, after, devices=args.devices,
                                interval_s=args.heartbeat_interval_s,
                                gap_s=args.recreate_gap_s,
                                allow_pruned=args.allow_pruned)
        failed = {c["check"] for c in rep.failed}
        devices_after = sorted((after.get("devices") or {}))

        # Every failed ledger check has to land on a row. Anything that is not a device or a
        # cache statement is a conservation/continuity statement, which is row 4 -- an
        # unattributed failure must never silently vanish from the receipt.
        by_row = {3: set(), 4: set(), 5: set()}
        for check in sorted(failed):
            if "device" in check:
                by_row[3].add(check)
            elif "cache" in check:
                by_row[5].add(check)
            else:
                by_row[4].add(check)

        rows[3] = _row("FAIL" if by_row[3] else "PASS",
                       "; ".join(sorted(by_row[3])) or "%d devices after cutover: %s"
                       % (len(devices_after), devices_after), "ledger snapshots")
        rows[4] = _row("FAIL" if by_row[4] else "PASS",
                       "; ".join(sorted(by_row[4])) or
                       "every pre-cutover row survived and no per-device gap exceeded the "
                       "interval plus the Recreate gap", "ledger snapshots")
        rows[5] = _row("FAIL" if by_row[5] else "PASS",
                       "; ".join(sorted(by_row[5])) or
                       "cache_attempts(failed) did not increase and successes advanced",
                       "ledger snapshots")

    # 6 -- Redis compatibility keys
    if args.redis_keys:
        try:
            text = pathlib.Path(args.redis_keys).read_text()
        except OSError as exc:
            raise InputError("cannot read %s: %s" % (args.redis_keys, exc))
        keys = [ln.strip() for ln in text.splitlines()
                if ln.strip().startswith("dama:hear:")]
        ttls = [ln.strip() for ln in text.splitlines()
                if re.fullmatch(r"\(integer\)\s+\d+|\d+", ln.strip())]
        positive_ttl = any(int(re.sub(r"[^0-9-]", "", t) or -1) > 0 for t in ttls)
        rows[6] = _row("PASS" if keys and positive_ttl else "FAIL",
                       "%d dama:hear:* keys, TTL evidence %s"
                       % (len(keys), "present and positive" if positive_ttl else "absent"),
                       args.redis_keys)

    # 7 -- no pip install ran
    if args.logs:
        try:
            text = pathlib.Path(args.logs).read_text()
        except OSError as exc:
            raise InputError("cannot read %s: %s" % (args.logs, exc))
        bad = [ln for ln in text.splitlines()
               if "pip install" in ln or "installing redis" in ln
               or "Collecting redis" in ln]
        rows[7] = _row("FAIL" if bad else "PASS",
                       "found: %s" % bad[:3] if bad else
                       "no install preamble in the pod log; the first line is the receiver's",
                       args.logs)

    # 8 -- ConfigMap applied and unreferenced
    cm_ok = None
    if args.live_configmap:
        cm = _read_json(args.live_configmap)
        cm_ok = cm.get("kind") == "ConfigMap" and bool(cm.get("data"))
    if args.pod and cm_ok is not None:
        pod_json = _read_json(args.pod)
        items = pod_json.get("items", [pod_json])
        volumes = [v.get("name") for p in items
                   for v in ((p.get("spec") or {}).get("volumes") or [])]
        unreferenced = "code" not in volumes and "deps" not in volumes
        rows[8] = _row("PASS" if cm_ok and unreferenced else "FAIL",
                       "ConfigMap present=%s; pod volumes=%s" % (cm_ok, sorted(set(volumes))),
                       "live ConfigMap + pod json")

    # 9 -- rollback performed
    if args.rollback_pod and args.snapshot_rollback and args.snapshot_after:
        pod_row = _pod_row(_read_json(args.rollback_pod),
                           expect_image_substring=args.rollback_image)
        rep = compare_snapshots(_read_json(args.snapshot_after),
                                _read_json(args.snapshot_rollback),
                                devices=args.devices,
                                interval_s=args.heartbeat_interval_s,
                                gap_s=args.recreate_gap_s,
                                transition="rollback", allow_pruned=args.allow_pruned)
        ok = pod_row["state"] == "PASS" and rep.ok
        rows[9] = _row("PASS" if ok else "FAIL",
                       "pod: %s; ledger across the rollback: %s"
                       % (pod_row["detail"],
                          "; ".join(c["check"] for c in rep.failed) or "continuous"),
                       "rollback pod json + rollback snapshot")

    # 10 -- the digest, source commit and lock hash are in git
    if published:
        repository, tag, dig = published
        lock = repo / LOCK
        manifest_ok = False
        try:
            manifest_ok = manifest_image(
                repo / "deploy" / "k8s" / ("%s.proposed.yaml" % WORKLOAD)).endswith("@" + dig)
        except InputError:
            manifest_ok = False
        rows[10] = _row("PASS" if lock.exists() and manifest_ok else "FAIL",
                        "digests.txt %s:%s -> %s; manifest agrees=%s; lock=%s"
                        % (repository, tag, dig, manifest_ok, LOCK),
                        "git")
    elif rows[10]["state"] == MISSING:
        rows[10] = _row("FAIL", "digests.txt still says `pending`: nothing was published, so no "
                                "cutover can have happened", "digests.txt")

    return rows


def cmd_receipt(args):
    rows = build_receipt(args)
    complete = all(r["state"] == "PASS" for r in rows.values())
    if args.json:
        print(json.dumps({"gate": "1", "workload": WORKLOAD, "complete": complete,
                          "rows": {str(k): v for k, v in rows.items()}},
                         indent=2, sort_keys=True))
    else:
        print("# Gate 1 evidence receipt -- %s" % WORKLOAD)
        print()
        print("| # | Evidence | State | Detail |")
        print("|---|---|---|---|")
        for n, title in RECEIPT_ROWS:
            row = rows[n]
            detail = str(row["detail"]).replace("|", "\\|")
            print("| %d | %s | **%s** | %s |" % (n, title, row["state"], detail))
        print()
        print("Gate 1 is %s." % ("CLOSED -- all ten rows pass" if complete else
                                 "OPEN: %s" % ", ".join(
                                     "row %d %s" % (n, rows[n]["state"])
                                     for n, _ in RECEIPT_ROWS if rows[n]["state"] != "PASS")))
    return 0 if complete else 1


# --------------------------------------------------------------------------- plan

PLAN_HEADER = """\
⚠️PRINTED, NOT RUN. Every line below is for an operator to read, understand and execute
deliberately. This tool executes none of it and reaches no cluster, registry or node.
"""


def cmd_plan(args):
    repo = pathlib.Path(args.repo)
    ns = args.namespace
    try:
        published = published_digest(repo)
    except InputError as exc:
        print("cannot read the digest record: %s" % exc, file=sys.stderr)
        return 2

    print(PLAN_HEADER)
    if args.rollback:
        _print_rollback(ns)
        return 0

    if published is None:
        print("== CUTOVER BLOCKED AT STEP 1")
        print()
        print("deploy/images/service/digests.txt records %s as `pending`, and" % WORKLOAD)
        print("deploy/k8s/%s.proposed.yaml carries the all-zero digest sentinel." % WORKLOAD)
        print("No image has been published: a pull request publishes nothing, and only a `main`")
        print("build pushes to ghcr.io. Applying the proposed manifest now would produce")
        print("ImagePullBackOff on a digest that cannot exist -- it would not pull `something`.")
        print()
        print("To unblock, in this order:")
        print("  1. Merge the packaging change; let the `main` run of .github/workflows/ci.yml")
        print("     build and publish the image; read the digest out of the job summary, or")
        print("     without pulling:")
        print("       docker buildx imagetools inspect \\")
        print("         ghcr.io/rjmendez/dama-hear/%s:<commit-sha>" % WORKLOAD)
        print("  2. Record it in deploy/images/service/digests.txt *and* in the `image:` field")
        print("     of deploy/k8s/%s.proposed.yaml, in one commit." % WORKLOAD)
        print("  3. Re-run: python3 tools/verify_heartbeat_cutover.py preflight")
        print()
        print("Then this command prints the apply procedure. Until then it will not.")
        return 1

    repository, tag, digest = published
    ref = "%s@%s" % (repository, digest)
    print("== CUTOVER -- %s, gate 1, deploy/images/service/README.md" % WORKLOAD)
    print()
    print("image: %s   (tag %s)" % (ref, tag))
    print()
    print("0. Verify the provenance of the published image, before anything is pulled:")
    print("     docker buildx imagetools inspect %s --raw" % ref)
    print("     gh attestation verify oci://%s --repo rjmendez/dama-hear" % ref)
    print("     cosign verify-attestation --type slsaprovenance %s   # if cosign is the path"
          % ref)
    print("   The provenance must name this repository, the images workflow and commit %s." % tag)
    print()
    print("1. Reconcile live against repo (separate change if it differs):")
    print("     kubectl -n %s get deploy %s -o json > live-deploy.json" % (ns, WORKLOAD))
    print("     kubectl -n %s get cm %s -o json > live-cm.json" % (ns, BUNDLE))
    print("     python3 tools/verify_heartbeat_cutover.py preflight \\")
    print("         --live-deployment live-deploy.json --live-configmap live-cm.json")
    print()
    print("2. Back the ledger up with the SQLite backup API -- never a raw copy of a live WAL:")
    print("     kubectl -n %s exec deploy/%s -- python3 -c \\" % (ns, WORKLOAD))
    print("       'import sqlite3;s=sqlite3.connect(\"file:/state/heartbeat-receiver.sqlite3"
          "?mode=ro\",uri=True);d=sqlite3.connect(\"/state/pre-cutover.sqlite3\");s.backup(d);"
          "d.close();s.close()'")
    print("     kubectl -n %s cp %s/<pod>:/state/pre-cutover.sqlite3 ./pre-cutover.sqlite3"
          % (ns, ns))
    print("     python3 tools/verify_heartbeat_cutover.py snapshot --sqlite ./pre-cutover.sqlite3 \\")
    print("         --label before --out snapshot-before.json")
    print("   Note max_record_id from that file: it is the watermark the after snapshot is")
    print("   measured against, and without it continuity and conservation are both guesses.")
    print()
    print("3. Capture the pre-cutover health and the Redis keys:")
    print("     curl -s http://<node>:5051/healthz > healthz-before.json")
    print("     redis-cli --scan --pattern 'dama:hear:*' > redis-before.txt")
    print()
    print("4. Pre-pull on the node that holds hostPort 5051, so the Recreate gap is a container")
    print("   start and not a registry round-trip:")
    print("     sudo k3s ctr images pull %s" % ref)
    print("     sudo k3s ctr images ls | grep %s > node-images.txt" % digest[:19])
    print("     python3 tools/verify_heartbeat_cutover.py preflight --node-images node-images.txt")
    print()
    print("5. Apply. `Recreate` takes the old pod down first, which is required: one pod holds")
    print("   hostPort 5051 and an RWO PVC, and two writers on one SQLite ledger is the failure")
    print("   this strategy exists to prevent. Expect a visible gap of one pod cycle.")
    print("     kubectl -n %s apply -f deploy/k8s/%s.proposed.yaml" % (ns, WORKLOAD))
    print("     kubectl -n %s rollout status deploy/%s --timeout=180s" % (ns, WORKLOAD))
    print()
    print("6. Collect the evidence (after at least two heartbeat intervals):")
    print("     kubectl -n %s get pod -l app=%s -o json > pod-after.json" % (ns, WORKLOAD))
    print("     kubectl -n %s logs deploy/%s > logs-after.txt" % (ns, WORKLOAD))
    print("     curl -s http://<node>:5051/healthz > healthz-after.json")
    print("     # backup + snapshot again, as in step 2, but with the watermark:")
    print("     #   snapshot --sqlite ./post-cutover.sqlite3 --label after \\")
    print("     #            --since-id <max_record_id from snapshot-before.json> \\")
    print("     #            --out snapshot-after.json")
    print("     python3 tools/verify_heartbeat_cutover.py compare \\")
    print("         --before snapshot-before.json --after snapshot-after.json")
    print()
    _print_rollback(ns)
    print()
    print("8. Build the receipt. Gate 1 does not close until all ten rows pass:")
    print("     python3 tools/verify_heartbeat_cutover.py receipt \\")
    print("         --pod pod-after.json --logs logs-after.txt \\")
    print("         --health-before healthz-before.json --health-after healthz-after.json \\")
    print("         --snapshot-before snapshot-before.json --snapshot-after snapshot-after.json \\")
    print("         --redis-keys redis-after.txt --live-configmap live-cm.json \\")
    print("         --rollback-pod pod-rollback.json --snapshot-rollback snapshot-rollback.json")
    return 0


def _print_rollback(ns):
    print("7. ROLLBACK -- part of the gate, not a contingency. It is performed, evidenced, and")
    print("   then the cutover is re-applied if the evidence is good.")
    print("     kubectl -n %s apply -f deploy/k8s/%s.yaml   # the pre-cutover manifest"
          % (ns, WORKLOAD))
    print("     # or, for the Deployment alone:")
    print("     kubectl -n %s rollout undo deployment/%s" % (ns, WORKLOAD))
    print("     kubectl -n %s rollout status deploy/%s --timeout=180s" % (ns, WORKLOAD))
    print()
    print("   The ConfigMap it re-references was never deleted, so there is nothing to restore")
    print("   first. If it somehow was:")
    print("     kubectl -n %s apply -f deploy/k8s/%s.yaml" % (ns, BUNDLE))
    print("   (`python3 deploy/k8s/gen_configmap.py %s` reproduces that file byte for byte.)"
          % BUNDLE)
    print()
    print("   No state is reversed in either direction: the cutover never touched /state, the")
    print("   PVC, the sqlite path or the uid. Evidence the rollback the same way:")
    print("     kubectl -n %s get pod -l app=%s -o json > pod-rollback.json" % (ns, WORKLOAD))
    print("     # backup + snapshot into snapshot-rollback.json, then:")
    print("     python3 tools/verify_heartbeat_cutover.py compare --transition rollback \\")
    print("         --before snapshot-after.json --after snapshot-rollback.json")


# --------------------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(
        prog="verify_heartbeat_cutover",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", default=str(ROOT), help="checkout to read (default: this one)")
    sub = p.add_subparsers(dest="command", required=True)

    pf = sub.add_parser("preflight", help="repo and live gates that must hold before an apply")
    pf.add_argument("--applied", help="deploy/k8s/hear-heartbeat.yaml")
    pf.add_argument("--proposed", help="deploy/k8s/hear-heartbeat.proposed.yaml")
    pf.add_argument("--live-deployment", help="kubectl -n dama get deploy hear-heartbeat -o json")
    pf.add_argument("--live-configmap", help="kubectl -n dama get cm hear-heartbeat-code -o json")
    pf.add_argument("--node-images", help="sudo k3s ctr images ls, captured on the hostPort node")
    pf.add_argument("--state-backup", help="a SQLite-backup-API copy of the ledger")
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(func=cmd_preflight)

    sn = sub.add_parser("snapshot", help="read-only fingerprint of the durable ledger")
    sn.add_argument("--sqlite", required=True)
    sn.add_argument("--label")
    sn.add_argument("--out")
    sn.add_argument("--since-id", type=int,
                    help="the before snapshot's max_record_id: records the first row each device "
                         "wrote after it, which is what continuity is measured against")
    sn.add_argument("--allow-hot", action="store_true",
                    help="read a database with a live WAL sidecar in place (still mode=ro)")
    sn.set_defaults(func=cmd_snapshot)

    cp = sub.add_parser("compare", help="record conservation and continuity between snapshots")
    cp.add_argument("--before", required=True)
    cp.add_argument("--after", required=True)
    cp.add_argument("--devices", type=int, default=DEFAULT_DEVICE_COUNT)
    cp.add_argument("--heartbeat-interval-s", type=float, default=DEFAULT_HEARTBEAT_INTERVAL_S)
    cp.add_argument("--recreate-gap-s", type=float, default=DEFAULT_RECREATE_GAP_S)
    cp.add_argument("--transition", default="cutover")
    cp.add_argument("--allow-pruned", type=int, default=0,
                    help="how many pre-cutover rows retention pruning may have removed during "
                         "the window (default 0: a packaging change may not lose state)")
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(func=cmd_compare)

    hl = sub.add_parser("health", help="/healthz shape and target parity across the cutover")
    hl.add_argument("--before", required=True)
    hl.add_argument("--after", required=True)
    hl.add_argument("--json", action="store_true")
    hl.set_defaults(func=cmd_health)

    rc = sub.add_parser("receipt", help="the ten-row gate-1 evidence table")
    rc.add_argument("--pod")
    rc.add_argument("--logs")
    rc.add_argument("--health-before")
    rc.add_argument("--health-after")
    rc.add_argument("--snapshot-before")
    rc.add_argument("--snapshot-after")
    rc.add_argument("--snapshot-rollback")
    rc.add_argument("--rollback-pod")
    rc.add_argument("--rollback-image", default="python:3.13-slim")
    rc.add_argument("--redis-keys")
    rc.add_argument("--live-configmap")
    rc.add_argument("--devices", type=int, default=DEFAULT_DEVICE_COUNT)
    rc.add_argument("--heartbeat-interval-s", type=float, default=DEFAULT_HEARTBEAT_INTERVAL_S)
    rc.add_argument("--recreate-gap-s", type=float, default=DEFAULT_RECREATE_GAP_S)
    rc.add_argument("--allow-pruned", type=int, default=0)
    rc.add_argument("--json", action="store_true")
    rc.set_defaults(func=cmd_receipt)

    pl = sub.add_parser("plan", help="print the cutover/rollback commands (never runs them)")
    pl.add_argument("--namespace", default="dama")
    pl.add_argument("--rollback", action="store_true", help="print only the rollback procedure")
    pl.set_defaults(func=cmd_plan)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except InputError as exc:
        print("input error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
