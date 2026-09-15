# Durable outbox induced-failure drill (hear-mqtt-bridge) — maintenance-window runbook

Status: **procedure only. Nothing in this document has been executed. No outage has been induced,
no cluster object has been changed, and no job has been scheduled.** Running it requires an
operator, an explicit window, and the gate in §1 to be satisfied first.

Purpose: produce the Phase 2 durability receipt — evidence that a Redis loss cannot lose a durable
write, that pending records are visible, that cache replay repairs the Redis contract without
duplication, that a process restart mid-fault is survivable, and that recovery is automatic and
bounded — with a rollback/abort plan that never risks the ledger or the other ten Redis tenants.

Target: live `dama/hear-mqtt-bridge` only (durable SQLite outbox, running since 2026-09-15 13:12 ET
on PVC `hear-mqtt-bridge-state`, 5Gi RWO `local-path`). `hear-heartbeat` is **out of scope**; it has
a separate ledger and PVC and is drilled separately if ever.

Related: `docs/resilience.md` (invariants), session `files/bridge-durable-plan/PLAN.md` (rollout +
rollback ladder), session `files/audit-phase2-postgres/PHASE2-POSTGRES-INTERFACE-AUDIT.md` (defects
D1/D2/D3 this drill must exercise **after** they are fixed).

---

## 0. Measured baseline this procedure is sized against

Read-only, 2026-09-15 ~14:50 ET, cluster k3s `desktop-bvrdk4j`:

| Fact | Value |
| --- | --- |
| Bridge pod | `hear-mqtt-bridge-5fbd8b4478-dk777`, Running 1/1, 0 restarts, `hostNetwork`, node IP `172.21.171.198` |
| Rollout strategy | `Recreate` (already patched; RollingUpdate would put two writers on the RWO PVC) |
| Outbox | `/state/mqtt-bridge.sqlite3`, `durable_records` 3,284 rows, window `13:12:59Z .. 14:48:50Z`, `pending = 0`, `cache_attempts.outcome='failed' = 0` |
| Ingest rate | ≈ 36 records/min (6 nodes, 10 s heartbeat cadence + events) ≈ 1.5 KB/record on disk |
| Redis | `audit-redis-0` StatefulSet in `infra`, redis 8.10.1, `requirepass`, `--save 60 1000`, 43,272 keys, 85.7 MB used of 6 GB, **10 in-cluster consumers**, pod restarted 17× (last ~142 min before baseline) |
| Redis contract keys | `dama:hear:{node}` (TTL 30 s), `dama:hear:devices`, `dama:hear:latest`, `dama:hear:event:{node}`, stream `dama:hear:events` (maxlen 1024) |
| Client library | redis-py 7.4.0 loaded from `/deps` (`PYTHONPATH=/deps`), **default retry = 3 with backoff on ConnectionError/TimeoutError**, `socket_timeout=None` (blocking) |
| MQTT | plaintext `127.0.0.1:31883`, `client_id=hear-mqtt-bridge`, **`clean_session=True`**, subscribe qos 1 |
| Replay | `DurableReplayWorker` every `HEAR_DURABLE_REPLAY_INTERVAL_S` (unset ⇒ 5.0 s), `HEAR_DURABLE_REPLAY_LIMIT=256`; `DurablePruneWorker` hourly, retention 30 d, prunes only cache-acknowledged rows |

Two consequences that shape every choice below:

1. **`clean_session=True`** ⇒ any bridge restart loses the MQTT messages published during the gap;
   they never reach the outbox. Restarts are therefore budgeted and counted, not casual.
2. **redis-py retries 3× with backoff and has no socket timeout** ⇒ a *momentary* Redis disruption is
   absorbed silently (good resilience, bad evidence), and a *hung* Redis (paused, not closed) blocks
   the bridge thread forever. The fault must therefore be a **refused connection**, not a stall and
   not a single dropped connection.

---

## 1. Gate — do not run before the correctness fix is merged AND deployed

`fix-durable-outbox-correctness` is producing the fix for the audited defects. Drilling before it
lands would certify the broken semantics and would have to be repeated.

Required, in order:

1. The fix PR is **merged to `main`** and covers, at minimum:
   - **D1** `record_uid` scoped by `device_id` (no cross-device silent drop);
   - **D2** atomic dedupe/claim so concurrent duplicates publish once;
   - **D3** deduplicated heartbeats still refresh `dama:hear:{node}` TTL;
   - bounded **looped** replay drain (not one 256-row sweep per start);
   - monotonic replay guard (a replayed older record must not overwrite newer cached state).
2. `deploy/k8s/hear-mqtt-bridge-code.yaml` regenerated from the merged tree and applied.
   **Never `kubectl apply -f deploy/k8s/hear-mqtt-bridge.yaml`** — that file bundles the
   unprovisioned mTLS 8883 cutover and would move live traffic to a dead port.
3. Bridge rolled onto the new ConfigMap (`Recreate`, one ≈15–30 s ingest gap) and stable ≥ 30 min
   with `pending = 0`, `failed = 0`, 0 restarts.

Gate verification (all must pass, read-only):

```bash
cd <fresh-clone>            # git clone https://github.com/rjmendez/dama-hear.git
git log --oneline -5        # fix commit present on main
python3 -m pytest tests/test_hear_mqtt_bridge.py tests/test_hear_heartbeat_receiver.py \
                 tests/test_hear_mqtt_bridge_manifest.py -q

# live code == repo bundle (byte level)
kubectl -n dama get cm hear-mqtt-bridge-code -o json \
 | python3 -c "import json,sys;d=json.load(sys.stdin)['data'];[open('live_'+k,'w').write(v) for k,v in d.items()]"
python3 -c "import yaml;y=yaml.safe_load(open('deploy/k8s/hear-mqtt-bridge-code.yaml'));[open('repo_'+k,'w').write(v) for k,v in y['data'].items()]"
diff live_tools_hear_mqtt_bridge.py repo_tools_hear_mqtt_bridge.py
diff live_tools_hear_heartbeat_receiver.py repo_tools_hear_heartbeat_receiver.py
kubectl -n dama get cm hear-mqtt-bridge-code -o jsonpath='{.metadata.annotations.dama-hear/commit}{"\n"}'
```

If any diff is non-empty, **stop**: the drill would measure code that is not in the repo.

---

## 2. Window, roles, budget

* **Window:** 60 min wall clock, low-activity, single operator at the keyboard with a second person
  reachable. Start at `HH:00`. Avoid minutes `:04 :07 :11 :12 :15 :19 :22 :26 :30` — `hear-drain`,
  `hear-tdoa`, `hear-score`, `hear-embed`, `hear-birdnet`, `hear-tag` fire there. None of them read
  `dama:hear:*`, so the conflict is only on node/disk contention; still, keep the fault clear of them.
* **Fault duration:** target **10 min**, soft bound 12 min, **hard abort at 15 min**.
  10 min × 36 rec/min ≈ **360 pending records > the 256 replay limit** — this is deliberate: it is
  the only way to prove looped drain rather than a single sweep.
* **Restart budget:** exactly **3** bridge restarts (fault-in, mid-fault, fault-out), each ≈15–30 s,
  each losing that window's MQTT messages (≈2–3 heartbeats/node, expected, recorded in the receipt).
* **Redis liveness gap:** `dama:hear:{node}` TTL is 30 s, so from T+30 s all six nodes read "dead"
  in Redis until recovery. No in-repo or in-cluster alerting consumes those keys today
  (`tools/check_fleet_health.py` is run by hand), but announce the window anyway.
* **Do not** run this drill and the `hear-heartbeat` equivalent in the same window.

---

## 3. Prechecks (all must pass; any failure ⇒ postpone)

```bash
DRILL=~/hear-drill-$(date +%Y%m%d-%H%M%S); mkdir -p $DRILL/{backup,evidence}
PW=$(kubectl -n dama get secret dama-redis-secret -o jsonpath='{.data.REDIS_PASS}' | base64 -d)
POD=$(kubectl -n dama get pod -l app=hear-mqtt-bridge -o jsonpath='{.items[0].metadata.name}')
```

| # | Check | Command | Pass |
| --- | --- | --- | --- |
| P1 | Gate §1 satisfied | above | all diffs empty, tests green |
| P2 | Bridge healthy | `kubectl -n dama get pod -l app=hear-mqtt-bridge -o wide` | 1/1 Running, restarts 0, age ≥ 30 min |
| P3 | Strategy is Recreate | `kubectl -n dama get deploy hear-mqtt-bridge -o jsonpath='{.spec.strategy.type}'` | `Recreate` |
| P4 | Outbox clean | snapshot script §5 | `pending = 0`, `failed = 0`, `max(created_at)` within 2 min of now |
| P5 | Disk headroom | `kubectl -n dama exec $POD -- sh -lc 'df -h /state; du -sh /state'` | ledger + WAL < 3.5 Gi of the 5 Gi claim, and ≥ 5 Gi free on the backing disk (baseline: `/dev/sdc` 1007 G, 75 % used, 244 G free — `local-path` shares the node disk, so watch both numbers) |
| P6 | Redis stable | `kubectl -n infra get pod audit-redis-0`; `redis-cli info server` | Running, **uptime ≥ 60 min**, no restart in the last hour |
| P7 | Redis headroom | `redis-cli info memory` | `used_memory` < 50 % of `maxmemory` |
| P8 | Fleet baseline recorded | `redis-cli smembers dama:hear:devices` + per-node TTL + `select device_id,max(created_at) ... group by 1` | **≥ 5 of 6 nodes reporting within the last 2 min.** Any silent node must be silent *before* the window and named in the receipt; every success criterion below is keyed to the *precheck-live* node set, not to a hard-coded 6. Known at baseline: **`rankine` has been silent since 2026-09-15T14:25:09Z (TTL −2)** — a pre-existing fleet-hardware issue, not a durability defect; either recover it first or run 5-node and say so. |
| P9 | MQTT flowing | `kubectl -n dama logs deploy/hear-mqtt-bridge --tail=50` | no `rejected`-storm beyond the known `mach` schema noise; no `database is locked` |
| P10 | No concurrent change | `kubectl -n dama rollout history deploy/hear-mqtt-bridge` | no rollout in the last 30 min; no other operator active |
| P11 | Rollback artifacts exist | §4 complete | backup dir populated and checksummed |
| P12 | Escape hatch | `kubectl auth can-i patch deploy -n dama` | `yes`, and kubeconfig works from a second shell |

---

## 4. Backup (before any fault)

```bash
kubectl -n dama get deploy hear-mqtt-bridge -o yaml    > $DRILL/backup/deploy-hear-mqtt-bridge.pre.yaml
kubectl -n dama get cm hear-mqtt-bridge-code -o yaml   > $DRILL/backup/cm-hear-mqtt-bridge-code.pre.yaml
kubectl -n dama get pvc hear-mqtt-bridge-state -o yaml > $DRILL/backup/pvc.pre.yaml
kubectl -n dama rollout history deploy/hear-mqtt-bridge > $DRILL/backup/rollout-history.pre.txt
kubectl -n dama get deploy hear-mqtt-bridge -o jsonpath='{.metadata.resourceVersion}{"\n"}' \
                                                        > $DRILL/backup/resourceversion.pre.txt

# consistent online copy of the ledger (does not stop the writer)
kubectl -n dama exec $POD -- python3 -c "
import sqlite3; s=sqlite3.connect('/state/mqtt-bridge.sqlite3'); d=sqlite3.connect('/state/drill-backup.sqlite3')
s.backup(d); d.close()"
kubectl -n dama cp dama/$POD:/state/drill-backup.sqlite3 $DRILL/backup/mqtt-bridge.pre.sqlite3
kubectl -n dama exec $POD -- rm -f /state/drill-backup.sqlite3
sha256sum $DRILL/backup/* > $DRILL/backup/SHA256SUMS
```

Redis side: `--save 60 1000` already persists. Optionally force a point-in-time RDB **before** the
fault (cheap at 85 MB, and the only Redis write this drill performs):

```bash
kubectl -n infra exec audit-redis-0 -- redis-cli -a "$PW" --no-auth-warning bgsave
kubectl -n infra exec audit-redis-0 -- redis-cli -a "$PW" --no-auth-warning info persistence | grep rdb_last_bgsave
```

**Never** delete `pvc/hear-mqtt-bridge-state`, and never `FLUSHDB`/`FLUSHALL`: pending records live
only on that PVC, and the Redis keyspace is shared with nine other workloads.

---

## 5. Evidence snapshot (single command, used at every checkpoint)

Save as `$DRILL/snapshot.sh` (same shape as the bridge-recreate drill's snapshot tool). A
validated copy plus a read-only sample run is kept at session
`files/phase2-durable-drill/snapshot.sh` and `files/phase2-durable-drill/evidence/00-tooling-validation.txt`
(executed read-only 2026-09-15T14:55Z against the live bridge; it induced no fault):

```bash
#!/bin/bash
# usage: ./snapshot.sh <label>   -> writes $DRILL/evidence/<label>.txt
set -u
L=${1:-snap}; OUT=${DRILL:?}/evidence/$L.txt
PW=$(kubectl -n dama get secret dama-redis-secret -o jsonpath='{.data.REDIS_PASS}' | base64 -d)
POD=$(kubectl -n dama get pod -l app=hear-mqtt-bridge -o jsonpath='{.items[0].metadata.name}')
{
echo "=== $L @ $(date -u +%Y-%m-%dT%H:%M:%SZ) pod=$POD ==="
kubectl -n dama get pod -l app=hear-mqtt-bridge -o wide --no-headers
kubectl -n dama exec $POD -- python3 -c "
import sqlite3,json
c=sqlite3.connect('file:/state/mqtt-bridge.sqlite3?mode=ro',uri=True); q=lambda s:c.execute(s).fetchall()
print(json.dumps({
 'records':q('select count(*),max(id) from durable_records')[0],
 'window':q('select min(created_at),max(created_at) from durable_records')[0],
 'by_device':q('select device_id,count(*) from durable_records group by 1 order by 2 desc'),
 'by_path':q('select telemetry_path,count(*) from durable_records group by 1'),
 'pending':q(\"select count(*) from durable_records r where not exists(select 1 from cache_attempts a where a.record_uid=r.record_uid and a.outcome='succeeded')\")[0][0],
 'oldest_pending':q(\"select min(created_at) from durable_records r where not exists(select 1 from cache_attempts a where a.record_uid=r.record_uid and a.outcome='succeeded')\")[0][0],
 'attempts':q('select outcome,count(*) from cache_attempts group by 1'),
 'last_failure':q(\"select max(created_at) from cache_attempts where outcome='failed'\")[0][0],
 'dup_uid_rows':q('select count(*) from (select record_uid from durable_records group by 1 having count(*)>1)')[0][0],
}, indent=1, default=str))"
kubectl -n dama exec $POD -- sh -lc 'df -h /state | tail -1; ls -l /state'
echo "--- redis"
for d in gold nyquist kasami mach ageev rankine; do
  echo -n "ttl $d: "; kubectl -n infra exec audit-redis-0 -- redis-cli -a "$PW" --no-auth-warning ttl dama:hear:$d
done
kubectl -n infra exec audit-redis-0 -- redis-cli -a "$PW" --no-auth-warning smembers dama:hear:devices
kubectl -n infra exec audit-redis-0 -- redis-cli -a "$PW" --no-auth-warning xlen dama:hear:events
kubectl -n infra exec audit-redis-0 -- redis-cli -a "$PW" --no-auth-warning xrevrange dama:hear:events + - COUNT 3
kubectl -n infra exec audit-redis-0 -- redis-cli -a "$PW" --no-auth-warning info clients | grep connected_clients
echo "--- logs"
kubectl -n dama logs deploy/hear-mqtt-bridge --tail=80
} > "$OUT" 2>&1
echo "wrote $OUT"
```

Checkpoint labels used below: `00-precheck`, `10-fault-start`, `11-fault-t2`, `12-fault-t5`,
`13-midfault-restart`, `14-fault-t9`, `20-recovery-t0`, `21-recovery-t1m`, `22-recovery-t5m`,
`30-negatives`, `40-final`.

---

## 6. Fault injection — choice and rejected alternatives

### 6.1 Chosen

**F0 — connection-kill smoke test (zero blast radius, no restart, ≤ 30 s).**
Kill exactly the bridge's own Redis connections, identified by socket inode inside the pod's PID
namespace (so no other host-network client can be hit):

```bash
# 1. list the bridge's own redis sockets (pod PID namespace => bridge process only)
kubectl -n dama exec $POD -- python3 - <<'EOF'
import os,glob,struct,socket
inodes={}
for fd in glob.glob("/proc/[0-9]*/fd/*"):
    try: t=os.readlink(fd)
    except OSError: continue
    if t.startswith("socket:["): inodes[t[8:-1]]=fd.split("/")[2]
dec=lambda a:(socket.inet_ntoa(struct.pack("<I",int(a.split(':')[0],16))),int(a.split(':')[1],16))
for line in open("/proc/net/tcp").read().splitlines()[1:]:
    f=line.split()
    if f[9] in inodes and dec(f[2])[1]==6379:
        print(f"{dec(f[1])[0]}:{dec(f[1])[1]}")
EOF
# 2. kill only those addresses (repeat per address printed)
kubectl -n infra exec audit-redis-0 -- redis-cli -a "$PW" --no-auth-warning client kill addr 172.21.171.198:<port>
```

Expected: redis-py reconnects transparently (retry = 3), `pending` stays 0, no `failed` attempt, and
no telemetry gap. F0 proves the reconnect path and proves the drill tooling works. If F0 *does*
produce a pending record, that is still a pass — record it and continue.

**F1 — bounded connection-refused outage scoped to the bridge (the main drill).**
Repoint the bridge's Redis port to a closed port. Nothing else in the cluster is touched; the Redis
server keeps serving its other nine consumers at full speed.

```bash
# FAULT IN  (T+0) — note the change-cause for the audit trail
kubectl -n dama patch deploy/hear-mqtt-bridge --type=json -p '[
 {"op":"replace","path":"/spec/template/spec/containers/0/env/5/value","value":"6390"}]'   # verify index 5 == REDIS_PORT first!
kubectl -n dama annotate deploy/hear-mqtt-bridge \
  kubernetes.io/change-cause="phase2 durable failure drill FAULT IN $(date -u +%FT%TZ)" --overwrite
kubectl -n dama rollout status deploy/hear-mqtt-bridge --timeout=120s
```

Verify the index before patching:
`kubectl -n dama get deploy hear-mqtt-bridge -o jsonpath='{.spec.template.spec.containers[0].env[5].name}'`
must print `REDIS_PORT`. Port 6390 must be closed on the node — verified empty in both `/proc/net/tcp` and `/proc/net/tcp6` at baseline; re-verify in the window (`ss` is not installed in the bridge image; use the `/proc` parser from F0) — so the failure is an immediate
`ECONNREFUSED`, not a hang.

**FAULT OUT** is the exact inverse patch back to `"6379"` plus a `rollout status`.

### 6.2 Rejected, with reasons (do not improvise these)

| Option | Why rejected |
| --- | --- |
| `kubectl -n infra scale sts/audit-redis --replicas=0` or deleting `audit-redis-0` | Ten in-cluster consumers (`fleet-api`, `consensus-engine`, `dama-gpu-feedback`, `charlie`, `oxalis`, `prometheus-oxalis-orchestrator`, `ant-mirror-daemon`, `dama-bridge-rust`, `hear-heartbeat`, the bridge). Single-node cluster: a cold Redis start also re-reads 43 k keys. Fleet-wide outage to test one workload. |
| `CLIENT PAUSE` / `DEBUG SLEEP` | Server-global, and the bridge has `socket_timeout=None`: it would **block indefinitely** instead of failing, so no durable pending would even be produced — the wrong failure shape, applied to everyone. |
| `FLUSHDB` / `FLUSHALL` / renaming keys | Destroys the frozen `dama:hear:*` contract and other tenants' data. Never. |
| Host `iptables`/`tc netem` on the node | The bridge is `hostNetwork`, so any host rule also hits `hear-heartbeat` and races kube-proxy's iptables reconciliation on the only control-plane node. |
| NetworkPolicy egress deny | No effect on a `hostNetwork` pod. |
| Unmounting/deleting the PVC, or `rm` on the sqlite file | The pending records are the evidence; destroying them is not a durability test. |
| Changing `MQTT_*`, TLS env, `replicas`, `hostNetwork`, `image` | Out of bounds: moves live traffic or creates a second writer on an RWO PVC. |
| Setting `HEAR_DURABLE_STORE=none` | Removes the system under test. |

---

## 7. Legs, with expected observations

Timings are from **T+0 = FAULT IN completed**.

| Leg | Action | SQLite outbox | Redis | MQTT / logs |
| --- | --- | --- | --- | --- |
| **L0** T−5 | `snapshot.sh 00-precheck` | `pending 0`, `failed 0`, records rising ≈36/min | 6 TTLs > 0, `xlen` steady | connected, subscribing |
| **L1** T+0 | FAULT IN patch + rollout | new pod starts; startup replay finds nothing to drain (pending 0) | last pre-fault values frozen | pod recreated, `durable backend=sqlite path=/state/mqtt-bridge.sqlite3`, then repeating Redis `ConnectionError` |
| **L2** T+0..+2m `snapshot.sh 10-fault-start`, `11-fault-t2` | **records keep growing** (durable write precedes Redis), `pending` grows ≈36/min, `failed` attempts grow (write + 5 s replay retries), `oldest_pending` pinned at T+0 | every precheck-live `dama:hear:{node}` TTL expires by T+30 s ⇒ `ttl = -2`; `dama:hear:devices` unchanged (a set, no TTL); `xlen` frozen | `connected; subscribing` still true — MQTT ingest is unaffected; per-record `ConnectionError` / `Connection refused` in logs |
| **L3** T+5m `snapshot.sh 12-fault-t5` | `pending ≈ 180`, still no row loss; WAL grows; no `database is locked` | unchanged | replay worker logs a failed sweep every 5 s |
| **L4** T+6m mid-fault restart: `kubectl -n dama rollout restart deploy/hear-mqtt-bridge && kubectl -n dama rollout status --timeout=120s deploy/hear-mqtt-bridge`; then `snapshot.sh 13-midfault-restart` | **`max(id)` and record count never decrease across the restart** (WAL survives pod death); `pending` continues from where it was (+ the ≈15–30 s gap of lost MQTT messages, which were never accepted and so are legitimately absent); startup replay attempts ≤256 and fails cleanly without crash-looping | unchanged | pod restart count +1; bridge reconnects to MQTT; no crash loop |
| **L5** T+9m `snapshot.sh 14-fault-t9` | `pending ≳ 300` (> the 256 replay limit — required for the looped-drain proof) | unchanged | steady failure logging, no unbounded memory growth |
| **L6** T+10m FAULT OUT (revert env to `6379`, `rollout status`), `snapshot.sh 20-recovery-t0` | replay begins: `pending` falls monotonically; with looped drain it reaches 0 in one to two sweeps | `dama:hear:{node}` re-armed for every precheck-live node (TTL back in 1..30), `dama:hear:latest` = a **current** record, `xlen` grows by **at most the number of distinct pending events**, never by the number of replay attempts | `connected; subscribing`, replay summary lines |
| **L7** T+11m / T+15m `21-recovery-t1m`, `22-recovery-t5m` | `pending = 0`; `failed` count **frozen** at its fault-window value (no post-recovery growth); no duplicate `record_uid` rows | precheck-live node TTLs healthy and refreshing every 10 s; devices set unchanged (plus any drill probe ids from §9, cleaned up after) | no errors |

Sanity arithmetic to do live at L6: `records(T+10m) − records(T+0) ≈ 360 − (restart gap ≈ 12)`, and
`pending(T+10m) == records(T+10m) − records(T+0) − (any successes)`. A discrepancy beyond ±10 % of
the ingest rate is a finding, not a rounding error.

---

## 8. Abort triggers and abort procedure

Abort **immediately** (execute §8.1) on any of:

1. Fault elapsed **> 15 min** for any reason, including operator uncertainty.
2. `pending` **stops growing while the fault is active** (means records are being *dropped*, not
   queued — the worst outcome; capture logs before recovering).
3. Any `database is locked`, `disk I/O error`, `malformed database`, or SQLite corruption message.
4. `records`/`max(id)` **decreases** at any checkpoint.
5. `/state` free space < 500 Mi, or `df` shows > 90 % used.
6. Bridge pod `CrashLoopBackOff`, or restart count rises beyond the 3 budgeted restarts.
7. **Any non-bridge Redis consumer degrades** (`audit-redis-0` restart, `connected_clients` collapse,
   `used_memory` climbing abnormally, `fleet-api`/`consensus-engine` errors).
8. MQTT ingest stops (`records` stops growing while Redis is the only thing broken).
9. Node-level trouble on the single-node cluster: kubelet pressure, `kubectl` latency, disk pressure.
10. Operator loses the second shell / kubeconfig, or the window is interrupted.

### 8.1 Abort = recover, then stop (never "undo" the ledger)

```bash
# 1. remove the fault (same patch, back to 6379)
kubectl -n dama patch deploy/hear-mqtt-bridge --type=json -p '[
 {"op":"replace","path":"/spec/template/spec/containers/0/env/5/value","value":"6379"}]'
kubectl -n dama rollout status deploy/hear-mqtt-bridge --timeout=120s
# 2. if the spec is in doubt, restore the exact pre-drill spec
kubectl -n dama apply -f $DRILL/backup/deploy-hear-mqtt-bridge.pre.yaml
# 3. or step back one revision
kubectl -n dama rollout undo deploy/hear-mqtt-bridge && kubectl -n dama rollout status deploy/hear-mqtt-bridge --timeout=120s
# 4. confirm the invariants that must never change
kubectl -n dama get deploy hear-mqtt-bridge -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}'
#    MQTT_HOST=127.0.0.1  MQTT_PORT=31883  REDIS_PORT=6379  HEAR_DURABLE_STORE=sqlite
#    HEAR_DURABLE_DB=/state/mqtt-bridge.sqlite3  strategy=Recreate
./snapshot.sh 99-abort
```

Escalation if `pending` does not drain after recovery (see §9 order). If the ledger itself is
suspect, **do not delete or recreate the PVC**: copy it out (`kubectl cp` of a `sqlite3 .backup`)
and raise a defect with the copy attached. `$DRILL/backup/mqtt-bridge.pre.sqlite3` is the
pre-drill reference for record-loss comparison.

---

## 9. Recovery order (normal end of drill)

1. **Remove the fault**: revert `REDIS_PORT` to `6379`; `rollout status --timeout=120s`.
2. **Prove Redis reachable from the bridge** before judging replay:
   `kubectl -n dama exec $POD -- sh -lc 'PYTHONPATH=/deps python3 -c "import os,redis;print(redis.Redis(host=os.environ[\"REDIS_HOST\"],port=int(os.environ[\"REDIS_PORT\"]),password=os.environ.get(\"REDIS_PASS\") or None).ping())"'`
3. **Let automatic replay work**: sample `pending` every 15 s for up to **5 min** (startup drain +
   5 s background worker). Expect monotonic decline to 0. Record the drain curve — it is the receipt.
4. If `pending > 0` after 5 min: one `kubectl -n dama rollout restart` (restart #3 of the budget) to
   force a startup drain; re-sample for 3 min.
5. If still `pending > 0`: manual bounded replay, in-process, no restart:
   `kubectl -n dama exec $POD -- sh -lc 'PYTHONPATH=/deps python3 -c "..."'` is **not** available
   (the bridge owns the store); instead capture `oldest_pending`, the failing `error_text`
   (`select error_text,count(*) from cache_attempts where outcome=\"failed\" group by 1`), and stop —
   an undrainable backlog is a **fail** of the drill and a defect for `fix-durable-outbox-correctness`,
   not something to paper over.
6. **Verify the Redis contract is repaired**: every precheck-live `dama:hear:{node}` present with TTL 1..30;
   `dama:hear:latest` timestamp within one heartbeat; `dama:hear:devices` unchanged from the precheck membership (+ any drill probe ids pending cleanup; note the pre-existing `test-verify` member); `dama:hear:event:{node}` present for nodes that emitted events.
7. **Verify idempotence** (§10 N3): `xlen dama:hear:events` grew by **no more than** the number of
   distinct event records created during the fault; no duplicate `record_uid` rows in the ledger.
8. **Clean up drill probes** (§10), then `./snapshot.sh 40-final` and write the receipt (§11).
9. Leave the deployment exactly as found: `Recreate`, `REDIS_PORT=6379`, `MQTT_PORT=31883`,
   durable env unchanged, PVC intact, annotation `change-cause` set to `drill complete <UTC>`.

---

## 10. Negative and edge cases (run after L7, on synthetic probe ids only)

Publish probes to the live broker with `mosquitto_pub` from the node (topic `dama/<id>/telemetry`,
qos 1) using device ids `drill-probe-a` / `drill-probe-b` — the fleet already has a `test-verify`
precedent in `dama:hear:devices`. Never reuse a real node id; never hand-write into Redis or SQLite.

| # | Case | Method | Post-fix expectation (pre-fix behaviour in brackets) |
| --- | --- | --- | --- |
| N1 | **D1** cross-device idempotency collision | two events, different `device_id`, identical `idempotency_key` | two ledger rows, two Redis publishes [pre-fix: second silently dropped, first device's payload returned] |
| N2 | **D3** dedupe must still refresh liveness | identical heartbeat payload republished ~20 s apart | `dama:hear:drill-probe-a` TTL re-armed to 30 [pre-fix: TTL keeps decaying to −2 while "heartbeating"] |
| N3 | **D2** concurrent duplicate publish | 4 identical event payloads published concurrently | 1 ledger row, `xlen` delta 1 [pre-fix: 4 stream entries] |
| N4 | Replay monotonic guard | with the bridge briefly faulted, ensure an older probe record replays after a newer one has been cached | `dama:hear:drill-probe-a` keeps the **newer** payload [pre-fix: stale overwrite + re-armed TTL on stale content] |
| N5 | Backlog > replay limit | already covered by the 10-min fault (≈360 > 256) | drains to 0 without operator action |
| N6 | Pre-outbox rejects are not recoverable | observe the known `mach` `time must be an object` rejects during the fault | they never appear in `durable_records` — documented limitation, not a drill failure |
| N7 | Prune never eats pending | `select count(*) from durable_records` before/after an hourly prune tick during the fault | pruning touches only cache-acknowledged rows older than 30 d; pending count unaffected |

Cleanup (Redis only; ledger rows are immutable evidence and stay):

```bash
for id in drill-probe-a drill-probe-b; do
  kubectl -n infra exec audit-redis-0 -- redis-cli -a "$PW" --no-auth-warning del dama:hear:$id dama:hear:event:$id
  kubectl -n infra exec audit-redis-0 -- redis-cli -a "$PW" --no-auth-warning srem dama:hear:devices $id
done
```

Stream entries for probe events age out of the 1024-entry `dama:hear:events` maxlen on their own;
do not `XDEL` (other consumers read by id ranges).

---

## 11. Receipt / evidence package

Directory `$DRILL/` (copy to session `files/phase2-durable-drill/<UTC-date>/`, never `/tmp`):

```
backup/   deploy, cm, pvc, rollout-history, resourceVersion, mqtt-bridge.pre.sqlite3, SHA256SUMS
evidence/ 00-precheck .. 40-final snapshots (§5), plus:
          logs-full-fault-window.txt   kubectl logs --since=<window>
          drain-curve.csv              utc,pending,failed,records  (15 s samples, L6..L7)
          pod-events.txt               kubectl -n dama get events --sort-by=.lastTimestamp
          redis-clients-during.txt     connected_clients / other-tenant sanity samples
RECEIPT.md
```

`RECEIPT.md` must state: gate commit sha + ConfigMap annotation sha256; window start/end UTC; exact
patches applied (both directions) with change-cause; records/pending/failed at every checkpoint;
restart count consumed; measured MQTT loss during each restart gap; drain time to `pending = 0`;
`xlen` delta vs distinct event count; the precheck-live node TTLs at T+15 m; every negative-case result; abort
triggers hit (if any); and the sign-off line `phase2-durable-failure-drill: PASS|FAIL <operator>`.

---

## 12. Success criteria (all must hold; any miss ⇒ FAIL)

1. **No durable loss**: `durable_records` count and `max(id)` are strictly monotonic across the whole
   drill, including the mid-fault restart; the only missing telemetry is the MQTT published during
   the ≈15–30 s restart gaps, quantified in the receipt.
2. **Pending is visible and truthful**: `pending` grows at ≈ the ingest rate while Redis is refused,
   `oldest_pending` pins at fault start, and `cache_attempts.outcome='failed'` grows with attempts.
3. **Automatic repair**: after FAULT OUT, `pending` returns to 0 within **5 min with no operator
   action**, including a backlog larger than `HEAR_DURABLE_REPLAY_LIMIT=256` (looped drain proven).
4. **Idempotence**: zero duplicate `record_uid` rows; `dama:hear:events` grows by no more than the
   number of distinct event records in the fault window; N1–N4 all pass.
5. **Contract repaired**: every precheck-live `dama:hear:{node}` key re-armed with TTL ≤ 30 s, `dama:hear:latest`
   fresh, `dama:hear:devices` membership identical to the precheck snapshot after probe cleanup.
6. **Restart survivability**: the mid-fault restart loses no ledger row, causes no crash loop, and no
   `database is locked` appears anywhere in the logs.
7. **No collateral**: `audit-redis-0` restart count unchanged; no other namespace's workload errors in
   the window; `/state` usage grows only by the expected ≈1.5 KB × records.
8. **Clean exit state**: deployment spec byte-identical to `$DRILL/backup/deploy-*.pre.yaml` except
   the `change-cause` annotation; PVC intact; `failed` counter frozen after recovery.
9. **Evidence complete**: every artifact in §11 present and checksummed.

A FAIL on 1, 3, 4 or 6 blocks Phase 2 outright and is handed back to
`fix-durable-outbox-correctness`. A FAIL on 7 blocks re-running the drill until the fault choice is
re-scoped.

---

## 13. After the drill

* File the receipt path in `phase2-soak-day1-review`; the same procedure is re-run once at day 7
  (`phase2-soak-day7-review`) with `00-precheck` compared against this run's `40-final`.
* Feed any defect found straight into `fix-durable-outbox-correctness` (or a successor) — do not
  patch live.
* Open follow-ups, non-blocking: the bridge still exposes **no `/healthz`**, so `pending` is only
  observable by `kubectl exec` (ingest a `pending_records` gauge before the fleet grows); ledger
  `health()` still does `COUNT(*)` scans; `mach`'s pre-outbox schema rejects remain unrecoverable.
