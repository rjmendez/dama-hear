# Repository structure and extraction plan

## Recommendation

Use one **DAMA platform monorepo** for the sensor platform and its deployable services,
and keep `dama-gotchi` as a separate application repository. A small team benefits from
atomic contract changes, one CI policy, shared fixtures, and one release train. Splitting
every service into a repository now would create version-skew and coordination overhead
without creating useful ownership boundaries.

The monorepo is a build boundary, not a runtime boundary: services still own their data,
APIs, migrations, and deployable artifacts. `dama-gotchi` consumes published APIs or
versioned SDKs and is never imported, built, or required by the platform.

## Target layout

```text
dama-platform/
  contracts/
    schemas/                 # canonical event and resource schemas
    proto/                   # internal RPC definitions
    openapi/                 # public HTTP descriptions
    codegen/                 # deterministic generators and compatibility checks
    fixtures/                # cross-language golden payloads
  firmware/
    hear-node/               # ESP32 capture, clocking, edge detection, uplink
    boards/                  # board capabilities and pin maps
    tools/                   # enroll, flash, inspect, package
  edge/
    ingest-gateway/          # MQTT/LoRa/serial adapters -> canonical ingest
    device-agent/            # optional local buffering and provisioning
  services/
    ingest/                  # authenticated envelopes, deduplication, quarantine
    fleet/                   # enrollment, config, health, rollout state
    events/                  # canonical detections, labels, search
    clips/                   # manifests, signed object access, retention
    calibration/             # calibration runs and geometry versions
    localization/            # jobs, tracks, uncertainty, provenance
    models/                  # model registry and rollout channels
    alerts/                  # rules, incidents, notifications
    exports/                 # snapshots and delivery jobs
  dsp/
    platform/                # windows, resampling, clocks, features, quality
    localization/             # association, solvers, uncertainty, refusal gates
    modules/
      supersonic/            # shockwave detector and trajectory model
      bioacoustic/            # cicada/katydid detector and point-source model
  adapters/
    gotchi/                  # legacy MQTT/JSON translation only
    generic-http/
    generic-mqtt/
  ui/
    console/                 # operator web UI
  simulation/
    replay/                  # recorded capture and event replay
    scenarios/               # synthetic geometry and noise cases
    benchmarks/              # latency, accuracy, resource budgets
  deploy/
    local/                   # one-command developer stack
    k8s/                     # environment overlays and manifests
    terraform/               # infrastructure modules only
  docs/
    architecture/
    operations/
    decisions/
  tests/
    contract/
    integration/
    e2e/
```

The current `dama-hear/hear/` and `modules/` map naturally to `dsp/`; current
`firmware/`, `deploy/k8s/`, `tests/`, and `docs/` move with little semantic change.
The current `dama-gotchi` repository remains intact while its acoustic-facing code is
replaced by `adapters/gotchi` or a separately released adapter package.

## Language and ownership

| Area | Recommended language | Owner and boundary |
| --- | --- | --- |
| Firmware and board tools | C++ (Arduino/ESP-IDF) plus small Python CLI tools | Firmware owns sampling, PPS discipline, local detection, enrollment, and wire encoding. It cannot call service databases or UI code. |
| Schemas and code generation | Protobuf for internal RPC/events; OpenAPI + JSON Schema for public REST and fixtures; generator in Python or Go | Contracts owns names, field meaning, compatibility, and generated outputs. Hand-written business logic does not live here. |
| Ingest and core services | Rust for ingest, fleet, localization workers, and high-throughput paths; Python only for orchestration or ML-heavy workers | Each service owns one durable store and its migrations. Services communicate through contracts, not shared tables or in-process imports. |
| DSP and localization | Python/NumPy/SciPy for research and reference implementations; Rust crate for stable hot paths after profiling | DSP owns deterministic algorithms and golden vectors. A detector module cannot reach into fleet or transport state. |
| UI | TypeScript with a single web console; Android remains in `dama-gotchi` | UI uses REST/SSE and signed URLs only. It does not embed solver or database logic. |
| Deployment | Terraform for cloud/cluster primitives; Helm/Kustomize for workloads; shell only as thin operator wrappers | Deployment owns wiring and policy, not application behavior. |
| Simulation and tests | Python for scenario generation and analysis; Rust/Python integration harnesses | Simulation consumes schemas and published inputs; it must run without production credentials or live infrastructure. |
| Docs | Markdown plus generated OpenAPI/protobuf references | Architecture decisions describe ownership and compatibility, not implementation trivia. |
| Adapters | Rust or Python according to transport; no shared domain model beyond generated contracts | An adapter translates legacy/external payloads at the edge and may be removed without changing platform truth. |

Avoid a second general-purpose shared library. Share only generated contracts, small
protocol utilities, and test fixtures. Shared domain code creates the same coupling under
a new package name.

## Monorepo versus multirepo

**Monorepo advantages:** atomic schema and consumer changes, one set of golden fixtures,
simple local replay, coordinated firmware/service releases, and lower operational overhead
for a small team. Directory-level CODEOWNERS and independent CI jobs provide practical
ownership without repository fragmentation.

**Multirepo advantages:** independent permissions, release cadence, and smaller checkouts.
Those matter for a larger organization or genuinely independent products, not for the
current platform. The exception is `dama-gotchi`: it has a different product purpose,
Android toolchain, deployment lifecycle, and broad sensor scope, so it should remain
separate.

Revisit a split only when a component has an independent team, a stable public API, or a
materially different security/release lifecycle. If that happens, extract `contracts`
first as a versioned package; do not split by current directory names alone.

## Boundaries that prevent `dama-gotchi` coupling

1. Platform northbound APIs are REST/JSON under `/v1`; bounded internal calls use
   protobuf/gRPC; events use immutable versioned CloudEvents. MQTT is an edge transport,
   not the canonical application API.
2. `dama-gotchi` receives a client SDK or adapter package, never a platform database
   credential, ORM model, Rust workspace path dependency, or internal message-bus topic.
3. Legacy gotchi topic names and payloads are accepted only by `adapters/gotchi`, mapped
   into canonical ingest envelopes, and tagged with their source and adapter version.
4. Platform services own enrollment, fleet health, detections, clips, geometry,
   localization, models, alerts, and exports. Gotchi may cache or display them but cannot
   become the system of record.
5. Contract CI rejects breaking changes, verifies idempotency and auth behavior, and runs
   adapter compatibility fixtures. A platform build must pass with the gotchi adapter
   disabled.
6. No platform package may import an Android class, gotchi namespace, gotchi database
   schema, or gotchi-specific environment variable. Enforce this with dependency-graph
   checks and CODEOWNERS review.

## Staged extraction

**Stage 0 - freeze the seam.** Keep the current repositories. Treat
`docs/api-boundaries.md` as the northbound contract, inventory gotchi payloads, and add
canonical fixtures for telemetry, detections, health, clips, and localization.

**Stage 1 - create the platform monorepo.** Move `dama-hear` platform code, firmware,
modules, deployment, tests, and docs into the target layout. Keep import-compatible
Python entry points temporarily. Add contract generation and a local replay stack before
moving production workloads.

**Stage 2 - extract transport and service ownership.** Put the gotchi MQTT/JSON translation
behind `adapters/gotchi`; move ingest, fleet, and event persistence behind service APIs.
Run dual-write or shadow translation only where needed, with reconciliation metrics and a
time-bounded removal plan.

**Stage 3 - separate runtime deployables.** Build independent images for ingest, fleet,
localization, tagging, and exports. Deploy them together initially, then scale or split
only when measurements justify it.

**Stage 4 - retire legacy paths.** Stop new gotchi-specific fields and topics, publish a
sunset date, remove direct database/broker access, and delete the adapter after all clients
use canonical APIs.

## Build, release, and versioning

- **Workspace tooling:** Rust uses one Cargo workspace; Python uses `uv`/`pyproject.toml`
  per service with locked environments; TypeScript uses one pnpm workspace. Firmware uses
  pinned Arduino/ESP-IDF toolchains and reproducible generator checks.
- **CI:** path-filtered jobs run contract compatibility, affected-language tests, DSP
  golden vectors, firmware builds, container builds, and deployment validation. A small
  full integration matrix runs on merge to `main` and nightly.
- **Artifacts:** publish immutable OCI images, firmware bundles, Python wheels, generated
  contract packages, and UI bundles. Every artifact includes commit SHA, source contract
  version, toolchain, SBOM, and checksums.
- **Versions:** use SemVer for public API/SDK packages and services. Use independent
  service versions but a coordinated platform release manifest. Firmware versions include
  board class and protocol compatibility range. Database migrations are ordered and
  backward-compatible for at least one rollout.
- **Contracts:** REST major versions live in the path (`/v1`); protobuf packages and event
  types carry immutable major versions. Additive fields are compatible. Breaking changes
  require a new major, migration notes, compatibility fixtures, and a deprecation window.
- **Promotion:** merge builds immutable artifacts; staging deploys by digest; smoke,
  replay, and contract checks promote the same digests to production. Tags create a
  signed release manifest rather than rebuilding source.
- **Rollback:** retain the previous image, firmware, model, and schema-compatible
  migration. Roll back deployment references first; destructive data migrations require a
  separate, explicitly approved step.

The first practical milestone is not a full rewrite: publish the contracts, add the gotchi
adapter seam, and make a clean platform build possible without importing `dama-gotchi`.
