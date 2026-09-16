# Durable-outbox soak runbook (hear-mqtt-bridge)

`deploy/hear-mqtt-bridge` writes every accepted telemetry record to a SQLite outbox on a dedicated
`hear-mqtt-bridge-state` PVC mounted at `/state` before it touches the Redis cache, and replays
anything Redis has not acknowledged. Phase 2 is the soak that proves this holds over time rather
than over a minute: the same evidence is collected at T0, T+24h, T+7d and T+14d and compared.

`tools/bridge_soak_evidence.py` collects and grades that evidence, one sample at a time, against
the cluster it is reading. `tools/soak_evidence_validate.py` then validates the whole *series*
offline, from the collected snapshot files only, so a day-1 / day-7 / day-14 review is reproducible
by someone who cannot read the cluster.

The authoritative baseline is **T0 = `2026-09-15T18:26:22Z`**, the corrected durable-outbox
semantics rollout. Every snapshot records it; a snapshot that names a different baseline is not
part of this soak.

Coverage is **five nodes** — Nyquist, Mach, Kasami, Ageev and Gold. **Rankine is excluded**, and
the exclusion is carried in every snapshot with its reason (offline pending physical USB recovery,
`docs/fleet-hardware-remediation-2026-09-15.md`). Five-node coverage is never six-node coverage,
and an absent node is never silently absent.

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
* **coverage** — which of the five in-soak nodes appear in the ledger, which do not, and which
  nodes are deliberately excluded and why
* **receiver / refusals / cache** — the `hear-heartbeat` receiver's own `durable_store` health; the
  refusal quarantine as its *own* surface (stored refusals, row/byte caps, evictions, suppressed
  floods); Redis `DBSIZE`/`XLEN` and the per-node `TTL` of each `dama:hear:<node>` key where the
  cache can be reached
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
| `all_nodes_covered` | all five in-soak device ids present |
| `excluded_nodes_documented` | every excluded node carries a reason and is not also expected |
| `soak_baseline_declared` | the snapshot names the authoritative baseline |
| `refusal_caps_hold` | stored refusals are inside the row cap (graded separately from durability) |
| `cache_ttl_armed` | every heartbeat key is volatile and within `HEAR_HEARTBEAT_TTL_S` |
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

Rates come from the pre-correction measurement: 2,810 records in 77.75 min across six nodes
(~52k/day, ~1.49 KB/record including indexes). The soak runs five nodes, so the default expected
rate is pro-rated to **43,300 records/day** (~65 MB/day; 30-day retention settles well inside the
5 GiB PVC). `--records-per-day` overrides it if the fleet changes again.

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

## Reproducible day-1 / day-7 / day-14 review

`tools/bridge_soak_evidence.py` can only grade the sample in front of it. The scheduled reviews ask
questions that span samples, and they must be answerable later, from the files, with no cluster
access at all:

    python3 tools/soak_evidence_validate.py --series-dir ~/bridge-soak --review day-1
    python3 tools/soak_evidence_validate.py --series-dir ~/bridge-soak --review day-14 --require-pass

It reads `snapshot.json` files (or their dated directories), orders them by capture time and grades
the series. It runs no subprocess, opens no socket and reads no database — the same review run on
the same files a year later produces the same verdict.

| Series check | Fails when |
|---|---|
| `baseline_declared` / `baseline_sample_present` | a snapshot names another baseline, or T0 was not taken at `2026-09-15T18:26:22Z` |
| `milestone_labels_distinct` / `required_milestones_present` | a milestone is duplicated, unrecognised or missing for the review being run |
| `milestone_timing` / `series_is_chronological` | a sample is labelled as a milestone it was not taken at, or the series is out of order |
| `record_accounting_consistent` | `pending` exceeds `records`, or acknowledged records are not backed by successful cache attempts |
| `accepted_records_conserved` | the ledger shrank without the prune that would explain it |
| `per_node_records_conserved` | a node's record count went backwards outside a prune |
| `pending_drained_at_every_sample` | any snapshot carries a backlog |
| `cache_failures_did_not_grow` | cache failures grew after the baseline |
| `receiver_pending_drained` | the receiver's own ledger held records |
| `expectation_stable` / `all_expected_nodes_covered` | the expected fleet was redefined mid-soak, or a node stopped publishing |
| `exclusions_documented` / `exclusions_stable` | an exclusion has no reason, a node is both expected and excluded, an undeclared device appears, or the excluded set changed mid-soak |
| `cache_ttl_volatile` / `cache_ttl_rearmed` | a heartbeat key lost its expiry, exceeded the TTL bound, vanished while its node kept writing, or stopped being re-armed across the series |
| `refusal_caps_hold` / `refusals_separate_from_health` | stored refusals passed their cap, or refusal counters were folded into the durable health surface |
| `ledger_identity_stable` / `ledger_not_restarted` | the database file, journal mode, schema version or table set changed, or the record window restarted instead of advancing |
| `state_volume_stable` / `writer_never_restarted` | the PVC was rebound or unbound, or the bridge pod restarted |
| `code_digest_stable` / `deployment_identity_stable` / `durable_semantics_stable` | the code ConfigMap digest, image, strategy, generation, durable env or plaintext MQTT identity changed mid-soak |

The last row is the one that quietly ruins a soak: a re-rollout during the window restarts the
semantics while the milestone labels keep counting. When it fails, the finding is not "fix the
tool" — it is *record a new baseline and start the soak again*.

Missing evidence grades `unknown`, never `pass`; the series verdict is then `incomplete`.
`--require-pass` exits 2 on anything that is not `pass`, and an unreadable or foreign file exits 1
rather than being half-graded.

A refusal flood on its own never fails the durable verdict: refused input is dropped by validation
*before* the outbox commit, is bounded by the receiver's row/byte caps and rate limiter, and is
reported on its own surface. What the series checks is that those caps held and that the two
surfaces stayed separate.
