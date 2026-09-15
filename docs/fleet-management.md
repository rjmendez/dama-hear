# Fleet management design

## Outcome and boundary

Provide a standalone fleet control plane for DAMA Hear nodes. It owns device
identity, inventory, capability truth, desired/reported state, software delivery
and the lifecycle audit. It is usable by a web UI, CLI, automation, or a future
`dama-gotchi` adapter; none of those clients is required for the control plane
to operate.

This design extends the **Enrollment & Configuration** and **Fleet Health**
services in [API boundaries](api-boundaries.md). It does not put audio, detection
ingest, localization, or calibration computation in the fleet service. It
records their dependencies and consumes their resulting health facts.

### Acceptance criteria

* A node is enrolled with a durable device identity, hardware class, capabilities,
  credential, ownership/site assignment, and immutable audit history.
* Desired state is versioned separately from device-reported state. Reconciliation
  is idempotent and conflict-safe even for intermittently connected nodes.
* Each firmware artifact is signed and declares exactly which hardware classes,
  bootloader/API versions, partition layout, and required capabilities it supports.
  A node rejects an incompatible artifact before writing it.
* A rollout can target a stable site/group snapshot, progress through canary and
  staged cohorts, pause on health gates, and automatically roll a bad cohort back.
* Calibration validity, maintenance state, and decommissioning prevent unsafe
  configuration changes or use in localization rather than merely displaying a
  warning.
* Operators can request bounded diagnostics without obtaining a general remote
  shell; every request and result is audited.

### Non-goals

The first implementation is not an MDM, a mesh-routing controller, a remote
terminal, or a replacement for field USB recovery. It does not OTA arbitrary
bootloaders/partition tables, transfer raw audio, select hardware automatically,
or silently repair a failed calibration.

## Domain model and lifecycle

All records are tenant-scoped. A `site` is a logical operating location and can
contain nested `groups` expressed by labels (`site=...`, `role=...`,
`board_class=...`, `ring=...`). Group membership is evaluated when a command is
created and persisted as a target snapshot; later label edits cannot expand or
shrink an in-flight rollout.

| Resource | Owned facts |
|---|---|
| `device` | Opaque ID, public key/certificate fingerprint, serial/MAC claims, lifecycle state, assigned site, labels, hardware profile reference |
| `hardware_profile` | Board class and revision, MCU, flash/partition layout, bootloader family/version, radio/storage/audio/GNSS/PPS capabilities, supported agent protocol versions |
| `desired_state` | Monotonic revision, configuration document, artifact/channel pin, maintenance intent, calibration requirement and author |
| `reported_state` | Reported desired revision, running artifact digest/version, boot slot, agent/bootloader versions, capabilities, self-test, health summary and timestamp |
| `artifact` | Immutable digest, signed manifest/SBOM, compatibility selector, minimum versions, payload hashes, release provenance and revocation state |
| `campaign` | Target snapshot, ordered stages, gate policy, per-device attempt state, rollback policy and immutable event stream |
| `calibration_binding` | Device sensor/calibration version, valid-from/to, evidence reference, geometry dependency, approval state |
| `diagnostic_job` | Allowed collection set, target, expiry, result references/redaction class and approval |
| `audit_event` | Append-only actor, action, before/after version or digest, request/operation ID, reason and timestamp |

Device states are `unclaimed -> enrolled -> active -> maintenance ->
decommissioning -> decommissioned`. `quarantined` may be entered from any live
state by an enrollment/identity/health policy. State transitions are commands,
require `If-Match` and an audit reason. A decommissioned device never accepts
new desired state or artifact assignments; re-use requires a fresh factory
claim and enrollment, never a state flip.

## Enrollment, inventory, and trust

1. Manufacture or field preparation installs a device key in a protected key
   store and a minimal recovery-capable agent. The board exposes a signed
   hardware attestation: device public key, board class/revision, bootloader,
   partition schema hash and immutable hardware identifiers.
2. An operator creates a short-lived, single-use enrollment token constrained
   to tenant/site and optionally an expected board class. The local USB flow
   may carry network credentials, but credentials are stored device-side and
   never embedded in release images.
3. The agent uses mTLS to call enrollment. The service verifies the token,
   attestation signature, uniqueness of hardware claims, and profile match,
   then issues a device certificate and the initial desired revision.
4. The agent reports inventory and periodic heartbeats. Server-side inventory
   retains both the attested baseline and current report; unexpected change to
   a protected fact quarantines the device and blocks campaigns.

The existing `/status` fields (`node`, `class`, firmware, NVS provenance and
self-test) are an input compatibility adapter. The canonical agent report adds
protocol version, artifact digest, active/pending boot slot, boot count since
activation, profile/partition hashes, and capability map. Missing fields are
**unknown**, not healthy defaults.

Use per-device X.509 certificates with SPIFFE-compatible URI identities such as
`spiffe://dama/tenant/<tenant>/device/<id>`, rotated through an enrollment
credential. Bind enrollment to a public-key proof of possession. This prevents
a copied Wi-Fi secret or a mutable node name from becoming device identity.

## Desired/reported configuration

Desired state is a typed, schema-versioned JSON document, signed by the service
and addressed by `(device_id, revision)`. Its top-level sections are:

```json
{
  "schema": "dama.device-config.v1",
  "sampling": {"profile": "acoustic-48k-v1"},
  "telemetry": {"interval_s": 60},
  "operating": {"mode": "active"},
  "calibration": {"required_version": "cal_01..."},
  "artifact": {"digest": "sha256:..."}
}
```

The server validates fields against the enrolled `hardware_profile`; the agent
validates the same constraints before applying. Per-field ownership is explicit:
the service owns policy/configuration, while the agent owns measured values,
free space, GNSS fix and other observations. A report acknowledges the highest
fully applied desired revision and includes rejected paths with stable reason
codes. Neither side overwrites the other’s document.

The agent fetches or receives a retained desired-state notification, then
reconciles in dependency order: verify maintenance policy, artifact, config,
self-test, calibration eligibility, and finally acknowledgement. Commands use
idempotency keys; document updates use ETags. An offline device simply reports
the old revision until it reconnects, where it converges without replaying
side effects.

## Hardware classes and wrong-image prevention

Board class is not inferred from a friendly name, IP address, or capabilities
that a running application can spoof. The signed `hardware_profile` is the
compatibility authority. Classes include the presently heterogeneous
`xiao-s3-*`, `esp32s3-i2s-gps`, `esp32s3-speaker`, `esp32s3-box3`, `puc-pps`
and `puc-ntp` profiles; their PPS/GNSS/SD differences are capabilities, not
exceptions in rollout code.

Every release contains a signed manifest with:

```text
artifact digest and payload hash; board_class + allowed revisions;
bootloader range; partition_schema_hash; minimum agent protocol;
required/forbidden capabilities; config schema range; SBOM/provenance;
anti-rollback version; signing key ID and revocation status.
```

Admission requires the service selector **and** the agent/bootloader verifier
to match all fields. The server sends the expected artifact digest and profile
hash with the download authorization; the agent verifies HTTPS, the manifest
signature, payload hash, class/revision, partition hash, version floor, and
capacity before writing an inactive A/B application slot. The bootloader repeats
the signature, slot and anti-rollback checks at boot. No class match, unknown
class, unsigned/revoked manifest, missing A/B capacity, or mismatched partition
schema means refusal with an auditable reason -- never a best-effort flash.

This retains and generalizes `flash.py`'s live `/status class` release check:
USB recovery remains explicitly class-selected, and normal OTA cannot bypass
the device-side gate. A manifest is produced for every board-class payload; a
release tag alone is never an image selector.

## OTA campaigns, health gates, and rollback

A campaign pins immutable artifact/config/calibration revisions and the target
snapshot. It has `draft -> scheduled -> running -> paused -> completed |
rolled_back | cancelled` state. Stage progression is deterministic:

1. Validate every target's lifecycle, certificate, profile, compatibility,
   baseline health and calibration dependency; record exclusions rather than
   silently dropping them.
2. Deliver first to one canary per compatible profile/site where available.
   The agent downloads, verifies and writes its inactive slot, then reboots.
3. Wait through a soak window, evaluate gates from fresh reported state and
   independent ingest/health facts, then advance the next bounded percentage
   or pause.
4. Mark a device successful only after it confirms the expected digest,
   desired revision, healthy self-test and stable boot count after the soak.
   Repeated boot/crash or absent confirmation remains `unknown`, not success.

Default gates require no increase in boot failures, no device identity/profile
mismatch, acceptable heartbeat/clock/link/self-test measures for that profile,
and no material regression in accepted telemetry/observations compared with the
pre-stage baseline. Gates are profile-aware: `puc-ntp` cannot be rejected for
having no PPS, while a PPS-required localization role cannot pass eligibility
without one. A campaign may also require a minimum number and distribution of
healthy nodes per site so an update does not remove measurement coverage.

Each candidate image has a known-good predecessor per device. The bootloader
marks a new slot pending, requires the agent to report a healthy boot before a
deadline, and reverts locally if it does not. The campaign controller issues
rollback to all successfully updated devices in the failed stage when a gate
fails, pauses later stages, and preserves evidence. Automatic rollback is
limited to the previous signed compatible artifact; a security revocation,
partition migration, or absent predecessor requires a stopped campaign and
explicit recovery plan, not unsafe automation.

## Health, maintenance, calibration, and diagnostics

Fleet Health derives status from fresh signed agent reports **and** independent
ingest/collector facts. Its states are `healthy`, `degraded`, `unhealthy`,
`offline`, and `unknown`; staleness becomes `unknown`, never `healthy`.
Profile-specific policy maps raw measures (PPS/clock quality, sensor self-test,
storage, power, network, boot rate, config/artifact compliance) to these
states. The current three-layer field procedure remains useful operational
evidence, but the control plane persists the measurements and their sources.

Entering maintenance requires a reason, expiry, actor and optional work order.
It drains or suppresses use in localization, locks incompatible rollouts and
config changes, and advertises `operating.mode=maintenance` to the device.
Expiry returns the device to `active` only after health and calibration
eligibility pass; otherwise it is `degraded`/`quarantined`.

Calibration bindings identify the sensor response and geometry version required
for an operational role. A calibration is invalidated on a profile/sensor
change, explicit field action, expiry, or reported revision mismatch. Desired
state that enables localization must reference a valid, approved calibration
binding. A rollout that changes a calibration-sensitive artifact/config either
proves compatibility in its manifest or stops after maintenance pending a new
calibration. It must not leave a node apparently active with unknown geometry.

Diagnostics are queued command resources with strict allowlists: status,
bounded/redacted logs, self-test, network metrics, storage summary, GNSS/PPS
statistics, and a short diagnostic capture where policy permits. They have
target/site authorization, byte/time limits, expiry, cancellation and signed
results stored by reference. There is no arbitrary command execution, shell,
credential readback or unrestricted audio retrieval.

## Decommissioning and audit

Decommissioning first blocks campaigns and operational use, retains the final
reported inventory/health, revokes device certificates/download grants, and
requests a signed credential wipe. The device becomes `decommissioned` only
after wipe acknowledgement or an explicitly recorded physically-unreachable
exception. Retention policies preserve the audit trail and only the minimum
diagnostic/telemetry evidence required by policy; site access and labels are
removed.

Audit events are append-only, immutable and exportable. They cover enrollment,
identity/profile changes, desired-state changes, artifacts/signature decisions,
campaign stage/gate/rollback decisions, maintenance, diagnostics, calibration
approval/invalidation, credential rotation and decommissioning. Include
actor/service identity, delegated authorization, reason/ticket, request and
correlation IDs, target snapshot hash, and before/after revisions/digests.
Write to a transactionally coupled event/outbox stream and periodically
checkpoint hashes to tamper-evident object storage.

## APIs and open components

Keep REST/JSON under `/v1` for operator and automation commands; use protobuf
over gRPC for enrollment and agent control/reporting. Publish OpenAPI,
protobuf descriptors and JSON Schemas. Use CloudEvents 1.0 for durable events:
`dama.device.enrolled.v1`, `dama.device.reported.v1`,
`dama.desired-state.changed.v1`, `dama.campaign.stage.v1`,
`dama.campaign.gate-failed.v1`, and `dama.device.decommissioned.v1`.
MQTT 5 with mutual TLS is suitable for constrained node notification/report
transport (retained desired-state revision, QoS 1); it is not the public
control API and must not be the sole durable source of truth.

Representative commands:

```text
POST /v1/enrollment-tokens
POST /v1/devices:enroll
GET  /v1/devices?label=site:alpha
PATCH /v1/devices/{id}/desired-state
POST /v1/artifacts
POST /v1/campaigns
POST /v1/campaigns/{id}:pause
POST /v1/devices/{id}:maintenance
POST /v1/devices/{id}/diagnostics
POST /v1/devices/{id}:decommission
GET  /v1/audit-events
```

Recommended open building blocks:

| Concern | Component/protocol | Why |
|---|---|---|
| Identity | SPIFFE/SPIRE, X.509 mTLS, OIDC | Workload/device identities and least-privilege operator access |
| Device transport | MQTT 5, gRPC/protobuf, HTTPS | Constrained notifications plus typed control and resumable artifact downloads |
| Artifact security | Sigstore Cosign, in-toto/SLSA provenance, SPDX or CycloneDX SBOM | Signed/verifiable releases and key revocation |
| OTA agent | Eclipse hawkBit DDI or Mender client concepts; RAUC/SWUpdate with MCUboot on A/B-capable targets | Mature campaign/agent patterns; retain the DAMA profile verifier as the final authority |
| Broker/events | NATS JetStream or MQTT broker plus CloudEvents | Durable commands/facts with replay and decoupled services |
| Policy/audit | Open Policy Agent, PostgreSQL outbox, WORM/S3-compatible object storage | Explicit admission/gate policy and durable evidence |
| Observability | OpenTelemetry, Prometheus, Loki | Gate inputs, device telemetry and bounded diagnostic logs |

Adopt components behind service interfaces. Do not couple the public resource
model to a particular OTA vendor, MQTT broker, or UI.

## MVP cut

Ship the narrowest safe end-to-end path for the current hear-node firmware:

1. PostgreSQL-backed device/profile/inventory/desired/reported/audit records,
   REST control API, and mTLS enrollment with a one-time token.
2. A signed `hardware_profile` registry for the existing board classes, plus a
   signed per-class artifact manifest and agent-side class/partition/digest
   verification. Keep the existing USB/live-status safeguard as recovery.
3. MQTT 5 or HTTPS polling for desired revision and signed reports; report
   artifact digest, config revision, boot slot/count, NVS provenance, profile
   hash, self-test and existing health fields.
4. One artifact-only campaign with target snapshot, canary then percentage
   stages, fresh heartbeat/self-test/boot-loop/config-digest gates, pause and
   local A/B rollback to the previous compatible image.
5. Site labels, time-bounded maintenance, read-only bounded status/log/self-test
   diagnostics, calibration eligibility flag, decommission certificate
   revocation, and append-only audit export.

Defer dynamic groups, multi-artifact bundles, delta images, bootloader/partition
migrations, delegated site administration, automatic remediation other than
A/B rollback, offline USB orchestration, full SIEM integration, arbitrary
diagnostics, and a fleet UI. These are useful only after the MVP demonstrates
that enrolled devices can safely reject wrong images and that a staged campaign
can prove or reverse a failed change.
