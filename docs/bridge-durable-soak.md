# Durable-outbox soak runbook (hear-mqtt-bridge)

`deploy/hear-mqtt-bridge` writes every accepted telemetry record to a SQLite outbox on a dedicated
`hear-mqtt-bridge-state` PVC mounted at `/state` before it touches the Redis cache, and replays
anything Redis has not acknowledged. Phase 2 is the soak that proves this holds over time rather
than over a minute: the same evidence is collected at T0, T+24h, T+7d and T+14d and compared.

`tools/bridge_soak_evidence.py` collects and grades that evidence.

## What it does

    python3 tools/bridge_soak_evidence.py --milestone T0
    python3 tools/bridge_soak_evidence.py --milestone T+24h \
        --baseline ~/bridge-soak/T0-20260915T143000Z --require-pass

Each run writes `<out-dir>/<milestone>-<UTC stamp>/{snapshot.json,report.md}` (default out-dir
`~/bridge-soak`, outside the repo) and prints the report. It records:

* **window** — `captured_at` plus the start/finish of the sample itself
* **deployment** — generation vs `observedGeneration`, image, strategy, replicas, host network,
  the durable env (with unset values filled in from the receiver's defaults and marked as
  defaults), and any drift from the plaintext `127.0.0.1:31883` identity
* **configmap** — a deterministic sha256 over `hear-mqtt-bridge-code`'s data plus the
  `dama-hear/commit` and `dama-hear/source-sha256` provenance annotations
* **pvc / pod** — binding, capacity, storage class; pod phase, readiness, restart count
* **outbox** — record count, oldest/newest record, per-path and per-device counts, `pending`,
  `succeeded`, `failed`, failure target breakdown, journal mode, database and `/state` size
* **coverage** — which of the six publishing nodes appear in the ledger, and which do not
* **receiver / cache** — the `hear-heartbeat` receiver's own `durable_store` health, and Redis
  `DBSIZE`/`XLEN` where the cache can be reached
* **errors** — counts of `database is locked`, durable errors, tracebacks, rejections, Redis
  errors, replays and reconnects, with one redacted sample of each

## Pass/fail criteria

Graded at every milestone:

| Criterion | Passes when |
|---|---|
| `durable_store_enabled` | `HEAR_DURABLE_STORE=sqlite` |
| `state_volume_mounted` | `/state` is mounted and `HEAR_DURABLE_DB` lives under it |
| `pvc_bound` | `hear-mqtt-bridge-state` is `Bound` |
| `plaintext_mqtt_preserved` | `MQTT_HOST=127.0.0.1`, `MQTT_PORT=31883` |
| `rollout_settled` | `generation == observedGeneration`, ready and nothing unavailable |
| `pod_restarts_zero` | restart count 0 |
| `ledger_has_records` | the ledger is non-empty |
| `pending_drained` | `pending = 0` at sample time |
| `no_cache_failures` | `failed = 0`, or unchanged since the baseline (a survived outage) |
| `all_nodes_covered` | all six device ids present |
| `ledger_fresh` | newest record within 120 s of the sample |
| `logs_clean` | no `database is locked`, durable error or traceback |
| `receiver_ledger_drained` | the receiver's own `pending_records = 0` |
| `state_within_budget` | `/state` under 64 MB (T0), 200 MB (T+24h), 1.2 GB (T+7d), 2.2 GB (T+14d) |

Added at T+24h / T+7d / T+14d, which require `--baseline`:

| Criterion | Passes when |
|---|---|
| `baseline_supplied` | a T0 snapshot was given |
| `window_matches_milestone` | elapsed time is within 25% of 24 h / 7 d / 14 d |
| `record_growth` | growth is within 20% of 52,000 records/day pro-rated over the window |
| `prune_within_retention` | the oldest record is no older than `HEAR_DURABLE_RETENTION_DAYS + 1` |
| `coverage_not_regressed` | no node that was writing at T0 has stopped |

Verdict is `pass`, `fail` (any criterion failed) or `incomplete` (evidence missing). The default
exit code is 0 — it reports. `--require-pass` exits 2 on anything that is not `pass`, which is the
form to use from a scheduled job. `--from-snapshot` re-grades a stored snapshot without touching
the cluster; `--records-per-day`, `--growth-tolerance` and `--expected-nodes` tune the bands.

Rates come from the T0 measurement: 2,810 records in 77.75 min across six nodes (~52k/day,
~1.49 KB/record including indexes, so ~78 MB/day; 30-day retention settles near 2.3 GiB of the
5 GiB PVC).

## What it will not do

The tool is evidence collection, not operation, and the boundary is enforced in code
(`assert_read_only`), not by convention:

* only read kubectl verbs are issued — `apply`, `patch`, `delete`, `scale`, `rollout`, `cp` and
  anything else are refused before `kubectl` is spawned, so it cannot restart a pod or change
  cluster configuration
* the outbox is opened `file:...?mode=ro` with `PRAGMA query_only=ON`; the sample cannot write the
  WAL, prune or delete a record. A pending record is telemetry Redis has not yet acknowledged, and
  it exists only there
* `kubectl exec` is limited to a whitelisted inline snippet, a read-only `ls`/`du`, or a read-only
  `redis-cli` verb; `KEYS` is excluded because it is O(N) on a live cache
* env values whose names look like credentials are redacted, secret-backed env is reported as its
  source, and every free-text field has high-precision decimals blunted before it is written —
  snapshots get pasted into issues, where `tools/coord_guard.py` is not watching

Redis requires authentication and this tool deliberately holds no credential, so `DBSIZE` may come
back `NOAUTH`; that is recorded as a limitation and never graded. The authoritative cache evidence
is the receiver's own ledger, which needs no Redis access.

## Notes for whoever runs the T+7d and T+14d samples

* A `rejected ... message` line is counted but never graded: validation drops a malformed payload
  *before* the outbox commit, so it is an upstream payload-schema defect, not a durability defect.
  It does mean the outbox is not a complete wire-level record.
* `deploy/k8s/hear-mqtt-bridge.yaml` in this repo is the mTLS/8883 variant and does **not**
  describe the live plaintext Deployment. Do not `kubectl apply` it during the soak; the TLS
  cutover is a separate, sequenced workstream. `plaintext_mqtt_preserved` fails loudly if it
  happens anyway.
* Keep the snapshot directories: `record_growth`, `coverage_not_regressed` and the historic-outage
  allowance in `no_cache_failures` all read the T0 baseline.
