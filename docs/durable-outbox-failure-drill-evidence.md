# Durable-outbox failure drill — offline evidence contract and execution gate

Status: **preparation only. The drill described in
[`durable-outbox-failure-drill-runbook.md`](durable-outbox-failure-drill-runbook.md) has not been
executed.** Nothing in this document runs a command against the cluster, the shared
`audit-redis-0`, or the live `hear-mqtt-bridge`. Everything here is a local file and a local check.

The runbook is the procedure. This document is the part of the procedure that can be *checked
before the window opens* and re-checked afterwards, by `tools/bridge_drill_evidence.py`:

| Question | Answer |
| --- | --- |
| May the drill run at all? | `python3 tools/bridge_drill_evidence.py gate --bundle <bundle.json> --require-pass` |
| Is the planned command sequence inside the blast-radius boundary? | `... plan --plan-file <plan.txt>` |
| Did the drill pass? | `... analyze --bundle <bundle.json> --receipt RECEIPT.md --require-pass` |
| Is the receipt complete? | `... receipt --bundle <bundle.json> --receipt RECEIPT.md` |

The tool is offline by construction: it imports no `subprocess`, `socket`, HTTP client, `sqlite3`
or Redis client, and contains no `exec`/`eval`/`Popen`. It cannot start a fault, extend one, read a
secret, or touch a shared instance — `tests/test_bridge_drill_evidence.py` asserts each of those.
It reads one JSON bundle and one Markdown receipt that the operator wrote by hand from the §5
snapshots, and grades them.

Grading is conservative in one specific way: **missing evidence is `unknown`, never `pass`**, and
the gate only opens when every blocking finding is an explicit `pass`. A drill does not start on an
assumption.

---

## 1. The exact abort boundary

These are the numbers from runbook §8, expressed once, in code (`tools/bridge_drill_evidence.py`),
so that "abort" is arithmetic rather than judgement at 02:00.

| id | Trips when | Constant |
| --- | --- | --- |
| `T1_fault_over_hard_bound` | fault elapsed (FAULT IN → FAULT OUT, or → now if still active) **exceeds 900 s** | `FAULT_HARD_ABORT_S = 900.0` (target 600 s, soft bound 720 s) |
| `T2_pending_stalled` | between two fault-phase checkpoints ≥ 60 s apart, `pending` grew by **< 50 % of the 36 rec/min ingest** — records are being dropped, not queued | `PENDING_STALL_FRACTION = 0.5`, `INGEST_RECORDS_PER_MIN = 36.0` |
| `T3_sqlite_integrity` | any checkpoint reports `database is locked`, `disk I/O error`, `malformed database` or corruption | log counters, any non-zero |
| `T4_ledger_regressed` | `records` or `max(id)` **decreases** between any two checkpoints | strict monotonicity |
| `T5_state_disk_floor` | `/state` free **< 500 MiB** or **> 90 % used** at any checkpoint | `STATE_FREE_FLOOR_BYTES`, `STATE_USED_PCT_MAX` |
| `T6_restart_budget` | restarts consumed **> 3**, or `CrashLoopBackOff` at any point | `RESTART_BUDGET = 3` |
| `T7_redis_collateral` | `audit-redis-0` restart count moves, a named consumer degrades, or `connected_clients` drops by more than the bridge's own single connection (≥ 25 %) | tenant-health samples |
| `T8_mqtt_ingest_stopped` | `records` flat across ≥ 60 s while only Redis is faulted | ingest must be unaffected |
| `T9_node_pressure` | operator declares kubelet/disk/node pressure | `operator.node_pressure` |
| `T10_operator_lost_control` | operator declares the second shell, kubeconfig or window lost | `operator.operator_control_lost` |

Two distinct verdicts come out of `evaluate_abort`:

* `abort: true` — at least one trigger fired. Execute §8.1 (revert `REDIS_PORT` to `6379`,
  `rollout status`, `snapshot.sh 99-abort`), then stop.
* `continue_allowed: true` — every trigger is *provably* clear, including the two the operator has
  to declare. An unknown trigger does not permit continuing; it means the evidence needed to judge
  the fault is not being collected, which is its own reason to close the window.

---

## 2. The evidence bundle

One JSON file, `bundle.json`, written alongside the `$DRILL/evidence/` snapshots. Schema
`dama-hear/bridge-drill-evidence/v1`. Every field is transcribed from a §5 snapshot; the tool never
fetches anything itself.

```jsonc
{
  "schema": "dama-hear/bridge-drill-evidence/v1",
  "executed": false,
  "config":  { "replay_limit": 256 },
  "gate":    { /* §3 below */ },
  "backup":  {
    "method": "sqlite3 .backup copy then kubectl cp; originals untouched",
    "files":  { "deploy-hear-mqtt-bridge.pre.yaml": "<sha256>", "...": "<sha256>" },
    "ledger": { "records": 3284, "max_id": 3284, "pending": 0, "failed": 0 }
  },
  "fault": {
    "kind": "connection-refused", "redis_port": 6390, "closed_port_verified": true,
    "started_at": "<UTC>", "ended_at": "<UTC>",
    "restarts_during": 1, "distinct_event_records": 12
  },
  "recovery":  { "operator_actions": [] },
  "negatives": { "probe_ids": ["drill-probe-a", "drill-probe-b"],
                 "results": { "N1": "pass", "...": "pass" } },
  "operator":  { "node_pressure": false, "operator_control_lost": false },
  "plan":      ["kubectl -n dama patch deploy/hear-mqtt-bridge ... \"6390\" ...", "..."],
  "checkpoints": [
    { "label": "00-precheck", "phase": "pre", "at": "<UTC>",
      "records": 3284, "max_id": 3284, "pending": 0, "failed": 0, "restarts": 0,
      "oldest_pending_at": null, "duplicate_record_uids": 0,
      "state_free_bytes": 214748364800, "state_used_pct": 75.0,
      "logs":  { "sqlite_locked": 0, "connection_refused": 0, "crashloop": false },
      "redis": { "ttls": {"gold": 12, "...": 12}, "devices": ["gold", "..."],
                 "xlen": 1024, "connected_clients": 31, "audit_redis_restarts": 17,
                 "degraded_consumers": [] },
      "pvc":   { "claim": "hear-mqtt-bridge-state", "volume": "pvc-…", "phase": "Bound" } }
  ]
}
```

`phase` is one of `pre` / `fault` / `recovery` / `post`, and the checkpoint labels are exactly the
§5 labels. These eight must exist for the receipt to validate: `00-precheck`, `10-fault-start`,
`12-fault-t5`, `13-midfault-restart`, `14-fault-t9`, `20-recovery-t0`, `22-recovery-t5m`,
`40-final`.

No credential ever enters the bundle. `REDIS_PASS` is read by the operator's shell during the
drill and never transcribed; the tool redacts any key whose *name* looks like a credential, and
blunts ≥ 4-place decimals the same way `tools/coord_guard.py` and `tools/bridge_soak_evidence.py`
do, because receipts get pasted into issues.

---

## 3. What the execution gate actually requires

`gate` is the answer to "may we run this yet?". Every item below is **blocking**: the gate stays
shut while any one of them is `fail` **or** `unknown`.

| Finding | Evidence that must exist first |
| --- | --- |
| `correctness_fix_merged` | `fix-durable-outbox-correctness` merged to `main`, commit sha recorded |
| `fix_covers_audited_defects` | that fix covers `D1`, `D2`, `D3`, looped drain and the monotonic replay guard |
| `code_configmap_matches_repo` | live `hear-mqtt-bridge-code` ConfigMap byte-identical to the regenerated repo bundle, annotation sha256 recorded |
| `gate_tests_green` | `tests/test_hear_mqtt_bridge.py` and `tests/test_hear_mqtt_bridge_manifest.py` green on that commit |
| `post_fix_soak_stable` | ≥ 30 min on the new ConfigMap with `pending = 0`, `failed = 0`, 0 restarts |
| `strategy_is_recreate` | deployment strategy is `Recreate` (RollingUpdate would put two writers on the RWO PVC) |
| `window_agreed` | window start and named operator (plus a reachable second person) |
| `heartbeat_drill_not_concurrent` | the `hear-heartbeat` drill is **not** in the same window |
| `backup_and_conservation_baseline_captured` | all seven §4 artifacts present and sha256'd, and the pre-fault ledger `records`/`max_id` recorded — this is the conservation reference |
| `command_plan_screened` | the planned command sequence passes §4 below |
| `redis_precheck_read_only` | precheck Redis access limited to `TTL`/`SMEMBERS`/`XLEN`/`INFO` reads |

`drill_unexecuted` is advisory and records the obvious: while `executed` is `false`, nothing has
happened yet.

---

## 4. Blast-radius screening of the command plan

`plan` reads the planned commands as **text** and refuses the §6.2 alternatives before anybody
types them. It runs nothing, which is the only reason it is safe to point at a file containing
`kubectl patch`.

Refused shapes (id → why): `redis_scale_down`, `redis_pod_delete` (a fleet-wide outage for ten
tenants to test one workload), `redis_flush` (destroys the frozen `dama:hear:*` contract and other
tenants' data), `redis_pause` (`CLIENT PAUSE`/`DEBUG SLEEP` is server-global and, with
`socket_timeout=None`, blocks instead of failing), `redis_shutdown` (`redis-cli config set`,
`shutdown`, `replicaof`, `failover`), `redis_rename_keys`, `host_netfilter` (`iptables`/`netem` on a
`hostNetwork` node also hits `hear-heartbeat`), `network_policy` (no effect on a `hostNetwork` pod),
`pvc_destruction`, `mtls_manifest_apply` (`deploy/k8s/hear-mqtt-bridge.yaml` bundles the
unprovisioned mTLS 8883 cutover), `durable_store_disable`, `replica_change`, `heartbeat_in_scope`.

Required shapes: the FAULT IN patch (`"6390"`), its exact inverse (`"6379"`), `rollout status`, and
at least one `snapshot.sh` call. `fault_is_reversible` fails unless both patches are present — a
fault whose inverse is not written down is not a bounded fault.

---

## 5. Post-drill grading

`analyze` runs six reports plus the abort boundary. The verdict is `fail` if anything failed or any
abort trigger fired, `incomplete` if evidence is missing, `pass` only when everything is proven.

* **backup** — seven artifacts, all sha256'd, a copy-only backup method, and the pre-fault ledger
  reference.
* **record_conservation** — `records`/`max(id)` monotonic across the whole drill including the
  mid-fault restart; final count ≥ the pre-fault backup count; the ingest arithmetic closes
  (36 rec/min ± 10 %, minus up to 60 records per budgeted restart gap, because `clean_session=True`
  means a restart legitimately loses that window's MQTT); zero duplicate `record_uid` rows.
* **refusal** — the fault was *refused*, not hung: `kind = connection-refused`, the bridge's own
  `REDIS_PORT` repointed to a **verified-closed** port, connection-refused log lines during the
  fault, `cache_attempts.failed` growing, and `oldest_pending` pinned within 120 s of FAULT IN.
* **cap** — peak `pending` **exceeds** `HEAR_DURABLE_REPLAY_LIMIT` (256), so a single 256-row sweep
  cannot explain the drain; the drain is monotonic, reaches 0 within 300 s of FAULT OUT, happens
  with **no operator action**, and the `failed` counter freezes afterwards.
* **cache** — every **precheck-live** node key re-armed with TTL in `1..30` (never a hard-coded six;
  a node silent before the window is not required to come back), `dama:hear:devices` membership
  identical to the precheck snapshot, probe ids cleaned up, and — advisory — liveness actually
  expired during the fault.
* **claim** — the PVC claim and volume never move and stay `Bound`; dedupe/claim is atomic (zero
  duplicate `record_uid`); `dama:hear:events` grows by **no more than** the number of distinct event
  records; N1–N4 pass (N5–N7 recorded, advisory).
* **receipt** — every required field present, the `phase2-durable-failure-drill: PASS|FAIL
  <operator>` sign-off line present, all eight required checkpoints captured, every tripped abort
  trigger named in the receipt, and no coordinate-shaped decimals left in the text.

---

## 6. What this does *not* do

It does not read the cluster, so it cannot tell you whether the gate items are *true* — only
whether they have been **evidenced**. Collecting them stays the operator's job, with
`tools/bridge_soak_evidence.py` (read-only, its own guard) and the §5 snapshot script. And it does
not authorise anything: the drill remains unexecuted until an operator, in an agreed window, runs
the runbook with a green gate.
