# 0002 - Canonical home for `hear.ingest.v1` and published contracts

- Status: accepted
- Date: 2026-09-15
- Scope: repository layout, ownership and CI for published contract artifacts. This record
  changes no writer, no reader, no schema, no payload and no deployment. It does not select
  a codec; `docs/decisions/0001-hear-ingest-v1-envelope-and-codec.md` owns that.
- Enforced by: `tools/check_contract_layout.py`, CI job `contract layout and frozen baseline`.

## Problem

`hear.ingest.v1` needs somewhere to live before Phase 4 moves traffic. The repository had no
answer: `docs/repository-structure.md` describes a `dama-platform/` monorepo that does not
exist yet, contract-shaped artifacts were scattered across `testdata/`, `tests/fixtures/`
and `docs/data/`, and schema identity was expressed as constants inside the modules that
happen to write it (`hear/pool.py` `SCHEMA_VERSION`, `hear/clips.py`
`CLIP_SCHEMA_VERSION`, `hear/tags.py` `TAG_SCHEMA_VERSION`).

Choosing the *target* layout now would be the expensive mistake. The current JSON/Wi-Fi/MQTT
path is live and must not move during a mixed-version migration:

| Live path | Producer | Consumer |
|---|---|---|
| HTTPS JSON push over Wi-Fi | `firmware/hear_node/hear_push_payload.h` | `tools/hear_heartbeat_receiver.py` |
| MQTT `dama/+/telemetry` | AWS ingest forwarder | `tools/hear_mqtt_bridge.py` |
| SD drain over HTTP | `firmware/hear_node` | `tools/hear_drain.py`, `hear/pool.py` |
| LoRa/mesh frames | `hear/wire.py` v1/v2 | `hear/backend/pipeline.py` |

None of those may change to acquire a contract directory.

## Decision

**The smallest compatibility-safe canonical home is one new top-level directory, and
nothing else moves.**

```text
contracts/
  schemas/    contracts/schemas/<contract-id>.schema.json
  fixtures/   contracts/fixtures/<contract-id>/*.json + manifest.json
docs/decisions/
              NNNN-kebab-title.md, one per contract or layout decision
```

with the source of truth staying in the owning Python module
(`hear/ingest/envelope.py` for `hear.ingest.v1`) and the published artifacts generated from
it by `tools/gen_*.py`.

1. **`contracts/` is additive.** Nothing is moved into it. `testdata/`, `tests/fixtures/`
   and `docs/data/` keep their current contents and meaning; they hold golden vectors and
   captured evidence, not published interfaces. Moving them would rewrite paths that the
   frozen Phase 0 baseline hashes, for no compatibility gain.
2. **`contracts/` is generated-only.** It is the published form of a contract. No Python,
   no hand-edited JSON. A reviewer can trust that a diff there is a consequence of a source
   diff, never an independent claim.
3. **The source of truth is the owning module, not the artifact.** The field table lives
   next to the code that must obey it, so a field cannot be added to a schema without the
   validator seeing it. This is why `contracts/` can be generated-only without becoming
   write-only.
4. **`docs/decisions/` is the home for the compatibility argument.** A schema states shape;
   it cannot state why an identity tuple is frozen or what a rollback does. Both belong in
   one numbered, append-only record per contract.
5. **`proto/`, `openapi/` and `codegen/` are not created now.** `docs/repository-structure.md`
   lists them, but an empty directory is a promise, not a contract. They arrive with the
   first service that needs them.
6. **The target `dama-platform/` layout is reached by renaming this directory, not by
   rewriting its contents.** `contracts/` is already the name and shape that layout uses, so
   Stage 1 of the extraction plan moves a directory and nothing else.

## Why this is compatibility-safe

- **No live path changes.** Every producer and consumer in the table above is untouched. A
  node running today's firmware, the AWS/MQTT chain, the drain and the pool all keep working
  byte-for-byte, because `contracts/` is a description of an envelope that nothing is yet
  required to emit.
- **Forward.** A newer writer adding fields is additive within a major; readers accept and
  preserve unknown fields, so an upgraded writer talking to a not-yet-upgraded reader does
  not lose data and does not duplicate events.
- **Rollback.** Rolling back is a revert of a directory that no runtime reads. Because the
  published artifacts are generated, a rollback cannot leave a schema describing code that
  no longer exists: regeneration would reproduce the old artifact exactly, and CI checks it.
- **Mixed version.** During the dual-write window both the legacy JSON shapes and the
  canonical envelope are described in-tree at the same time — the legacy shapes by the
  Phase 0 frozen baseline, the canonical one by `contracts/`. Neither description is
  deleted to make room for the other, which is what makes a comparison possible at all.
- **No-dependency baseline.** The contract core (`hear/ingest/`, `contracts/`, this rule
  checker) imports only the standard library. A cross-language or clean-room adapter can
  validate against the published schema and fixtures on a bare interpreter, with no
  `numpy`/`scipy`/`redis`/`paho`/`boto3` present. `tools/check_contract_layout.py` enforces
  this, and the CI job deliberately runs before any `pip install` so the claim is tested and
  not merely asserted.
- **Import boundary.** The boundary is placed at `hear/ingest/`: core modules may import it,
  it may import nothing but the standard library, and no adapter, transport, cloud or
  deployment package may be imported by it. Transport names — MQTT topics, Redis keys, PVC
  paths, AWS ARNs, Android classes — stay outside, as `docs/standalone-migration.md`
  requires. Contract artifacts therefore carry no transport vocabulary that a future
  adapter would have to honour.

## Version and unknown-field behaviour (layout consequences)

The rules themselves are stated in decision 0001. What the layout must guarantee is that
those rules are *checkable from outside this repository*:

- One schema file per **major**. A new major is a new file, never an edit; the old file
  stays so a mixed-version fleet has both descriptions.
- Every fixture declares its expected validation outcome in `manifest.json`. An adapter in
  another language asserts against the manifest without importing any Python, which is what
  makes "unknown fields are preserved" and "an unsupported major is refused" testable for a
  producer this repository does not own.
- A fixture file that is not declared in the manifest is a layout violation: an undeclared
  golden payload is an assertion nobody can make.

## Generated artifact reproducibility

- Every contract artifact has a generator under `tools/gen_*.py`, and that generator must
  appear in `.github/workflows/ci.yml`. An artifact whose generator has no drift gate
  silently stops describing its source; the layout checker rejects it.
- `tools/freeze_contracts.py --check` now runs in CI. The Phase 0 baseline was reproducible
  but ungated, so a contract-affecting change could land without the frozen inventory
  noticing. This closes that.
- **Known gap, closed after the envelope lane merged.** `freeze_contracts.py` originally
  scanned `hear`, `modules`, `tools`, `testdata`, `tests/fixtures` and `docs/data` — not
  `contracts/`. A published artifact was covered by its generator's drift gate but was not
  hashed into the frozen baseline, so a generated schema, fixture or manifest could be
  edited while `--check` still passed. The freeze now carries a `published_contracts`
  section that records each published contract's schema, fixture and manifest bytes plus
  the manifest's declared outcomes. It stays an inventory: the generator still owns what
  the artifacts say, `check_contract_layout.py` still owns where they may live, and the
  freeze only guarantees that editing one moves the baseline hash.
- The published artifacts are inventoried, never treated as a source of truth. Rule 1 reads
  `schemas.schema_identifiers`, which continues to scan owning modules only, so a generated
  `contracts/` path can never satisfy a contract's source-of-truth requirement.

## Ownership

`docs/standalone-migration.md` names four owner roles. This is where each one's artifacts
live, so a cross-owner change is visible as a path:

| Owner | Owns | Paths |
|---|---|---|
| Core | canonical schemas, wire profiles, clock/survey meaning, conformance fixtures | `hear/ingest/`, `hear/wire.py`, `contracts/`, `docs/decisions/` |
| Node | firmware, node HTTP/MQTT producers | `firmware/`, `tools/hear_heartbeat_receiver.py`, `tools/hear_mqtt_bridge.py` |
| Deployment | manifests, storage, scheduling, backup | `deploy/` |
| Gotchi adapter | the optional adapter and phone producer schemas | future `adapters/gotchi/`; never `contracts/` or `hear/` |

A cross-owner change ships its fixture in the same commit as the code that needs it. A
single-maintainer repository cannot enforce that with review routing, so it is enforced by
the checker instead: a published contract without a source of truth, a decision record, a
generator or a fixture manifest fails CI.

## Rules, in the form the checker applies them

1. Every published contract id has a source of truth recorded in the frozen baseline.
2. Every published contract id is named by a decision record.
3. Every published contract has a generator, and that generator runs in CI.
4. Every fixture is declared in its `manifest.json` with an expected outcome.
5. The contract core imports only the standard library.
6. Decision records are uniquely numbered and state a status.
7. `contracts/` contains generated artifacts only.

A checkout with no `contracts/` directory passes rules 1-4 and 7 vacuously. That is
intentional: the gate arms when the directory appears, which lets this record and its
checker land independently of the lane that creates the first contract.

## Consequences

- One new top-level directory, no moved files, no changed live path, no new dependency.
- The `dama-platform/` extraction becomes a directory rename rather than a content rewrite.
- Adding a contract is mechanically constrained: source module, generator, CI wiring,
  decision record, fixtures with declared outcomes — or it does not merge.
- The freeze-scope gap this record created is closed: published artifacts are hashed into
  `docs/data/phase0-freeze-contracts.v1.json`, so a contract edit that skips its generator
  fails `tools/freeze_contracts.py --check` as well as the generator's own drift gate.
