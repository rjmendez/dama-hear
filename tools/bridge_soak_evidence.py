#!/usr/bin/env python3
"""Dated, read-only soak evidence for the live `hear-mqtt-bridge` durable outbox.

    python3 tools/bridge_soak_evidence.py --milestone T0
    python3 tools/bridge_soak_evidence.py --milestone T+24h --baseline ~/bridge-soak/<T0>/snapshot.json

The durable SQLite outbox went live on `deploy/hear-mqtt-bridge` with a dedicated
`hear-mqtt-bridge-state` PVC mounted at /state. Phase 2 is a soak: the same evidence has to be
re-collected at T+24h, T+7d and T+14d and compared against the T0 baseline. Doing that by hand is
how a soak quietly stops being evidence, so this tool collects it and grades it.

What it records: the sample window, deployment generation/image/strategy, the code ConfigMap
checksum and provenance annotations, PVC binding, durable record counts and per-node coverage,
pending/failed replay state, prune/retention settings, receiver and Redis cache evidence, and
bridge error/rejection counts.

Three hard rules, enforced in code rather than by convention (see `assert_read_only`):

1. Every kubectl invocation is a read verb. `apply`, `patch`, `delete`, `scale`, `rollout`, `cp`
   and friends are refused, so this tool can never mutate the cluster or restart a pod.
2. The outbox is opened `file:...?mode=ro` with `PRAGMA query_only=ON`, so the sample cannot write
   the WAL, cannot prune and cannot delete a record. A pending record is telemetry that Redis has
   not yet acknowledged; losing one here would destroy the very thing the soak is proving.
3. Nothing secret and nothing positional is printed. Env values are redacted by name, and every
   free-text field (log lines, error text) has high-precision decimals stripped before it reaches
   a file -- soak snapshots get pasted into issues, and `tools/coord_guard.py` is not watching
   those.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

DEFAULT_NAMESPACE = "dama"
DEFAULT_DEPLOYMENT = "hear-mqtt-bridge"
DEFAULT_CONFIGMAP = "hear-mqtt-bridge-code"
DEFAULT_PVC = "hear-mqtt-bridge-state"
DEFAULT_RECEIVER = "hear-heartbeat"
DEFAULT_RECEIVER_PORT = 5051
# Redis lives in `infra` as a StatefulSet, so the cache read needs TYPE/NAME, not a bare name.
DEFAULT_REDIS_NAMESPACE = "infra"
DEFAULT_REDIS_WORKLOAD = "sts/audit-redis"
DEFAULT_STATE_DIR = "/state"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_LOG_TAIL = 500
DEFAULT_OUT_DIR = "~/bridge-soak"

# The six nodes that publish `dama/<node>/telemetry`; every one of them must appear in the ledger.
EXPECTED_NODES: Tuple[str, ...] = ("gold", "nyquist", "mach", "rankine", "ageev", "kasami")

# Measured at T0 (2026-09-15): 2,810 records in 77.75 min across six nodes.
DEFAULT_RECORDS_PER_DAY = 52000
DEFAULT_GROWTH_TOLERANCE = 0.2
# ~1.49 KB/record including indexes, so ~78 MB/day; the caps leave room for a burst and still sit
# far under the 5Gi PVC.
STATE_BUDGET_MB: Dict[str, float] = {"T0": 64.0, "T+24h": 200.0, "T+7d": 1200.0, "T+14d": 2200.0}
MILESTONES: Tuple[str, ...] = ("T0", "T+24h", "T+7d", "T+14d")
MILESTONE_ELAPSED_H: Dict[str, Optional[float]] = {
    "T0": None, "T+24h": 24.0, "T+7d": 168.0, "T+14d": 336.0,
}
# How far the observed elapsed time may sit from the milestone's nominal one and still grade it.
ELAPSED_TOLERANCE = 0.25
# The ledger's newest row must be recent, or the bridge has stopped writing.
FRESHNESS_LIMIT_S = 120.0

# Network identity of the live bridge. Phase 2 must not move traffic; the TLS cutover is a
# separate, sequenced workstream (an mTLS/8883 variant of this Deployment exists in deploy/k8s
# and must not be applied here).
REQUIRED_ENV: Dict[str, str] = {"MQTT_HOST": "127.0.0.1", "MQTT_PORT": "31883"}
DURABLE_ENV_KEYS: Tuple[str, ...] = (
    "HEAR_DURABLE_STORE",
    "HEAR_DURABLE_DB",
    "HEAR_DURABLE_REPLAY_LIMIT",
    "HEAR_DURABLE_REPLAY_INTERVAL_S",
    "HEAR_DURABLE_RETENTION_DAYS",
    "HEAR_DURABLE_PRUNE_INTERVAL_S",
)
# Defaults from tools/hear_heartbeat_receiver.py; unset env means the default is active.
DURABLE_ENV_DEFAULTS: Dict[str, str] = {
    "HEAR_DURABLE_STORE": "none",
    "HEAR_DURABLE_DB": "/state/heartbeats.sqlite3",
    "HEAR_DURABLE_REPLAY_LIMIT": "256",
    "HEAR_DURABLE_REPLAY_INTERVAL_S": "5.0",
    "HEAR_DURABLE_RETENTION_DAYS": "30",
    "HEAR_DURABLE_PRUNE_INTERVAL_S": "3600",
}

# kubectl verbs that only read. Anything absent is refused rather than reasoned about.
READ_VERBS = frozenset({"get", "describe", "logs", "version", "exec", "top", "api-resources"})
# `kubectl exec` is a read verb only because the remote command is whitelisted below.
EXEC_ALLOWED_BINARIES = frozenset({"python3", "python", "sh", "redis-cli"})
# Shell fragments allowed inside `sh -lc`; every one only reports.
EXEC_SHELL_ALLOWED = re.compile(r"^(?:ls|du|df|stat|cat /proc/[\w/]+|wc)\b[^;&|><`$]*$")
EXEC_SHELL_SPLIT = re.compile(r"\s*;\s*")
# redis-cli subcommands that cannot write. `KEYS` is excluded on purpose (O(N) on a live cache).
REDIS_READ_COMMANDS = frozenset({"DBSIZE", "INFO", "EXISTS", "XLEN", "TTL", "TYPE", "SCARD",
                                 "STRLEN", "MEMORY", "PING", "XINFO"})
# Statements that would mutate the ledger. Present here so the snippet can be asserted, not just
# reviewed.
SQL_WRITE = re.compile(r"\b(?:insert|update|delete|drop|alter|create|replace|vacuum|reindex)\b",
                       re.IGNORECASE)
# Anything an inline snippet could use to change the pod instead of reporting on it.
PYTHON_SIDE_EFFECT = re.compile(
    r"\b(?:os\.(?:remove|unlink|rename|rmdir|truncate|system|kill|makedirs|mkdir)|"
    r"shutil\.|subprocess|os\.exec|signal\.|open\([^)]*[\"'][rbt]*[wax]\+?[bt]*[\"']|"
    r"\.backup\(|\.write\()")

# Anything whose *name* looks like a credential never has its value recorded.
SECRET_NAME = re.compile(r"(?:PASS|PASSWORD|SECRET|TOKEN|KEY|CRED|AUTH)", re.IGNORECASE)
REDACTED = "<redacted>"
# A decimal with >= 4 fractional places is the spelling a coordinate travels in; soak text has no
# legitimate need for one, so it is blunted rather than parsed. Mirrors coord_guard's FRACTION.
PRECISION = re.compile(r"(?<![\w.])(-?\d{1,3})[.,](\d{4,})(?!\d)")
PRECISION_MASK = r"\1.<precision-redacted>"

# Bridge log lines worth counting. The bodies are never kept, only the counts and a redacted
# sample, because a rejected payload is attacker/field-controlled text.
LOG_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("sqlite_locked", re.compile(r"database is locked", re.IGNORECASE)),
    ("rejected", re.compile(r"\brejected\b", re.IGNORECASE)),
    ("durable_error", re.compile(r"durable.*(?:error|failed)|DurableStoreError", re.IGNORECASE)),
    ("redis_error", re.compile(r"redis.*(?:error|refused|timeout|unavailable)", re.IGNORECASE)),
    ("replay", re.compile(r"replay(?:ed|ing)?\b", re.IGNORECASE)),
    ("reconnect", re.compile(r"reconnect|disconnected|connection lost", re.IGNORECASE)),
    ("traceback", re.compile(r"^Traceback \(most recent call last\)", re.MULTILINE)),
)
# A `rejected` line is a payload-schema defect upstream, not a durability defect: the message is
# dropped by validation *before* the outbox commit. It is recorded and never graded.
GRADED_LOG_KINDS: Tuple[str, ...] = ("sqlite_locked", "durable_error", "traceback")


class UnsafeCommand(RuntimeError):
    """A command that could mutate the cluster, the outbox or a pod was refused."""


@dataclass
class WarningSink:
    items: List[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        text = redact_text(text)
        if text not in self.items:
            self.items.append(text)


@dataclass
class Criterion:
    name: str
    verdict: str  # "pass" | "fail" | "unknown"
    detail: str

    def as_dict(self) -> Dict[str, str]:
        return {"name": self.name, "verdict": self.verdict, "detail": self.detail}


# ---------------------------------------------------------------- redaction

def redact_text(value: Any) -> Any:
    """Blunts high-precision decimals anywhere in free text. Idempotent."""
    if not isinstance(value, str):
        return value
    return PRECISION.sub(PRECISION_MASK, value)


def redact_env_value(name: str, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if SECRET_NAME.search(name or ""):
        return REDACTED if value else ""
    return redact_text(value)


def redact_tree(obj: Any) -> Any:
    """Recursively redacts a structure that is about to be written or printed."""
    if isinstance(obj, Mapping):
        return {k: (REDACTED if (isinstance(k, str) and SECRET_NAME.search(k)
                                 and isinstance(obj[k], str) and obj[k])
                    else redact_tree(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_tree(v) for v in obj]
    return redact_text(obj)


# ---------------------------------------------------------------- read-only enforcement

def _assert_exec_command(remote: Sequence[str]) -> None:
    if not remote:
        raise UnsafeCommand("kubectl exec with no command is refused")
    binary = os.path.basename(remote[0])
    if binary not in EXEC_ALLOWED_BINARIES:
        raise UnsafeCommand("kubectl exec may only run %s, not %r"
                            % ("/".join(sorted(EXEC_ALLOWED_BINARIES)), remote[0]))
    if binary in ("python3", "python"):
        if list(remote[1:2]) != ["-c"]:
            raise UnsafeCommand("kubectl exec python must run an inline -c snippet")
        code = " ".join(remote[2:])
        if SQL_WRITE.search(code):
            raise UnsafeCommand("kubectl exec python snippet contains a writing SQL statement")
        if PYTHON_SIDE_EFFECT.search(code):
            raise UnsafeCommand("kubectl exec python snippet may not write, spawn or signal")
        if "sqlite3" in code and ("mode=ro" not in code or "query_only" not in code):
            raise UnsafeCommand("kubectl exec python snippet must open sqlite read-only "
                                "(mode=ro + PRAGMA query_only)")
        return
    if binary == "sh":
        if list(remote[1:2]) not in (["-lc"], ["-c"]):
            raise UnsafeCommand("kubectl exec sh must be `sh -lc <script>`")
        script = " ".join(remote[2:])
        for part in EXEC_SHELL_SPLIT.split(script.strip()):
            if part and not EXEC_SHELL_ALLOWED.match(part):
                raise UnsafeCommand("kubectl exec shell fragment is not a read command")
        return
    if binary == "redis-cli":
        words = [w for w in remote[1:] if not w.startswith("-")]
        # skip a `-h host -p port`-style value that survived the filter
        head = next((w for w in words if w.upper() in REDIS_READ_COMMANDS
                     or w.upper().isalpha()), None)
        if head is None or head.upper() not in REDIS_READ_COMMANDS:
            raise UnsafeCommand("redis-cli may only run %s"
                                % ", ".join(sorted(REDIS_READ_COMMANDS)))


def assert_read_only(args: Sequence[str]) -> None:
    """Raises UnsafeCommand unless `args` is a kubectl read.

    This is the single choke point every cluster call goes through; it is what makes "this tool
    cannot restart a pod" a property of the code instead of a promise in a docstring.
    """
    args = list(args)
    verb_idx = None
    skip_next = False
    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg == "--":
            break
        if arg.startswith("-"):
            skip_next = "=" not in arg and arg in ("-n", "--namespace", "-o", "--output",
                                                   "-l", "--selector", "--tail", "--since",
                                                   "--context", "--timeout", "-c", "--container")
            continue
        verb_idx = i
        break
    if verb_idx is None:
        raise UnsafeCommand("kubectl call has no verb")
    verb = args[verb_idx]
    if verb not in READ_VERBS:
        raise UnsafeCommand("kubectl %s is not a read verb" % verb)
    if verb == "exec":
        if "--" not in args:
            raise UnsafeCommand("kubectl exec must separate the remote command with --")
        _assert_exec_command(args[args.index("--") + 1:])


KubectlRunner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def run_kubectl(args: Sequence[str], timeout: float = DEFAULT_TIMEOUT_S
                ) -> "subprocess.CompletedProcess[str]":
    assert_read_only(args)
    return subprocess.run(["kubectl", *args], check=False, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


@dataclass
class Cluster:
    """Every cluster read the snapshot needs, behind one injectable runner."""

    namespace: str = DEFAULT_NAMESPACE
    timeout: float = DEFAULT_TIMEOUT_S
    runner: KubectlRunner = run_kubectl
    warnings: WarningSink = field(default_factory=WarningSink)

    def available(self) -> bool:
        return self.runner is not run_kubectl or shutil.which("kubectl") is not None

    def _run(self, args: Sequence[str], what: str) -> Optional[str]:
        if not self.available():
            self.warnings.add("kubectl is unavailable; cannot read %s" % what)
            return None
        try:
            assert_read_only(args)
        except UnsafeCommand:
            raise
        try:
            proc = self.runner(list(args), self.timeout)
        except UnsafeCommand:
            raise
        except Exception as exc:  # subprocess failures must degrade, not abort the snapshot
            self.warnings.add("kubectl failed reading %s: %s" % (what, exc))
            return None
        if proc.returncode != 0:
            self.warnings.add("kubectl could not read %s: %s"
                              % (what, (proc.stderr or proc.stdout or "").strip()))
            return None
        return proc.stdout

    def get_json(self, kind: str, name: str, what: Optional[str] = None) -> Optional[Dict[str, Any]]:
        out = self._run(["-n", self.namespace, "get", kind, name, "-o", "json"],
                        what or "%s/%s" % (kind, name))
        return _loads(out, what or "%s/%s" % (kind, name), self.warnings)

    def list_json(self, kind: str, selector: str, what: str) -> Optional[Dict[str, Any]]:
        out = self._run(["-n", self.namespace, "get", kind, "-l", selector, "-o", "json"], what)
        return _loads(out, what, self.warnings)

    def logs(self, target: str, tail: int) -> Optional[str]:
        return self._run(["-n", self.namespace, "logs", target, "--tail", str(tail)],
                         "logs for %s" % target)

    def exec_out(self, pod: str, remote: Sequence[str], what: str) -> Optional[str]:
        return self._run(["-n", self.namespace, "exec", pod, "--", *remote], what)


def _loads(text: Optional[str], what: str, warnings: WarningSink) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        warnings.add("invalid JSON for %s: %s" % (what, exc))
        return None
    if not isinstance(data, dict):
        warnings.add("JSON for %s is %s, not an object" % (what, type(data).__name__))
        return None
    return data


# ---------------------------------------------------------------- the outbox sample

# Runs inside the bridge pod. Read-only twice over: the URI is `mode=ro` (sqlite refuses to create
# or write the file) and `PRAGMA query_only=ON` (the connection refuses writes even if a future
# edit adds one). immutable=0 is deliberate: the writer is live and the WAL must still be read.
OUTBOX_SNIPPET = r"""
import json, os, sqlite3, time
path = os.environ.get("SOAK_DB", "/state/mqtt-bridge.sqlite3")
con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5.0)
con.execute("PRAGMA query_only=ON")
one = lambda s: con.execute(s).fetchone()
all_ = lambda s: [list(r) for r in con.execute(s).fetchall()]
tables = [r[0] for r in con.execute(
    "select name from sqlite_master where type='table' order by name")]
out = {"path": path, "tables": tables, "sampled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                   time.gmtime())}
if "durable_records" in tables:
    out["records"] = one("select count(*) from durable_records")[0]
    out["window"] = list(one("select min(created_at), max(created_at) from durable_records"))
    out["by_path"] = all_(
        "select telemetry_path, count(*) from durable_records group by 1 order by 1")
    out["by_device"] = all_(
        "select device_id, count(*) from durable_records group by 1 order by 2 desc, 1")
    out["pending"] = one(
        "select count(*) from durable_records r where not exists ("
        "select 1 from cache_attempts a where a.record_uid = r.record_uid"
        " and a.outcome = 'succeeded')")[0]
    out["schema_versions"] = all_(
        "select distinct durable_schema_version from durable_records order by 1")
if "cache_attempts" in tables:
    out["succeeded"] = one(
        "select count(*) from cache_attempts where outcome = 'succeeded'")[0]
    out["failed"] = one("select count(*) from cache_attempts where outcome = 'failed'")[0]
    row = one("select created_at from cache_attempts where outcome = 'failed'"
              " order by id desc limit 1")
    out["last_failure_at"] = None if row is None else row[0]
    out["failed_by_target"] = all_(
        "select cache_target, count(*) from cache_attempts where outcome = 'failed'"
        " group by 1 order by 1")
out["page_count"] = one("pragma page_count")[0]
out["page_size"] = one("pragma page_size")[0]
out["journal_mode"] = one("pragma journal_mode")[0]
print(json.dumps(out, sort_keys=True))
"""


def outbox_command(db_path: str) -> List[str]:
    return ["python3", "-c", OUTBOX_SNIPPET.replace(
        '"/state/mqtt-bridge.sqlite3"', json.dumps(db_path))]


def normalize_outbox(raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Shapes the in-pod sample into the snapshot's `outbox` block."""
    if not isinstance(raw, Mapping):
        return {"available": False}
    window = raw.get("window") or [None, None]
    pages, size = _as_int(raw.get("page_count")), _as_int(raw.get("page_size"))
    return {
        "available": True,
        "path": raw.get("path"),
        "tables": list(raw.get("tables") or []),
        "records": _as_int(raw.get("records")),
        "oldest_record_at": window[0] if len(window) > 0 else None,
        "newest_record_at": window[1] if len(window) > 1 else None,
        "pending": _as_int(raw.get("pending")),
        "succeeded": _as_int(raw.get("succeeded")),
        "failed": _as_int(raw.get("failed")),
        "last_failure_at": raw.get("last_failure_at"),
        "failed_by_target": {str(k): _as_int(v) for k, v in (raw.get("failed_by_target") or [])},
        "by_path": {str(k): _as_int(v) for k, v in (raw.get("by_path") or [])},
        "by_device": {str(k): _as_int(v) for k, v in (raw.get("by_device") or [])},
        "schema_versions": [_as_int(v[0]) if isinstance(v, (list, tuple)) else _as_int(v)
                            for v in (raw.get("schema_versions") or [])],
        "db_bytes": None if pages is None or size is None else pages * size,
        "journal_mode": raw.get("journal_mode"),
        "sampled_at": raw.get("sampled_at"),
    }


def node_coverage(outbox: Mapping[str, Any], expected: Sequence[str]) -> Dict[str, Any]:
    by_device = outbox.get("by_device") or {}
    present = sorted(k for k in by_device if by_device.get(k))
    missing = [n for n in expected if n not in by_device or not by_device.get(n)]
    return {
        "expected": list(expected),
        "present": present,
        "missing": missing,
        "unexpected": sorted(set(present) - set(expected)),
        "complete": not missing and bool(present),
        "counts": {k: by_device[k] for k in sorted(by_device)},
    }


# ---------------------------------------------------------------- cluster object summaries

def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _container(deploy: Mapping[str, Any], name: str) -> Dict[str, Any]:
    containers = (((deploy.get("spec") or {}).get("template") or {}).get("spec") or {}
                  ).get("containers") or []
    for c in containers:
        if isinstance(c, Mapping) and c.get("name") == name:
            return dict(c)
    return dict(containers[0]) if containers and isinstance(containers[0], Mapping) else {}


def deployment_env(deploy: Mapping[str, Any], container: str = "bridge") -> Dict[str, Optional[str]]:
    """Env as a name -> redacted value map. A valueFrom entry is reported as its source kind."""
    out: Dict[str, Optional[str]] = {}
    for item in _container(deploy, container).get("env") or []:
        if not isinstance(item, Mapping) or not item.get("name"):
            continue
        name = str(item["name"])
        if "valueFrom" in item:
            out[name] = "<from:%s>" % ",".join(sorted(item["valueFrom"] or {}))
        else:
            out[name] = redact_env_value(name, item.get("value"))
    return out


def summarize_deployment(deploy: Optional[Mapping[str, Any]],
                         container: str = "bridge") -> Dict[str, Any]:
    if not isinstance(deploy, Mapping):
        return {"available": False}
    meta, spec, status = (deploy.get("metadata") or {}, deploy.get("spec") or {},
                          deploy.get("status") or {})
    env = deployment_env(deploy, container)
    c = _container(deploy, container)
    strategy = spec.get("strategy") or {}
    pod_spec = ((spec.get("template") or {}).get("spec") or {})
    durable = {k: env.get(k, DURABLE_ENV_DEFAULTS.get(k)) for k in DURABLE_ENV_KEYS}
    durable_explicit = {k: k in env for k in DURABLE_ENV_KEYS}
    network = {k: env.get(k) for k in REQUIRED_ENV}
    drift = {k: {"want": v, "have": env.get(k)} for k, v in REQUIRED_ENV.items()
             if env.get(k) != v}
    mounts = [m.get("mountPath") for m in (c.get("volumeMounts") or [])
              if isinstance(m, Mapping)]
    claims = {v.get("name"): ((v.get("persistentVolumeClaim") or {}).get("claimName"))
              for v in (pod_spec.get("volumes") or []) if isinstance(v, Mapping)}
    return {
        "available": True,
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "generation": _as_int(meta.get("generation")),
        "observed_generation": _as_int(status.get("observedGeneration")),
        "annotations": {k: redact_text(v) for k, v in (meta.get("annotations") or {}).items()
                        if not k.startswith("kubectl.kubernetes.io/last-applied")},
        "image": c.get("image"),
        "container": c.get("name"),
        "replicas": _as_int(spec.get("replicas")),
        "ready_replicas": _as_int(status.get("readyReplicas")) or 0,
        "updated_replicas": _as_int(status.get("updatedReplicas")) or 0,
        "unavailable_replicas": _as_int(status.get("unavailableReplicas")) or 0,
        "strategy": strategy.get("type"),
        "host_network": bool(pod_spec.get("hostNetwork")),
        "durable_env": durable,
        "durable_env_explicit": durable_explicit,
        "network_env": network,
        "network_drift": drift,
        "volume_mounts": mounts,
        "volume_claims": {k: v for k, v in claims.items() if v},
        "env_names": sorted(env),
        "rolled_out": (_as_int(meta.get("generation")) is not None
                       and _as_int(meta.get("generation")) == _as_int(status.get("observedGeneration"))),
    }


def configmap_digest(cm: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """sha256 over the ConfigMap's data, computed the same way on every run.

    Keys are sorted and each item hashed as `name\\0len\\0bytes`, so a rename or a truncation
    changes the digest. This is the drift signal for "live code is still the code the repo
    generated"; the tool never fetches or applies the repo bundle.
    """
    if not isinstance(cm, Mapping):
        return {"available": False}
    data = cm.get("data") or {}
    h = hashlib.sha256()
    items = []
    for key in sorted(data):
        blob = str(data[key]).encode("utf-8")
        h.update(key.encode("utf-8") + b"\0" + str(len(blob)).encode("ascii") + b"\0" + blob)
        items.append({"key": key, "bytes": len(blob),
                      "sha256": hashlib.sha256(blob).hexdigest()})
    ann = (cm.get("metadata") or {}).get("annotations") or {}
    return {
        "available": True,
        "name": (cm.get("metadata") or {}).get("name"),
        "checksum_sha256": h.hexdigest(),
        "items": items,
        "annotations": {k: redact_text(v) for k, v in ann.items()
                        if k.startswith("dama-hear/")},
    }


def summarize_pvc(pvc: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not isinstance(pvc, Mapping):
        return {"available": False}
    spec, status = pvc.get("spec") or {}, pvc.get("status") or {}
    return {
        "available": True,
        "name": (pvc.get("metadata") or {}).get("name"),
        "phase": status.get("phase"),
        "capacity": (status.get("capacity") or {}).get("storage"),
        "requested": ((spec.get("resources") or {}).get("requests") or {}).get("storage"),
        "access_modes": list(status.get("accessModes") or spec.get("accessModes") or []),
        "storage_class": spec.get("storageClassName"),
        "volume": spec.get("volumeName"),
        "bound": status.get("phase") == "Bound",
    }


def summarize_pod(pod: Optional[Mapping[str, Any]], container: str = "bridge") -> Dict[str, Any]:
    if not isinstance(pod, Mapping):
        return {"available": False}
    meta, status = pod.get("metadata") or {}, pod.get("status") or {}
    statuses = [s for s in (status.get("containerStatuses") or []) if isinstance(s, Mapping)]
    picked = next((s for s in statuses if s.get("name") == container), statuses[0] if statuses else {})
    restarts = sum(_as_int(s.get("restartCount")) or 0 for s in statuses)
    return {
        "available": True,
        "name": meta.get("name"),
        "phase": status.get("phase"),
        "ready": bool(picked.get("ready")),
        "restarts": restarts,
        "started_at": (picked.get("state") or {}).get("running", {}).get("startedAt"),
        "pod_start_time": status.get("startTime"),
        "image_id": picked.get("imageID"),
        "last_termination": redact_tree((picked.get("lastState") or {}).get("terminated")),
    }


def pick_pod(pods: Optional[Mapping[str, Any]], container: str = "bridge") -> Dict[str, Any]:
    items = [i for i in ((pods or {}).get("items") or []) if isinstance(i, Mapping)]
    running = [i for i in items if (i.get("status") or {}).get("phase") == "Running"]
    chosen = sorted(running or items,
                    key=lambda i: str((i.get("metadata") or {}).get("name") or ""))
    return summarize_pod(chosen[0] if chosen else None, container)


def parse_state_listing(text: Optional[str]) -> Dict[str, Any]:
    """Turns `ls -l <state>; du -sk <state>` output into sizes. Tolerates either half missing."""
    if not text:
        return {"available": False}
    files: Dict[str, int] = {}
    total_kb: Optional[int] = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(\d+)\s+(\S.*)$", line)
        if m and not line.startswith(("total", "-", "d", "l")):
            total_kb = int(m.group(1))
            continue
        parts = line.split()
        if len(parts) >= 9 and parts[0][:1] in "-dlbcps":
            size = _as_int(parts[4])
            if size is not None:
                files[parts[-1]] = size
    return {
        "available": bool(files or total_kb is not None),
        "files": files,
        "total_bytes": None if total_kb is None else total_kb * 1024,
    }


def scan_logs(text: Optional[str]) -> Dict[str, Any]:
    """Counts the log kinds that grade a soak; keeps at most one redacted sample of each."""
    if text is None:
        return {"available": False, "counts": {}, "samples": {}}
    counts: Dict[str, int] = {}
    samples: Dict[str, str] = {}
    lines = text.splitlines()
    for kind, pattern in LOG_PATTERNS:
        hits = [ln for ln in lines if pattern.search(ln)]
        counts[kind] = len(hits)
        if hits:
            samples[kind] = redact_text(hits[-1])[:240]
    return {"available": True, "lines": len(lines), "counts": counts, "samples": samples,
            "graded": {k: counts.get(k, 0) for k in GRADED_LOG_KINDS}}


def summarize_receiver(health: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The hear-heartbeat receiver's own durable_store block from /healthz."""
    if not isinstance(health, Mapping):
        return {"available": False}
    durable = health.get("durable_store")
    if not isinstance(durable, Mapping):
        durable = health if "pending_records" in health else {}
    return {
        "available": bool(durable),
        "status": health.get("status"),
        "backend": durable.get("backend"),
        "enabled": durable.get("enabled"),
        "pending_records": _as_int(durable.get("pending_records")),
        "cache_successes": _as_int(durable.get("cache_successes")),
        "cache_failures": _as_int(durable.get("cache_failures")),
        "last_cache_failure_at": durable.get("last_cache_failure_at"),
    }


def summarize_cache(raw: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Redis-side evidence: reachable, key count, and stream lengths if they were asked for."""
    if not isinstance(raw, Mapping):
        return {"available": False}
    return {
        "available": True,
        "reachable": bool(raw.get("reachable")),
        "authenticated": bool(raw.get("authenticated")),
        "dbsize": _as_int(raw.get("dbsize")),
        "error": redact_text(raw.get("error")),
        "streams": {str(k): _as_int(v) for k, v in (raw.get("streams") or {}).items()},
    }


# ---------------------------------------------------------------- grading

def _parse_ts(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _elapsed_hours(baseline: Optional[Mapping[str, Any]],
                   snapshot: Mapping[str, Any]) -> Optional[float]:
    if not isinstance(baseline, Mapping):
        return None
    start, end = _parse_ts(baseline.get("captured_at")), _parse_ts(snapshot.get("captured_at"))
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 3600.0


def _verdict(ok: Optional[bool]) -> str:
    return "unknown" if ok is None else ("pass" if ok else "fail")


def evaluate(snapshot: Mapping[str, Any], baseline: Optional[Mapping[str, Any]] = None,
             milestone: str = "T0", *, expected_nodes: Sequence[str] = EXPECTED_NODES,
             records_per_day: int = DEFAULT_RECORDS_PER_DAY,
             tolerance: float = DEFAULT_GROWTH_TOLERANCE,
             now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """Grades a snapshot against the Phase 2 criteria for `milestone`.

    T0 grades the state that must hold at every sample. T+24h / T+7d / T+14d add the ones that
    only a baseline can answer: growth, prune behaviour and disk budget.
    """
    checks: List[Criterion] = []
    deploy = snapshot.get("deployment") or {}
    pvc = snapshot.get("pvc") or {}
    pod = snapshot.get("pod") or {}
    outbox = snapshot.get("outbox") or {}
    coverage = snapshot.get("coverage") or {}
    logs = snapshot.get("logs") or {}
    state = snapshot.get("state_dir") or {}
    window = snapshot.get("window") or {}
    now = (now or _parse_ts(window.get("finished_at")) or _parse_ts(snapshot.get("captured_at"))
           or dt.datetime.now(dt.timezone.utc))

    def add(name: str, ok: Optional[bool], detail: str) -> None:
        checks.append(Criterion(name, _verdict(ok), detail))

    store = (deploy.get("durable_env") or {}).get("HEAR_DURABLE_STORE")
    add("durable_store_enabled", None if not deploy.get("available") else store == "sqlite",
        "HEAR_DURABLE_STORE=%s" % (store or "unset"))

    db_env = (deploy.get("durable_env") or {}).get("HEAR_DURABLE_DB")
    mounted = "/state" in (deploy.get("volume_mounts") or [])
    add("state_volume_mounted", None if not deploy.get("available") else
        bool(mounted and db_env and db_env.startswith("/state/")),
        "mounts=%s db=%s" % (deploy.get("volume_mounts"), db_env))

    add("pvc_bound", None if not pvc.get("available") else bool(pvc.get("bound")),
        "%s %s %s" % (pvc.get("name"), pvc.get("phase"), pvc.get("capacity")))

    drift = deploy.get("network_drift") or {}
    add("plaintext_mqtt_preserved", None if not deploy.get("available") else not drift,
        "no drift from %s" % REQUIRED_ENV if not drift else "drift: %s" % drift)

    add("rollout_settled", None if not deploy.get("available") else
        bool(deploy.get("rolled_out") and deploy.get("ready_replicas")
             and not deploy.get("unavailable_replicas")),
        "generation=%s observed=%s ready=%s" % (deploy.get("generation"),
                                                deploy.get("observed_generation"),
                                                deploy.get("ready_replicas")))

    add("pod_restarts_zero", None if not pod.get("available") else pod.get("restarts") == 0,
        "restarts=%s phase=%s ready=%s" % (pod.get("restarts"), pod.get("phase"),
                                           pod.get("ready")))

    records = outbox.get("records")
    add("ledger_has_records", None if not outbox.get("available") else bool(records),
        "records=%s" % records)

    pending = outbox.get("pending")
    add("pending_drained", None if pending is None else pending == 0, "pending=%s" % pending)

    failed = outbox.get("failed")
    baseline_failed = ((baseline or {}).get("outbox") or {}).get("failed")
    if failed is None:
        add("no_cache_failures", None, "failed=unknown")
    elif failed == 0:
        add("no_cache_failures", True, "failed=0")
    elif baseline_failed is not None and failed == baseline_failed:
        # A Redis outage during an earlier window is allowed to have left failures behind; what
        # must not happen is the count growing while pending is 0.
        add("no_cache_failures", True,
            "failed=%s unchanged since baseline (historic outage)" % failed)
    else:
        add("no_cache_failures", False, "failed=%s (baseline %s)" % (failed, baseline_failed))

    add("all_nodes_covered", None if not coverage.get("counts") else bool(coverage.get("complete")),
        "missing=%s present=%s" % (coverage.get("missing"), len(coverage.get("present") or [])))

    newest = _parse_ts(outbox.get("newest_record_at"))
    if newest is None:
        add("ledger_fresh", None, "newest=unknown")
    else:
        age = max(0.0, (now - newest).total_seconds())
        add("ledger_fresh", age <= FRESHNESS_LIMIT_S,
            "newest record %.0fs old (limit %.0fs)" % (age, FRESHNESS_LIMIT_S))

    graded = (logs.get("graded") or {}) if logs.get("available") else {}
    if not logs.get("available"):
        add("logs_clean", None, "logs unavailable")
    else:
        bad = {k: v for k, v in graded.items() if v}
        add("logs_clean", not bad, "clean" if not bad else "found %s" % bad)

    receiver = snapshot.get("receiver") or {}
    if not receiver.get("available"):
        add("receiver_ledger_drained", None, "receiver /healthz unavailable")
    else:
        receiver_pending = receiver.get("pending_records")
        add("receiver_ledger_drained",
            None if receiver_pending is None else receiver_pending == 0,
            "receiver backend=%s pending=%s failures=%s"
            % (receiver.get("backend"), receiver_pending, receiver.get("cache_failures")))

    budget_mb = STATE_BUDGET_MB.get(milestone)
    used = state.get("total_bytes") if state.get("available") else outbox.get("db_bytes")
    if used is None or budget_mb is None:
        add("state_within_budget", None, "state size unknown")
    else:
        add("state_within_budget", used <= budget_mb * 1024 * 1024,
            "%.1f MB of %.0f MB budget" % (used / 1048576.0, budget_mb))

    elapsed_h = _elapsed_hours(baseline, snapshot)
    nominal_h = MILESTONE_ELAPSED_H.get(milestone)
    if milestone != "T0":
        if baseline is None:
            add("baseline_supplied", False, "--baseline is required to grade %s" % milestone)
        else:
            add("baseline_supplied", True, "baseline captured %s" % baseline.get("captured_at"))
        if elapsed_h is None or nominal_h is None:
            add("window_matches_milestone", None, "elapsed unknown")
        else:
            low, high = nominal_h * (1 - ELAPSED_TOLERANCE), nominal_h * (1 + ELAPSED_TOLERANCE)
            add("window_matches_milestone", low <= elapsed_h <= high,
                "elapsed %.1fh, expected %.0fh +/- %.0f%%"
                % (elapsed_h, nominal_h, ELAPSED_TOLERANCE * 100))

        base_records = ((baseline or {}).get("outbox") or {}).get("records")
        if base_records is None or records is None or not elapsed_h:
            add("record_growth", None, "growth unknown")
        else:
            grew = records - base_records
            expect = records_per_day * elapsed_h / 24.0
            low, high = expect * (1 - tolerance), expect * (1 + tolerance)
            add("record_growth", low <= grew <= high,
                "+%d records in %.1fh, expected %.0f +/- %d%%"
                % (grew, elapsed_h, expect, int(tolerance * 100)))

        retention = _as_int((deploy.get("durable_env") or {}).get("HEAR_DURABLE_RETENTION_DAYS"))
        oldest = _parse_ts(outbox.get("oldest_record_at"))
        if retention is None or oldest is None:
            add("prune_within_retention", None, "retention or oldest record unknown")
        else:
            age_d = (now - oldest).total_seconds() / 86400.0
            add("prune_within_retention", age_d <= retention + 1,
                "oldest record %.1fd old, retention %dd" % (age_d, retention))

        base_cov = ((baseline or {}).get("coverage") or {}).get("present") or []
        present = coverage.get("present") or []
        if not base_cov:
            add("coverage_not_regressed", None, "baseline coverage unknown")
        else:
            lost = sorted(set(base_cov) - set(present))
            add("coverage_not_regressed", not lost, "lost=%s" % lost if lost else "none lost")

    failures = [c for c in checks if c.verdict == "fail"]
    unknowns = [c for c in checks if c.verdict == "unknown"]
    return {
        "milestone": milestone,
        "elapsed_hours": None if elapsed_h is None else round(elapsed_h, 3),
        "checks": [c.as_dict() for c in checks],
        "failed": [c.name for c in failures],
        "unknown": [c.name for c in unknowns],
        "verdict": "fail" if failures else ("incomplete" if unknowns else "pass"),
    }


# ---------------------------------------------------------------- collection

def utc_stamp(when: Optional[dt.datetime] = None) -> str:
    return (when or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def utc_now_iso(when: Optional[dt.datetime] = None) -> str:
    return (when or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect(cluster: Cluster, args: argparse.Namespace,
            when: Optional[dt.datetime] = None) -> Dict[str, Any]:
    started = when or dt.datetime.now(dt.timezone.utc)
    deploy_json = cluster.get_json("deploy", args.deployment)
    deploy = summarize_deployment(deploy_json, args.container)
    cm = configmap_digest(cluster.get_json("cm", args.configmap))
    pvc = summarize_pvc(cluster.get_json("pvc", args.pvc))
    pod = pick_pod(cluster.list_json("pods", "app=%s" % args.deployment,
                                     "pods for %s" % args.deployment), args.container)

    db_path = ((deploy.get("durable_env") or {}).get("HEAR_DURABLE_DB")
               or DURABLE_ENV_DEFAULTS["HEAR_DURABLE_DB"])
    outbox_raw = None
    state_raw = None
    if pod.get("name"):
        target = "pod/%s" % pod["name"]
        out = cluster.exec_out(target, outbox_command(db_path), "durable outbox sample")
        outbox_raw = _loads(out, "durable outbox sample", cluster.warnings)
        state_raw = cluster.exec_out(
            target, ["sh", "-lc", "ls -l %s; du -sk %s" % (args.state_dir, args.state_dir)],
            "state directory listing")
    else:
        cluster.warnings.add("no %s pod found; outbox and state evidence are missing"
                             % args.deployment)

    outbox = normalize_outbox(outbox_raw)
    logs = scan_logs(cluster.logs("deploy/%s" % args.deployment, args.log_tail))
    receiver = summarize_receiver(read_receiver_health(cluster, args))
    cache = summarize_cache(read_cache_evidence(cluster, args))
    finished = dt.datetime.now(dt.timezone.utc) if when is None else when

    snapshot: Dict[str, Any] = {
        "schema": "dama-hear/bridge-soak-evidence/v1",
        "milestone": args.milestone,
        "captured_at": utc_now_iso(started),
        "window": {"started_at": utc_now_iso(started), "finished_at": utc_now_iso(finished)},
        "namespace": args.namespace,
        "deployment": deploy,
        "configmap": cm,
        "pvc": pvc,
        "pod": pod,
        "outbox": outbox,
        "coverage": node_coverage(outbox, args.expected_nodes),
        "state_dir": parse_state_listing(state_raw),
        "logs": logs,
        "receiver": receiver,
        "cache": cache,
        "warnings": list(cluster.warnings.items),
    }
    return redact_tree(snapshot)


def read_receiver_health(cluster: Cluster, args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    """Reads the receiver's /healthz from inside its own pod (no Service exposure needed)."""
    if not args.receiver:
        return None
    code = ("import json,sys,urllib.request;"
            "print(urllib.request.urlopen(sys.argv[1], timeout=5).read().decode())")
    url = "http://127.0.0.1:%d/healthz" % args.receiver_port
    out = cluster.exec_out("deploy/%s" % args.receiver,
                           ["python3", "-c", code, url], "receiver /healthz")
    return _loads(out, "receiver /healthz", cluster.warnings)


def redis_value(out: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
    """(value, error) for one redis-cli reply. `NOAUTH` is an expected answer, not a crash.

    The cache is password-protected and this tool deliberately holds no credential, so an
    unauthenticated probe is recorded as a limitation. The authoritative cache evidence is the
    receiver's own ledger (`cache_successes` / `cache_failures`), which needs no Redis access.
    """
    if out is None:
        return None, "unreachable"
    text = out.strip()
    if not text:
        return None, "empty reply"
    if re.match(r"^(?:NOAUTH|NOPERM|ERR|WRONGPASS|\(error\))", text, re.IGNORECASE):
        return None, redact_text(text.splitlines()[0])[:120]
    value = _as_int(text.split()[-1])
    return value, None if value is not None else "unparsable reply"


def read_cache_evidence(cluster: Cluster, args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    """DBSIZE plus any requested stream lengths. Never enumerates keys on a live cache."""
    if not args.redis_workload:
        return None
    redis = Cluster(namespace=args.redis_namespace, timeout=cluster.timeout,
                    runner=cluster.runner, warnings=cluster.warnings)
    target = args.redis_workload if "/" in args.redis_workload else "deploy/%s" % args.redis_workload
    out = redis.exec_out(target, ["redis-cli", "DBSIZE"], "redis DBSIZE")
    dbsize, error = redis_value(out)
    if out is None:
        return {"reachable": False, "error": error}
    evidence: Dict[str, Any] = {"reachable": True, "authenticated": error is None,
                                "dbsize": dbsize, "error": error}
    streams: Dict[str, Any] = {}
    for key in args.cache_stream or []:
        value, key_error = redis_value(
            redis.exec_out(target, ["redis-cli", "XLEN", key], "redis XLEN %s" % key))
        streams[key] = value if key_error is None else None
    evidence["streams"] = streams
    return evidence


# ---------------------------------------------------------------- rendering and writing

def _fmt_bytes(value: Optional[int]) -> str:
    if value is None:
        return "?"
    mb = value / 1048576.0
    return "%.1f MB" % mb if mb >= 1 else "%d B" % value


def format_snapshot(snapshot: Mapping[str, Any],
                    grading: Optional[Mapping[str, Any]] = None) -> str:
    deploy = snapshot.get("deployment") or {}
    outbox = snapshot.get("outbox") or {}
    coverage = snapshot.get("coverage") or {}
    pvc = snapshot.get("pvc") or {}
    pod = snapshot.get("pod") or {}
    cm = snapshot.get("configmap") or {}
    lines = [
        "# hear-mqtt-bridge durable soak evidence -- %s" % snapshot.get("milestone"),
        "",
        "captured_at %s (UTC), namespace %s, read-only"
        % (snapshot.get("captured_at"), snapshot.get("namespace")),
        "",
        "## deployment",
        "generation %s/%s  image %s  strategy %s  replicas %s ready %s"
        % (deploy.get("generation"), deploy.get("observed_generation"), deploy.get("image"),
           deploy.get("strategy"), deploy.get("replicas"), deploy.get("ready_replicas")),
        "durable env %s" % json.dumps(deploy.get("durable_env") or {}, sort_keys=True),
        "network env %s (drift: %s)"
        % (json.dumps(deploy.get("network_env") or {}, sort_keys=True),
           deploy.get("network_drift") or "none"),
        "configmap %s checksum %s" % (cm.get("name"), cm.get("checksum_sha256")),
        "pvc %s %s %s %s" % (pvc.get("name"), pvc.get("phase"), pvc.get("capacity"),
                             pvc.get("storage_class")),
        "pod %s %s ready=%s restarts=%s"
        % (pod.get("name"), pod.get("phase"), pod.get("ready"), pod.get("restarts")),
        "",
        "## outbox",
        "records %s  pending %s  succeeded %s  failed %s"
        % (outbox.get("records"), outbox.get("pending"), outbox.get("succeeded"),
           outbox.get("failed")),
        "window %s .. %s" % (outbox.get("oldest_record_at"), outbox.get("newest_record_at")),
        "db %s  journal %s  state dir %s"
        % (_fmt_bytes(outbox.get("db_bytes")), outbox.get("journal_mode"),
           _fmt_bytes((snapshot.get("state_dir") or {}).get("total_bytes"))),
        "by path %s" % json.dumps(outbox.get("by_path") or {}, sort_keys=True),
        "",
        "## node coverage",
        "present %s" % (", ".join(coverage.get("present") or []) or "none"),
        "missing %s" % (", ".join(coverage.get("missing") or []) or "none"),
        "counts %s" % json.dumps(coverage.get("counts") or {}, sort_keys=True),
        "",
        "## receiver and cache",
        "receiver %s" % json.dumps(snapshot.get("receiver") or {}, sort_keys=True),
        "cache %s" % json.dumps(snapshot.get("cache") or {}, sort_keys=True),
        "",
        "## log evidence",
        "counts %s" % json.dumps((snapshot.get("logs") or {}).get("counts") or {}, sort_keys=True),
    ]
    if grading:
        lines += ["", "## %s criteria -- %s" % (grading.get("milestone"), grading.get("verdict"))]
        for check in grading.get("checks") or []:
            lines.append("%-5s %-26s %s" % (check.get("verdict"), check.get("name"),
                                            check.get("detail")))
    warnings = snapshot.get("warnings") or []
    if warnings:
        lines += ["", "## warnings"] + ["- %s" % w for w in warnings]
    return "\n".join(lines) + "\n"


def write_snapshot(out_dir: str, snapshot: Mapping[str, Any],
                   grading: Optional[Mapping[str, Any]] = None) -> str:
    """Writes `<out_dir>/<milestone>-<UTC stamp>/{snapshot.json,report.md}`; never overwrites."""
    stamp = utc_stamp(_parse_ts(snapshot.get("captured_at")))
    label = str(snapshot.get("milestone") or "T0").replace("+", "").replace("/", "-")
    target = os.path.join(os.path.expanduser(out_dir), "%s-%s" % (label, stamp))
    os.makedirs(target, exist_ok=True)
    payload = dict(snapshot)
    if grading:
        payload["grading"] = grading
    with open(os.path.join(target, "snapshot.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")
    with open(os.path.join(target, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(format_snapshot(snapshot, grading))
    return target


def load_baseline(path: Optional[str], warnings: WarningSink) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    resolved = os.path.expanduser(path)
    if os.path.isdir(resolved):
        resolved = os.path.join(resolved, "snapshot.json")
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        warnings.add("could not read baseline %s: %s" % (path, exc))
        return None
    if not isinstance(data, dict):
        warnings.add("baseline %s is not an object" % path)
        return None
    return data


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Read-only dated soak evidence for the hear-mqtt-bridge durable outbox.")
    ap.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    ap.add_argument("--deployment", default=DEFAULT_DEPLOYMENT)
    ap.add_argument("--container", default="bridge")
    ap.add_argument("--configmap", default=DEFAULT_CONFIGMAP)
    ap.add_argument("--pvc", default=DEFAULT_PVC)
    ap.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    ap.add_argument("--receiver", default=DEFAULT_RECEIVER,
                    help="heartbeat receiver deployment to read /healthz from ('' to skip)")
    ap.add_argument("--receiver-port", type=int, default=DEFAULT_RECEIVER_PORT)
    ap.add_argument("--redis-namespace", default=DEFAULT_REDIS_NAMESPACE)
    ap.add_argument("--redis-workload", default=DEFAULT_REDIS_WORKLOAD,
                    help="Redis workload for cache evidence, TYPE/NAME ('' to skip)")
    ap.add_argument("--cache-stream", action="append", default=[],
                    help="Redis stream key to XLEN; repeatable")
    ap.add_argument("--milestone", choices=MILESTONES, default="T0")
    ap.add_argument("--baseline", help="T0 snapshot.json (or its directory) to compare against")
    ap.add_argument("--expected-nodes", default=",".join(EXPECTED_NODES),
                    help="comma-separated device ids that must appear in the ledger")
    ap.add_argument("--records-per-day", type=int, default=DEFAULT_RECORDS_PER_DAY)
    ap.add_argument("--growth-tolerance", type=float, default=DEFAULT_GROWTH_TOLERANCE)
    ap.add_argument("--log-tail", type=int, default=DEFAULT_LOG_TAIL)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--no-write", action="store_true", help="print only; write no snapshot dir")
    ap.add_argument("--format", choices=("md", "json"), default="md")
    ap.add_argument("--require-pass", action="store_true",
                    help="exit 2 when any criterion fails (default reports only)")
    ap.add_argument("--from-snapshot",
                    help="grade an existing snapshot.json instead of reading the cluster")
    return ap


def main(argv: Optional[Sequence[str]] = None, runner: KubectlRunner = run_kubectl) -> int:
    args = build_parser().parse_args(list(argv or []))
    args.expected_nodes = [n.strip() for n in str(args.expected_nodes).split(",") if n.strip()]
    if not args.receiver:
        args.receiver = ""
    warnings = WarningSink()

    if args.from_snapshot:
        snapshot = load_baseline(args.from_snapshot, warnings)
        if snapshot is None:
            print("\n".join("warning: %s" % w for w in warnings.items), file=sys.stderr)
            return 1
        snapshot = dict(snapshot)
        snapshot["milestone"] = args.milestone
    else:
        cluster = Cluster(namespace=args.namespace, timeout=args.timeout, runner=runner,
                          warnings=warnings)
        snapshot = collect(cluster, args)

    baseline = load_baseline(args.baseline, warnings)
    grading = evaluate(snapshot, baseline, args.milestone,
                       expected_nodes=args.expected_nodes,
                       records_per_day=args.records_per_day,
                       tolerance=args.growth_tolerance)
    snapshot["warnings"] = list(warnings.items)

    target = None
    if not args.no_write and not args.from_snapshot:
        try:
            target = write_snapshot(args.out_dir, snapshot, grading)
        except OSError as exc:
            warnings.add("could not write snapshot under %s: %s" % (args.out_dir, exc))

    if args.format == "json":
        print(json.dumps({"snapshot": snapshot, "grading": grading,
                          "written_to": target}, indent=2, sort_keys=True, default=str))
    else:
        print(format_snapshot(snapshot, grading), end="")
        if target:
            print("\nwritten to %s" % target)
    for warning in warnings.items:
        print("warning: %s" % warning, file=sys.stderr)
    if args.require_pass and grading["verdict"] != "pass":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
