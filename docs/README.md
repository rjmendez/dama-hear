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
| [REDESIGN-LESSONS.md](REDESIGN-LESSONS.md) | Postmortem of the existing fleet: observed failure modes and the systems-design lessons that constrain the redesign. Evidence-tagged. |
| [loci-memory-validation.md](loci-memory-validation.md) | Validation of the Loci spatial/memory behaviour used by the localization lane. |

## Decision records

Numbered, dated records of decisions that constrain later phases. A decision record states
what was chosen, the in-tree evidence it rests on, and what was deliberately left open.

| Record | What it decides |
|---|---|
| [0001 - `hear.ingest.v1` envelope and codec](decisions/0001-hear-ingest-v1-envelope-and-codec.md) | The canonical ingest envelope as a codec-agnostic logical schema, UTF-8 JSON as the only normative v1 wire codec, and a closed identity tuple for `event_id`. |
| [0002 - Phase 4 HTTPS batch ingest adapter](decisions/0002-phase4-https-batch-ingest-adapter.md) | An additive HTTPS batch ingest path: frame versioned separately from items, receipt-based contiguous acknowledgement, identity-based replay safety, and no broker or queue at this scale. |
| [phase4-https-batch-ingest.md](phase4-https-batch-ingest.md) | The operational specification behind 0002: API and auth, sizing and limits, retry/offline durability, timestamp and version semantics, refusal visibility, TLS/key lifecycle, observability, dual-write comparison and rollback. |

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
