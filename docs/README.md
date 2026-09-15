# DAMA Hear documentation index

The documents in the first section below are the **agreed target architecture** for the
migration of DAMA Hear to a standalone, self-hosted acoustic platform — a system that runs
without AWS, without a shared PVC as an interchange, and without `dama-gotchi` as a
dependency. Phase 0 (contract freeze and baseline) is implemented and merged; Phase 1 and
later phases are in progress. These are design records, not runbooks: where a design and the
current code disagree, the design states the intended end state.

## Migration and target architecture

| Document | What it defines |
|---|---|
| [standalone-migration.md](standalone-migration.md) | The phased, reversible expand/migrate/verify/rollback path from the current AWS + PVC + Redis coupling to a site-owned platform. Start here. |
| [migration-risk-register.md](migration-risk-register.md) | The dated, evidence-backed register of what is currently true: architecture blockers versus operational debts, their owners, exit gates, and which of them gate the next phase. |
| [api-boundaries.md](api-boundaries.md) | The northbound service map and contract conventions: which service owns which durable resource, its API style, and the events it publishes. |
| [repository-structure.md](repository-structure.md) | The target repository layout and the extraction plan: one platform monorepo, `dama-gotchi` kept as a separate consumer application. |
| [deployment.md](deployment.md) | Progressive deployment profiles (single machine/Compose, small HA cluster, multi-site Kubernetes) sharing one image, config schema, and data layout. |
| [fleet-management.md](fleet-management.md) | The standalone fleet control plane: device identity, desired vs reported state, signed firmware compatibility, staged rollouts, and lifecycle audit. |
| [ml-lifecycle.md](ml-lifecycle.md) | The minimal self-hosted acoustic ML lifecycle: corpus and retention, labelling, reproducible training, promotion, edge deployment, and rollback. |
| [phase0-freeze-contracts.v1.md](phase0-freeze-contracts.v1.md) | The Phase 0 contract freeze: the hashed, reproducible inventory of wire profiles, schemas, and firmware build metadata that later phases must not silently break. |
| [adapter-conformance.md](adapter-conformance.md) | The Phase 1 suite every ingress adapter must pass with no live service, and the merge-blocking recoupling import boundary over `hear/` and `modules/`. |
| [durable-postgres-schema.md](durable-postgres-schema.md) | The Phase 2 canonical Postgres schema for the durable heartbeat/event outbox: device-scoped identity, claim leases, partitions and retention, O(1) health, access control, backfill and rollback. Design and DDL only; not deployed. |
| [phase2-postgres-migration-plan.md](phase2-postgres-migration-plan.md) | The Phase 2 execution plan for moving the durable outbox onto that schema: the measured SQLite→Postgres data map, the expand/migrate/verify/contract stages, feature flags, write ordering and dual-write error semantics, backfill watermarks, hash reconciliation, shadow compare, observability, rollback, and the entry/exit gates. Plan only; nothing is provisioned or cut over. |
| [object-store-backend.md](object-store-backend.md) | The Phase 3 object-store backend capability audit: what `hear/objectstore/backend.py` demands of a store, the measured host limits behind it (the ext4 root is a VHDX on a disk with 52 GB left, not the 240 G `df` reports), probed filesystem semantics on both candidate roots, a backend comparison grounded in upstream evidence, the importer gaps that blocked a live run whichever backend wins (streaming, the client-side encryption boundary, atomic lease acquisition and the republish path — now closed in `hear/objectstore/`), and the choices still owed to an operator. Audit only; no backend is selected, provisioned or configured. |
| [decisions/](decisions/) | Numbered, append-only decision records. One per contract or layout decision: what was decided, what evidence forced it, and what it means for forward/rollback/mixed-version behaviour. [0001](decisions/0001-hear-ingest-v1-envelope-and-codec.md) the `hear.ingest.v1` envelope and codec; [0002](decisions/0002-contract-repository-layout.md) the canonical home for published contracts; [0003](decisions/0003-phase2-durable-outbox-postgres-cutover.md) the Phase 2 durable-outbox cut-over (dual-write, backfill, reversal); [0004](decisions/0004-phase4-https-batch-ingest-adapter.md) the Phase 4 additive HTTPS batch ingest adapter. |
| [REDESIGN-LESSONS.md](REDESIGN-LESSONS.md) | Postmortem of the existing fleet: observed failure modes and the systems-design lessons that constrain the redesign. Evidence-tagged. |
| [loci-memory-validation.md](loci-memory-validation.md) | Validation of the Loci spatial/memory behaviour used by the localization lane. |

## Decision records

Numbered, dated records of decisions that constrain later phases. A decision record states
what was chosen, the in-tree evidence it rests on, and what was deliberately left open.

| Record | What it decides |
|---|---|
| [0001 - `hear.ingest.v1` envelope and codec](decisions/0001-hear-ingest-v1-envelope-and-codec.md) | The canonical ingest envelope as a codec-agnostic logical schema, UTF-8 JSON as the only normative v1 wire codec, and a closed identity tuple for `event_id`. |
| [0002 - contract repository layout](decisions/0002-contract-repository-layout.md) | The canonical home for a published contract: schema, fixtures, generator, and the decision record that names it. |
| [0003 - Phase 2 durable outbox Postgres cut-over](decisions/0003-phase2-durable-outbox-postgres-cutover.md) | The dual-write, backfill and reversal plan for moving the heartbeat outbox from SQLite to Postgres. |
| [0004 - Phase 4 HTTPS batch ingest adapter](decisions/0004-phase4-https-batch-ingest-adapter.md) | An additive HTTPS batch ingest path: frame versioned separately from items, receipt-based contiguous acknowledgement, identity-based replay safety, and no broker or queue at this scale. |
| [0005 - Phase 4 dual-write observability and reconciliation](decisions/0005-phase4-dual-write-observability.md) | How a future legacy + HTTPS dual-write is observed: recorded join key, a closed classification vocabulary, grace-window `pending`, and redacted mismatch receipts. |
| [0006 - `HEAR_ADMIN_TOKEN` scope, custody and lifecycle](decisions/0006-admin-token-provisioning-policy.md) | Per-node versus fleet-wide admin tokens, authority of record, enrollment injection, rotation/revocation, lost-token recovery, redaction rules, and the tooling changes each option implies. Generates no credential. |
| [phase4-https-batch-ingest.md](phase4-https-batch-ingest.md) | The operational specification behind 0004: API and auth, sizing and limits, retry/offline durability, timestamp and version semantics, refusal visibility, TLS/key lifecycle, observability, dual-write comparison and rollback. |

## System and subsystem references

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | Current system overview. |
| [acoustic-stack.md](acoustic-stack.md) | The acoustic processing stack end to end. |
| [clip-pipeline.md](clip-pipeline.md) | Clip capture, drain, indexing, and retention. |
| [timing.md](timing.md) | Clocking, boot-relative time, and trusted UTC. |
| [uplink.md](uplink.md) | Node uplink transports and envelopes. |
| [esp32s3-lora-node.md](esp32s3-lora-node.md) | ESP32-S3 LoRa node design. |
| [node-hardware.md](node-hardware.md) | Node hardware reference. |
| [faketec-pin-budget.md](faketec-pin-budget.md) | Board pin budget. |
| [l86-reference.md](l86-reference.md) | L86 GNSS module reference. |
| [hear-latency-calibration-runbook.md](hear-latency-calibration-runbook.md) | Latency calibration procedure. |
| [pool-backup-restore.md](pool-backup-restore.md) | The backup and restore design for PVC `dama/hear-pool` and its explicit G0 acceptance test: measured storage facts (including that the node's ext4 root is a VHDX on a 95 %-full Windows disk), why no snapshot primitive exists here, generational encrypted archives to a second physical disk, per-class consistency rules for append-only JSONL and a live-WAL SQLite database, key custody, retention, and the restore-isolation boundary. Phase 3 import is blocked until its G0 passes. |
| [bridge-durable-soak.md](bridge-durable-soak.md) | Durable-outbox soak runbook for `hear-mqtt-bridge`: what `tools/bridge_soak_evidence.py` records, the T+24h/7d/14d pass/fail criteria, and the read-only boundary it enforces. |

## Operations, policy, and cross-cutting design

| Document | What it defines |
|---|---|
| [analytics.md](analytics.md) | The analytics layer above geometry and detectors: event model, modality naming rules, correlation, tracks, geofences, and alerts with provenance back to raw evidence. |
| [cost-capacity.md](cost-capacity.md) | The fleet sizing model: uplink, storage, and clip/audio cost formulas with stated default variables, to be recalculated per deployment. |
| [data-governance.md](data-governance.md) | Self-hosted acoustic data governance: data classes, recording indicators, retention, access, export, and audit controls for captured and derived data. |
| [resilience.md](resilience.md) | Failure-mode design: the invariants that must survive, degraded-mode behaviour per dependency, and how operators learn about failure without reading a stack trace. |
| [durable-outbox-failure-drill-runbook.md](durable-outbox-failure-drill-runbook.md) | Maintenance-window procedure for the `hear-mqtt-bridge` durable-outbox induced-failure drill: gate, prechecks, backup, scoped fault injection, expected observations, abort triggers, recovery order, evidence receipt, and pass criteria. |
| [ota-release-credentials.md](ota-release-credentials.md) | The tokenless-release incident, the firmware secret audit, and the NVS credential contract that keeps published images secret-free without making a node OTA-unrecoverable. |
| [release-v0.1.6-readiness.md](release-v0.1.6-readiness.md) | The pre-cut gate for v0.1.6: required merged prerequisites, artifact variants and the `fs_acquisition_hz: 48000` manifest check, the unprovisioned-vs-fleet-ready public asset contract, operator-selected admin-token provisioning, pre-cut checks, per-node rollout order and no-go gates, the deliberate `nyquist` rollback receipt, and failure/rollback procedures. Not executed. |

## Field findings and calibration records

Dated, point-in-time evidence; superseded only by a later dated record.

* [findings-2026-09-05.md](findings-2026-09-05.md), [findings-2026-09-06.md](findings-2026-09-06.md)
* [crack-blast-2026-09-05.md](crack-blast-2026-09-05.md)
* [field-pull-2026-09-08.md](field-pull-2026-09-08.md)
* [localisation-2026-09-08.md](localisation-2026-09-08.md)
* [placement-weekend-2026-09-08.md](placement-weekend-2026-09-08.md)
* [clip-calibration-2026-09-10.md](clip-calibration-2026-09-10.md)
* [findings-clap-calibration-2026-09-14.md](findings-clap-calibration-2026-09-14.md)
* [validation-full-captures.md](validation-full-captures.md)
