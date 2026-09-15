# Phase 4: HTTPS batch ingest adapter

Design specification for an **additive** HTTPS batch ingest path. Decision record:
[`decisions/0004-phase4-https-batch-ingest-adapter.md`](decisions/0004-phase4-https-batch-ingest-adapter.md).
Item contract: [`decisions/0001-hear-ingest-v1-envelope-and-codec.md`](decisions/0001-hear-ingest-v1-envelope-and-codec.md).
Frame contract: `hear/ingest/batch.py`, generated into
`contracts/schemas/hear.ingest.batch.v1.schema.json`,
`contracts/schemas/hear.ingest.batch.receipt.v1.schema.json` and
`contracts/fixtures/hear.ingest.batch.v1/`.

> **Status.** Design and contracts only. Nothing here is deployed, no path is cut over, and
> no production adapter is written by this document. It is the plan that stage 2 of
> [`standalone-migration.md`](standalone-migration.md) ("dual-write, old readers
> authoritative") is executed against.

## 1. Scope

**In scope:** the batch endpoint and its wire contracts; authentication; sizing and limits;
acknowledgement, idempotency and replay ordering; retry, backoff and offline durability;
timestamp semantics; version and unknown handling; refusal visibility; TLS and key
lifecycle; observability; dual-write comparison, reconciliation and rollback.

**Explicitly not in scope, and not to be added quietly later:**

- A message broker, stream processor, queue service or service mesh. The target is six nodes
  on one machine (`gold`, `kasami`, `ageev`, `nyquist`, `mach`, `rankine`). Durability is a
  local transactional store plus an outbox table, as `standalone-migration.md` already
  specifies. Revisit only on a measured trigger: sustained backlog a single process cannot
  drain within the site SLO, or a second independent consumer that genuinely needs its own
  offsets.
- A device-side batch client or firmware uplink spool. Separate, later, firmware-gated.
- mTLS device identity. Target state; see §11.
- Retiring, disabling or modifying any existing writer or reader. Phase 4 is purely additive.
- Closing the 2 MB scene-tail RPO (issue #107). Batch ingest does not fix it and must not be
  presented as if it did.

## 2. What exists now, and what Phase 4 does to it

Every row below is current in-tree behavior. The right column is the Phase 4 change — and
for most rows it is deliberately "none".

| Writer / reader | Today | Phase 4 |
|---|---|---|
| `firmware/hear_node/hear_node.ino` push | HTTPS JSON, **one record per request**, `POST /api/hear/heartbeat`, `POST /api/hear/event`; `WiFiClientSecure` + one pinned root CA; `X-Hear-Token` or `Authorization: Bearer`; 3 s connect / 2 s read; 2xx only; heartbeat backoff to 60 s; failed event pushes are **not spooled** | **Unchanged.** No reflash, no config change, no new failure mode. |
| same, `HEAR_PUSH_WRAP_BATCH` | Compiles to `POST /ingest/batch` with `{"device_id","messages":[<one item>]}` | Stays **off**. The frame is designed to be shape-compatible with it, but enabling it needs the receipt/spool loop (§7.3) that the firmware does not have. |
| `tools/hear_heartbeat_receiver.py` | Plain HTTP :5051, optional `X-Hear-Token`, SQLite durable write **then** Redis cache, `204` empty body | **Unchanged.** Remains the authoritative single-record path for all of Phase 4, including its ordering guarantee, which §7.2 copies rather than reinvents. |
| `tools/hear_mqtt_bridge.py` | `dama/+/telemetry` QoS 1, topic vs body `device_id` check, **drops malformed before durability** (issue #104) | **Unchanged** in Phase 4. It is the worked example of the defect the batch path must not reproduce (§10). Its migration to the canonical envelope is a later phase. |
| `tools/hear_drain.py` | Pull: `/status`, `/detections` (`cursor-v1`), `/ls`, `/sd`; raw-before-parse; cursor advances only after ingest; 2 MB scene byte tail | **Unchanged as the authoritative writer.** It gains an *optional, off-by-default* shadow submitter that also posts already-archived rows to the batch endpoint (§13). It never becomes a second fetcher: one HTTP client per node, always. |
| `hear/pool.py` readers (`hear.corpus`, `hear.tags`, `hear.validate`, `hear_score.py`, `hear_tdoa.py`) | Read the pool | **Unchanged.** No reader gains a transport-specific key, per ADR 0001. |
| AWS path | Firmware default host is an AWS-API-Gateway-shaped HTTPS endpoint with Amazon Root CA 1 embedded; no `boto3`/SQS/S3 client exists in-tree | Treated as **one more HTTPS producer of the same legacy bodies**. No AWS SDK is introduced. The only AWS-specific Phase 4 work is trust-store migration (§11.2). |
| `dama-gotchi` | Optional producer (`source=gotchi`) | **Unchanged and still optional.** A gotchi outage may not block node ingestion. |
| `deploy/k8s/` | k3s `dama`: CronJobs, PVCs, Redis, external MQTT broker; no proxy, no TLS terminator, no PostgreSQL server, no queue | One new ingress route and one TLS terminator (§11.1). No broker, no queue, no mesh. |

## 3. API

### 3.1 Route and media types

```
POST /v1/ingest/batches
Content-Type: application/vnd.dama.hear.ingest.batch.v1+json   (application/json accepted)
Accept:       application/vnd.dama.hear.ingest.batch-receipt.v1+json
```

Route and conventions are taken from [`api-boundaries.md`](api-boundaries.md), which already
lists `POST /v1/ingest/batches` and mandates `Idempotency-Key`, RFC 9457 Problem Details and
`X-Request-ID`. A compatibility alias `POST /ingest/batch` MAY be mounted so the existing
`HEAR_PUSH_WRAP_BATCH` shape reaches the same handler; it is an alias only, with identical
semantics, and it does not get its own contract.

### 3.2 Request headers

| Header | Required | Meaning |
|---|---|---|
| `Authorization: Bearer <device-credential>` | yes | §4. `X-Hear-Token` accepted only on the legacy routes, never here. |
| `Idempotency-Key` | yes | Opaque, ≤128 chars. Scoped to site + principal + route (`batch.idempotency_scope`). |
| `Content-Type` | yes | Registered batch media type. Anything else is `415`. |
| `Content-Length` | yes | Absent or chunked bodies are refused; the size bound must be checkable before reading. |
| `X-Request-ID` | no | Echoed; generated if absent. |
| `Content-Encoding` | no | Reserved. Not enabled in Phase 4 (unbounded decompression is a DoS surface). |

### 3.3 Request frame

```json
{
  "batch_schema_version": 1,
  "batch_id": "018f2c1a-batch-0007",
  "device_id": "nyquist",
  "sent_at": "2026-09-14T18:03:11.990000Z",
  "messages": [ { "...": "hear.ingest.v1 envelope, or a legacy telemetry body" } ],
  "producer": {
    "boot_id": "018f2b90-5f3c-7c21-9a7e-1d2c3b4a5e6f",
    "boot_epoch_us": 1789495200000000,
    "batch_sequence": 7,
    "spool_backlog": 118
  },
  "adapter": {"name": "hear-drain-shadow", "version": "0.1.0"}
}
```

`producer` and `adapter` are optional and additive. Their absence means the producer exposes
no continuity evidence — not that continuity is fine. `spool_backlog` is the producer's own
count of what it still holds; it is a backpressure input (§12), never a correctness input.

### 3.4 Receipt

```json
{
  "batch_schema_version": 1,
  "batch_id": "018f2c1a-batch-poison-1",
  "received_at": "2026-09-14T18:03:12.100000Z",
  "ack_through_index": 2,
  "counts": {"submitted": 3, "accepted": 2, "duplicate": 0, "refused": 1, "deferred": 0},
  "results": [
    {"index": 0, "status": "accepted", "event_id": "ebee7b5c-…", "dispatchable": true,
     "reasons": [], "raw_ref": "raw/2026/09/14/nyquist/…", "classification": "canonical"},
    {"index": 1, "status": "refused", "event_id": null, "dispatchable": false,
     "reasons": ["field_missing"], "raw_ref": "raw/quarantine/…", "classification": "canonical"},
    {"index": 2, "status": "accepted", "event_id": "25c4998a-…", "dispatchable": true,
     "reasons": [], "raw_ref": "raw/2026/09/14/nyquist/…", "classification": "canonical"}
  ],
  "retry_after_s": null,
  "server": {"adapter": "ingest-batch", "version": "0.1.0", "envelope_major": 1}
}
```

The receipt does not echo item bodies. A producer that needs the stored form re-reads it by
`event_id`; quoting bodies back would double every batch's bytes on the link that is already
the constraint.

### 3.5 Status codes

| Code | When |
|---|---|
| `200` | Frame admitted. **Per-item outcomes are in the receipt, including refusals.** A refused item is not an HTTP error: it is a durable, attributable result. |
| `400` | Undecodable body, `not_an_object`, malformed frame fields. |
| `401` | Missing/invalid/expired credential, or no credential identity resolved (`credential_missing`). The route is never served open. |
| `403` | Valid credential lacking `ingest:write`, or authorized for a different site. |
| `409` | `Idempotency-Key` reused with a different body fingerprint. |
| `413` | Body over `max_batch_bytes`, rejected before decoding. |
| `415` | Unregistered media type. |
| `422` | Frame decoded but refused: `batch_schema_version_unsupported`, `batch_empty`, `batch_too_many_items`, `batch_id_invalid`, `device_identity_mismatch`. |
| `429` | Rate limit or admission control; `Retry-After` set. |
| `503` | Durable store unavailable; `Retry-After` set. Nothing was stored, nothing is acknowledged. |

Errors use RFC 9457 Problem Details with a stable `code`, per `api-boundaries.md`. Every
`4xx` frame refusal still produces a durable refusal record (§10) — an HTTP error code is a
response, not an accounting entry.

## 4. Authentication and authorization

1. **Per-device bearer credentials over TLS.** One credential per device identity, not one
   shared fleet token. Today's receiver treats `HEAR_AUTH_TOKEN` as optional and disables
   auth when unset; on `/v1/ingest/batches` auth is **mandatory**, and a build that cannot
   load a credential store refuses to serve the route rather than serving it open.
2. **Identity comes from the credential, at both layers.** `site_id` is derived from the
   credential and never read from the body (ADR 0001 field note, `api-boundaries.md` rule).
   `device_id` in the **frame** is cross-checked against the credential; a mismatch is `422`
   `device_identity_mismatch` — refused, not silently corrected — mirroring the MQTT bridge's
   topic/body cross-check. Each **item** is checked too, and that check is the one that
   matters: `device_id` is an input to the closed identity tuple, so an authenticated node
   smuggling an item attributed to a neighbour would mint a durable, dispatchable event for a
   node that never sent it — and, because dedup is by `event_id`, could collide with that
   node's real events. A device-scoped credential pins every item's `device_id`
   (`item_identity_mismatch`); a site-scoped gateway credential pins `site_id`
   (`item_site_mismatch`) and may submit for many devices.
3. **No credential means refusal, never a free pass.** A missing credential identity is
   `credential_missing`, not "skip the check". The reader takes the credential as a required
   argument so an auth layer that failed open cannot be mistaken for one that authorized the
   caller.
4. **Scope.** Device credentials carry `ingest:write` only. They are not usable on query,
   export or configuration routes.
5. **Gateway submissions.** When a drain/import adapter submits on a node's behalf it
   presents its own service credential, sets `adapter` in the frame, and is authorized for
   the site rather than for a single device. Its submissions are attributable to the adapter,
   not laundered into looking like device-originated traffic.
6. **Rotation without reflash.** Credentials are overlapping-window: a new credential is
   valid before the old one is revoked, so rotation never requires a firmware reflash. This
   is a hard requirement from `standalone-migration.md` ("never require a firmware reflash to
   rotate server credentials or migrate storage").
7. **No credential material in logs, receipts, events or metrics labels.**

## 5. Batch sizing, framing and limits

Server-enforced, published in the schema under `x-limits`, and mirrored in the fixture
manifest so a cross-language adapter reads them instead of guessing:

| Limit | Value | Why this number |
|---|---|---|
| `max_items_per_batch` | 64 | A drain page and an ESP32's plausible spool flush both fit; small enough that one refused frame costs little to resend. |
| `max_batch_bytes` | 262 144 (256 KiB) | Checked **before** decoding, against `Content-Length` and the read count. Fits comfortably in a single-process handler and in an ESP32's TLS write loop without heap pressure. |
| `max_item_bytes` | 65 536 (64 KiB) | Canonical form of one item. Comfortably above a `hear/wire.py` frame or a scene row; low enough that one item cannot monopolize a batch. |
| `max_clock_skew_s` | 900 | Sanity bound on frame `sent_at` only (§8). Never applied to `observed_at`. |

Framing rules:

- The frame is a single JSON object. Items are in producer order in `messages`. Order is
  preserved for acknowledgement (§6) and is **not** identity.
- One item per observation. The frame never merges, splits or re-times items.
- Oversized **item** → that item is refused (`item_too_large`); the batch proceeds. Oversized
  **body** → `413` for the frame; nothing is interpreted.
- The frame reader classifies each item shallowly — `canonical`, `translation_required`,
  `unrecognized` — and never interprets a legacy body itself. Translation is the adapter's
  attributable act (`adapter.name`/`adapter.version` on the resulting envelope).
- A `translation_required` item has **no receipt-ready outcome**. The adapter translates it
  and resolves it to a real status; a receipt may not be issued while any item is still
  untranslated (`build_receipt` raises). Reporting an untranslated item as `deferred` would
  tell the producer to resend a body that will be classified identically every time — an
  infinite loop over a legacy node's entire backlog. A translation that cannot succeed
  resolves to `refused` with a reason, which is durable and therefore acknowledged.
- A body that cannot be decoded safely — invalid UTF-8, `NaN`/`Infinity` literals the
  canonical form cannot represent, or nesting deep enough to exhaust the JSON decoder — is a
  `400` refusal with a durable record, never an unhandled crash with no accounting entry.

## 6. Acknowledgement, idempotency, replay and ordering

### 6.1 Conservation

`submitted = accepted + duplicate + refused + deferred`, enforced in `batch.counts()` and
asserted by fixtures. This is the same equation `standalone-migration.md` requires of
historical imports, extended with `deferred` for "not durable, retry it". There is no fifth
state and no silent drop.

### 6.2 Acknowledgement

`ack_through_index` is the last index made durable **with no gap before it**; `-1`
acknowledges nothing. A producer may free spooled items only through that index. If index 3
is durable but index 2 is only `deferred`, the acknowledgement stops at 1 — acknowledging 3
would let the producer delete a record the server never stored.

A `refused` item **inside** the acknowledged prefix is acknowledged. It is durable as a
refusal record, so it must never be resent; resending it would loop forever on a body the
server will never accept. `batch.unacknowledged_indices()` is the normative retry set.

### 6.3 Idempotency

- **Permanent:** `event_id` from ADR 0001's closed identity tuple. A retried, reordered or
  differently re-split batch converges on the same durable events. An item whose `event_id`
  is already durable is `duplicate`, which is a **success**, not an error.
- **Transport-level:** `Idempotency-Key`, scoped to site + principal + route, retained ≥24 h
  with its stored receipt. Same key + same body fingerprint replays the stored receipt
  byte-for-byte. Same key + different fingerprint is `409`, because returning either receipt
  would be a lie about which body was stored.
- `batch_id` is **not** a dedup key. It correlates a receipt with a log line and nothing else.

### 6.4 Replay ordering

Ordering is producer-local evidence, never a global sequence:

- Within one batch, item order is the producer's order and drives only `ack_through_index`.
- Across batches, `producer.boot_id` + `producer.batch_sequence` detect gaps. A `boot_id`
  change means sequences are not comparable, exactly as `hear_drain.py` already treats a
  changed boot ID as a cursor discontinuity rather than a continuation.
- `producer.cursor` in each item preserves the drain's `cursor-v1` position so a gap remains
  visible after ingest.
- Late arrival is normal, not an anomaly. An SD backlog replayed days later is ingested on
  its `observed_at`, and downstream readers are already at-least-once safe.
- The server never reorders, never buffers items to restore order, and never rejects an item
  for arriving out of order.

## 7. Retry, backoff and offline durability

### 7.1 Client rules

- Retry only `408`, `429`, `5xx` and transport failures. Never retry `400`, `401`, `403`,
  `409`, `413`, `415` or a non-retryable `422` — those are `api-boundaries.md`'s rules and
  they are unchanged here.
- Exponential backoff with full jitter, capped at 60 s, matching the firmware heartbeat
  backoff that already exists (`hear_node.ino:2556-2570`) rather than inventing a second
  policy.
- `retry_after_s` in a receipt, and `Retry-After` on `429`/`503`, raise the floor of the next
  attempt. They never lower it and never substitute for client backoff.
- On retry, resend the **same** `Idempotency-Key` and the same bytes. After a partial
  acknowledgement, send a **new** batch containing only `unacknowledged_indices`, with a new
  `batch_id` and a new key.

### 7.2 Server durability ordering

Raw bytes are written first, then the durable record, then any cache. Concretely:

1. Retain the raw frame bytes (atomic temp-then-rename), before parsing — the rule
   `hear_drain.py` already follows and the rule the MQTT bridge violates (issue #104).
2. Decode, validate the frame, classify items.
3. Per item: write the inbox row keyed by `event_id` and the normalized record in one
   transaction, with the outbox record in the same transaction.
4. Only then update Redis or any projection. Redis may accelerate; it can never be the only
   acknowledgement, and its failure must not un-acknowledge a durable item. This is exactly
   the ordering `hear_heartbeat_receiver.py` already implements (durable SQLite, then cache,
   retain for replay on cache failure).
5. Build and store the receipt **after** step 3, so `ack_through_index` can never describe
   something that is not durable. If the process dies between 3 and 5, the retry finds the
   items already durable and reports them as `duplicate` — the same outcome by a slower path.

### 7.3 Offline durability

- The node's SD card remains the capture authority. A network outage must never become
  capture loss; nothing in Phase 4 changes node capture behavior.
- Server-side producers (drain/import shadow submitter) spool in their existing durable
  storage and release only through `ack_through_index`.
- A device-side uplink spool is **not** built in Phase 4. Until firmware can hold one,
  batch ingest recovers a node's backlog the way it already does: `hear_drain.py` pulls it.

## 8. Timestamp semantics

Three distinct times, never conflated, matching ADR 0001 and the fleet's clock reality:

| Field | Meaning | Rules |
|---|---|---|
| `observed_at` (item) | When the observation happened | RFC3339 UTC or `null`. The only time a solver may use. `clock.valid=true` with a null `observed_at` is refused as self-contradictory. |
| `received_at` (item/receipt) | When the adapter accepted the bytes | Server-assigned. **Never** a producer claim. |
| `sent_at` (frame) | When the producer handed the batch to the transport | Producer claim, RFC3339 UTC or `null`. Diagnostic only. |

- Timestamps are RFC3339 **UTC with `Z`**. `+00:00` is refused
  (`timestamp_not_rfc3339_utc`), never coerced. The firmware already emits `...Z`.
- `clock.tier` (`gps_pps` / `wall` / `monotonic`) and `clock.sigma_ns` travel with every
  item. Wall and monotonic time are never treated as PPS time, and a batch never upgrades an
  item's clock tier.
- A node without a usable anchor sends `observed_at: null` with `clock.valid: false`. The
  item is stored and replayable; it is not a TDoA arrival. Ingest never invents a time —
  substituting `received_at` for a missing `observed_at` would manufacture arrivals.
- Frame `sent_at` beyond `max_clock_skew_s` from server time is a **warning metric**, not a
  refusal. A node whose clock is wrong is exactly the node whose data is most needed.
- `producer.boot_epoch_us` plus item `producer.sequence` reconstruct pre-anchor ordering
  without claiming wall time, which is what the firmware's `ts_ms = uptime_s*1000 + 1`
  convention already encodes.

## 9. Version and unknown handling

| Layer | Unsupported major | Unknown field | Unknown enum member |
|---|---|---|---|
| Frame (`batch_schema_version`) | Refuse the **whole** frame before reading any item, with `batch_schema_version_unsupported` and a durable refusal record. A v2 frame may reuse `messages` with different framing, so item interpretation would be a guess. | Accept and preserve verbatim (`additionalProperties: true`). | Preserved, reported, never guessed. |
| Item (`schema_version`) | Refuse **that item only**, with `schema_version_unsupported` and a durable refusal record. The frame and its siblings proceed. | Accept and preserve verbatim; reported as a warning. | Accept and preserve; the record becomes **non-dispatchable** — stored and replayable, but never handed to a worker that would have to guess the member's meaning. |

- `strict_unknown` remains producer-side CI only. A reader that enabled it would turn a newer
  peer's additive field into a durable refusal and make rollout order load-bearing.
- A legacy telemetry body (`telemetry_path` / `telemetry_schema_version`) is
  `translation_required`, not an error. Every node emits that shape today.
- A server release must read old nodes before any firmware rollout. The batch route is
  additive precisely so that ordering holds.
- Adding a codec is a registry edit (ADR 0001). Changing the identity tuple, narrowing a
  type, or changing a field's meaning is a major bump at the layer that owns the field.

## 10. Durable refusal visibility

**No refusal is a drop.** Issue #104 exists because the MQTT bridge refuses before anything
durable is written, which makes row conservation unclosable. The batch path must not repeat
that.

1. Raw bytes are retained before parsing, for both frames and items, under an immutable key
   (`raw/<site>/<device>/<batch_id>/<sha256>`, matching the existing raw-key convention).
2. Every refusal writes a durable refusal record: `raw_sha256`, `raw_bytes`, machine
   `reasons`, `source`, `adapter`, `received_at`, `raw_ref`, and `batch_id` for frames
   (`batch.batch_refusal_record`, `envelope.refusal_record`).
3. Refusal reasons are stable machine codes, never prose: `batch_schema_version_unsupported`,
   `batch_empty`, `batch_too_many_items`, `batch_id_invalid`, `device_identity_mismatch`,
   `credential_missing`, `item_unrecognized`, `item_too_large`, `item_not_canonicalizable`,
   `item_identity_mismatch`, `item_site_mismatch`, plus the item-level
   codes from ADR 0001 (`schema_version_unsupported`, `field_missing`, `type_invalid`,
   `value_out_of_range`, `timestamp_not_rfc3339_utc`, `clock_valid_without_observed_at`).
4. Refusals are visible three ways: in the receipt (immediately, to the producer), as a
   durable quarantine record (queryable by device, day and reason), and as a counter with a
   `reason` label (§12).
5. Refusals are replayable. A fixed adapter re-reads quarantine and replays; canonical rows
   are never hand-edited. `decode_ValueError`-class failures (issue #104) are refusal
   records, not reasons to discard rows.
6. A refusal spike is an alert, and a **zero-refusal** period on a path known to carry
   malformed rows is also suspicious — a zero-result import is a failure, not a quiet success.

## 11. TLS and key lifecycle

### 11.1 Transport

- TLS 1.2 minimum, 1.3 preferred, terminated at a cluster ingress in front of the ingest
  handler. No such terminator is checked in today; adding one is the single new piece of
  infrastructure Phase 4 introduces.
- Plaintext HTTP is not served for `/v1/ingest/batches` in any environment.
- The existing plain-HTTP receiver on :5051 stays cluster-internal and unchanged.
- Certificates are renewed automatically with an overlap window; renewal never requires a
  node reflash or a node restart.

### 11.2 Device trust store — decide before cutover, not during

The firmware pins **one** root CA (Amazon Root CA 1) via `setCACert`, sized for the current
AWS-shaped endpoint. Moving nodes to a self-hosted endpoint therefore has exactly three
options, and picking one is a prerequisite for any future cutover:

1. Issue the ingest certificate from a **public CA whose root the node already trusts**, or
2. ship a firmware build using a **CA bundle** containing both the current root and the new
   one, before any endpoint change, or
3. accept a reflash at cutover — which contradicts "never require a reflash to rotate
   credentials or migrate storage" and is recorded here only to be rejected.

Option 2 is preferred: the bundle is deployed while the old endpoint still works, so trust
migration and endpoint migration are separate, independently reversible steps.
`HEAR_PUSH_TLS_INSECURE` is a build-time escape hatch and must fail CI for any release build.

### 11.3 Credential lifecycle

- One credential per device identity, issued at enrollment, stored server-side as a hash.
- Overlapping rotation windows; revocation is immediate and takes effect without a reflash.
- Credentials are never logged, never in receipts, never in metric labels, never committed.
- A lost or suspect credential is revoked per device; there is no shared fleet token to
  revoke, which is the main reason per-device credentials are mandatory on this route.
- mTLS remains the target (`api-boundaries.md`). Trigger to adopt: firmware that can store a
  client key, plus a decided PKI and rotation story.

## 12. Observability

Metrics (labels: `site`, `device_id`, `source`, `adapter`, `reason`, `status`; never tokens
or clip bytes):

| Metric | Type | Use |
|---|---|---|
| `ingest_batches_total{status}` | counter | admitted vs refused frames |
| `ingest_items_total{status,reason}` | counter | the conservation equation, live |
| `ingest_items_nondispatchable_total` | counter | forward traffic from a newer writer |
| `ingest_batch_items` | histogram | actual batch occupancy vs the 64 limit |
| `ingest_batch_bytes` | histogram | headroom against the 256 KiB bound |
| `ingest_ack_gap_items` | histogram | `submitted − (ack_through_index + 1)`; sustained non-zero means durability is falling behind |
| `ingest_lag_seconds` | histogram | `received_at − observed_at`; p99 is the site SLO signal |
| `ingest_refusals_total{reason}` | counter | alert on spikes **and** on implausible zeros |
| `ingest_idempotent_replays_total` | counter | retry storms and client bugs |
| `ingest_idempotency_conflicts_total` | counter | `409` key reuse: always a client defect |
| `ingest_producer_spool_backlog` | gauge | from `producer.spool_backlog`; backpressure input |
| `ingest_sequence_gaps_total` | counter | `batch_sequence` gaps within a `boot_id` |
| `ingest_clock_skew_seconds` | histogram | frame `sent_at` vs server time |
| `ingest_dual_write_mismatch_total{class}` | counter | §13; the Phase 4 exit gate |

Every request carries `X-Request-ID` and is logged with site, device, `batch_id`, item
counts and latency. Every response and every stored record identifies the answering adapter
and build (`server.adapter`, `server.version`, `server.envelope_major`), so a bad translation
or a bad rollout is attributable to a release. Dashboards and mismatch receipts for the
dual-write comparison are specified by the companion task
`phase4-dual-write-observability`; this document defines the counters they read.

## 13. Dual-write, shadow compare and reconciliation

Phase 4 executes stage 2 of `standalone-migration.md` and stops there. Legacy readers stay
authoritative throughout.

### 13.1 Dual-write rules

- One validated envelope produces both writes. The canonical write never re-derives, re-times
  or re-profiles what the legacy write recorded.
- If the canonical write fails, the legacy write stands and an alert fires. If the legacy
  write fails, canonical capture continues and exposes the adapter failure. Neither path may
  silently mask the other.
- HTTP drain data is archived **once** and fanned out. There is never a second fetcher
  competing for a node's single HTTP client — the drain's shadow submitter posts rows it has
  already archived, and posts nothing it has not.
- The gotchi adapter may dual-write; no gotchi outage blocks node ingestion.
- Dual-write is behind a per-source flag, default off, enabled one source at a time.

### 13.2 Shadow comparison

Compare by **stable identity and source partition, never by arrival order**:

| Class | Tolerance |
|---|---|
| Raw objects | byte-exact; SHA-256 must match |
| GPS-anchored timestamps | exact |
| Unanchored timestamps | compared as `null`/tier, never as a value |
| Counters and row counts | exact; conservation must close per source/day/node |
| Solver and model outputs | recorded numerical tolerance plus model-card identity |

Sample at least one complete day per board class (`esp32s3-i2s-gps`, `xiao-s3-pps`) and one
historical pool partition. The sample must include: a node outage and reconnect, MQTT
duplication and reordering, a Redis loss, a replayed batch, and a read-only PVC failure.

### 13.3 Reconciliation

- A scheduled job joins legacy and canonical records on `event_id`, emits a mismatch receipt
  per difference (identity, class, both values, adapter/build on each side), and counts them
  by class.
- Repair is **replay only** — replay the inbox/outbox. Canonical rows are never hand-edited.
- Exit gate for the dual-write stage: seven days with zero unexplained `event_id` collisions,
  no missing canonical objects, conservation closing per source/day/node, bounded lag (p99
  within the site SLO), and every remaining difference classified as an expected clock-tier
  or schema difference. Legacy views remain the operator default at the end of Phase 4.

## 14. Rollback

Rollback is configuration-first, and it is cheap precisely because Phase 4 is additive.

| Trigger | Action |
|---|---|
| Canonical persistence loses or duplicates events outside the declared idempotency rule | stop advertising the batch route; legacy paths never stopped |
| Raw checksum mismatch, or conservation does not close | disable dual-write for that source; quarantine and replay |
| Shadow reads disagree beyond tolerance | disable comparison-driven changes; keep collecting mismatch receipts |
| p99 ingest lag breaches the site SLO for 15 minutes | apply `retry_after_s` backpressure; if unresolved, disable the route |
| Restore cannot reproduce a known event | full stop on the phase; no further enablement |
| A node's capture or SD behavior changes | immediate rollback; capture is never traded for ingest |

Procedure: disable the batch route by configuration → keep both writers running → replay
canonical inbox rows after repair → **code rolls back before data**. An additive schema
problem is fixed forward, never by a database downgrade. Nothing is deleted in the same
release as a code change: no objects, no Redis keys, no PVC snapshots. Firmware rollback is a
separate canary decision and is never inferred from a server rollback.

Because no existing writer or reader is modified in Phase 4, the worst case is that the batch
route is switched off and the system is exactly what it is today.

## 15. Phase 4 gates

1. Contracts generated, fixtures green, `tools/gen_ingest_contracts.py --check` clean in CI.
2. Ingest handler built against the contracts, with the raw-before-parse and
   durable-before-cache ordering of §7.2, and a replay test that survives a kill between the
   durable write and the receipt.
3. Auth, TLS terminator and credential rotation demonstrated **without a reflash**.
4. Dual-write enabled for one source, then one more, never all at once.
5. Seven-day reconciliation report passing §13.3.
6. Rollback drill executed, not just documented.

Gates 2–6 are later work. This document and its contracts are gate 1.

## 16. Open questions

These are recorded rather than silently decided. None blocks the contract work.

1. **Site SLO for p99 ingest lag.** Referenced by the migration document, never given a
   number. Needs an operator decision before gate 5 can pass.
2. **Quarantine retention.** How long refusal records and their raw bytes are kept is a
   retention/destruction decision, which `standalone-migration.md` requires to be approved
   separately.
3. **Public CA versus private PKI** for the self-hosted ingest endpoint (§11.2). Decide
   before any endpoint migration; it determines whether a firmware bundle build is needed.
4. **Credential issuance mechanics** at enrollment — owned by the Enrollment service, which
   does not exist yet. Phase 4 assumes only "per-device, rotatable without reflash".
