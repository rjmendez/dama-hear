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
| [decisions/](decisions/) | Numbered, append-only decision records. One per contract or layout decision: what was decided, what evidence forced it, and what it means for forward/rollback/mixed-version behaviour. |
| [deployment.md](deployment.md) | Progressive deployment profiles (single machine/Compose, small HA cluster, multi-site Kubernetes) sharing one image, config schema, and data layout. |
| [fleet-management.md](fleet-management.md) | The standalone fleet control plane: device identity, desired vs reported state, signed firmware compatibility, staged rollouts, and lifecycle audit. |
| [ml-lifecycle.md](ml-lifecycle.md) | The minimal self-hosted acoustic ML lifecycle: corpus and retention, labelling, reproducible training, promotion, edge deployment, and rollback. |
| [phase0-freeze-contracts.v1.md](phase0-freeze-contracts.v1.md) | The Phase 0 contract freeze: the hashed, reproducible inventory of wire profiles, schemas, and firmware build metadata that later phases must not silently break. |
| [adapter-conformance.md](adapter-conformance.md) | The Phase 1 suite every ingress adapter must pass with no live service, and the merge-blocking recoupling import boundary over `hear/` and `modules/`. |
| [durable-postgres-schema.md](durable-postgres-schema.md) | The Phase 2 canonical Postgres schema for the durable heartbeat/event outbox: device-scoped identity, claim leases, partitions and retention, O(1) health, access control, backfill and rollback. Design and DDL only; not deployed. |
| [phase2-postgres-migration-plan.md](phase2-postgres-migration-plan.md) | The Phase 2 execution plan for moving the durable outbox onto that schema: the measured SQLite→Postgres data map, the expand/migrate/verify/contract stages, feature flags, write ordering and dual-write error semantics, backfill watermarks, hash reconciliation, shadow compare, observability, rollback, and the entry/exit gates. Plan only; nothing is provisioned or cut over. |
| [object-store-backend.md](object-store-backend.md) | The Phase 3 object-store backend capability audit: what `hear/objectstore/backend.py` demands of a store, the measured host limits behind it (the ext4 root is a VHDX on a disk with 52 GB left, not the 240 G `df` reports), probed filesystem semantics on both candidate roots, a backend comparison grounded in upstream evidence, the importer gaps that blocked a live run whichever backend wins (streaming, the client-side encryption boundary, atomic lease acquisition and the republish path — now closed in `hear/objectstore/`), and the choices still owed to an operator. Audit only; no backend is selected, provisioned or configured. |
| [decisions/](decisions/) | Architecture decision records: [0001](decisions/0001-hear-ingest-v1-envelope-and-codec.md) the `hear.ingest.v1` envelope and codec; [0002](decisions/0002-contract-repository-layout.md) the canonical home for published contracts; [0003](decisions/0003-phase2-durable-outbox-postgres-cutover.md) the Phase 2 durable-outbox cut-over (dual-write, backfill, reversal). |
| [REDESIGN-LESSONS.md](REDESIGN-LESSONS.md) | Postmortem of the existing fleet: observed failure modes and the systems-design lessons that constrain the redesign. Evidence-tagged. |
| [loci-memory-validation.md](loci-memory-validation.md) | Validation of the Loci spatial/memory behaviour used by the localization lane. |

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
| [bridge-durable-soak.md](bridge-durable-soak.md) | Durable-outbox soak runbook for `hear-mqtt-bridge`: what `tools/bridge_soak_evidence.py` records, the T+24h/7d/14d pass/fail criteria, and the read-only boundary it enforces. |

## Operations, policy, and cross-cutting design

| Document | What it defines |
|---|---|
| [analytics.md](analytics.md) | The analytics layer above geometry and detectors: event model, modality naming rules, correlation, tracks, geofences, and alerts with provenance back to raw evidence. |
| [cost-capacity.md](cost-capacity.md) | The fleet sizing model: uplink, storage, and clip/audio cost formulas with stated default variables, to be recalculated per deployment. |
| [data-governance.md](data-governance.md) | Self-hosted acoustic data governance: data classes, recording indicators, retention, access, export, and audit controls for captured and derived data. |
| [resilience.md](resilience.md) | Failure-mode design: the invariants that must survive, degraded-mode behaviour per dependency, and how operators learn about failure without reading a stack trace. |

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
