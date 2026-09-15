# Standalone migration risk register

Companion to [standalone-migration.md](standalone-migration.md). That document states the
intended end state; this one states what is *currently true* and what would stop the next gate.

Every row is backed by an observation, not by a plan. Where a previously recorded assumption has
been disproven by evidence, the row says so explicitly — a register that carries stale fears is
worse than no register, because it spends review attention on the wrong things.

**Status as of 2026-09-15T14:52Z.** Re-date this document whenever a row changes class, closes,
or a gate is evaluated.

## How to read this

Two classes, and the distinction is the point of the document:

* **Architecture blocker (`ARCH`)** — the migration cannot correctly proceed past a named gate
  until this is resolved. Resolving it changes a contract, a schema, or a durability guarantee.
* **Operational debt (`OPS`)** — the migration can proceed, but the system is carrying avoidable
  exposure. These are scheduled, not gated. An `OPS` row may still be urgent.

`Likelihood` is the chance the risk produces a bad outcome before its exit gate, given current
behaviour. `Impact` is the consequence if it does. `Owner` names the work lane, not a person.

## Correction of previously recorded assumptions

Recording these explicitly, because several were load-bearing in earlier planning:

| Was assumed | Current evidence | Consequence |
|---|---|---|
| Gold and ageev have dead microphones | Disproven. `mic_state: capture-failure` is a latched boot-probe artifact: `selftest_mic_probe()` samples 768 frames immediately after `i2s.begin()` with no settle delay, then freezes the result for the whole boot. Live `/audio`, I2S counters and pin map show working capture. | No microphone replacement. The firmware probe is the defect (R11). |
| Kasami's marginal mic numbers indicate degradation | Disproven. Same latched boot-probe artifact; per-boot trend is improving and PSRAM is octal/healthy. | Do not alter kasami. |
| Phase 0 is partially delivered | Phase 0 code is complete and the design docs are merged. | Phase 0 rows retired from this register. |
| Durable SQLite is a proposal | It is live on real `hear-mqtt-bridge` traffic and carrying the fleet. | Its defects are now *production* defects, not design defects (R1–R3). |
| The 14-day soak started at 13:12 **ET** | The first durable record is `2026-09-15T13:12:59Z` — 13:12 **UTC** (09:12 ET). | The Phase 2 gate date is 2026-09-29T13:12Z, not 2026-09-29 17:12Z. See "Soak validity". |
| Gold's PSRAM mismatch is outstanding | Fixed in the repository (`firmware/hear_node/board_profiles.py` pins gold to the quad profile per node, not per class). | Remaining work is physical, not code (R12). |

## Register

### Architecture blockers

| ID | Risk | Likelihood | Impact | Owner | Evidence | Mitigation | Exit gate |
|---|---|---|---|---|---|---|---|
| R1 | **Device-unscoped durable identity (D1).** `durable_records.record_uid` is `TEXT NOT NULL UNIQUE` with no `device_id` component, so two devices emitting the same `idempotency_key` collide: the second device's record is discarded by `INSERT OR IGNORE`, never published, and the caller is handed the *first* device's payload with HTTP 204. | Medium — requires a key collision across devices; firmware currently makes that unlikely but nothing prevents it, and a node reflash or a key-scheme change makes it likely | **Critical** — silent cross-device data loss with a success-shaped response. No counter, log line or metric fires. Absence of observed collisions is not evidence of absence, because the loss is by construction invisible | `fix-durable-outbox-correctness` | Live schema read from `/state/mqtt-bridge.sqlite3`; audit probe 1 wrote `nyquist/evt-1` then `mach/evt-1` and got ledger rows `[('nyquist',)]` with one Redis write. `docs` audit §3.2 | Scope the uid by device: `_record_uid()` returns `sha256(device_id + key)`; keep the raw key in `idempotency_key`. Postgres target: `UNIQUE (device_id, record_uid, received_at)` | D1a/D1b regressions pass (two devices, same key ⇒ 2 records, 2 publishes, each caller gets its own payload); backfill B03 rescopes existing rows and preserves the legacy value |
| R2 | **Dedupe is not atomic with publish (D2).** Concurrent duplicates in the threaded receiver all read `cached=False` and all publish to Redis. | **Observed in production** | High — duplicate stream entries reach every downstream consumer; any consumer that is not itself idempotent double-counts | `fix-durable-outbox-correctness` | Live: 3303 `cache_attempts` against 3278 `durable_records`; 25 distinct `record_uid`s have more than one `succeeded` attempt (~0.8%) in the first 100 minutes of soak. Audit probe 2: 4 concurrent identical events ⇒ 1 row, 4 Redis pipeline executes | Make claim-then-publish a single atomic step (conditional insert returning claimed/not-claimed) so exactly one caller publishes | D2a (8 concurrent identical ⇒ exactly 1 `xadd`) and D2b (one publish fails ⇒ never both-published) pass; soak duplicate-publish rate reaches 0 |
| R3 | **Deduplicated heartbeats do not refresh liveness TTL (D3).** `_write()` returns early on `stored.cached` without touching Redis, so `setex dama:hear:{node} 30` is never re-armed. Heartbeats without an `idempotency_key` hash the whole payload, so a rebooting node replaying identical `uptime_s`, zeroed counters and `time.valid=false` (hence `ts: null`) emits byte-identical payloads. | Medium-high — the exact reboot-loop shape that triggers it is already present on the fleet (issue #99, and rankine's panic reset below) | High — a node that is healthily heartbeating is declared offline in Redis. This is the *same class* of misleading health flag the redesign postmortem exists to eliminate | `fix-durable-outbox-correctness` | Audit probe 4: identical payload ⇒ identical uid; uid only changes when a field such as `uptime_s` changes. Code path `_write()` `if stored.cached: return` | Split liveness-TTL refresh from event publication: refresh the TTL on every accepted heartbeat, suppress only the stream side effect | D3a (duplicate heartbeat re-arms `setex`, no duplicate stream entry) and D3b (reboot loop never shows offline) pass; soak records no new false `offline` transitions |
| R4 | **Rejection happens before durable persistence, with no refusal row.** The bridge rejects a malformed message and drops it; nothing durable records that the message existed. This directly contradicts the migration rule that raw input is durably retained *before* parsing. | **Observed in production** | High — undetectable ingest loss during exactly the mixed-version window the migration creates. It also makes row conservation unclosable, which is a declared rollback trigger | Phase 1 envelope / `phase2-postgres-schema` | Live bridge log: `rejected hear/event message on dama/mach/telemetry: time must be an object`, ×2 in one pod lifetime. Mach's `hear/event` rows in the outbox: **zero**. Open issue #104 | Persist a quarantine/refusal row (raw bytes, topic, reason, receipt time) before validation can reject; make refusals countable and replayable after a decoder fix | A malformed-message conformance case produces a durable refusal row and a nonzero refusal metric; `hear.ingest.v1` defines the refusal outcome in the response contract |
| R5 | **`esp32s3-i2s-gps` capture-path bias is unmeasured**, so gold, ageev and kasami stay localization-ineligible. Good GNSS/PPS quality does not substitute for microphone/I2S/DMA path calibration. | Certain — this is current state, not a forecast | Medium — blocks TDoA contribution from half the fleet; does **not** block Phases 1–3 | Calibration lane (issue #102) | `hear/nodeclass.py` has no accepted path bias for the class; the 2026-09-14 clap attempt produced MADs of 1.0–2.3 ms, too broad to accept | Co-located reference calibration (microphones within 5–20 cm, 20–30 impulses, ≥12 accepted coincidences), bound to firmware, capture profile and geometry | An accepted calibration artifact exists with fit statistics and a validity interval; `nodeclass` admits the class; until then the typed refusal must remain |
| R6 | **The Phase 3 import source has no backup, snapshot or replica.** `/pool` (PVC `hear-pool`, 9.8 G of corpus, the evidence authority) is `local-path` on a single node with `reclaimPolicy: Delete`. Cluster backups cover only the k3s datastore. | Low per-day, but cumulative over a multi-week migration | **Critical** — an import that goes wrong has no source to roll back *to*. "Rollback = delete the shadow" is only safe while the source is intact | `phase3-object-import-plan` | `kubectl get sc local-path` ⇒ `RECLAIM=Delete`; PVC `hear-pool` `local-path`, node affinity `desktop-bvrdk4j`; storage inventory §8 records zero PVC backup jobs | Back up `/pool` before any import copy begins. `annotations.sqlite3` must be captured with the SQLite backup API, never a raw file copy of a live WAL | A restorable `/pool` backup exists and has been test-restored before the first import byte is written |

### Operational debts

| ID | Risk | Likelihood | Impact | Owner | Evidence | Mitigation | Exit gate |
|---|---|---|---|---|---|---|---|
| R7 | **The soak evidence itself is destructible.** PVC `hear-mqtt-bridge-state` holds the only copy of the 14-day Phase 2 evidence on `local-path` (`reclaimPolicy: Delete`), single-node, unbacked. A PVC delete, a namespace prune or a disk loss destroys the gate evidence irrecoverably and restarts a two-week clock. | Low-medium — it survives pod restarts, but not an object delete or a disk failure | High — not data loss in the platform sense, but total loss of the artifact the gate is waiting on | `phase2-soak-evidence-automation` | PVC listed `local-path`; `local-path-retain` exists in the cluster and is *not* used; no CronJob backs up any PVC | Periodically copy the outbox out of the PVC using the SQLite backup API into dated snapshots; move to `local-path-retain` at the next recreate | Dated off-PVC soak snapshots exist and one has been verified restorable |
| R8 | **Live cluster state is ahead of the repository.** The `Recreate` rollout strategy is applied live (generation 8, one pod) while PR #189 is still open. Any `kubectl apply` from `main` reverts the deployment to `RollingUpdate`. | Medium — routine reconciliation or a redeploy does it accidentally | High — `RollingUpdate` on an RWO SQLite PVC means two writers against one durable ledger during a rollout | `bridge-recreate-strategy-patch` | Live `spec.strategy.type=Recreate`; PR #189 `fix(k8s): use Recreate rollout for the hear MQTT bridge` open | Land #189; treat the drift window as "do not apply bridge manifests from main" | PR #189 merged and the live generation reconciles against the committed manifest |
| R9 | **Runtime package installation at pod start.** The bridge installs `redis==7.4.0` and `paho-mqtt==1.6.1` into `/deps` on every start, and mounts its code from a ConfigMap. | Medium over a 14-day soak | Medium — a restart during a registry outage or a yanked package leaves the durable writer down; the running artifact is not reproducible from a digest | `oci-worker-base-image-plan` | Live bridge log line `installing redis==7.4.0 paho-mqtt==1.6.1 into /deps` | Replace ConfigMap-mounted source and runtime installs with a pinned OCI image; keep the ConfigMap path as rollback | The pilot worker runs from an immutable digest with a verified rollback manifest |
| R10 | **Rankine is silently absent from the soak.** Rankine panicked (`sys.reset: "panic"`) and rebooted at ~14:42Z, and has published **no** telemetry since `2026-09-15T14:25:09Z` despite being HTTP-reachable, Wi-Fi associated at −63 dBm, `mic_state: normal`, GPS 3D with 17 satellites. Its uplink did not recover with the node. | **Occurring now** | High for the gate — the soak's "all six devices" precondition has not held since 14:25Z; it is also live corroboration of the reboot shape R3 mishandles | Fleet lane (issues #99, #103) | Live `/status` on 172.16.100.50 (`uptime_s` ≈ 400 after a 14:25Z last record); outbox `max(received_at)` for rankine frozen at 14:25:09Z while the other five advance to 14:52:02Z | Diagnose the post-panic uplink failure; record the coverage exception against the soak window | Rankine publishes continuously for the remainder of the window, and the panic has a root cause or a documented containment |
| R11 | **The microphone self-test produces false hardware failures.** `selftest_mic_probe()` samples 768 frames immediately after `i2s.begin()` with no settle delay and latches the verdict for the whole boot. Gold and ageev consequently report `mic_state: capture-failure` with healthy hardware. | Certain on affected boards | Medium — it already caused two incorrect hardware conclusions. Any deploy gate or fleet-health check that trusts this flag will refuse healthy nodes | `fleet-hardware-remediation-plan` | Live: gold and ageev report `capture-failure` while kasami and nyquist report `normal` on identical firmware; prior `/audio`, I2S counter and pin-map evidence shows working capture | Add a settle delay and re-probe rather than latching; keep `selftest.mic` for compatibility | A probe change is flashed and gold/ageev report a state consistent with their measured capture |
| R12 | **Gold's quad-PSRAM build is correct in the repo but not on the board.** `board_profiles.py` binds the FQBN to the node (gold = quad) rather than the class, but gold has not been flashed with it. | Certain until flashed | High if mishandled — a quad board that receives the default octal asset is the original failure mode | `gold-quad-flash-readiness` | `firmware/hear_node/board_profiles.py` and `flash.py` comments encode the per-node rule; live gold runs `v0.1.4-123-g5c8a818` and reports no `psram_bus` field (only `v0.1.5` reports it) | Staged flash with artifact/version verification, backup and rollback; never ship gold the class-default asset | Gold runs the quad build and reports `psram_bus: quad`, `psram_fault: false` |
| R13 | **Fleet firmware is fragmented across three versions**: rankine `v0.1.5`, gold/ageev/kasami/nyquist `v0.1.4-123-g5c8a818`, mach `v0.1.4-5-g2355270`. Mach's old build is the direct cause of R4's rejected events. | Certain | Medium — every contract test must hold across three wire behaviours simultaneously, and the oldest one already fails validation | Fleet lane / Phase 1 compat | Live `/status` on all six nodes; `mic_state` present on `v0.1.5` and `v0.1.4-123` but absent on mach | Bring mach forward, or make the envelope accept its shape and record the refusal (R4) | The fleet is on at most two adjacent versions and the envelope compat tests cover both |
| R14 | **Gold has no GNSS sky lock**, and the PMTK boards publish no position. Gold reports `fix: 6` (estimated/dead-reckoning) on 2 satellites; ageev `fix: 1` on 4; kasami `fix: 1` on 5. | Certain until antenna/placement is addressed | Medium — these nodes cannot supply trusted UTC or position, which constrains the envelope's clock tier and keeps them out of precise localization | Fleet lane (issues #101, #108) | Live `/status` GPS blocks on 172.16.100.82/.83/.90 | Antenna and placement remediation; keep emitting `ts: null` when `time.valid` is false (the #185 behaviour) and refuse these nodes for precise localization | Gold reports a real 3D fix, or is formally accepted as a clock-untrusted event node |
| R15 | **The node HTTP surface is unauthenticated**, including `/update` (OTA), `/reboot` and `/format`; push telemetry uses `setInsecure()`; the MQTT bridge runs plaintext without mTLS. | Medium on a trusted LAN, high on any exposure | **Critical if reached** — unauthenticated remote firmware write on every node | Security lane | `SECURITY-REVIEW.md` findings 1–7; live bridge log `MQTT TLS is DISABLED (no --ca-certs configured); connecting in plaintext` | Do not widen exposure during the migration. Authenticate privileged endpoints and move MQTT to 8883 with mTLS and topic ACLs as a scheduled lane | Privileged endpoints require authorization and the bridge negotiates mTLS |
| R16 | **Premature Postgres schema lock.** The Phase 2 schema is being designed against a seam whose idempotency semantics are about to change under R1–R3. | Medium | Medium — a schema frozen on the defective semantics has to be migrated again immediately | `phase2-postgres-schema` | The audit's proposed DDL already carries the D1 fix in `UNIQUE (device_id, record_uid, received_at)`, so the dependency is recognised but not yet discharged | Keep the schema in design until the SQLite seam fixes land and soak | No Postgres implementation begins before the 14-day gate; the schema is re-reviewed against the fixed seam |

## Soak validity

The Phase 2 soak began at `2026-09-15T13:12:59Z` — the first durable record. Nominal gate date
`2026-09-29T13:12Z`.

**Nothing invalidates the soak retroactively, but it cannot count toward the Phase 2 gate in its
current form, and it must be reset once the correctness fixes land.** The reasoning:

* The 14:38:49Z restart for the `Recreate` patch does **not** reset the clock. Ledger continuity
  was verified across it: integrity ok, zero failed attempts, all devices resumed. Restart
  survival is what a soak is supposed to demonstrate.
* **R1–R3 force a reset when they land.** The soak exists to certify the durable outbox's
  idempotency and liveness semantics. Those semantics are known-defective and are about to be
  replaced, including a change to how `record_uid` is derived and a schema-level uniqueness
  change. Evidence gathered under the old semantics cannot certify the new ones. Restart the
  14-day window from the first record written by the fixed build.
* **R2 is already failing a declared soak criterion.** Soak check S03 requires the
  duplicate-publish rate to reach zero; it is currently ~0.8% and structural. The window cannot
  pass as it stands.
* **R10 has already broken the coverage precondition.** Six-device coverage held from 13:12:59Z
  to 14:25:09Z and has not held since. Record this as a coverage exception; if the fixed-build
  window is to claim continuous six-device coverage, rankine must be recovered first.

The current run is therefore best treated as a **pre-gate burn-in**: it is producing real and
useful evidence about PVC behaviour, restart continuity, message cadence and the D2 duplication
rate, and it should continue. It is not the gate window.

Practical consequence: fix R1–R3, recover rankine (R10), give the outbox an off-PVC backup (R7)
and land PR #189 (R8) — then start the counted window.

## Gate summary

| Gate | Blocked by | Not blocked by |
|---|---|---|
| Phase 1 — envelope and conformance suite | R4 (refusal rows must exist in the contract) | R5, R12, R14 |
| Phase 1.5 — OCI images | — | R9 is the lane's own motivation, not a blocker |
| Phase 2 — Postgres implementation | R1, R2, R3 (fix and re-soak), R16 | R5, R11, R12 |
| Phase 3 — object store import | R6 (no backed-up source, no rollback) | R1–R3 |
| Localization eligibility for `esp32s3-i2s-gps` | R5 | every other row |
