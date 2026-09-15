# Reversible migration to a standalone self-hosted platform

Status: design only. This document defines the expand, migrate, verify and rollback path; it
does not authorize removing AWS, dama-gotchi, Redis or the shared PVC.

## Scope and target

The current system is already split conceptually into node firmware, the `hear/` platform,
sound modules, and deployment workers, but its operational seams are accidental:

```text
node -> WiFi/MQTT or AWS API -> Lambda/SQS -> cluster MQTT
     -> hear-mqtt-bridge -> Redis (heartbeat/event state)
node -> HTTP drain -> shared hear-pool PVC
dama-gotchi phone lane -> shared PVC / MQTT topic -> hear pool
```

The target is a site-owned, independently operable platform:

```text
node/adapter -> canonical ingress -> durable event journal + object store
                               -> PostgreSQL metadata/job state
                               -> replayable workers -> API/UI/export
```

The first supported profile is the repository's single-machine Compose profile: PostgreSQL,
content-addressed local object storage behind an S3-compatible interface, Mosquitto, and
optional disposable Redis. The same contracts must deploy to the existing k3s profile and later
to HA PostgreSQL/object storage/MQTT. PVCs become staging/cache only; no consumer reads another
workload's application directory directly.

## Dependency inventory and ownership

| Current dependency | Current use and coupling | Standalone disposition | Owner of the boundary |
|---|---|---|---|
| ESP32 node SD card/HTTP | Durable `dets`, `scene`, `health`, clips; `hear_drain.py` discovers files, archives raw bytes and content-deduplicates | Keep as a node protocol and backfill source; add cursor/range capability later | `hear-node` adapter |
| Node WiFi/HTTP | `/status`, `/ls`, `/sd`, `/audio`, OTA and drain | Keep for local recovery and bulk pull; never make live capture depend on it | `node-http` adapter |
| MQTT | Node/gotchi transport and current `dama/<device>/telemetry` fan-in | Keep as one transport into canonical ingress, not as a store | `mqtt-ingress` adapter |
| AWS API Gateway/Lambda/SQS/Oxalis forwarder | Current public ingest path for telemetry when the cluster cannot reach nodes | Optional compatibility adapter; no new canonical data is written only there | `gotchi-cloud` adapter |
| Redis | Short-TTL heartbeat keys, latest event, bounded event stream; used by receiver/bridge and health views | Retain only as cache/coordination. Canonical heartbeat/event records must be durable and replayable elsewhere | `redis-cache` adapter |
| Shared `hear-pool` PVC | Corpus, raw archive, scores, clips, Python libraries; dama-gotchi's sketch corpus writes into a path that drain reads | Import it once into object storage/PostgreSQL; keep a read-only snapshot during cutover, then isolate per-service volumes | `pool-import` adapter |
| k3s/ConfigMaps/CronJobs | Worker packaging and schedules; generated bundles have drift and size/apply hazards | Package immutable OCI images and Compose/Helm from one release manifest; no runtime `pip install` into PVC | platform release |
| PostgreSQL/object storage | Described by `docs/deployment.md` but not yet the current source of truth | Make them canonical, with backup/restore and idempotent replay | standalone platform |
| dama-gotchi phone/GNSS/sketch corpus | Optional producer sharing MQTT topics and the pool; its records use the same sketch concepts but not the node identity/clock guarantees | Keep as an opt-in adapter that submits canonical envelopes; never require gotchi for node operation | `gotchi-adapter` |

Current evidence that affects the plan: Redis state is explicitly short-TTL
(`tools/hear_heartbeat_receiver.py`); the bridge must ignore gotchi messages on the shared topic
(`tools/hear_mqtt_bridge.py`); the pool is content-addressed and records raw files before parsing
(`hear/pool.py`, `tools/hear_drain.py`); and the live drain/PVC contract has already drifted from
the checked-in manifest (`deploy/k8s/README.md`). These are migration risks, not reasons to copy
the current topology into the new platform.

## Dedicated dependency-cut analysis

The cut is complete only when a clean-room site can capture, persist, replay, solve and restore
node data with no dama-gotchi deployment, AWS/Oxalis account, shared Redis, shared PVC, phone
producer or Android collector. The following are the long-term dependencies found in the current
tree; each has an explicit disposition rather than remaining an undocumented assumption.

| Dependency | Evidence in current tree | Cut decision |
|---|---|---|
| **dama-gotchi phone corpus** | `deploy/k8s/hear-drain.yaml` passes `--phone-corpus /pool/sketch_corpus`; its comments identify gotchi's `dama-sketch-corpus` as the other writer on the same PVC. | Optional import adapter. A node-only site omits the flag and still has a complete capture/solve/restore path. |
| **dama-gotchi Android sketch port** | `tools/gen_golden.py` and `tests/test_sketch_port.py` require byte agreement with `AcousticSketch.kt`/`SketchWindow.kt`. | Keep golden vectors as a compatibility consumer, not an import in `hear/`. Android changes require a new fixture/version and cannot redefine a core profile. |
| **Phone schemas and `gotchi-phone` identity** | `hear/solve/point.py` and `tools/hear_tdoa.py` use `source="phone"` and the `gotchi-phone` fallback class; phone rows have wall-clock/unsurveyed behavior. | Normalize through `hear.ingest.v1` with `source=gotchi`, explicit clock tier and optional survey. Phone fields are never required for node records, and wall time is never treated as PPS time. |
| **AWS/Oxalis ingest chain** | `tools/hear_mqtt_bridge.py` and `deploy/k8s/hear-mqtt-bridge.yaml` document API Gateway -> Lambda -> SQS -> Oxalis SQS -> `dama-sqs-consumer.py` -> local MQTT. | `aws-compat`/`gotchi-cloud` adapter only. Canonical persistence also accepts local HTTP/MQTT and offline import; no worker calls AWS or Oxalis. |
| **Shared MQTT topic and foreign phone traffic** | The bridge subscribes to `dama/+/telemetry` and must ignore gotchi messages sharing that topic pattern. | Transport-specific ingress. Topic names, filtering and timestamp normalization stay in the adapter; the core receives canonical envelopes only. |
| **Shared Redis host, secret and key namespace** | Receiver and bridge default to `audit-redis.infra.svc.cluster.local` and write `dama:hear:*` keys/streams. | Site-owned cache/lease adapter with a unique prefix and ACL. Redis loss must not lose durable observations, block replay, or change solver results. |
| **Shared `hear-pool` PVC** | Drain declares `hear-pool`; score/tag lanes mount it, and gotchi writes the phone corpus into it. Runtime `pip install` also writes under `/pool`. | One-time `pool-import` into object storage plus durable metadata. After cutover, no workload reads another workload's application directory; PVCs are per-service staging/cache only. |
| **Hard-coded node addresses and host networking** | Drain embeds `name=172.16.100.x`; heartbeat/bridge use `hostNetwork` and the bridge uses `127.0.0.1:31883`. | Site configuration owned by the node/transport adapter. Discovery, broker address and network mode are deployment details, not core APIs. |
| **Cron cadence and overlap semantics** | Drain runs every 15 minutes with `Forbid`; comments document skipped ticks and separate hourly checks. | Scheduler is an operational adapter. Core requires source cursors, freshness and replay; it must not infer health from a CronJob exit code or schedule. |
| **Node SD/HTTP behavior** | `tools/hear_drain.py` depends on `/status`, `/ls`, `/sd`, rolling files, clips and bounded tails. | Keep as a recovery/backfill adapter. Live capture must survive HTTP, Wi-Fi, broker and cloud outages by recording locally and replaying later. |
| **Mutable ConfigMaps and runtime package/model installs** | Kubernetes code bundles mount Python files from ConfigMaps and install `numpy`, `redis` and `paho-mqtt` into PVC/`emptyDir` paths. | Forbidden in the standalone release. Images, lockfiles, model cards and schemas are immutable and digest-pinned. |
| **Survey/site and clock assumptions** | TDoA requires survey/origin; wire v2 requires explicit profile geometry and per-frame UTC unwrapping. | Core-owned contracts. Missing survey or untrusted clock is a typed state/refusal, never an invented coordinate or timestamp. |

### Standalone core boundary

The core is the dependency-free domain package consisting of node identity and clock semantics,
wire v1/v2 decoding, the append-only profile registry, detection/scene/clip/heartbeat records,
survey and geometry validation, module interfaces, solving, labels, and replay-safe normalization.
It may depend on the standard library and pinned numerical libraries, but must not import Redis,
MQTT/AWS/Oxalis SDKs, Kubernetes clients, Android/gotchi code, filesystem layout conventions or
deployment manifests.

The core consumes canonical records and produces domain results. Persistence, transport, scheduling,
credentials, object storage and cache behavior are ports outside `hear/` and `modules/`. Raw bytes
are retained by the adapter before parsing; normalized records preserve source, schema/profile,
cursor and refusal reason.

### Stable plugin and adapter interfaces

All adapters implement these versioned ports:

* `IngressAdapter.read() -> Iterable[RawEnvelope]`: receives bytes/messages and records source
  coordinates without interpreting acoustic meaning.
* `Normalizer.normalize(raw) -> IngestEnvelope`: validates a versioned envelope, maps timestamps and
  clock tier, and returns an explicit accepted/duplicate/refused result.
* `DurableSink.put(envelope)`: atomically stores the raw object, idempotency key, normalized metadata
  and replay/outbox state.
* `ObjectStore.put/get(ref)` and `CursorStore.load/advance(cursor)`: replace PVC paths and node-tail
  assumptions; cursor advance is permitted only after durable output commits.
* `EphemeralState.set_expiry/get` and `WorkLease.acquire/release`: optional cache/coordination ports;
  implementations may use Redis, PostgreSQL or an in-process test double.
* `ResultSink.publish(result)`: emits versioned solve/score/tag/export records and never writes
  transport-specific keys.

The canonical `hear.ingest.v1` envelope is defined below. Ports must work in clean-room tests with
in-memory implementations; no interface may expose a Redis key, MQTT topic, PVC path, AWS ARN,
Android class name or gotchi-specific field.

### Optional dama-gotchi adapter

The adapter may receive gotchi/AWS traffic, convert phone/GNSS/sketch payloads to
`hear.ingest.v1`, preserve the original payload and producer schema, attach `source=gotchi`, classify
clock tier and survey status, and import phone corpus/calibration artifacts through explicit object
manifest APIs. It may retry, deduplicate and publish compatibility views. It must not own core
profiles, solver rules, durable truth, shared Redis namespaces or the standalone deployment.
Disabling it must leave node ingestion, health, storage, replay, TDoA and restore green.

### Forbidden dependencies and recoupling gates

These are merge-blocking violations: imports from gotchi/cloud/deployment packages into `hear/`
or `modules/`; core code naming AWS, Oxalis, Redis keys, PVC paths, Android classes or
`gotchi-phone`; direct core writes to Redis/MQTT/PVC; a new wire/profile meaning without an
append-only profile and fixture; a required gotchi credential/service in the default deployment;
or a test that passes only when the shared cluster topology exists. Adapters may depend on the core,
but the core may never depend on an adapter.

Ownership is explicit: core maintainers own canonical schemas, wire profiles, clock/survey meaning
and conformance fixtures; node maintainers own firmware and node HTTP/MQTT adapters; deployment
maintainers own Compose/Helm, storage, scheduling and backup implementations; gotchi maintainers
own the optional adapter and phone producer schemas. Cross-owner changes require a contract fixture,
compatibility window and conformance result in the same change.

### Adapter conformance tests

Every adapter must pass the same suite with no external services:

1. valid and malformed `hear.ingest.v1` envelopes, schema/version/type rejection, and durable
   refusal records;
2. wire v1/v2 golden decode, append-only profile behavior, invalid-field rejection and midnight
   unwrapping;
3. duplicate/reordered/replayed input produces one event and never advances a cursor before commit;
4. Redis absence, PVC absence, AWS absence, gotchi absence and Android absence do not break node-only
   capture or restore;
5. foreign phone telemetry is ignored by the node adapter, while the gotchi adapter preserves it
   with its source and clock tier;
6. raw bytes/checksums and source coordinates survive normalization, and clean-room replay
   reproduces the same domain result;
7. deployment artifacts contain no runtime package install, shared application PVC, unpinned image,
   undeclared external endpoint or required cloud credential.

The independence gate is concrete: a clean-room install with only the core, one node adapter,
local durable storage and test doubles passes this suite and replays an offline SD backlog; enabling
gotchi changes only the optional input set, never the core behavior or ownership graph.

## Strangler interfaces and canonical adapter

Introduce a versioned `hear.ingest.v1` envelope. Every ingress adapter must normalize into it
before persistence; downstream workers must not import MQTT, AWS, Redis or gotchi-specific shapes.

Required envelope fields:

```json
{
  "event_id": "stable content-derived UUID",
  "source": "node-http|node-mqtt|gotchi|import",
  "site_id": "site token",
  "device_id": "node/device identity",
  "device_class": "board or producer class",
  "firmware_version": "producer build",
  "observed_at": "UTC timestamp or null",
  "received_at": "UTC timestamp",
  "clock": {"valid": true, "tier": "gps_pps|wall|monotonic", "sigma_ns": 0},
  "kind": "heartbeat|detection|scene|clip|sketch|health",
  "schema_version": 1,
  "payload": {},
  "raw_ref": "object-store key or null",
  "adapter": {"name": "node-http", "version": "..." }
}
```

`event_id` must be deterministic from producer identity, source partition/cursor, timestamp/sample
counter and payload bytes. Retries therefore produce one durable event. The adapter stores the
original bytes before parsing and records parse/refusal reasons; no adapter silently drops a row.
For detection frames, `hear/wire.py` remains the compatibility decoder: accept v1 and v2, preserve
the original version/profile, and reject an unknown geometry rather than guessing. For scene and
CSV data, preserve the original row plus normalized fields and the source file/byte range.

The canonical write path is:

1. Authenticate and validate the envelope.
2. Write raw bytes/object (atomic temp then rename) and an inbox row keyed by `event_id`.
3. Commit normalized metadata and an outbox record in one PostgreSQL transaction.
4. A replayable dispatcher publishes derived work; Redis may accelerate leases but cannot be the
   only queue or acknowledgement.

The read path is similarly strangled: existing Redis health endpoints, pool readers and exports
remain compatibility views over canonical records while new API/worker code reads PostgreSQL and
object manifests. A response includes `source_of_truth`, schema version and correlation ID so a
shadow mismatch is diagnosable.

## Staged migration

### 0. Freeze contracts and inventory

- Capture a manifest of nodes, board classes, firmware releases, MQTT topics/ACLs, AWS routes,
  Redis keys/TTLs, PVC paths, object/file checksums, model cards and deployment digests.
- Record the exact current pool and AWS/MQTT schemas as fixtures.
- Declare compatibility windows: server accepts node wire v1/v2 and telemetry schema 1; a new
  server release must read old nodes before any firmware rollout.
- Do not enable new taggers or unmeasured arrival classes as migration work. PR #151 adds a broad
  tagger stack; PR #141 changes GPS fallback while issue #102 still refuses unmeasured
  `esp32s3-i2s-gps` arrivals. These are separate release decisions.

**Gate:** inventory is checksummed, every node has a last-seen/firmware/class record, and restore
of the current pool into a disposable directory is repeatable.

### 1. Expand: adapters and canonical store

- Build `node-http`, `mqtt`, `gotchi`, `aws-compat` and `pool-import` adapters against the same
  canonical envelope.
- Import raw node files before parsing, preserving `dets.csv`, `dets-prev.csv`, dated scene
  partitions, health files and clips. Keep `hear.pool.key()` as the initial dedup seed, but store
  the full hash and original source coordinates in the canonical record.
- Add durable tables for devices, firmware/schema support, event inbox, observations, object
  manifests, labels, model versions, processing runs, and migration reconciliation.
- Add a canonical health projection with expiry semantics; Redis mirrors it with TTL but expiry
  cannot delete the durable observation.

**Gate:** a fixture replay is idempotent; every source row is `accepted`, `duplicate` or
`refused` with a reason; object checksums and row-count conservation close.

### 2. Dual-write, old readers authoritative

- Keep the existing AWS/MQTT/Redis and PVC paths active.
- For each live node telemetry message, write the legacy path and canonical path from one
  validated envelope. If canonical persistence fails, retain the legacy write and raise an
  alert; if the legacy path fails, canonical capture continues and exposes the adapter failure.
- For HTTP drain data, archive once and fan out to both the existing pool and canonical import.
  Never have two independent fetchers compete for the node's single HTTP client.
- `dama-gotchi` remains optional: its adapter can dual-write gotchi observations, but no gotchi
  outage blocks node ingestion.

**Gate:** seven days of dual-write with zero unexplained event-id collisions, no missing canonical
objects, and bounded lag (heartbeat/event p99 under the agreed site SLO). Legacy views remain the
operator default.

### 3. Shadow-read, compare and repair

- Read health, event counts, scene tails, sketch scores and selected TDoA inputs from canonical
  storage while serving the legacy result.
- Compare by stable event ID and source partition, not by arrival order. Compare values with
  explicit tolerances: bytes/checksums exact for raw objects; timestamps exact where GPS-anchored;
  solver/model outputs require a recorded numerical tolerance and model-card identity.
- Sample at least one complete day per board class and one historical pool partition. Include
  node outage/reconnect, MQTT duplication/reordering, Redis loss, AWS replay and a PVC read-only
  failure.
- Repair only by replaying the inbox/outbox; never hand-edit canonical rows.

**Gate:** canonical and legacy reads agree for the agreed sample, all differences are classified
as expected clock-tier/schema differences or fixed, and replay after deleting Redis produces the
same durable result.

### 4. Cut over reads, retain writes

- Switch health/API, scoring, tagging, scene and TDoA workers to canonical reads behind a
  feature flag. Keep dual-write and legacy exports enabled for one full compatibility window.
- Keep `hear_drain.py` as a recovery/export adapter, not as the canonical writer. The existing
  2 MB scene tail remains a known loss boundary; issue #107 must either be solved with a bounded
  range catch-up or explicitly accepted as a documented RPO before the old drain is retired.
- Treat issue #104's `decode_ValueError` accounting as a required refusal record, not a reason to
  discard historical rows.

**Gate:** two clean backup/restore drills, one planned canonical outage with legacy fallback,
and no increase in node-side data loss. Operators can identify the active release, source and
adapter for every result.

### 5. Retire selectively, never all at once

Retire one dependency at a time only after its exit gate:

1. Stop using Redis as a source of truth; keep it as disposable cache.
2. Stop sharing the PVC between workloads after its immutable import and restore check; retain a
   read-only archive until retention policy expires.
3. Disable AWS forwarding for sites proven to reach the local broker/API; keep the adapter package
   and replay credentials for rollback.
4. Remove the gotchi adapter only per site/operator choice; it is not part of the core platform.

Do not delete old objects, Redis keys, AWS queues or PVC snapshots in the same release as a code
cutover. Contract work requires a separately approved retention/destruction record.

## Historical data conversion

1. Snapshot the PVC and all known exports; generate SHA-256 manifests.
2. Import raw files/clip bytes first, with immutable object keys
   `raw/<site>/<device>/<source-file>/<sha256>`.
3. Parse `dets`, scene, health, MQTT JSONL and gotchi rows through adapters. Preserve source line,
   file, byte range, original schema, node label, `node_alias_of`, and refusal reason.
4. Normalize timestamps without inventing time: PPS-anchored rows are trusted; unanchored and
   wall-clock gotchi rows remain queryable but are not TDoA arrivals.
5. Convert scores and derived results only after source observations are present, retaining model
   card, AUC/provenance and computation version. Derived artifacts are replayable and may be
   regenerated instead of treated as primary history.
6. Reconcile counts by source/day/node/geometry and compare the existing pool ledger. The
   conservation equation is `input = accepted + duplicate + refused`; a zero-result import is a
   failure, not a successful empty period.

## Firmware and mixed-version compatibility

Server releases must accept the existing telemetry schema 1 and detection wire v1/v2. The v2
profile table is append-only; changing the meaning of an existing profile is forbidden. Firmware
rollout is independent from server rollout:

- canary one node per board class, verify `/status`, heartbeat freshness, PPS/time state, identity,
  file schemas and upload/replay;
- retain old endpoints, SD files and OTA release assets until all nodes report the new compatible
  range;
- never require a firmware reflash to rotate server credentials or migrate storage;
- keep `esp32s3-i2s-gps` out of TDoA until issue #102's capture-path bias is measured, and keep
  PMTK/NMEA position semantics explicit until issues #101/#108 are resolved;
- investigate issue #99's boot-loop stall and #103's unreachable-node behavior before using
  heartbeat freshness as evidence of a successful fleet rollout.

A node must continue recording to SD when the network is unavailable. Canonical ingestion catches
up from the node adapter; it must not turn a transient broker/AWS outage into capture loss.

## Rollback criteria and procedure

Trigger rollback if any of the following occurs: canonical persistence loses or duplicates events
outside the declared idempotency rule; raw checksum mismatch; row conservation does not close;
shadow reads disagree beyond tolerance; p99 ingest lag breaches the site SLO for 15 minutes;
restore cannot reproduce a known event; a node's capture/SD behavior changes; or a board class
requires an unapproved wire/schema interpretation.

Rollback is configuration-first: switch reads to the legacy views, stop canonical consumers after
their leases expire, keep both writers running, and replay canonical inbox rows after repair.
Rollback code before data; for an additive schema issue use a forward repair, not a database
downgrade. Restore PostgreSQL, object manifests, broker ACLs and then workers in that order.
Firmware rollback is a separate canary decision and must not be inferred from a server rollback.

## Acceptance gates and sequencing

The release sequence is: contract fixtures -> canonical schema/adapters -> historical dry run ->
dual-write -> shadow-read -> canonical-read canary -> site-wide canonical reads -> dependency
retirement. Each gate produces a signed manifest, metrics snapshot, reconciliation report and
rollback point. The migration is complete only when a clean-room restore can ingest a node's
offline SD backlog, replay MQTT duplicates, survive Redis absence, and reproduce the same
health/event/scene result without AWS or dama-gotchi.

### dama-gotchi remains optional

`dama-gotchi` is a producer adapter, not a platform dependency. Its phone/GNSS/sketch messages
use the canonical envelope with `source=gotchi`, explicit clock tier and producer schema. The
adapter may use MQTT, the existing AWS path, or an offline JSONL import, and can be disabled per
site. Gotchi-derived data is retained and useful for labels/training, but node capture, storage,
health, TDoA and restore must all work with zero gotchi deployments.

## Relevant current work

- Open PR #141 (`fix(spatial): use node GPS survey fallback`) and issue #105 affect the spatial
  read projection, but do not change the canonical clock trust rules.
- Open PR #151 adds broad tagger modules; keep it behind the canonical event/model contract.
- Issue #107 is a known unrecoverable scene-tail gap after a long drain outage.
- Issue #104 requires durable refusal rows for decode failures.
- Issues #101/#108 concern PMTK GPS semantics; #102 blocks one board class from TDoA; #99 and
  #103 show why freshness and boot health need durable, source-aware observability.
