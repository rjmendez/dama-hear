# 0002 - Phase 4 HTTPS batch ingest adapter

- Status: accepted (design only)
- Date: 2026-09-15
- Scope: the design of an **additive** HTTPS batch ingest path and its wire contracts. This
  record does not deploy anything, does not cut any path over, does not modify a live writer
  or reader, and does not ship a production adapter. It adds a frame contract, generated
  schemas and fixtures, and the specification in
  [`docs/phase4-https-batch-ingest.md`](../phase4-https-batch-ingest.md).
- Builds on: [0001 - `hear.ingest.v1` canonical envelope and wire codec](0001-hear-ingest-v1-envelope-and-codec.md).
- Depends on nothing being retired. Stage 2 of
  [`docs/standalone-migration.md`](../standalone-migration.md) requires the legacy paths to
  stay live and authoritative while this one is proven.

## Context

ADR 0001 defined *what an item is* and deliberately left the transport open. Phase 4 has to
decide *how items arrive*, for a target that is six nodes on one machine — not a fleet that
justifies a broker, a stream processor or a mesh.

What exists today (verified in-tree, not assumed):

| Path | Today |
|---|---|
| `firmware/hear_node/hear_node.ino` | HTTPS JSON, one telemetry record per request, to `POST /api/hear/heartbeat` and `POST /api/hear/event`. `WiFiClientSecure` with a single pinned root CA, `X-Hear-Token` or bearer token, 3 s connect / 2 s read timeouts, 2xx-only success. |
| same, `HEAR_PUSH_WRAP_BATCH` | Already compiles to `POST /ingest/batch` with `{"device_id": "...", "messages": [ <one message> ]}`. A batch shape is therefore **already present in firmware**, carrying exactly one item. |
| `tools/hear_heartbeat_receiver.py` | Plain HTTP on :5051 behind the cluster, optional `X-Hear-Token`, SQLite durable write **before** the Redis cache update, HTTP 204 with an empty body. |
| `tools/hear_mqtt_bridge.py` | `dama/+/telemetry` QoS 1, topic/body `device_id` cross-check, and it **drops malformed messages before anything durable is written** (issue #104). |
| `tools/hear_drain.py` | Pull-based: `/status`, `/detections` (`cursor-v1`), `/ls`, `/sd`. Archives raw bytes before parsing, advances the cursor only after ingest, and reads a 2 MB scene **byte tail** with no range catch-up (issue #107). |
| `deploy/k8s/` | k3s namespace `dama`, CronJobs, PVCs, Redis, an external MQTT broker. No checked-in reverse proxy, no TLS terminator, no PostgreSQL server, no queue. |

Three facts drive the design. The firmware sends one record per request and has **no durable
uplink spool**, so a failed event push is simply lost from the uplink (SD remains the
capture authority). The receiver returns `204` with an empty body, so a producer learns
nothing about *what* was stored. And the batch wrapper already in firmware has no
acknowledgement semantics at all, so enabling it as-is would create a batch path that cannot
tell a producer which items are safe to forget.

## Decision

1. **Add `POST /v1/ingest/batches` beside the existing routes; change nothing that exists.**
   `/api/hear/heartbeat` and `/api/hear/event` keep their current behavior and stay
   authoritative for the whole of Phase 4. A node, a drain run or a bridge that never learns
   about the batch route must keep working unchanged, which is also the rollback path.

2. **Version the frame separately from the item.** `hear.ingest.batch.v1`
   (`hear/ingest/batch.py`) versions framing; each item carries its own `schema_version` per
   ADR 0001. An unsupported *frame* major is refused whole, before any item is interpreted.
   An unsupported *item* major refuses exactly that item. Collapsing the two would mean a
   single newer item could refuse an entire node's backlog.

3. **A batch is not a transaction.** Per-item outcomes are `accepted`, `duplicate`,
   `refused` or `deferred`, and `accepted + duplicate + refused + deferred = submitted`
   always — the same conservation equation the migration document already requires of
   imports. One unreadable row is refused with a durable refusal record while its siblings
   are stored. A batch that refuses whole on one bad row is a batch that a node with one
   corrupt detection can never drain past.

4. **Acknowledgement is a contiguous durable prefix, returned in a receipt.**
   `ack_through_index` is the last index made durable with no gap before it. A producer may
   release spooled items only through that index. A refused item inside the prefix is
   acknowledged — it is durable *as a refusal* — so a body the server will never accept
   cannot become an infinite retry loop. This replaces the current empty `204`, which is why
   the receipt exists at all.

5. **Deduplication is by `event_id`, never by `batch_id` or arrival order.** ADR 0001's
   closed identity tuple already makes a retried, reordered or differently re-split batch
   converge on the same durable events. `batch_id` is for correlation and log joins only.
   `Idempotency-Key` is a transport-level 24-hour convenience scoped to
   site + principal + route; identity dedup is the permanent guarantee.

6. **Legacy telemetry bodies are first-class batch items.** An item carrying
   `telemetry_path`/`telemetry_schema_version` is classified `translation_required` and
   handed to the edge adapter, not refused. Every node in the fleet emits that shape today;
   a frame reader that refused it would make the batch path useless until a full reflash,
   which inverts the required ordering ("a new server release must read old nodes before any
   firmware rollout"). Because `translation_required` is a classification and not an outcome,
   **no receipt may be issued while an item is still untranslated**: an untranslated item
   reported as `deferred` would be reclassified identically on every retry, which is the
   infinite loop rule 4 exists to prevent. A translation that cannot succeed resolves to
   `refused` with a reason — durable, and therefore acknowledged.

6a. **Item identity is bound to the credential, not just the frame's.** `device_id` is an
   identity input, so an authenticated node smuggling an item attributed to a neighbour would
   mint a durable, dispatchable event for a node that never sent it, and could collide with
   that node's real events. A device-scoped credential pins every item's `device_id`; a
   site-scoped gateway credential pins `site_id` instead, because a drain/import adapter
   legitimately submits for many devices. A missing credential is a refusal, never a skipped
   check: a reader that treated "no credential" as "no constraint" would trust the body's own
   claim about who sent it.

7. **The first client is the server-side edge adapter, not the firmware.** Batching pays off
   where a durable spool already exists — `hear_drain.py` replaying an SD backlog and the
   dual-write path. The device-side batch client needs a durable uplink spool the firmware
   does not have; that is firmware work, gated separately, and nothing in Phase 4 requires
   it. `HEAR_PUSH_WRAP_BATCH` stays disabled until the receipt/spool loop exists on the node.

8. **Bearer device credentials over TLS now; mTLS when the firmware can carry it.**
   `docs/api-boundaries.md` calls for mTLS-bound device identities. The push path today
   configures a server CA and no client certificate, so mTLS would require a firmware change,
   per-device key provisioning and a private PKI before a single batch could be sent. Phase 4
   therefore uses per-device bearer credentials over TLS 1.2+, rotatable **without a
   reflash** (the migration document forbids reflash-to-rotate), with mTLS recorded as the
   target and its trigger written down.

9. **No queue, no broker, no service mesh, no Kafka.** Durability is a local transactional
   store plus an outbox table, exactly as `docs/standalone-migration.md` already specifies.
   Six nodes on one machine do not produce a throughput problem; introducing a broker would
   add a new failure domain, a new upgrade surface and a new source of truth to reconcile,
   and would violate the "no unmeasured dependency" rule in the same document. The trigger to
   revisit is measured, not aesthetic: sustained ingest backlog that a single process cannot
   drain within the site SLO, or a second consumer that genuinely needs independent offsets.

10. **Dual-write is compared by identity, with receipts as the evidence.** Phase 4's exit
    gate is a reconciliation report keyed on `event_id`, not a latency graph. Rollback is
    configuration-first: stop advertising the batch route; the legacy routes never stopped
    working.

## Why this is grounded, and not a preference

| Evidence | Consequence for this design |
|---|---|
| `hear_node.ino:2497-2506,2644-2664` — `HEAR_PUSH_WRAP_BATCH` already emits `{"device_id", "messages": [...]}` | The frame keeps the field name `messages` and a compatible shape. A new name would orphan a wrapper that already ships. |
| `hear_node.ino:2773-2805` — a failed event push is logged, never spooled | A device batch client is not viable yet; batching starts server-side. Claiming otherwise would design against the firmware that exists. |
| `hear_heartbeat_receiver.py:842-875` — POST returns `204`, empty body | A receipt is the minimum addition that makes acknowledgement possible; there is nothing today for a producer to acknowledge against. |
| `hear_heartbeat_receiver.py:880-885` — optional `X-Hear-Token`, disabled when unset | Auth must become mandatory and per-device on the new route without breaking the old one. A shared, optional token cannot attribute a batch. |
| `hear_mqtt_bridge.py:158-220` — malformed messages dropped pre-durability (issue #104) | The batch path writes raw bytes before parsing and emits a durable refusal record for every refusal, so conservation closes. This is the defect the new path must not reproduce. |
| `hear_drain.py` cursor/gap handling and `SCENE_TAIL_BYTES = 2_000_000` (issue #107) | `producer.cursor` and `producer.boot_id` are carried in the frame and items so a gap stays visible; the batch path does not pretend to fix the scene-tail RPO. |
| `hear/pool.py:74-88` — content-addressed pool key | Identity-based dedup is already the house style; `batch_id` must not become a second, weaker one. |
| `deploy/k8s/README.md` — CronJobs, PVCs, Redis, no proxy or queue | Single-process ingest with a local durable store and an outbox. Adding a broker here would be new infrastructure nobody measured a need for. |
| `firmware/hear_node/hear_push_ca.h` — one pinned Amazon Root CA 1 | Trust migration is a real constraint: moving to a self-hosted endpoint needs either the same public root or a CA bundle build, decided before cutover, not during it. |
| `docs/api-boundaries.md` — `POST /v1/ingest/batches`, Problem Details, `Idempotency-Key`, cursor pagination | Route, error shape and idempotency semantics are taken from the committed contract rather than invented. |

## What was deliberately left open

- **mTLS device identity.** Target state, deferred. Trigger: a firmware release that can
  store a client key, plus a decided PKI and rotation story. Until then, credential rotation
  without reflash is the property that must hold.
- **Compression.** `Content-Encoding` is reserved, not enabled. The limits are small enough
  that compression buys little on a LAN, and an unbounded decompressor is a denial-of-service
  surface. Trigger: a measured link constraint, matching ADR 0001's stance on binary codecs.
- **A second codec.** Unchanged from ADR 0001: a registry edit, not a major bump.
- **Device-side batching and uplink spool.** Firmware phase, gated separately.
- **The scene-tail RPO (issue #107).** Out of scope here. Batch ingest does not close it, and
  saying otherwise would hide a known loss boundary behind a new endpoint.

## Consequences

- One new contract (`hear.ingest.batch.v1` plus its receipt), generated into
  `contracts/schemas/` and `contracts/fixtures/hear.ingest.batch.v1/` from the same field
  table, with eleven fixtures carrying declared outcomes for cross-language adapters.
- No existing writer or reader changes in this phase. The legacy routes remain authoritative.
- The receipt makes the answer "what did you store?" machine-readable for the first time,
  which is what makes dual-write comparison and spool release possible later.
- New infrastructure introduced: none.
