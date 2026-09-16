#!/usr/bin/env python3
"""Offline preparation and grading for the `hear-mqtt-bridge` durable-outbox failure drill.

    python3 tools/bridge_drill_evidence.py gate     --bundle <bundle.json>
    python3 tools/bridge_drill_evidence.py plan     --bundle <bundle.json>
    python3 tools/bridge_drill_evidence.py analyze  --bundle <bundle.json>
    python3 tools/bridge_drill_evidence.py receipt  --bundle <bundle.json> --receipt RECEIPT.md

`docs/durable-outbox-failure-drill-runbook.md` is the procedure; this module is the part of it
that can be checked *before* anybody touches the cluster, and re-checked afterwards against the
evidence the operator collected. The drill it describes has **not** been executed.

Why this exists as code instead of a checklist:

* the abort boundary has to be an exact number (15 min, 3 restarts, 500 MiB, 256-row backlog),
  not "use judgement at the keyboard"; `evaluate_abort` is that number;
* record conservation is the whole point of the drill, and comparing a pre-fault ledger backup to
  the post-recovery ledger by eye is how a lost row gets missed;
* the receipt is the deliverable, so an incomplete receipt should fail here, not in review;
* the fault must stay *refusal shaped* and *bridge scoped* -- a stall, a server-global pause or a
  Redis restart would hit the other nine tenants of `audit-redis-0`. `check_command_plan` refuses
  those command shapes from the plan text before the window opens.

Three hard rules, enforced by the code and asserted by `tests/test_bridge_drill_evidence.py`:

1. **This module is offline.** It imports no `subprocess`, no `socket`, no HTTP client, and issues
   no kubectl, redis-cli, mosquitto or SQL call. It only reads local JSON/Markdown the operator
   already captured. Running it can neither start nor extend a fault.
2. **It never holds or prints a credential**, and it blunts high-precision decimals the same way
   `tools/bridge_soak_evidence.py` does, because drill receipts get pasted into issues.
3. **It grades conservatively.** Missing evidence is `unknown`, never `pass`; the execution gate
   only opens when every blocking item is an explicit `pass`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SCHEMA = "dama-hear/bridge-drill-evidence/v1"

# ---------------------------------------------------------------- thresholds (the abort boundary)

# Everything here is a number the operator can be held to. Sourced from the runbook's measured
# baseline: 36 records/min ingest, HEAR_DURABLE_REPLAY_LIMIT=256, 30 s liveness TTL, 5Gi PVC.
FAULT_TARGET_S = 600.0          # 10 min: 360 records > the 256 replay limit
FAULT_SOFT_BOUND_S = 720.0      # 12 min: operator warning, start closing the window
FAULT_HARD_ABORT_S = 900.0      # 15 min: abort, no discussion, no extension
RESTART_BUDGET = 3              # fault-in, mid-fault, fault-out
INGEST_RECORDS_PER_MIN = 36.0
INGEST_TOLERANCE = 0.10         # +/-10% of the ingest rate before a discrepancy is a finding
PENDING_STALL_FRACTION = 0.5    # pending growing < 50% of ingest while refused == records dropped
MIN_CHECKPOINT_GAP_S = 60.0     # shorter gaps are too noisy to judge a stall on
REPLAY_LIMIT = 256              # HEAR_DURABLE_REPLAY_LIMIT; backlog must exceed it
REPLAY_INTERVAL_S = 5.0
DRAIN_DEADLINE_S = 300.0        # pending must reach 0 within 5 min with no operator action
STATE_FREE_FLOOR_BYTES = 500 * 1024 * 1024
STATE_USED_PCT_MAX = 90.0
LIVENESS_TTL_S = 30             # dama:hear:{node} TTL
RECORD_BYTES = 1536             # ~1.5 KB/record on disk
RESTART_GAP_RECORDS_MAX = 60    # clean_session=True: a 15-30 s gap may lose this many records
BACKUP_REQUIRED_ARTIFACTS: Tuple[str, ...] = (
    "deploy-hear-mqtt-bridge.pre.yaml",
    "cm-hear-mqtt-bridge-code.pre.yaml",
    "pvc.pre.yaml",
    "rollout-history.pre.txt",
    "resourceversion.pre.txt",
    "mqtt-bridge.pre.sqlite3",
    "SHA256SUMS",
)
REQUIRED_CHECKPOINTS: Tuple[str, ...] = (
    "00-precheck", "10-fault-start", "12-fault-t5", "13-midfault-restart",
    "14-fault-t9", "20-recovery-t0", "22-recovery-t5m", "40-final",
)
PHASES: Tuple[str, ...] = ("pre", "fault", "recovery", "post")
NEGATIVE_CASES: Tuple[str, ...] = ("N1", "N2", "N3", "N4", "N5", "N6", "N7")
# The four negatives that certify the audited defects D1/D2/D3 and the replay guard.
BLOCKING_NEGATIVES: Tuple[str, ...] = ("N1", "N2", "N3", "N4")
RECEIPT_REQUIRED_FIELDS: Tuple[str, ...] = (
    "gate commit", "configmap sha256", "window start", "window end", "fault in", "fault out",
    "records", "pending", "failed", "restarts", "mqtt loss", "drain", "xlen delta",
    "node ttl", "negative cases", "abort triggers",
)
RECEIPT_SIGNOFF = re.compile(
    r"^phase2-durable-failure-drill:\s*(PASS|FAIL)\s+(\S.*)$", re.IGNORECASE | re.MULTILINE)

# Mirrors tools/bridge_soak_evidence.py: a >=4-place decimal is how a coordinate travels.
PRECISION = re.compile(r"(?<![\w.])(-?\d{1,3})[.,](\d{4,})(?!\d)")
PRECISION_MASK = r"\1.<precision-redacted>"
SECRET_NAME = re.compile(r"(?:PASS|PASSWORD|SECRET|TOKEN|KEY|CRED|AUTH)", re.IGNORECASE)
REDACTED = "<redacted>"

# ---------------------------------------------------------------- refused command shapes

# Each entry: (id, pattern, why). These are the §6.2 rejected alternatives plus the two apply
# mistakes, expressed so a planned command sequence can be screened before the window opens.
FORBIDDEN_COMMANDS: Tuple[Tuple[str, "re.Pattern[str]", str], ...] = (
    ("redis_scale_down",
     re.compile(r"\bscale\b[^\n]*\b(?:sts|statefulset)[/ ]+audit-redis\b", re.IGNORECASE),
     "scaling audit-redis takes Redis away from all ten tenants to test one workload"),
    ("redis_pod_delete",
     re.compile(r"\bdelete\b[^\n]*\b(?:pod[/ ]+)?audit-redis-0\b", re.IGNORECASE),
     "deleting audit-redis-0 is a fleet-wide outage and a 43k-key cold start"),
    ("redis_flush",
     re.compile(r"\bflush(?:all|db)\b", re.IGNORECASE),
     "FLUSHDB/FLUSHALL destroys the frozen dama:hear:* contract and nine other tenants' data"),
    ("redis_pause",
     re.compile(r"\bclient\s+pause\b|\bdebug\s+sleep\b", re.IGNORECASE),
     "server-global stall; the bridge has socket_timeout=None and would block, not fail"),
    ("redis_shutdown",
     re.compile(r"\bredis-cli\b[^\n]*\b(?:shutdown|config\s+set|bgrewriteaof|replicaof|failover)\b",
                re.IGNORECASE),
     "mutates the shared Redis server rather than the bridge's own connection"),
    ("redis_rename_keys",
     re.compile(r"\brename(?:nx)?\s+dama:hear:", re.IGNORECASE),
     "renaming contract keys breaks the frozen dama:hear:* contract for other readers"),
    ("host_netfilter",
     re.compile(r"\b(?:iptables|nft|tc\s+qdisc|netem)\b", re.IGNORECASE),
     "the bridge is hostNetwork: a host rule also hits hear-heartbeat and races kube-proxy"),
    ("network_policy",
     re.compile(r"\bnetworkpolic(?:y|ies)\b", re.IGNORECASE),
     "no effect on a hostNetwork pod, and it edits cluster-wide policy for nothing"),
    ("pvc_destruction",
     re.compile(r"\bdelete\b[^\n]*\bpvc\b|\brm\b[^\n]*(?:mqtt-bridge\.sqlite3|/state\b)",
                re.IGNORECASE),
     "the pending records are the evidence; destroying them is not a durability test"),
    ("mtls_manifest_apply",
     re.compile(r"\bapply\b[^\n]*hear-mqtt-bridge\.yaml(?!\S)", re.IGNORECASE),
     "that manifest bundles the unprovisioned mTLS 8883 cutover and would move live traffic"),
    ("durable_store_disable",
     re.compile(r"HEAR_DURABLE_STORE\s*[=:]\s*[\"']?none", re.IGNORECASE),
     "removes the system under test"),
    ("replica_change",
     re.compile(r"\bscale\b[^\n]*\bdeploy(?:ment)?[/ ]+hear-mqtt-bridge\b|"
                r"--replicas[= ]*(?!1\b)\d+", re.IGNORECASE),
     "a second writer on an RWO PVC, or no writer at all"),
    ("heartbeat_in_scope",
     re.compile(r"\b(?:patch|scale|rollout|delete)\b[^\n]*\bhear-heartbeat\b", re.IGNORECASE),
     "hear-heartbeat is out of scope and is drilled separately, never in the same window"),
)
# Commands the drill legitimately needs. Anything else in a plan is reported as unclassified.
EXPECTED_COMMANDS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("fault_in", re.compile(r"patch\s+deploy/hear-mqtt-bridge[^\n]*\"6390\"")),
    ("fault_out", re.compile(r"patch\s+deploy/hear-mqtt-bridge[^\n]*\"6379\"")),
    ("rollout_status", re.compile(r"rollout\s+status\s+deploy/hear-mqtt-bridge")),
    ("snapshot", re.compile(r"snapshot\.sh\s+\S+")),
)


class DrillEvidenceError(RuntimeError):
    """The bundle could not be read or is not a drill evidence bundle."""


# ---------------------------------------------------------------- redaction


def redact_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return PRECISION.sub(PRECISION_MASK, value)


def redact_tree(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: (REDACTED if (isinstance(k, str) and SECRET_NAME.search(k)
                                 and isinstance(obj[k], str) and obj[k])
                    else redact_tree(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_tree(v) for v in obj]
    return redact_text(obj)


# ---------------------------------------------------------------- findings


@dataclass
class Finding:
    name: str
    verdict: str  # "pass" | "fail" | "unknown"
    detail: str
    blocking: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "verdict": self.verdict,
                "detail": redact_text(self.detail), "blocking": self.blocking}


@dataclass
class Report:
    kind: str
    findings: List[Finding] = field(default_factory=list)

    def add(self, name: str, ok: Optional[bool], detail: str, blocking: bool = True) -> None:
        verdict = "unknown" if ok is None else ("pass" if ok else "fail")
        self.findings.append(Finding(name, verdict, detail, blocking))

    @property
    def failed(self) -> List[str]:
        return [f.name for f in self.findings if f.verdict == "fail"]

    @property
    def unknown(self) -> List[str]:
        return [f.name for f in self.findings if f.verdict == "unknown"]

    @property
    def blocking_open(self) -> List[str]:
        return [f.name for f in self.findings if f.blocking and f.verdict != "pass"]

    @property
    def verdict(self) -> str:
        if any(f.verdict == "fail" for f in self.findings):
            return "fail"
        return "incomplete" if self.unknown else "pass"

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "verdict": self.verdict,
                "failed": self.failed, "unknown": self.unknown,
                "blocking_open": self.blocking_open,
                "findings": [f.as_dict() for f in self.findings]}


# ---------------------------------------------------------------- bundle access


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def load_bundle(path: str) -> Dict[str, Any]:
    """Reads a drill evidence bundle from a file or a directory containing `bundle.json`."""
    resolved = os.path.expanduser(path)
    if os.path.isdir(resolved):
        resolved = os.path.join(resolved, "bundle.json")
    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise DrillEvidenceError("could not read drill bundle %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise DrillEvidenceError("drill bundle %s is not an object" % path)
    schema = data.get("schema")
    if schema not in (None, SCHEMA):
        raise DrillEvidenceError("unexpected bundle schema %r (want %r)" % (schema, SCHEMA))
    return data


def checkpoints(bundle: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Checkpoints in captured order; entries without a usable timestamp keep their listed order."""
    items = [dict(c) for c in (bundle.get("checkpoints") or []) if isinstance(c, Mapping)]
    stamped = [(i, _parse_ts(c.get("at"))) for i, c in enumerate(items)]
    order = sorted(range(len(items)),
                   key=lambda i: (stamped[i][1] is None, stamped[i][1] or dt.datetime.min.replace(
                       tzinfo=dt.timezone.utc), i))
    return [items[i] for i in order]


def by_phase(bundle: Mapping[str, Any], phase: str) -> List[Dict[str, Any]]:
    return [c for c in checkpoints(bundle) if c.get("phase") == phase]


def checkpoint(bundle: Mapping[str, Any], label: str) -> Optional[Dict[str, Any]]:
    for c in checkpoints(bundle):
        if c.get("label") == label:
            return c
    return None


def fault_seconds(bundle: Mapping[str, Any], now: Optional[dt.datetime] = None) -> Optional[float]:
    """Fault duration in seconds: FAULT IN to FAULT OUT, or to `now` if still active."""
    fault = bundle.get("fault") or {}
    start = _parse_ts(fault.get("started_at"))
    if start is None:
        return None
    end = _parse_ts(fault.get("ended_at")) or now
    if end is None:
        return None
    return (end - start).total_seconds()


# ---------------------------------------------------------------- abort boundary


@dataclass
class AbortTrigger:
    id: str
    tripped: Optional[bool]
    detail: str
    at: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "tripped": self.tripped, "detail": redact_text(self.detail),
                "at": self.at}


def evaluate_abort(bundle: Mapping[str, Any],
                   now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """The exact abort boundary of §8, evaluated against captured checkpoints.

    `tripped=None` means the evidence needed to judge that trigger is absent -- which is itself a
    reason not to be mid-fault, so `ready` requires every trigger to be an explicit False.
    """
    triggers: List[AbortTrigger] = []
    samples = checkpoints(bundle)
    fault_samples = [c for c in samples if c.get("phase") == "fault"]

    elapsed = fault_seconds(bundle, now)
    if elapsed is None:
        triggers.append(AbortTrigger("T1_fault_over_hard_bound", None,
                                     "fault window timestamps missing"))
    else:
        triggers.append(AbortTrigger(
            "T1_fault_over_hard_bound", elapsed > FAULT_HARD_ABORT_S,
            "fault elapsed %.0fs (target %.0fs, soft %.0fs, hard abort %.0fs)"
            % (elapsed, FAULT_TARGET_S, FAULT_SOFT_BOUND_S, FAULT_HARD_ABORT_S)))

    # T2 pending must keep growing at roughly the ingest rate while Redis is refused.
    stall = _pending_stall(fault_samples)
    triggers.append(stall)

    # T3 any SQLite integrity signal.
    triggers.append(_log_trigger(
        samples, "T3_sqlite_integrity",
        ("sqlite_locked", "disk_io_error", "malformed_database", "database_corruption"),
        "SQLite integrity message in the bridge logs"))

    # T4 records/max_id must never decrease.
    triggers.append(_monotonic_trigger(samples))

    # T5 disk floor.
    triggers.append(_disk_trigger(samples))

    # T6 restart budget / crash loop.
    triggers.append(_restart_trigger(samples))

    # T7 collateral on the shared Redis.
    triggers.append(_collateral_trigger(samples))

    # T8 MQTT ingest must continue while only Redis is broken.
    triggers.append(_ingest_trigger(fault_samples))

    # T9/T10 operator-declared conditions; recorded so the receipt cannot omit them.
    for tid, key, text in (("T9_node_pressure", "node_pressure",
                            "node/kubelet/disk pressure on the single-node cluster"),
                           ("T10_operator_lost_control", "operator_control_lost",
                            "operator lost the second shell, kubeconfig or the window")):
        declared = (bundle.get("operator") or {}).get(key)
        triggers.append(AbortTrigger(tid, None if declared is None else bool(declared),
                                     "%s: %s" % (text, "declared" if declared else
                                                 ("not declared" if declared is not None
                                                  else "not recorded"))))

    tripped = [t.id for t in triggers if t.tripped]
    unknown = [t.id for t in triggers if t.tripped is None]
    return {
        "fault_elapsed_s": None if elapsed is None else round(elapsed, 1),
        "hard_abort_s": FAULT_HARD_ABORT_S,
        "soft_bound_s": FAULT_SOFT_BOUND_S,
        "restart_budget": RESTART_BUDGET,
        "triggers": [t.as_dict() for t in triggers],
        "tripped": tripped,
        "unknown": unknown,
        # "abort now" is any tripped trigger; "safe to continue" needs all of them provably clear.
        "abort": bool(tripped),
        "continue_allowed": not tripped and not unknown,
    }


def _pending_stall(fault_samples: Sequence[Mapping[str, Any]]) -> AbortTrigger:
    usable: List[Tuple[dt.datetime, int, str]] = []
    for c in fault_samples:
        at, pending = _parse_ts(c.get("at")), _as_int(c.get("pending"))
        if at is not None and pending is not None:
            usable.append((at, pending, str(c.get("label") or "")))
    if len(usable) < 2:
        return AbortTrigger("T2_pending_stalled", None,
                            "fewer than two fault-phase pending samples")
    worst: Optional[AbortTrigger] = None
    judged = False
    for (t0, p0, _), (t1, p1, label) in zip(usable, usable[1:]):
        gap = (t1 - t0).total_seconds()
        if gap < MIN_CHECKPOINT_GAP_S:
            continue
        judged = True
        expected = INGEST_RECORDS_PER_MIN * gap / 60.0
        floor = expected * PENDING_STALL_FRACTION
        if (p1 - p0) < floor:
            worst = AbortTrigger(
                "T2_pending_stalled", True,
                "pending grew %+d in %.0fs; at least %.0f expected (records are being dropped, "
                "not queued)" % (p1 - p0, gap, floor), label)
            break
    if worst is not None:
        return worst
    if not judged:
        return AbortTrigger("T2_pending_stalled", None,
                            "no two fault samples at least %.0fs apart" % MIN_CHECKPOINT_GAP_S)
    return AbortTrigger("T2_pending_stalled", False,
                        "pending grew at >= %.0f%% of the %.0f rec/min ingest at every step"
                        % (PENDING_STALL_FRACTION * 100, INGEST_RECORDS_PER_MIN))


def _log_trigger(samples: Sequence[Mapping[str, Any]], tid: str, keys: Sequence[str],
                 what: str) -> AbortTrigger:
    seen = False
    for c in samples:
        logs = c.get("logs")
        if not isinstance(logs, Mapping):
            continue
        seen = True
        for key in keys:
            count = _as_int(logs.get(key))
            if count:
                return AbortTrigger(tid, True, "%s: %s=%d" % (what, key, count),
                                    str(c.get("label") or ""))
    if not seen:
        return AbortTrigger(tid, None, "no checkpoint carries a log summary")
    return AbortTrigger(tid, False, "%s: none in any checkpoint" % what)


def _monotonic_trigger(samples: Sequence[Mapping[str, Any]]) -> AbortTrigger:
    prev_records: Optional[int] = None
    prev_max: Optional[int] = None
    judged = False
    for c in samples:
        records, max_id = _as_int(c.get("records")), _as_int(c.get("max_id"))
        label = str(c.get("label") or "")
        if records is not None and prev_records is not None:
            judged = True
            if records < prev_records:
                return AbortTrigger("T4_ledger_regressed", True,
                                    "records fell %d -> %d" % (prev_records, records), label)
        if max_id is not None and prev_max is not None:
            judged = True
            if max_id < prev_max:
                return AbortTrigger("T4_ledger_regressed", True,
                                    "max(id) fell %d -> %d" % (prev_max, max_id), label)
        prev_records = records if records is not None else prev_records
        prev_max = max_id if max_id is not None else prev_max
    if not judged:
        return AbortTrigger("T4_ledger_regressed", None, "fewer than two ledger counts")
    return AbortTrigger("T4_ledger_regressed", False, "records and max(id) never decreased")


def _disk_trigger(samples: Sequence[Mapping[str, Any]]) -> AbortTrigger:
    judged = False
    for c in samples:
        label = str(c.get("label") or "")
        free = _as_int(c.get("state_free_bytes"))
        used_pct = _as_float(c.get("state_used_pct"))
        if free is not None:
            judged = True
            if free < STATE_FREE_FLOOR_BYTES:
                return AbortTrigger("T5_state_disk_floor", True,
                                    "/state free %.0f MiB < %.0f MiB floor"
                                    % (free / 1048576.0, STATE_FREE_FLOOR_BYTES / 1048576.0),
                                    label)
        if used_pct is not None:
            judged = True
            if used_pct > STATE_USED_PCT_MAX:
                return AbortTrigger("T5_state_disk_floor", True,
                                    "/state %.1f%% used > %.0f%%" % (used_pct, STATE_USED_PCT_MAX),
                                    label)
    if not judged:
        return AbortTrigger("T5_state_disk_floor", None, "no /state capacity sample")
    return AbortTrigger("T5_state_disk_floor", False,
                        ">= %.0f MiB free and <= %.0f%% used at every checkpoint"
                        % (STATE_FREE_FLOOR_BYTES / 1048576.0, STATE_USED_PCT_MAX))


def _restart_trigger(samples: Sequence[Mapping[str, Any]]) -> AbortTrigger:
    counts = [(_as_int(c.get("restarts")), str(c.get("label") or "")) for c in samples]
    known = [(v, label) for v, label in counts if v is not None]
    for c in samples:
        logs = c.get("logs")
        if isinstance(logs, Mapping) and logs.get("crashloop"):
            return AbortTrigger("T6_restart_budget", True, "bridge pod in CrashLoopBackOff",
                                str(c.get("label") or ""))
    if not known:
        return AbortTrigger("T6_restart_budget", None, "no restart counts captured")
    base = known[0][0]
    for value, label in known:
        consumed = value - base
        if consumed > RESTART_BUDGET:
            return AbortTrigger("T6_restart_budget", True,
                                "%d restarts consumed, budget %d" % (consumed, RESTART_BUDGET),
                                label)
    return AbortTrigger("T6_restart_budget", False,
                        "%d of %d restarts consumed, no crash loop"
                        % (known[-1][0] - base, RESTART_BUDGET))


def _collateral_trigger(samples: Sequence[Mapping[str, Any]]) -> AbortTrigger:
    """Other tenants of audit-redis-0 must be untouched: no restart, no client collapse."""
    restarts: List[Tuple[int, str]] = []
    clients: List[Tuple[int, str]] = []
    degraded: List[Tuple[str, str]] = []
    for c in samples:
        label = str(c.get("label") or "")
        redis = c.get("redis")
        if not isinstance(redis, Mapping):
            continue
        value = _as_int(redis.get("audit_redis_restarts"))
        if value is not None:
            restarts.append((value, label))
        value = _as_int(redis.get("connected_clients"))
        if value is not None:
            clients.append((value, label))
        for name in (redis.get("degraded_consumers") or []):
            degraded.append((str(name), label))
    if degraded:
        return AbortTrigger("T7_redis_collateral", True,
                            "other Redis consumers degraded: %s"
                            % ", ".join(sorted({d for d, _ in degraded})), degraded[0][1])
    if restarts and len({v for v, _ in restarts}) > 1:
        return AbortTrigger("T7_redis_collateral", True,
                            "audit-redis-0 restart count moved %d -> %d"
                            % (restarts[0][0], restarts[-1][0]), restarts[-1][1])
    if clients:
        base = clients[0][0]
        for value, label in clients:
            # The bridge is one client; losing it is expected. Losing a quarter of them is not.
            if base and value < max(base - 1, base * 0.75):
                return AbortTrigger("T7_redis_collateral", True,
                                    "connected_clients collapsed %d -> %d" % (base, value), label)
    if not restarts and not clients:
        return AbortTrigger("T7_redis_collateral", None, "no Redis tenant-health samples")
    return AbortTrigger("T7_redis_collateral", False,
                        "audit-redis-0 restarts unchanged, connected_clients stable")


def _ingest_trigger(fault_samples: Sequence[Mapping[str, Any]]) -> AbortTrigger:
    usable = [(_parse_ts(c.get("at")), _as_int(c.get("records")), str(c.get("label") or ""))
              for c in fault_samples]
    usable = [(a, r, l) for a, r, l in usable if a is not None and r is not None]
    if len(usable) < 2:
        return AbortTrigger("T8_mqtt_ingest_stopped", None, "fewer than two fault record counts")
    for (t0, r0, _), (t1, r1, label) in zip(usable, usable[1:]):
        gap = (t1 - t0).total_seconds()
        if gap < MIN_CHECKPOINT_GAP_S:
            continue
        if r1 <= r0:
            return AbortTrigger("T8_mqtt_ingest_stopped", True,
                                "records flat at %d across %.0fs while only Redis is faulted"
                                % (r1, gap), label)
    return AbortTrigger("T8_mqtt_ingest_stopped", False, "records kept growing through the fault")


# ---------------------------------------------------------------- backup / conservation


def check_backup(bundle: Mapping[str, Any]) -> Report:
    """Pre-fault backup and the pre-fault ledger reference the conservation check needs."""
    report = Report("backup")
    backup = bundle.get("backup")
    if not isinstance(backup, Mapping):
        report.add("backup_captured", None, "bundle carries no backup block")
        report.add("backup_artifacts", None, "no backup block")
        report.add("backup_checksums", None, "no backup block")
        report.add("pre_fault_ledger_reference", None, "no backup block")
        report.add("destructive_backup_steps_absent", None, "no backup block")
        return report

    files = backup.get("files") if isinstance(backup.get("files"), Mapping) else {}
    report.add("backup_captured", bool(files), "%d artifact(s) recorded" % len(files))
    missing = [name for name in BACKUP_REQUIRED_ARTIFACTS if name not in files]
    report.add("backup_artifacts", not missing,
               "missing %s" % missing if missing else "all %d required artifacts present"
               % len(BACKUP_REQUIRED_ARTIFACTS))
    unchecksummed = sorted(k for k, v in files.items()
                           if k != "SHA256SUMS" and not re.fullmatch(r"[0-9a-f]{64}", str(v or "")))
    report.add("backup_checksums", not unchecksummed if files else None,
               "unchecksummed: %s" % unchecksummed if unchecksummed else "every artifact sha256'd")

    ledger = backup.get("ledger") if isinstance(backup.get("ledger"), Mapping) else {}
    records, max_id = _as_int(ledger.get("records")), _as_int(ledger.get("max_id"))
    report.add("pre_fault_ledger_reference", None if records is None or max_id is None else True,
               "pre-fault ledger records=%s max_id=%s pending=%s failed=%s"
               % (records, max_id, ledger.get("pending"), ledger.get("failed")))

    # The backup must be a copy, never a move: no drop/vacuum/delete in the recorded method.
    method = str(backup.get("method") or "")
    unsafe = [cid for cid, pattern, _ in FORBIDDEN_COMMANDS if pattern.search(method)]
    unsafe += ["sqlite_write"] if re.search(
        r"\b(?:drop|delete|vacuum|truncate)\b", method, re.IGNORECASE) else []
    report.add("destructive_backup_steps_absent", not unsafe if method else None,
               "refused: %s" % unsafe if unsafe else
               ("backup method is copy-only" if method else "backup method not recorded"))
    return report


def check_record_conservation(bundle: Mapping[str, Any]) -> Report:
    """No durable write may be lost: counts monotonic, and the arithmetic must close."""
    report = Report("record_conservation")
    samples = checkpoints(bundle)
    pre = checkpoint(bundle, "00-precheck") or (by_phase(bundle, "pre") or [None])[0]
    final = checkpoint(bundle, "40-final") or (by_phase(bundle, "post") or [None])[-1]

    monotonic = _monotonic_trigger(samples)
    report.add("ledger_monotonic",
               None if monotonic.tripped is None else not monotonic.tripped, monotonic.detail)

    backup_ledger = (bundle.get("backup") or {}).get("ledger")
    base_records = _as_int((backup_ledger or {}).get("records")) if isinstance(
        backup_ledger, Mapping) else None
    final_records = _as_int((final or {}).get("records"))
    if base_records is None or final_records is None:
        report.add("no_rows_lost_vs_backup", None,
                   "need both the pre-fault backup ledger count and a final checkpoint")
    else:
        report.add("no_rows_lost_vs_backup", final_records >= base_records,
                   "final %d vs pre-fault backup %d (%+d)"
                   % (final_records, base_records, final_records - base_records))

    # Ingest arithmetic across the fault: records(out) - records(in) ~= rate * minutes, less the
    # MQTT lost to each restart gap (clean_session=True).
    fault_in = checkpoint(bundle, "10-fault-start") or pre
    fault_out = checkpoint(bundle, "20-recovery-t0")
    elapsed = fault_seconds(bundle)
    grew = None
    if fault_in is not None and fault_out is not None:
        a, b = _as_int(fault_in.get("records")), _as_int(fault_out.get("records"))
        grew = None if a is None or b is None else b - a
    if grew is None or not elapsed:
        report.add("ingest_arithmetic_closes", None, "fault-window record deltas unavailable")
    else:
        expected = INGEST_RECORDS_PER_MIN * elapsed / 60.0
        restarts_in_fault = _as_int((bundle.get("fault") or {}).get("restarts_during")) or 0
        gap_allowance = restarts_in_fault * RESTART_GAP_RECORDS_MAX
        low = expected * (1 - INGEST_TOLERANCE) - gap_allowance
        high = expected * (1 + INGEST_TOLERANCE)
        report.add("ingest_arithmetic_closes", low <= grew <= high,
                   "+%d records in %.0fs; expected %.0f +/-%.0f%% less %d restart-gap records"
                   % (grew, elapsed, expected, INGEST_TOLERANCE * 100, gap_allowance))

    mid_index = next((i for i, c in enumerate(samples)
                      if c.get("label") == "13-midfault-restart"), None)
    mid = samples[mid_index] if mid_index is not None else None
    before_mid = None
    for c in samples[:mid_index or 0]:
        if _as_int(c.get("records")) is not None:
            before_mid = c
    if mid is None or before_mid is None:
        report.add("restart_lost_no_rows", None, "no mid-fault restart checkpoint pair")
    else:
        a, b = _as_int(before_mid.get("records")), _as_int(mid.get("records"))
        am, bm = _as_int(before_mid.get("max_id")), _as_int(mid.get("max_id"))
        ok = (a is not None and b is not None and b >= a
              and (am is None or bm is None or bm >= am))
        report.add("restart_lost_no_rows", ok,
                   "across the mid-fault restart records %s -> %s, max_id %s -> %s"
                   % (a, b, am, bm))

    dupes = [(_as_int(c.get("duplicate_record_uids")), str(c.get("label") or "")) for c in samples]
    known = [(v, l) for v, l in dupes if v is not None]
    if not known:
        report.add("no_duplicate_record_uids", None, "duplicate_record_uids never sampled")
    else:
        bad = [(v, l) for v, l in known if v]
        report.add("no_duplicate_record_uids", not bad,
                   "duplicates at %s" % [l for _, l in bad] if bad else "zero at every checkpoint")
    return report


# ---------------------------------------------------------------- drill-shape evidence


def check_refusal_evidence(bundle: Mapping[str, Any]) -> Report:
    """The fault has to be connection-*refused* and scoped to the bridge, not a stall."""
    report = Report("refusal")
    fault = bundle.get("fault") if isinstance(bundle.get("fault"), Mapping) else {}
    kind = str(fault.get("kind") or "")
    report.add("fault_is_connection_refused", None if not kind else kind == "connection-refused",
               "fault kind %r" % (kind or "unrecorded"))
    port = _as_int(fault.get("redis_port"))
    report.add("fault_scoped_to_bridge_env",
               None if port is None else (port != 6379 and bool(fault.get("closed_port_verified"))),
               "bridge REDIS_PORT repointed to %s, closed-port verification %s"
               % (port, "recorded" if fault.get("closed_port_verified") else "missing"))

    fault_samples = by_phase(bundle, "fault")
    refused = [(_as_int((c.get("logs") or {}).get("connection_refused")), str(c.get("label") or ""))
               for c in fault_samples if isinstance(c.get("logs"), Mapping)]
    known = [(v, l) for v, l in refused if v is not None]
    report.add("refused_not_hung", None if not known else any(v for v, _ in known),
               "connection-refused log lines during the fault: %s"
               % ([v for v, _ in known] or "none recorded"))

    failed = [_as_int(c.get("failed")) for c in fault_samples]
    failed = [v for v in failed if v is not None]
    if len(failed) < 2:
        report.add("failed_attempts_grew", None, "fewer than two fault-phase failed counts")
    else:
        report.add("failed_attempts_grew", failed[-1] > failed[0],
                   "cache_attempts failed %d -> %d during the fault" % (failed[0], failed[-1]))

    oldest = {str(c.get("oldest_pending_at") or "") for c in fault_samples
              if c.get("oldest_pending_at")}
    start = fault.get("started_at")
    if not oldest or not start:
        report.add("oldest_pending_pinned", None, "oldest_pending_at not sampled through the fault")
    else:
        pinned = len(oldest) == 1
        delta = None
        first = _parse_ts(sorted(oldest)[0])
        started = _parse_ts(start)
        if first is not None and started is not None:
            delta = abs((first - started).total_seconds())
        report.add("oldest_pending_pinned", pinned and (delta is None or delta <= 120.0),
                   "oldest_pending %s, %s from FAULT IN"
                   % (sorted(oldest), "unknown offset" if delta is None else "%.0fs" % delta))
    return report


def check_cap_evidence(bundle: Mapping[str, Any]) -> Report:
    """The backlog must exceed HEAR_DURABLE_REPLAY_LIMIT and still drain unattended."""
    report = Report("cap")
    limit = _as_int((bundle.get("config") or {}).get("replay_limit")) or REPLAY_LIMIT
    pendings = [(_as_int(c.get("pending")), str(c.get("label") or ""))
                for c in by_phase(bundle, "fault")]
    known = [(v, l) for v, l in pendings if v is not None]
    if not known:
        report.add("backlog_exceeds_replay_limit", None, "no fault-phase pending sample")
    else:
        peak, label = max(known, key=lambda item: item[0])
        report.add("backlog_exceeds_replay_limit", peak > limit,
                   "peak pending %d at %s vs replay limit %d" % (peak, label, limit))

    recovery = by_phase(bundle, "recovery") + by_phase(bundle, "post")
    curve = [(_parse_ts(c.get("at")), _as_int(c.get("pending")), str(c.get("label") or ""))
             for c in recovery]
    curve = [(a, p, l) for a, p, l in curve if a is not None and p is not None]
    if len(curve) < 2:
        report.add("drain_monotonic", None, "fewer than two recovery pending samples")
        report.add("drain_within_deadline", None, "no recovery drain curve")
    else:
        rises = [l for (_, p0, _), (_, p1, l) in zip(curve, curve[1:]) if p1 > p0]
        report.add("drain_monotonic", not rises,
                   "pending rose again at %s" % rises if rises else
                   "pending fell monotonically %d -> %d" % (curve[0][1], curve[-1][1]))
        zeroed = next(((a, l) for a, p, l in curve if p == 0), None)
        if zeroed is None:
            report.add("drain_within_deadline", False,
                       "pending never reached 0 (last %d at %s)" % (curve[-1][1], curve[-1][2]))
        else:
            took = (zeroed[0] - curve[0][0]).total_seconds()
            report.add("drain_within_deadline", took <= DRAIN_DEADLINE_S,
                       "pending 0 at %s, %.0fs after FAULT OUT (deadline %.0fs)"
                       % (zeroed[1], took, DRAIN_DEADLINE_S))

    unattended = (bundle.get("recovery") or {}).get("operator_actions")
    if unattended is None:
        report.add("drain_unattended", None, "operator actions during recovery not recorded")
    else:
        report.add("drain_unattended", not unattended,
                   "operator actions during drain: %s" % (unattended or "none"))

    frozen = [_as_int(c.get("failed")) for c in recovery]
    frozen = [v for v in frozen if v is not None]
    if len(frozen) < 2:
        report.add("failed_counter_frozen", None, "fewer than two post-recovery failed counts")
    else:
        report.add("failed_counter_frozen", frozen[-1] == frozen[0],
                   "failed %d -> %d after recovery" % (frozen[0], frozen[-1]))
    return report


def check_cache_evidence(bundle: Mapping[str, Any]) -> Report:
    """The frozen `dama:hear:*` contract must come back exactly as it went in."""
    report = Report("cache")
    pre = checkpoint(bundle, "00-precheck") or (by_phase(bundle, "pre") or [None])[0]
    final = checkpoint(bundle, "40-final") or (by_phase(bundle, "post") or [None])[-1]
    pre_redis = (pre or {}).get("redis") if isinstance((pre or {}).get("redis"), Mapping) else None
    post_redis = ((final or {}).get("redis")
                  if isinstance((final or {}).get("redis"), Mapping) else None)
    if pre_redis is None or post_redis is None:
        report.add("live_nodes_rearmed", None, "precheck or final Redis sample missing")
        report.add("devices_membership_unchanged", None, "precheck or final Redis sample missing")
        report.add("liveness_expired_during_fault", None, "no fault-phase Redis sample")
        report.add("probe_ids_cleaned_up", None, "no final Redis sample")
        return report

    pre_ttls = pre_redis.get("ttls") if isinstance(pre_redis.get("ttls"), Mapping) else {}
    post_ttls = post_redis.get("ttls") if isinstance(post_redis.get("ttls"), Mapping) else {}
    # Success is keyed to the nodes that were live at precheck, never to a hard-coded six.
    live = sorted(n for n, ttl in pre_ttls.items() if (_as_int(ttl) or -2) > 0)
    if not live:
        report.add("live_nodes_rearmed", None, "no precheck-live node TTLs")
    else:
        bad = {n: post_ttls.get(n) for n in live
               if not (1 <= (_as_int(post_ttls.get(n)) or -2) <= LIVENESS_TTL_S)}
        report.add("live_nodes_rearmed", not bad,
                   "not re-armed within 1..%ds: %s" % (LIVENESS_TTL_S, bad) if bad else
                   "all %d precheck-live nodes re-armed within 1..%ds" % (len(live), LIVENESS_TTL_S))

    probes = set(str(p) for p in ((bundle.get("negatives") or {}).get("probe_ids") or []))
    pre_devices = set(str(d) for d in (pre_redis.get("devices") or []))
    post_devices = set(str(d) for d in (post_redis.get("devices") or []))
    added, removed = post_devices - pre_devices, pre_devices - post_devices
    report.add("devices_membership_unchanged", not (added - probes) and not removed,
               "devices added=%s removed=%s (probe ids %s)"
               % (sorted(added), sorted(removed), sorted(probes) or "none"))
    report.add("probe_ids_cleaned_up", not (added & probes),
               "probe ids still in dama:hear:devices: %s" % sorted(added & probes)
               if added & probes else "no probe id left behind")

    fault_ttls = [c.get("redis", {}).get("ttls") for c in by_phase(bundle, "fault")
                  if isinstance(c.get("redis"), Mapping)]
    fault_ttls = [t for t in fault_ttls if isinstance(t, Mapping)]
    if not fault_ttls or not live:
        report.add("liveness_expired_during_fault", None, "no fault-phase node TTL sample")
    else:
        expired = all((_as_int(fault_ttls[-1].get(n)) or 0) < 0 for n in live)
        report.add("liveness_expired_during_fault", expired,
                   "precheck-live TTLs at the end of the fault: %s"
                   % {n: fault_ttls[-1].get(n) for n in live}, blocking=False)
    return report


def check_claim_evidence(bundle: Mapping[str, Any]) -> Report:
    """Claim/idempotence: one publish per distinct record, and the PVC claim never moves."""
    report = Report("claim")
    samples = checkpoints(bundle)
    pre = checkpoint(bundle, "00-precheck") or (by_phase(bundle, "pre") or [None])[0]
    final = checkpoint(bundle, "40-final") or (by_phase(bundle, "post") or [None])[-1]

    pre_pvc = (pre or {}).get("pvc") if isinstance((pre or {}).get("pvc"), Mapping) else None
    post_pvc = (final or {}).get("pvc") if isinstance((final or {}).get("pvc"), Mapping) else None
    if pre_pvc is None or post_pvc is None:
        report.add("pvc_claim_unchanged", None, "PVC identity not sampled at both ends")
    else:
        same = all(pre_pvc.get(k) == post_pvc.get(k) for k in ("claim", "volume"))
        report.add("pvc_claim_unchanged", same and post_pvc.get("phase") == "Bound",
                   "claim %s volume %s phase %s" % (post_pvc.get("claim"), post_pvc.get("volume"),
                                                    post_pvc.get("phase")))

    dupes = [_as_int(c.get("duplicate_record_uids")) for c in samples]
    dupes = [v for v in dupes if v is not None]
    report.add("dedupe_claim_atomic", None if not dupes else not any(dupes),
               "max duplicate record_uid rows %s" % (max(dupes) if dupes else "unsampled"))

    pre_xlen = _as_int(((pre or {}).get("redis") or {}).get("xlen"))
    post_xlen = _as_int(((final or {}).get("redis") or {}).get("xlen"))
    distinct = _as_int((bundle.get("fault") or {}).get("distinct_event_records"))
    if pre_xlen is None or post_xlen is None or distinct is None:
        report.add("stream_growth_bounded", None,
                   "need xlen at both ends and fault.distinct_event_records")
    else:
        delta = post_xlen - pre_xlen
        report.add("stream_growth_bounded", delta <= distinct,
                   "dama:hear:events grew %+d for %d distinct event records" % (delta, distinct))

    results = (bundle.get("negatives") or {}).get("results")
    results = results if isinstance(results, Mapping) else {}
    missing = [n for n in BLOCKING_NEGATIVES if n not in results]
    failed = sorted(n for n, v in results.items() if str(v).lower() not in ("pass", "n/a"))
    if missing:
        report.add("negative_cases_pass", None, "not run: %s" % missing)
    else:
        report.add("negative_cases_pass", not failed,
                   "failed: %s" % failed if failed else "N1-N4 pass")
    optional_missing = [n for n in NEGATIVE_CASES if n not in results]
    report.add("all_negative_cases_recorded",
               None if not results else not optional_missing,
               "not recorded: %s" % optional_missing if optional_missing else
               "all %d negative cases recorded" % len(NEGATIVE_CASES), blocking=False)
    return report


# ---------------------------------------------------------------- command plan screening


def check_command_plan(commands: Iterable[str]) -> Report:
    """Screens the planned command sequence for blast radius, before the window opens.

    This reads text. It never runs anything -- which is the only reason it is safe to point at a
    plan that contains `kubectl patch`.
    """
    report = Report("plan")
    text_lines = [str(c) for c in commands if str(c).strip()]
    blob = "\n".join(text_lines)
    refused: List[str] = []
    for cid, pattern, why in FORBIDDEN_COMMANDS:
        hits = [ln for ln in text_lines if pattern.search(ln)]
        if hits:
            refused.append(cid)
            report.add("refused_%s" % cid, False, "%s -- %s" % (redact_text(hits[0])[:160], why))
    if not refused:
        report.add("no_forbidden_commands", True,
                   "screened %d line(s) against %d refused shapes"
                   % (len(text_lines), len(FORBIDDEN_COMMANDS)))
    for name, pattern in EXPECTED_COMMANDS:
        report.add("plan_has_%s" % name, bool(pattern.search(blob)),
                   "%s %s" % (name, "present" if pattern.search(blob) else "absent"),
                   blocking=name in ("fault_in", "fault_out"))
    inverse = (bool(re.search(r"\"6390\"", blob)) and bool(re.search(r"\"6379\"", blob)))
    report.add("fault_is_reversible", inverse if text_lines else None,
               "fault-in and its exact inverse both present" if inverse else
               "the plan does not contain both the 6390 patch and the 6379 patch back")
    return report


# ---------------------------------------------------------------- receipt validation


def validate_receipt(text: Optional[str], bundle: Optional[Mapping[str, Any]] = None) -> Report:
    """The receipt is the deliverable; an incomplete one fails here, not in review."""
    report = Report("receipt")
    if not text or not text.strip():
        report.add("receipt_present", False, "receipt is empty or missing")
        return report
    report.add("receipt_present", True, "%d bytes" % len(text))
    lowered = text.lower()
    missing = [f for f in RECEIPT_REQUIRED_FIELDS if f not in lowered]
    report.add("receipt_fields_complete", not missing,
               "missing fields: %s" % missing if missing else
               "all %d required fields present" % len(RECEIPT_REQUIRED_FIELDS))

    match = RECEIPT_SIGNOFF.search(text)
    report.add("receipt_signed_off", bool(match),
               "sign-off %r" % redact_text(match.group(0)) if match else
               "no `phase2-durable-failure-drill: PASS|FAIL <operator>` line")
    if match:
        report.add("receipt_verdict_is_pass", match.group(1).upper() == "PASS",
                   "operator recorded %s" % match.group(1).upper(), blocking=False)

    if bundle is not None:
        labels = [str(c.get("label") or "") for c in checkpoints(bundle)]
        absent = [l for l in REQUIRED_CHECKPOINTS if l not in labels]
        report.add("checkpoints_complete", not absent,
                   "missing checkpoints: %s" % absent if absent else
                   "all %d required checkpoints captured" % len(REQUIRED_CHECKPOINTS))
        uncited = [l for l in labels if l and l not in text]
        report.add("receipt_cites_checkpoints", not uncited,
                   "checkpoints not cited in the receipt: %s" % uncited if uncited else
                   "every captured checkpoint is cited", blocking=False)
        aborts = evaluate_abort(bundle)
        for tid in aborts["tripped"]:
            if tid.lower() not in lowered and tid.split("_", 1)[0].lower() not in lowered:
                report.add("receipt_records_abort_%s" % tid, False,
                           "abort trigger %s fired but is not named in the receipt" % tid)
    leaked = [m.group(0) for m in PRECISION.finditer(text)]
    report.add("receipt_precision_clean", not leaked,
               "high-precision decimals in the receipt: %d" % len(leaked) if leaked else
               "no coordinate-shaped decimals", blocking=False)
    return report


# ---------------------------------------------------------------- the execution gate


def evaluate_gate(bundle: Mapping[str, Any]) -> Report:
    """What must be true, and evidenced, before the drill may be executed at all.

    Every blocking finding must be `pass`. `unknown` (evidence not captured) keeps the gate shut,
    which is the point: the drill does not start on an assumption.
    """
    report = Report("gate")
    gate = bundle.get("gate") if isinstance(bundle.get("gate"), Mapping) else {}

    merged = gate.get("fix_merged_to_main")
    report.add("correctness_fix_merged", None if merged is None else bool(merged),
               "fix commit %s merged=%s" % (gate.get("fix_commit") or "unrecorded", merged))
    defects = gate.get("defects_covered")
    defects = [str(d).upper() for d in defects] if isinstance(defects, (list, tuple)) else []
    want = ["D1", "D2", "D3", "LOOPED_DRAIN", "MONOTONIC_REPLAY"]
    lacking = [d for d in want if d not in defects]
    report.add("fix_covers_audited_defects", not lacking if defects else None,
               "not covered: %s" % lacking if lacking else "D1/D2/D3 + drain + replay guard")

    report.add("code_configmap_matches_repo",
               None if gate.get("configmap_matches_repo") is None
               else bool(gate.get("configmap_matches_repo")),
               "live ConfigMap vs repo bundle diff empty=%s, annotation %s"
               % (gate.get("configmap_matches_repo"),
                  gate.get("configmap_annotation_sha256") or "unrecorded"))

    tests = gate.get("tests_passed")
    tests = [str(t) for t in tests] if isinstance(tests, (list, tuple)) else []
    required_tests = ["tests/test_hear_mqtt_bridge.py", "tests/test_hear_mqtt_bridge_manifest.py"]
    lacking_tests = [t for t in required_tests if t not in tests]
    report.add("gate_tests_green", not lacking_tests if tests else None,
               "not recorded green: %s" % lacking_tests if lacking_tests else
               "%d suites green" % len(tests))

    stable = _as_float(gate.get("soak_stable_minutes"))
    pending, failed = _as_int(gate.get("soak_pending")), _as_int(gate.get("soak_failed"))
    restarts = _as_int(gate.get("soak_restarts"))
    if stable is None or pending is None or failed is None or restarts is None:
        report.add("post_fix_soak_stable", None, "post-rollout stability not recorded")
    else:
        report.add("post_fix_soak_stable",
                   stable >= 30.0 and pending == 0 and failed == 0 and restarts == 0,
                   "stable %.0f min, pending=%d failed=%d restarts=%d (need >=30 min, all zero)"
                   % (stable, pending, failed, restarts))

    strategy = gate.get("strategy")
    report.add("strategy_is_recreate", None if strategy is None else strategy == "Recreate",
               "deployment strategy %s (RollingUpdate would put two writers on the RWO PVC)"
               % (strategy or "unrecorded"))

    window = gate.get("window") if isinstance(gate.get("window"), Mapping) else {}
    report.add("window_agreed",
               None if not window else bool(window.get("start_at") and window.get("operator")),
               "window %s, operator %s, second contact %s"
               % (window.get("start_at") or "unscheduled", window.get("operator") or "unassigned",
                  window.get("second_contact") or "none"))
    report.add("heartbeat_drill_not_concurrent",
               None if gate.get("heartbeat_drill_concurrent") is None
               else not gate.get("heartbeat_drill_concurrent"),
               "hear-heartbeat drill in the same window: %s"
               % gate.get("heartbeat_drill_concurrent"))

    backup = check_backup(bundle)
    blocking_backup = [f for f in backup.findings
                       if f.name in ("backup_artifacts", "backup_checksums",
                                     "pre_fault_ledger_reference")]
    ok: Optional[bool] = True
    for f in blocking_backup:
        if f.verdict == "fail":
            ok = False
            break
        if f.verdict == "unknown":
            ok = None
    report.add("backup_and_conservation_baseline_captured", ok,
               "; ".join(f.detail for f in blocking_backup) or "no backup evidence")

    plan = bundle.get("plan")
    if isinstance(plan, (list, tuple)) and plan:
        screened = check_command_plan(plan)
        report.add("command_plan_screened", screened.verdict == "pass",
                   "plan verdict %s%s" % (screened.verdict,
                                          "" if screened.verdict == "pass"
                                          else " (%s)" % ", ".join(screened.blocking_open)))
    else:
        report.add("command_plan_screened", None, "no command plan recorded in the bundle")

    report.add("redis_precheck_read_only", None if gate.get("redis_precheck_read_only") is None
               else bool(gate.get("redis_precheck_read_only")),
               "precheck Redis access limited to TTL/SMEMBERS/XLEN/INFO reads: %s"
               % gate.get("redis_precheck_read_only"))
    report.add("drill_unexecuted", not bundle.get("executed"),
               "bundle marks the drill as %s"
               % ("executed" if bundle.get("executed") else "not executed"), blocking=False)
    return report


def analyze(bundle: Mapping[str, Any], receipt_text: Optional[str] = None,
            now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """Full post-drill grading: abort boundary, conservation, refusal/cap/cache/claim, receipt."""
    reports = [check_backup(bundle), check_record_conservation(bundle),
               check_refusal_evidence(bundle), check_cap_evidence(bundle),
               check_cache_evidence(bundle), check_claim_evidence(bundle)]
    if receipt_text is not None:
        reports.append(validate_receipt(receipt_text, bundle))
    aborts = evaluate_abort(bundle, now)
    verdicts = {r.kind: r.as_dict() for r in reports}
    failed = sorted({n for r in reports for n in r.failed})
    unknown = sorted({n for r in reports for n in r.unknown})
    verdict = "fail" if (failed or aborts["tripped"]) else ("incomplete" if unknown else "pass")
    return {"schema": SCHEMA, "kind": "analysis", "verdict": verdict,
            "abort": aborts, "reports": verdicts, "failed": failed, "unknown": unknown}


# ---------------------------------------------------------------- rendering


def format_report(report: Mapping[str, Any]) -> str:
    lines = ["# %s -- %s" % (report.get("kind"), report.get("verdict")), ""]
    for finding in report.get("findings") or []:
        lines.append("%-5s %-38s %s%s" % (finding.get("verdict"), finding.get("name"),
                                          finding.get("detail"),
                                          "" if finding.get("blocking") else "  [advisory]"))
    open_items = report.get("blocking_open") or []
    lines += ["", "blocking items still open: %s" % (", ".join(open_items) or "none")]
    return "\n".join(lines) + "\n"


def format_analysis(analysis: Mapping[str, Any]) -> str:
    lines = ["# durable-outbox failure drill analysis -- %s" % analysis.get("verdict"), ""]
    abort = analysis.get("abort") or {}
    lines += ["## abort boundary",
              "fault elapsed %s s (soft %s, hard %s), restart budget %s"
              % (abort.get("fault_elapsed_s"), abort.get("soft_bound_s"),
                 abort.get("hard_abort_s"), abort.get("restart_budget"))]
    for trigger in abort.get("triggers") or []:
        state = {True: "TRIP", False: "clear", None: "?"}[trigger.get("tripped")]
        lines.append("%-5s %-28s %s" % (state, trigger.get("id"), trigger.get("detail")))
    for kind, report in (analysis.get("reports") or {}).items():
        lines += ["", format_report(report).rstrip()]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI


def _read_text(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise DrillEvidenceError("could not read %s: %s" % (path, exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Offline gate, plan screening and grading for the hear-mqtt-bridge "
                    "durable-outbox failure drill. Reads local files only; runs nothing.")
    sub = ap.add_subparsers(dest="command", required=True)
    for name, help_text in (("gate", "is the drill allowed to run yet?"),
                            ("plan", "screen a planned command sequence for blast radius"),
                            ("analyze", "grade a completed drill's evidence"),
                            ("receipt", "validate RECEIPT.md against the bundle")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--bundle", required=name != "plan",
                       help="drill evidence bundle JSON (or a directory holding bundle.json)")
        p.add_argument("--format", choices=("md", "json"), default="md")
        p.add_argument("--require-pass", action="store_true",
                       help="exit 2 unless the verdict is pass")
        if name in ("analyze", "receipt"):
            p.add_argument("--receipt", help="RECEIPT.md to validate")
        if name == "plan":
            p.add_argument("--plan-file", help="file of planned commands, one per line")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv or []))
    try:
        bundle = load_bundle(args.bundle) if getattr(args, "bundle", None) else {}
        receipt_text = _read_text(getattr(args, "receipt", None))
        plan_text = _read_text(getattr(args, "plan_file", None))
    except DrillEvidenceError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if args.command == "gate":
        payload: Dict[str, Any] = evaluate_gate(bundle).as_dict()
        rendered = format_report(payload)
    elif args.command == "plan":
        commands = (plan_text.splitlines() if plan_text is not None
                    else [str(c) for c in (bundle.get("plan") or [])])
        payload = check_command_plan(commands).as_dict()
        rendered = format_report(payload)
    elif args.command == "receipt":
        payload = validate_receipt(receipt_text, bundle).as_dict()
        rendered = format_report(payload)
    else:
        payload = analyze(bundle, receipt_text)
        rendered = format_analysis(payload)

    payload = redact_tree(payload)
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(rendered, end="")
    if args.require_pass and payload.get("verdict") != "pass":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
