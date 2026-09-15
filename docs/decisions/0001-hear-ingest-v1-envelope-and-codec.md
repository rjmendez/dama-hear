# 0001 - `hear.ingest.v1` canonical envelope and wire codec

- Status: accepted
- Date: 2026-09-15
- Scope: the canonical ingest envelope only. This record does not deploy anything, does not
  change any live writer or reader, and does not cut over any path.
- Supersedes: nothing. Resolves the open ambiguity between "versioned binary envelope",
  an unsourced CBOR assumption, and the Protobuf/JSON Schema stack selection.

## Decision

1. `hear.ingest.v1` is defined as a **codec-agnostic logical schema** whose field table
   lives in `hear/ingest/envelope.py`. `contracts/schemas/hear.ingest.v1.schema.json` and
   `contracts/fixtures/hear.ingest.v1/` are generated from it by
   `tools/gen_ingest_contracts.py`, and CI fails if they drift.
2. The **only normative wire codec for v1 is UTF-8 JSON**, media type
   `application/vnd.dama.hear.ingest.v1+json` (`application/json` accepted as an alias).
3. **CBOR is not adopted.** No repository evidence supports it and no producer emits it.
4. **Protobuf is reserved for internal service-to-service RPC**, generated later from the
   same field table. It is not the device wire format in v1.
5. The **versioned binary artifact stays where it already is**: `hear/wire.py` v1/v2
   detection frames travel inside `payload` with their own `wire_version`/`profile_id`,
   opaque to the envelope. The envelope never reinterprets or re-profiles a frame.
6. `event_id` is derived from a **closed identity tuple** over a canonical serialization,
   not from the transport bytes and not from the whole envelope.
7. Adding a second codec later is a **registry edit, not a major version bump**, because
   identity and field meaning are defined independently of the encoding.

## Why this is grounded, and not a preference

The documentation ambiguity was real, and it resolves in one direction once the committed
docs and the actual writers are read together.

| Evidence | Says |
|---|---|
| `docs/standalone-migration.md` "Required envelope fields" | The envelope is specified as a **JSON object**, field by field. This is the only field-level definition of `hear.ingest.v1` that exists. |
| `docs/api-boundaries.md` service map | Ingest owns "HTTPS batch REST"; `POST /v1/ingest/batches`. REST/JSON is the public contract; gRPC/protobuf is explicitly "for bounded **internal** calls ... not required of external clients". |
| `docs/repository-structure.md` language table | "Protobuf for internal RPC/events; **OpenAPI + JSON Schema for public REST and fixtures**". |
| `docs/api-boundaries.md` versioning | "clients must ignore unknown JSON fields and protobuf fields"; schemas published as OpenAPI and protobuf descriptors. |
| `firmware/hear_node/hear_node.ino`, `firmware/hear_node/hear_push_payload.h` | Every node uplink already sends `Content-Type: application/json` with RFC3339 timestamps. There is no CBOR or protobuf encoder on the device. |
| `tools/hear_mqtt_bridge.py`, `tools/hear_heartbeat_receiver.py` | Both decode `json.loads(...)` and persist `payload_json`. |
| `hear/wire.py` | The *binary* versioned format that exists today is a detection **frame** (8 bits seq, 3 version, 4 profile, 1 retrigger — fully allocated), sized for Meshtastic. It is a payload format, not an envelope. |
| Whole repository | The string "CBOR" appears nowhere. |

So "versioned binary envelope" in the recovered notes refers to the `hear/wire.py` frame
lineage, and the CBOR assumption has no source. Choosing CBOR now would require writing a
new encoder for a constrained ESP32 that already emits JSON correctly, in exchange for a
saving nobody has measured on the path that matters. Choosing protobuf for the device wire
would contradict two committed documents and put a schema compiler in the firmware build.

## What was deliberately left open

Binary framing is **deferred, not rejected**. The trigger to revisit is a measured
constraint, not taste: the LoRa/mesh path is already byte-starved, which is exactly why
`hear/wire.py` exists and why it stays. If batch uplink bandwidth or ESP32 flash later
becomes the binding constraint on the HTTPS path, add a codec to `CODEC_MEDIA_TYPES`,
publish its media type, and negotiate with `Content-Type`. Because `event_id` is computed
over the canonical form rather than the transport bytes, the same observation encoded two
ways is still one durable event, so a codec rollout does not need a data migration.

## Writers, readers and data shape

| Producer | Path today | Under `hear.ingest.v1` |
|---|---|---|
| `hear_node` firmware push | HTTPS JSON to the heartbeat/event receiver | `source=node-http`, `kind=heartbeat|detection|clip`, adapter `node-http` |
| MQTT telemetry | `dama/+/telemetry` JSON, device_id cross-checked against the topic | `source=node-mqtt`; topic names stay in the adapter |
| SD drain | `dets.csv` / `scene.csv` pulled over HTTP, plus the `cursor-v1` detections envelope | `source=import`, `producer.cursor` carries the drain cursor, original row preserved in `payload` |
| LoRa/mesh frames | `hear/wire.py` v1/v2 binary | base64 in `payload` with `wire_version`/`profile_id` intact |
| dama-gotchi phone corpus | AWS/SQS/MQTT JSON | `source=gotchi`, `clock.tier=wall`, producer schema and original body preserved |

Readers (`hear/pool.py`, solvers, exports) consume the normalized envelope; none of them
gains a transport-specific key. No existing writer or reader is modified by this change.

## Versioning and unknown-field rules

- `schema_version` is the **major** version and the first thing checked. An unsupported
  major is refused before any field is interpreted, with reason
  `schema_version_unsupported` and a durable refusal record. A v2 body that reuses a v1
  field name with a new meaning therefore cannot be read as v1.
- Within a supported major, **unknown properties are accepted and preserved verbatim**.
  The published schema sets `additionalProperties: true`, and the reader reports them as
  warnings, never errors. `strict_unknown=True` exists for producer-side CI only; a reader
  that enabled it would turn a newer peer's additive field into data loss.
- **Unknown enum members** (`source`, `kind`, `clock.tier`) are accepted and preserved but
  mark the record **non-dispatchable**: it is stored and replayable, and a worker that
  would have to guess the member's meaning never sees it. The schema advertises known
  values as `x-known-values` rather than a closed `enum` for this reason.
- A known field with the wrong type is refused (`type_invalid`), never coerced. Timestamps
  must be RFC3339 UTC. `clock.valid=true` with a null `observed_at` is refused as a
  self-contradictory claim.
- Removing a field, narrowing a type, or changing a field's meaning requires `v2`.

## Identity, rollback and mixed-version behavior

`event_id` is a UUIDv8 over `SHA-256(namespace || canonical(identity tuple))`, where the
identity tuple is exactly:

```text
source, device_id, kind, observed_at,
producer.boot_id, producer.sequence, producer.cursor, payload
```

That set is closed and frozen for v1; extending it is a major version change and a test
asserts it. The consequences are the ones that matter for a partially upgraded fleet:

- **Forward.** A new writer that adds fields produces the **same** `event_id` as an old
  writer for the same observation, so an at-least-once retry across a version boundary
  deduplicates to one durable event instead of two.
- **Rollback.** Data already written by the newer writer stays valid, readable and
  replayable by the rolled-back reader, because unknown fields are preserved rather than
  stripped and identity does not depend on them.
- **Mixed version.** Old writer plus new reader works (optional `producer` absent), and new
  writer plus old reader works (additive fields ignored, unknown `kind` stored but not
  dispatched). Both directions are covered by fixtures and tests.
- **Codec change.** Canonicalization is JSON-independent of key order and of the transport
  encoding, so a future binary codec does not re-identify existing events.

## Consequences

- Firmware needs no new encoder, and the constrained-device build is unchanged.
- Cross-language adapters validate against one generated JSON Schema plus ten golden
  fixtures with declared expected outcomes in `contracts/fixtures/hear.ingest.v1/manifest.json`.
- Protobuf/gRPC work is deferred to the internal service seam and must be generated from
  the same field table, not hand-written alongside it.
- Nothing is deployed and no path is cut over by this record. Adapter wiring, the ingest
  service and dual-write live in later phases.
