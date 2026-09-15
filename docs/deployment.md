# Self-hosted deployment profiles

This is the deployment path for an independent acoustic system. The profiles deliberately use the
same application images, configuration schema, data layout, and operational commands. A site can
start on one machine and move to a cluster by restoring the same backups into more durable
dependencies; it should not need a second product or a data rewrite.

## Progressive profiles

| Profile | When to use | Packaging and supervisor | Durable dependencies |
|---|---|---|---|
| **Single machine / site** | One site, one operator, up to roughly 20 nodes, intermittent connectivity | Versioned OCI images, `compose.yaml`, and a systemd unit that starts/stops the Compose project | PostgreSQL, local filesystem object store, Mosquitto, optional Redis cache |
| **Small HA cluster** | One site where backend loss is unacceptable, or roughly 20–100 nodes | Same OCI images and values; Helm chart on k3s/RKE2, with three control-plane nodes | PostgreSQL HA, replicated S3-compatible object store, MQTT broker pair/cluster, optional Redis Sentinel |
| **Multi-site Kubernetes** | Several sites, central operations, independent upgrade domains, or more than 100 nodes | OCI registry plus signed Helm chart; one namespace per site and a shared control plane only where latency and policy permit | Managed or operator-backed PostgreSQL, S3/object storage, regional MQTT, cross-site replication/export |

Compose is the supported single-machine interface. systemd supervises the Compose project, not each
container individually. Kubernetes is the supported HA interface. Running ad-hoc Python processes,
mutable ConfigMaps as application packages, or a hand-maintained collection of containers is not a
deployment profile.

## Package and image contract

- Build one immutable image per service and architecture. Pin the base image by digest, install
  from lockfiles, and include the application, migrations, model cards, and runtime dependency
  manifest in the image. Do not `pip install` into a shared PVC at runtime.
- Publish an image digest, SBOM, provenance attestation, and checksums for every release. Tags are
  human-friendly aliases; production configuration records digests.
- Keep node firmware releases separate from server releases. A server release must declare the
  minimum and maximum wire/schema versions it accepts, and old nodes must continue to upload during
  a rolling server upgrade.
- Package the chart and Compose bundle from the same release metadata. Generated deployment files
  must carry the source commit and image digests, and CI must reject drift between the release
  manifest and generated Kubernetes/Compose output.
- Configuration is environment/site data, never baked into images: node addresses, site origin,
  survey, TLS material, broker credentials, retention, and model selection are injected through
  secrets/configuration. Secrets are never stored in the repository or in an image layer.

The application boundary is: node uplinks enter through MQTT/HTTP, workers write immutable event
and scene objects, PostgreSQL stores queryable metadata and job state, and derived results can be
recomputed. Raw audio and clips are objects, not database blobs.

## Profile A: single machine / site

Use a Linux host with Docker or Podman, SSD storage, a UPS, and enough free disk for at least one
backup cycle. Install the signed release bundle, create the site secret file, and run the Compose
project under a dedicated service account. A systemd unit should use `Type=oneshot`,
`RemainAfterExit=yes`, `ExecStart`/`ExecStop` for `compose up -d`/`compose down`, and restart on
boot; container health checks determine readiness.

The default stack is intentionally small:

- PostgreSQL is the source of truth for sites, nodes, surveys, ingestion cursors, labels, model
  versions, and job state. Use a local volume and daily WAL/base backups.
- The filesystem object store is a content-addressed directory with a documented layout and atomic
  temp-file-then-rename writes. Expose the same S3-compatible interface in the application so the
  move to MinIO or cloud object storage is configuration-only.
- Mosquitto provides the node uplink. Persist its queue, require per-node credentials, and bridge
  only explicitly selected topics. MQTT is transport, not the durable event store.
- Redis is optional and disposable in this profile; use it only for rate limits, live health, and
  short-lived work coordination. Never make recovery depend on Redis.

Keep the API, drain, scoring, tagging, and TDoA workers as separate containers even on one host.
They may share a machine, but separate health checks, resource limits, and restart policies make
failure visible and make the later Helm deployment mechanically equivalent. Scheduled work uses a
single scheduler/worker lane with concurrency keys; do not run duplicate CronJobs accidentally.

Recommended starting limits are 2 vCPU and 4 GiB RAM for the base stack, plus 1 vCPU/2 GiB for
bursty scoring or embedding jobs. Set explicit CPU/memory limits, queue depth, open-file limits,
and disk watermarks. Reserve 20% disk for compaction, temporary downloads, and restore staging.

## Profile B: small HA cluster

Use three Linux machines and k3s/RKE2 when a single host is the failure domain. Install the chart
with one values file per site. Keep the control plane and stateful workloads on separate labeled
nodes where possible; use anti-affinity for replicas and a default-deny NetworkPolicy.

- Run PostgreSQL with a supported HA operator or use an external HA PostgreSQL service. Synchronous
  replication is preferred for metadata; asynchronous replicas are acceptable for reporting.
- Run replicated S3-compatible storage (or use external object storage) with versioning enabled.
  The object store is the durable source for raw/derived files; PVCs are caches or staging areas.
- Run an HA MQTT deployment with persistent sessions and bounded retained queues. Bridge sites to a
  central broker only for the topics that need central processing.
- Redis remains a cache/coordination aid. If used for queue semantics, deploy Sentinel/cluster and
  document that jobs are replayable from PostgreSQL/object manifests.

Workers must be idempotent and lease work from PostgreSQL or a durable queue. A pod restart may
repeat a fetch or computation, but must not duplicate an event, corrupt an object, or advance a
cursor before the output is committed. Use Kubernetes Jobs/CronJobs for bounded batch work and
Deployments for APIs, broker bridges, and continuously consumed lanes. Do not use a resident pod
for a workload that is naturally a bounded job unless its latency requirement proves it necessary.

## Profile C: larger multi-site Kubernetes

Create a namespace and release per site. A site owns its node credentials, survey, origin, raw
retention, and local broker endpoint. Central services consume explicitly replicated event
manifests, not direct access to another site's PVC. Prefer one regional cluster per failure and
latency domain over a stretched cluster.

Use an OCI registry close to each site, signed images/charts, admission verification, Pod Security
restricted, NetworkPolicies, topology spread constraints, PodDisruptionBudgets, and priority
classes for ingest versus offline ML. Separate node-facing ingress from operator/API ingress.
Autoscale only stateless APIs and replayable workers; stateful systems scale according to their
operator's documented procedure. Keep raw audio local when bandwidth or policy requires it, and
replicate sketches, detections, manifests, and selected clips.

## Networking and TLS

Nodes need only outbound access to the site broker/API plus DNS and NTP fallback; do not expose
node administration or PostgreSQL to the node network. Site-to-site links use explicit egress
allowlists and mutually authenticated TLS. The broker requires client certificates or unique
credentials per node, topic ACLs scoped to that node, and bounded offline queues.

Terminate HTTPS at the site ingress with an ACME or internal-CA certificate. In air-gapped sites,
use an internal CA and distribute its trust bundle with the release. Use TLS for every hop that
crosses a host boundary, including PostgreSQL, object storage, Redis, MQTT bridges, and registry
pulls. Rotate certificates and credentials before expiry; never make rotation require a firmware
reflash unless the node credential itself is being revoked.

## Backups and restore

Back up three independent classes:

1. PostgreSQL: continuous WAL where available plus a daily base backup, retained across at least
   seven daily and four weekly restore points. Test point-in-time recovery monthly.
2. Object storage: versioned objects plus a daily inventory/checksum manifest. Replicate to a
   second disk/site when raw audio is material. Never treat a PVC snapshot as the only copy.
3. Deployment state: release manifest, Compose/Helm values (with secret references, not secret
   values), CA trust bundle, broker ACLs, survey, site origin, and node inventory.

Restore order is PostgreSQL, object storage, broker identity/ACLs, then application workers. Stop
derived workers until metadata and object manifests reconcile. Run an idempotent inventory check,
rebuild caches, replay uncommitted MQTT/event manifests, and verify a known event end-to-end.
Document RPO/RTO per profile: single machine targets RPO 24 hours/RTO 4 hours unless the operator
adds WAL and off-host object replication; HA targets RPO under 15 minutes/RTO under 1 hour; the
multi-site target is site-local operation during a central outage and replay after recovery.

## Upgrades and rollback

Release upgrades are staged: validate manifests and signatures, back up, run additive database
migrations, deploy the new API/workers, then enable new consumers. Keep old and new wire readers
for one compatibility window. Drain or lease jobs before changing schemas. Never run destructive
migrations in the same release as the code that first requires them.

Compose upgrades pull the exact digests, run migrations once, restart workers in dependency order,
and retain the previous release bundle for rollback. Helm upgrades use `--atomic` only for stateless
rollouts; database and object migrations need an explicit reversible runbook. Roll back code before
rolling back data, and use forward repair for irreversible migrations. Firmware rollout is a
separate canary: one node per board class, verify `/status`, heartbeat freshness, wire compatibility,
and fleet homogeneity before expanding.

## Air-gapped installation

Build a signed offline bundle containing image archives for every target architecture, the Helm
chart or Compose files, SBOMs/signatures, Python/OS package repositories or wheels, firmware
artifacts, model files and checksums, CA bundle, migration binaries, and the operator runbook.
Import it into a private registry or local image store; installation must not contact public
registries, ACME, package indexes, or telemetry endpoints.

The installer verifies signatures and checksums before loading images, performs a preflight for
CPU/RAM/disk/time/DNS, and records the release manifest locally. Model downloads and package
installs at runtime are forbidden. Updates arrive as a new signed bundle and follow the same
backup, canary, migration, and rollback process.

## Day-2 operations

Alert on data freshness, not just process health: node heartbeat age, broker queue age, drain
success, scene-tail gaps, object write failures, PostgreSQL replication/WAL lag, disk watermarks,
backup age, certificate expiry, and worker refusal/novelty counters. A zero-result solve is an
observation; a stale or empty input pipeline is a failure.

Provide operators with `status`, `doctor`, `backup`, `restore-check`, `upgrade`, and `rollback`
commands that target the active profile. Every command prints the site, release digest, and
correlation ID. Logs are structured and retained locally through one outage window; metrics and
heartbeats may be exported centrally but are never required for local capture.

The migration trigger from one profile to the next is operational, not aspirational: move from
single-machine Compose when host maintenance causes unacceptable downtime or storage recovery is
too slow; move from a small cluster when sites, isolation, or workload scheduling exceed one
cluster's operational envelope. Until then, keep the simplest profile.
