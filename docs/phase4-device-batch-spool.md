# Phase 4: device-side batch client and uplink spool

Design specification for the **device side** of `hear.ingest.batch.v1`: a bounded, durable
uplink spool on a node and the client that drains it into `POST /v1/ingest/batches`.

Decision record: [`decisions/0008-device-side-batch-client-spool.md`](decisions/0008-device-side-batch-client-spool.md).
Server contract: [`phase4-https-batch-ingest.md`](phase4-https-batch-ingest.md) and
[`decisions/0004-phase4-https-batch-ingest-adapter.md`](decisions/0004-phase4-https-batch-ingest-adapter.md).
Item contract: [`decisions/0001-hear-ingest-v1-envelope-and-codec.md`](decisions/0001-hear-ingest-v1-envelope-and-codec.md).
Fleet order and per-node card state: [`fleet-release-rollout-sequence.md`](fleet-release-rollout-sequence.md) §4, §6, §12.

> **Status. Design only, and nothing here is built.** No firmware file is modified by this
> document, no image is produced, no node is flashed, no credential is created or handled, no
> cluster object is changed, and no transport is cut over. Device behaviour after this
> document is **byte-for-byte what it is today**: one record per HTTPS request, no spool,
> `hear_drain.py` authoritative. The spool described here is compile-time absent
> (`HEAR_SPOOL` undefined) until a later, explicitly authorized implementation task.
>
> `phase4-https-batch-ingest.md` §1 and §7.3 deferred this deliberately ("A device-side batch
> client or firmware uplink spool. Separate, later, firmware-gated"). This document is that
> separate work, still on paper.

---

## 1. Scope

**In scope:** what a node may hold, where it holds it, how much, how it is bounded and
evicted; how a spooled record is identified and ordered; how batches are framed, sent,
acknowledged, retried and abandoned; what happens across power loss, corruption, reboot,
clock loss, OTA and rollback; the credential and trust boundary the client sits behind; what
the node reports about itself; and how all of it is turned off.

**Explicitly out of scope, and not to be added quietly later:**

- Any change to capture. The SD card is the capture authority and the detection ring, clip
  ring and CSV writers keep exactly the priority they have today (§3.4).
- Clip (WAV) or `scene.csv` upload. A clip is 480,044 B against a 65,536 B item limit and
  0.8 MB/h of scene rows against a link that already struggles; both stay on the
  `hear_drain.py` pull path. The spool carries detections, events and *one* heartbeat (§4.3).
- Replacing `hear_drain.py`. It stays the authoritative writer and the recovery path for
  everything the spool drops (§3.5). That is what makes aggressive spool bounds safe.
- mTLS device identity, a new credential store, or any token value. §9 states the boundary
  only, and `decisions/0006-admin-token-provisioning-policy.md` owns the open decision.
- LoRa/Meshtastic uplink. A 237 B payload is a different transport with a different contract
  (`docs/uplink.md`); `esp32s3-lora-pps` is not even a built board.
- Closing the 2 MB scene-tail RPO (issue #107), which this does not fix.

---

## 2. What exists today, and what this design would change

Every row is current in-tree behaviour. The right column is what the *implementation task*
would change — and this document changes none of it.

| Surface | Today | Under this design |
|---|---|---|
| `push_post_json()` (`hear_node.ino:2749`) | one record per `POST`, `WiFiClientSecure`, one pinned root CA, 3 s connect / 2 s read, reads the status line only, discards the body | unchanged; the batch client is a **second, additive** sender that reuses the same socket discipline and never runs concurrently with it |
| `HEAR_PUSH_WRAP_BATCH` (default **1**) | already wraps the single record as `{"device_id","messages":[one]}` and posts `/ingest/batch` | unchanged. ⚠️This is the correction §2 of `phase4-https-batch-ingest.md` needs: the wrapper is **on** in the default build, so nodes already emit a one-item batch-shaped body to the AWS endpoint. What is off is the *receipt/spool loop*, not the wrapper. |
| failed event push | dropped; only the heartbeat is rescheduled with backoff | the record would instead be appended to the spool, if and only if `HEAR_SPOOL` is compiled in and enabled |
| `dets.csv` / detection ring | ring is `DET_RING_MAX` 1024 slots in PSRAM, cursor namespace is `det_boot_id`, rows flushed to the card | unchanged and still authoritative; the spool is a *second copy* of the uplink item, not a replacement for the row |
| `sd_cache_prune()` + `sd_retention_policy.h` | capacity-relative free target (10%, capped 25%, floor 8 MiB); classifies each path `PROTECTED` / `ROLLING` / `UNKNOWN_ROLLING` | needs one new class for `/spool/` before the spool may be enabled (§3.6) — **the single blocking firmware prerequisite** |
| `tools/hear_drain.py` | pulls `/status`, `/detections` (`cursor-v1`), `/ls`, `/sd`; raw-before-parse; cursor advances only after ingest | unchanged, authoritative, and the recovery path for any spool loss |
| `tools/hear_mqtt_bridge.py`, `tools/hear_heartbeat_receiver.py` | unchanged legacy ingest | unchanged. The node has **no MQTT client**; "MQTT fallback" means the server-side legacy path, not a device one (§11.3) |
| NVS `hear_prov` record | `ptoken` / `atoken` per device, `HEAR_PROV_TOKEN_MAX`-bounded, written at enrollment | unchanged mechanism; the batch client reads `push_token_runtime()` and nothing else (§9) |

---

## 3. Where the spool lives, and what bounds it

### 3.1 Three candidate media, and why only one survives

| Medium | Size | Verdict |
|---|---|---|
| NVS (`nvs` partition, 0x5000 = 20 KiB on the stock table; 32 MB PUC table identical) | 20 KiB total, shared with Wi-Fi credentials, node id, class, `ptoken`, `atoken` | **Refused for records.** It is the credential store, it is not sized for a backlog, and NVS is wear-limited flash the fleet cannot afford to rewrite per batch. It may hold **nothing per-record**; see §3.3 for why even the ack watermark is not kept there. |
| App flash / a new data partition | `default_8MB` gives 2×3 MB OTA slots; PUC's table gives 2×3 MB + 26 MB `ffat` | **Refused on ESP32-S3 nodes.** Repartitioning a deployed node is a full erase over USB — exactly the "never require a reflash" rule `standalone-migration.md` sets. Only the PUC has spare FAT, and the PUC is deferred (§8.4). |
| SD card, `/spool/` | measured per node; see §3.2 | **Chosen.** It is already the capture authority, already power-loss-tested by the CSV writers, already has a free-space discipline, and costs no partition change. |

### 3.2 Card reality: capacity is per node and must be measured, never assumed

The fleet does not have one card size. `hear_node.ino:4603` records nyquist shipping on a
**Raspberry Pi boot card whose FAT partition is 40 MB, about half of it kernel images**, and
`hear_node.ino:2519` records the opposite extreme — a **30 GiB** card that an old fixed 6 MiB
clip budget made "behave like a 40 MiB card". `scene.csv` alone costs ~0.8 MB/h
(`hear_node.ino:2178`: ~227 B/row × 1.024 s rows = 9.6 MB per 12 h), and `health.csv` another
0.3 MB per 12 h.

Therefore:

- The spool budget is **derived at boot from `SD.totalBytes()`/`SD.usedBytes()`**, exactly as
  `sd_cache_target_free_bytes()` already derives the cache target, and is re-derived after
  every prune. No constant megabyte figure is compiled in as a promise.
- The spool's own cap is `min(SPOOL_MAX_BYTES, 10% of free-at-boot)` with
  `SPOOL_MAX_BYTES = 3 MiB` and a hard record cap `SPOOL_MAX_RECORDS = 3072`, whichever binds
  first. On the 20 MB-free nyquist card that is ~2 MB and ~7 days of that node's event rate;
  on a 30 GiB card it is still 3 MiB, because a bigger spool buys nothing the drain does not
  already recover (§3.5).
- A node whose free space is below the retention floor (`SD_CACHE_FREE_FLOOR_BYTES`, 8 MiB)
  gets **zero** spool budget and reports `spool.disabled_reason: "card_headroom"`. It does
  not get a small spool; a card that tight has a capture problem, not an uplink problem.

### 3.3 Layout

```
/spool/                     (a directory, so one classifier rule covers all of it)
  seg-<gen>-<nnnn>.spl      sealed or open segment, append-only, 64 KiB soft cap each
  ack-a.state               watermark slot A   (".state" -> PROTECTED today)
  ack-b.state               watermark slot B
```

- **Segments are append-only and immutable once sealed.** A record is never rewritten in
  place — not to add a timestamp, not to mark it sent. Space is reclaimed only by **unlinking
  a whole segment** whose every record is at or below the watermark. FAT plus an SD library
  with no atomic in-place update is not a transactional store, and pretending otherwise is how
  a half-written record becomes a silently mutated event.
- **Record frame** (little-endian, fixed 16 B header):

  | Field | Bytes | Meaning |
  |---|---|---|
  | `magic` | 2 | `0x48 0x53` (`HS`). A resync point for a torn scan. |
  | `fmt` | 1 | spool record format major. Unknown major ⇒ the reader stops, never guesses (§10.2). |
  | `flags` | 1 | bit 0 `clock_valid`, bit 1 `coalescable` (heartbeat), rest reserved zero. |
  | `seq` | 4 | this boot's monotonic spool sequence (§5.1). |
  | `len` | 4 | canonical item byte length, ≤ `SPOOL_MAX_RECORD_BYTES` (4096) |
  | `crc32` | 4 | CRC-32 over the payload bytes only |
  | payload | `len` | the canonical `hear.ingest.v1` item **as it will be sent**, byte-for-byte |

- The payload is stored **already encoded**. The batch client never re-encodes, never re-times
  and never re-derives an item at send time; it concatenates bytes. That is what makes a
  retry byte-identical (§7.4) and what makes `Content-Length` computable before the socket is
  opened (§6.2).
- **The watermark is two slots, not one.** `ack-a.state` / `ack-b.state` each carry
  `{fmt, boot_id, gen, acked_seq, oldest_seq, crc32}`; the reader takes the slot with the
  higher `gen` **and** a valid CRC. A single file plus "atomic temp-then-rename" is not
  available here: `hear_drain.py` gets that guarantee from POSIX, FATFS via the Arduino `SD`
  library does not, and a power cut during the rewrite of a single watermark would leave the
  node unable to say what had been acknowledged. Two slots written alternately means the
  previous good watermark always survives, and the worst case is re-sending an
  already-acknowledged batch — which the server reports as `duplicate`, a success (§7.3).
- The watermark is on the **card**, not in NVS, for the same wear reason as §3.1: it is
  rewritten once per receipt, which at the target batch rate is thousands of writes a day.

### 3.4 Append is bounded, and capture always wins

- One record append per `loop()` pass, maximum, in the style `CLIP_CHUNK_B` already
  establishes: a 480,044 B clip is written 4096 B per pass precisely so the I2S DMA (6 × 240
  frames ≈ 30 ms) is never starved. A ≤4 KiB spool record is one chunk.
- The append path calls `boot_wdt_service()` on the same schedule the push path does, and is
  called **only from `loop()`** — never from an ISR, never from a second task, and never from
  inside a card write already in progress. All of its buffers are `static`, for the reason
  `hear_node.ino:2612-2626` records in full: sizing the push buffers correctly put ~2.7 kB of
  stack live at the same moment `WiFiClientSecure`'s mbedtls handshake wanted several kB of
  the Arduino loop task's 8 kB stack, and nyquist panicked twice on real hardware.
- Before opening a spool file the writer asks `sd_cache_prune()` for headroom exactly as every
  other writer does. If headroom cannot be had, **the append is refused and counted**
  (`spool_refused_append`). It never evicts a clip, a detection row or a scene row to make
  room for an uplink copy. The card's job is the record; the spool is a convenience.
- A detection row reaches `dets.csv` **before** its copy reaches the spool. If the two orders
  are ever in tension, the card wins: an unspooled detection is a latency problem, an
  unwritten row is a data loss.

### 3.5 Eviction: oldest-first, loudly, and safe because the drain exists

When the spool is at its byte or record cap and a new record arrives:

1. Drop the **oldest unacknowledged** records (whole segments where possible), not the newest.
   The newest record is the one the operator is most likely to be waiting on, and the oldest
   is the one `hear_drain.py` has had the most opportunity to have already pulled.
2. Count every dropped record in `spool_evicted`, advance `oldest_seq`, and expose the count
   in `/status` and the heartbeat. **A silent drop is the failure this fleet keeps writing
   incidents about** (issue #104, `REDESIGN-LESSONS.md`); an evicted record is a number.
3. Do not attempt to "compact" or re-order. Eviction is unlink-a-segment.

This is only acceptable because the spool is **not** the record of truth. Every detection it
carries is also a `dets.csv` row that `hear_drain.py` will fetch through the `cursor-v1`
endpoint, raw-before-parse, with its cursor advancing only after ingest. Spool eviction costs
*latency*, not data. Any future proposal to make the spool authoritative must first close
that loop, and this document refuses it.

### 3.6 Blocking prerequisite: `/spool/` has no retention class today

`sd_cache_classify_path()` (`firmware/hear_node/sd_retention_policy.h`) returns
`SD_CACHE_PROTECTED` for anything whose basename contains `state` or ends `.state`, and
`SD_CACHE_UNKNOWN_ROLLING` for anything it does not recognise. So **today**:

- `ack-a.state` / `ack-b.state` would be `PROTECTED` — correct by accident.
- `seg-*.spl` would be `UNKNOWN_ROLLING` — the generic pruner would delete spool segments at
  arbitrary points, from *any* end, while the spool's own accounting still believed it held
  them. The spool would then advertise a `spool_backlog` it cannot send and an `oldest_seq`
  that no longer exists.

Therefore the implementation task **must** add a `SD_CACHE_SPOOL` class covering `/spool/`,
ranked to be reclaimed *after* clips and before anything protected, with the spool's own
bounded policy (§3.5) as the normal mechanism and the generic pruner as the last resort — and
the spool must remain compile-time absent until that exists. This is the one place where
"design only" produces a hard ordering constraint on the implementation, and it is recorded
here rather than discovered on a node.

---

## 4. Board-by-board constraints

Every figure below is from the tree; nothing is estimated.

⚠️**Half the fleet has no card, so half the fleet cannot spool at all.**
`docs/fleet-release-rollout-sequence.md` §12 records the measured state: only `nyquist`,
`mach` and `rankine` carry an SD card; **`gold`, `kasami` and `ageev` are ring-only
(`sd: false`)**. On those nodes §10.2 step 1 applies unconditionally — no card, no spool,
`disabled_reason: "no_card"`, behaviour identical to today — and the drain keeps covering
them through `/detections` (`cursor-v1`), which is card-independent and already reports
`ok +0` for all three. Fitting a card to a ring-only node is a hardware decision this
document does not make.

| Board class | Node(s) | Flash / partitions | PSRAM | Spool medium | Verdict |
|---|---|---|---|---|---|
| `xiao-s3-pps` | mach, nyquist, rankine | 8 MB, `default_8MB` (3 MB app ×2, OTA) | 8 MB octal | SD `/spool/` | **The only eligible nodes.** nyquist's card is the 40 MB Pi-boot-partition case (§3.2), so it is both the worst case and the right canary; rankine reports `sd 80` and is the highest-rate node but is mid-recovery in `fleet-release-rollout-sequence.md` §6, so it waits. |
| `esp32s3-i2s-gps` / octal | ageev | 16 MB flash | 8 MB octal | **none today** | **Ineligible today:** ring-only, `sd: false`. Eligible only if a card is ever fitted. |
| `esp32s3-i2s-gps` / quad | **gold** | 8 MB flash | **2 MB quad** | **none today** | **Ineligible today:** ring-only, `sd: false`. And if a card is ever fitted, gold is still the hard case: it has run an octal image with `psramFound()==false`, no raw ring, `heap_min` 92 B and `loop_max_ms` past 1.4 s (`board_profiles.py`, `REDESIGN-LESSONS.md` §1.8), which is why the spool's RAM cost is bounded by static buffers that fit the **internal** heap with no PSRAM at all (§8.1). |
| `esp32s3-i2s-gps`, bus mode **unknown** | kasami | 8 MB assumed | unknown | **none today** | **Ineligible today:** ring-only, `sd: false`. It would also be last regardless: `board_profiles.py` deliberately leaves kasami out of `NODE_PSRAM_MODES` because nobody has scanned it, and `fleet-release-rollout-sequence.md` §4 marks any drop in its `raw.span_s 80.0` a stop. |
| `puc-ntp` | PUC | 32 MB, custom table: 3 MB app ×2 + **26 MB `ffat`** | 8 MB octal | `ffat` (SD pins unmapped) | **Deferred.** Its microSD pins are still unknown (`firmware/boards/puc.h`), its L86 1PPS is not routed (`PPS_WIRED 0`), and `hear/nodeclass.py` does not admit `puc-ntp` arrivals. It is the only board where a flash-wear budget would be needed; that analysis is owed before it is in scope. |
| `esp32s3-lora-pps` | none | — | — | — | Out of scope. Not built, not registered in `nodeclass.py`, and a 237 B Meshtastic payload is not this transport. |

---

## 5. Record identity and ordering

### 5.1 Identity comes from the item, never from the spool

- Permanent identity is `hear.ingest.v1`'s **closed identity tuple** → `event_id` (ADR 0001).
  A record that is resent, re-batched, split differently after a reboot or replayed weeks
  later converges on the same durable event. The spool adds nothing to identity, and
  **`seq` is not an identity**.
- `seq` is a per-boot `uint32` counter, monotonic within a boot, used for exactly three
  things: ordering the spool, computing `ack_through_index`'s effect on the watermark, and
  populating `producer.batch_sequence` / item `producer.sequence` so the server can see gaps.
- `boot_id` (already in the firmware, already in every heartbeat and event payload, already
  the `cursor-v1` namespace as `det_boot_id`) scopes `seq`. A `boot_id` change means sequences
  are **not comparable** — the same rule `hear_drain.py` applies to a cursor discontinuity and
  the same rule §6.4 of the server spec states.
- `gen` in the segment/watermark names is the spool's own generation counter, bumped when a
  spool is created or reset, so a reboot cannot make a stale watermark look current.

### 5.2 Ordering

- Records are sent in `seq` order, oldest first. The spool never re-orders to favour a newer
  record, because contiguous acknowledgement (§7.2) makes a gap expensive and because the
  server explicitly never reorders either.
- Out-of-order arrival at the server is *normal and allowed* (a replayed backlog is ingested
  on its `observed_at`); the node simply has no reason to create it.
- Across a reboot, the node resumes from `acked_seq + 1` of the surviving watermark, with a
  new `boot_id`. Items spooled by the previous boot keep the `boot_id` they were encoded with
  — the payload is immutable (§3.3), so a pre-reboot item is still attributable to the boot
  that observed it.

### 5.3 Clock-invalid records are first-class, and are never repaired

- A record observed without a usable anchor is encoded `observed_at: null`, `clock.valid:
  false`, with `clock.tier` and `producer.boot_epoch_us` + `producer.sequence` carrying the
  pre-anchor ordering — exactly the firmware's existing `ts_ms = uptime_s*1000 + 1`
  convention, made explicit.
- **If a PPS anchor arrives later, the spooled record is not rewritten.** Back-filling a time
  would manufacture an arrival, change an identity-tuple input, and turn a replay into a
  different event. The record ships as observed. This is the device-side mirror of the server
  rule "ingest never invents a time".
- `clock.valid: true` with a null `observed_at` is refused **before** it is appended
  (`clock_valid_without_observed_at`), counted locally, and never spooled. The server would
  refuse it durably; spooling something guaranteed to be refused only burns card and link.
- Clock state changes (`LOCKED`/`HOLDOVER`/`DEGRADED`/`FAULT`, `discontinuity_flags`) never
  gate spooling. A node whose clock is wrong is exactly the node whose data is most needed.

---

## 6. Batching and the wire

### 6.1 Device-side limits are tighter than the server's, and for different reasons

| Limit | Server allows | Device uses | Why the device is tighter |
|---|---|---|---|
| items per batch | 64 | **8** | The receipt, not the request, is the binding constraint (§6.3). |
| request bytes | 262,144 | **≈8 KiB** | Streamed from the card, but `Content-Length` must be exact and the 2 s read timeout plus `boot_wdt_service()` cadence bound how long one exchange may take. |
| item bytes | 65,536 | **4,096** (`SPOOL_MAX_RECORD_BYTES`) | `HEAR_PUSH_BODY_MAX` is 768 B today; 4 KiB is 5× headroom for the Phase 0 clock fields that already grew a body by ~160 B and broke a 512 B wrapper. |
| batch interval | — | 30 s idle flush, or immediately at 8 records | Fewer TLS handshakes than one-record-per-push, which is the dominant energy and latency cost. |

### 6.2 `Content-Length` is computable before the socket opens — by construction

The server refuses absent or chunked bodies (`Content-Length` required, §3.2 of the server
spec) and reserves `Content-Encoding` (no gzip, which the node has no heap for anyway). The
node therefore cannot discover the length while streaming. It does not need to: each spool
record's `len` is in its frame header, so the client sums `len` over the chosen records, adds
the fixed envelope overhead (`{"batch_schema_version":1,"batch_id":...,"device_id":...,
"sent_at":...,"producer":{...},"messages":[` + `,` separators + `]}`), and writes the header
before the first payload byte. The envelope prefix and suffix are built in a `static` buffer
sized by the same derived-chain discipline as `HEAR_PUSH_REQ_MAX`; the bodies stream from the
card in ≤4 KiB reads.

### 6.3 Receipts: a bounded scan, not a JSON parser

Today `push_post_json()` reads the status line and discards the body. The batch loop cannot:
`ack_through_index` is the whole point. Constraints and the resulting rule:

- Read at most `SPOOL_RECEIPT_MAX_BYTES = 4096` of the body, then stop and close. A receipt
  for 8 items is ~8 × 200 B plus the frame ≈ 1.8 KiB, comfortably inside it. **This is why
  the device batch is 8 items and not 64**: a 64-item receipt would not fit a bounded static
  buffer that has to coexist with mbedtls on an 8 kB stack.
- Extract exactly three things with a bounded scanner over that buffer — `ack_through_index`
  (integer), `retry_after_s` (integer or null), and the `results[].status` run only far enough
  to count `refused` inside the acknowledged prefix. No DOM, no `String`, no allocation.
- **Fail closed.** A receipt that is truncated, unparseable, or whose `ack_through_index`
  exceeds the number of items submitted acknowledges **nothing**. The batch is retried whole
  and the server reports `duplicate`. A parse failure must never be read as "all acknowledged"
  — that is the one bug in this design that would delete real data, and the default direction
  of the failure is chosen accordingly. Count it: `spool_receipt_parse_fail`.
- HTTP 2xx with an unparseable receipt is still counted as a *transport* success for backoff
  purposes but as an *acknowledgement* failure, and the two counters are separate.

### 6.4 Idempotency key

`Idempotency-Key = "<node_id>.<boot_id_hex>.<first_seq>.<count>.<sha256(body)[0:16]>"`,
≤128 chars. Deterministic from the bytes, so:

- a retry of the same bytes — including after a reboot, since the payloads are immutable on
  the card — regenerates the same key and replays the stored receipt byte-for-byte;
- a re-batched remainder after a partial ack has a different `first_seq`/`count`/digest, hence
  a new key and a new `batch_id`, as §7.1 of the server spec requires;
- the node can never produce the `409` case (same key, different body) without a code defect,
  which is exactly what `ingest_idempotency_conflicts_total` is for.

`batch_id` is `<node_id>-<boot_id_hex>-<batch_sequence>`; it correlates a log line and nothing
else, and is explicitly not a dedup key.

### 6.5 What a frame carries

`producer.boot_id`, `producer.boot_epoch_us`, `producer.batch_sequence` and
`producer.spool_backlog` (records still held, after this batch). `adapter` is **absent**: the
node is not an adapter, it is the origin, and `adapter` is reserved for a gateway submitting on
another device's behalf. `sent_at` is the node's own claim and is `null` when the clock is not
valid — a null claim, never a fabricated one.

---

## 7. Acknowledgement, retry, backoff, duplicates

### 7.1 Only the contiguous prefix is freed

`ack_through_index` is the last index made durable with **no gap before it**. The client
translates index → `seq` through the batch it just sent (it kept the `first_seq` and the count;
the mapping is positional and needs no extra state), writes the new `acked_seq` into the
*other* watermark slot with an incremented `gen`, and only then unlinks any segment whose
highest `seq` is ≤ `acked_seq`. Order: **receipt → watermark → unlink.** Never unlink first.

- `ack_through_index == -1` frees nothing.
- A `refused` item *inside* the acknowledged prefix is acknowledged and freed. It is durable
  server-side as a refusal record; resending it would loop forever. Count it as
  `spool_items_refused_by_server` so a firmware bug that produces refusable items is visible
  from the fleet rather than from the server only.
- `deferred` items and everything after the prefix are re-batched next cycle with a new
  `batch_id` and key.

### 7.2 Which failures retry, and which are poison

| Outcome | Action |
|---|---|
| transport failure, `408`, `429`, `5xx` | retry with backoff (§7.3); nothing freed, nothing dropped |
| `401`, `403` | **stop sending**, keep spooling to the cap, set `spool.auth_state` and surface it in `/status` + heartbeat. Never drop records for an auth failure; the operator's rotation is the fix (§9). |
| `409` | client defect. Do not retry the same key; re-batch from the watermark with a fresh key and count it. |
| `400`, `413`, `415`, non-retryable `422` | **frame poison.** The server has a durable refusal record for the frame, so retrying is an infinite loop over a body it will never accept. Halve the batch and retry once to isolate; if a single-item batch still gets the same class of refusal, drop **that one record**, count `spool_dropped_poison`, and continue. A poison record must never head-of-line-block a node's whole backlog. |
| `2xx`, receipt unparseable | §6.3: no acknowledgement, retry whole |

### 7.3 Backoff is the existing policy, not a second one

Reuse `push_backoff_ms()` exactly: exponential from `HEAR_PUSH_RETRY_BASE_MS` (1 s), doubling,
capped at `HEAR_PUSH_HEARTBEAT_MAX_MS` (60 s), **full jitter** (`random(cap+1)`). This already
matches the server spec's "exponential backoff with full jitter, capped at 60 s". `Retry-After`
and receipt `retry_after_s` raise the floor of the next attempt and never lower it.

The batch client and the legacy heartbeat push share one failure counter domain and are
**mutually exclusive in time**: both run from `loop()`, never overlapping, so there is never
more than one `WiFiClientSecure` alive. That is a hard requirement, not an optimisation — it is
the condition under which the static push buffers are safe.

### 7.4 Duplicates are a success

Retry resends the same bytes with the same key. The server answers `duplicate`, which
conservation counts as accepted-and-already-durable. The node treats `duplicate` exactly as
`accepted` when advancing the watermark. No de-dup state is kept on the node; identity is the
server's job and doing it twice would be two sources of truth.

---

## 8. RAM, power, and the loop budget

### 8.1 RAM

Every buffer `static`, sized from what it must hold, in the derived chain
`HEAR_PUSH_BODY_MAX → WRAPPED → REQ_MAX` already establishes:

| Buffer | Size | Note |
|---|---|---|
| record staging | 4,096 | one spool record, shared by append and send |
| envelope prefix/suffix | ~512 | derived from `node_id`, `boot_id`, `batch_id`, `sent_at` widths |
| request header | `HEAR_PUSH_REQ_HEADER_MAX` | reuse, unchanged |
| receipt | 4,096 | §6.3 |

≈9 KiB of static `.bss`, and **zero** PSRAM. That is deliberate: gold has run in production
with `psramFound()==false`, no raw ring and `heap_min` at 92 B, so a spool that needed PSRAM
would be a spool that silently disappears on the node most likely to need it. No `String`, no
`malloc` on the send path, nothing on the stack that competes with the mbedtls handshake.

### 8.2 Power and link

Batching *reduces* work: today a busy node pays one TLS handshake per event plus one per 10 s
heartbeat; at 8 items per batch the handshake count falls by up to 8× on the event path. The
spool never increases radio-on time relative to today except when draining a backlog, which is
precisely when that is the correct trade.

### 8.3 Loop budget

One record append **or** one batch exchange per `loop()` pass, never both, with
`boot_wdt_service()` at the same points `push_post_json()` calls it (before connect, before
write, before the read). `loop_max_ms` is already a published health column; a spool that moves
it is a spool that gets turned off by §11.

### 8.4 The PUC exception

`ffat` is 26 MB of flash, not a card. Flash has an erase-cycle budget that a card's wear
levelling hides, and a spool is by design a write-heavy, rewrite-in-place-adjacent workload.
No wear budget exists for that part in this tree, its SD pins are unmapped, and its PPS is not
routed. The PUC is out of scope until someone writes that analysis.

---

## 9. Credential and trust boundary

**This document handles no credential, generates none, prints none and stores none.** It states
where the boundary is and what the implementation may assume.

1. The batch client uses the **device push credential** already reachable as
   `push_token_runtime()` — NVS `hear_prov.ptoken`, per device, `HEAR_PROV_TOKEN_MAX`-bounded,
   injected at enrollment over USB by `enroll.py`, which prints only a masked summary.
2. It is **not** `HEAR_ADMIN_TOKEN`. Admin authority (`/update`, `/reboot`, `/format`, `/gate`)
   and ingest authority are different scopes with different blast radii, and
   `decisions/0006-admin-token-provisioning-policy.md` owns the per-node-versus-fleet-wide
   decision that is still open. The spool inherits whatever that record decides for custody
   mechanics, and requires only "per-device, rotatable without a reflash".
3. **No credential means spool, do not send.** A node with no usable credential appends to the
   spool and reports `auth_state: "missing"`. It does not fall back to an unauthenticated POST,
   and the server would refuse it anyway (`credential_missing`, never served open).
4. Nothing token-shaped may appear in `/status`, in a log line, in a heartbeat field, in a
   metric label, in a spool record or in a segment name. `/status` already reports only
   `configured` / `src`, and that is the pattern.
5. **Trust store is a prerequisite, not a detail.** The firmware pins one root CA
   (`setCACert(HEAR_PUSH_CA_CERT)`, Amazon Root CA 1). A spool pointed at an endpoint the node
   does not trust does not fail loudly — it *fills*, then evicts, which looks like a quiet
   fleet. So §11.2 of the server spec must be decided and the **CA-bundle build (option 2)
   deployed while the old endpoint still works** before any node's spool is enabled.
   `HEAR_PUSH_TLS_INSECURE` remains a bench-only escape hatch that fails CI for release builds.
6. Rotation never requires a reflash; the enrollment line already carries `ptoken`, and an
   overlapping-window rotation is invisible to the spool (the old credential works until the
   new one is in NVS).

---

## 10. Power loss, corruption, and recovery

### 10.1 The three states a card can be in after a power cut

| State | Detection | Recovery |
|---|---|---|
| Torn record at the tail of the open segment | `magic` mismatch, `len` beyond EOF, or `crc32` mismatch | Truncate the segment at the last good record. Count `spool_torn_tail`. A torn record is by definition one that was never sent. |
| Corrupt record in the middle (bit rot, a bad block) | `crc32` mismatch with a valid frame after it | **Skip that record**, count `spool_crc_drop`, continue from the next `magic`. Never send a record whose CRC fails — a corrupted payload with a valid-looking `event_id` is worse than a missing one, because it mints a durable event that never happened. |
| Both watermark slots invalid | CRC fails on `ack-a` and `ack-b` | Treat the whole spool as unacknowledged and resend from `oldest_seq`. Every resent item is `duplicate` server-side. Count `spool_watermark_lost`. This is the designed-for worst case, and it costs link, not data. |

### 10.2 Boot sequence

1. Mount the card. **No card ⇒ no spool**, `disabled_reason: "no_card"`, node behaves exactly
   as today.
2. Read both watermark slots; take the higher `gen` with a valid CRC.
3. Scan only the **last** segment for a torn tail (earlier segments are sealed and immutable);
   bounded by the 64 KiB segment cap, so boot time is bounded.
4. If any segment header carries an **unknown `fmt` major**: do not read it, do not delete it,
   disable the spool with `disabled_reason: "fmt_unsupported"`, and report it. An older image
   after an OTA rollback will see a newer spool and must leave it alone (§11.2) — the records
   are also `dets.csv` rows, so the drain covers them.
5. Derive the budget (§3.2), publish `spool.*` in `/status`, and start.

### 10.3 What a reboot does *not* do

It does not clear the spool, does not renumber existing records, does not re-time them, and
does not merge two boots' sequences. `boot_id` changes; `seq` restarts; the watermark carries
the previous boot's `acked_seq` and the segments carry their own `boot_id` in each payload.

---

## 11. Rollout, mixed versions, rollback, and the off switch

### 11.1 Rollout is per node, off by default, and the canary is chosen on evidence

1. `HEAR_SPOOL` is a **compile-time** feature, default **0**. A build without it is today's
   firmware plus dead code that the linker drops.
2. With it compiled in, the spool is still **runtime-disabled** until enabled per node through
   the existing provisioning/config path — no new credential, no new endpoint, no reflash to
   toggle.
3. Order: bench node → **nyquist** → **mach** → **rankine**, and that is the whole list.
   It is the card-bearing subset of the already-merged fleet order in
   `docs/fleet-release-rollout-sequence.md` §4 (`nyquist → mach → kasami → ageev → gold`), in
   that document's order, intersected with §4 above: `gold`, `kasami` and `ageev` are ring-only
   and have nowhere to spool. This document does **not** propose a second, competing fleet
   order; where the two touch, the release sequence wins.
   - `nyquist` first: worst-case 40 MB card, already the release canary and the failback-receipt
     node, and the node whose push-buffer truncation incident is documented, so its failure
     signature is known.
   - `mach` second, to prove the canary was not node-specific — the same reasoning the release
     sequence gives.
   - `rankine` last and **not before its USB recovery completes** (`fleet-release-rollout-sequence.md`
     §6). It is the highest-rate node (~249 clips/day) and `sd 80`, so it is the most informative
     and the least safe to move while it is the open half of a split fleet.
4. One node at a time, each soaking through at least one full outage-and-reconnect cycle
   before the next, and **never inside a firmware release window** — a spool enablement and a
   tag rollout must not share a change window, or neither is attributable. The server-side gate
   is the dual-write reconciliation already specified in `phase4-dual-write-observability.md`;
   the device gate is §11.4.

### 11.2 Mixed versions, both directions

- **Newer node, older server.** The node must not be the first thing that breaks. Until the
  `/v1/ingest/batches` route answers, the client stays disabled; the legacy per-record push is
  untouched. A server that returns `404`/`501` for the route is treated as "route absent" —
  disable the client, keep spooling below cap, report it. Never retry-storm a route that does
  not exist.
- **Older node, newer server.** Already handled by the server contract: legacy bodies are
  `translation_required`, not errors, and "a server release must read old nodes before any
  firmware rollout".
- **OTA rollback (`hear_boot_guard`, `HEAR_BOOT_MAX_TRIES`).** The reverted older image has no
  spool code and will see unknown files under `/spool/`. Two consequences, both stated so
  neither is a surprise: (a) it must not crash — it will not, it never opens the directory; and
  (b) `sd_cache_classify_path()` in the older image classifies `seg-*.spl` as
  `UNKNOWN_ROLLING` and may prune it, which is an acceptable loss because `dets.csv` holds the
  same detections and the drain is the recovery path. Firmware rollback is a separate canary
  decision and is never inferred from a server rollback.

### 11.3 Fallback to the legacy paths

There is no device-side MQTT client and this design does not add one; `hear_mqtt_bridge.py` is
a **server-side** legacy path for `dama/+/telemetry` and is untouched. The device fallbacks
are, in order:

1. **Legacy HTTPS single-record push** — always running, never disabled by the spool, unchanged
   bodies, unchanged endpoints, unchanged backoff. This is the fallback.
2. **`hear_drain.py` pull** — unchanged, authoritative, and the recovery path for anything the
   spool evicts, drops or cannot send.
3. **The SD card itself** — unchanged capture authority.

A node with the spool disabled by any of the reasons in this document is exactly a node as it
is today. That is the property that makes this cheap to abandon.

### 11.4 Kill switches and triggers

| Trigger | Action |
|---|---|
| `loop_max_ms` regression, `heap_min` fall, `drop_s` increase, or `write_fail` increase attributable to the spool | disable the spool on that node; capture is never traded for uplink |
| `spool_evicted > 0` on a node that is online | the spool is mis-sized or the link is broken: investigate before enabling another node |
| `spool_crc_drop` or `spool_watermark_lost` non-zero | stop the rollout; this is a durability defect, not a tuning issue |
| repeated `409` or `spool_dropped_poison` | client defect; disable and fix, do not "clean up" the spool by hand |
| a receipt ever acknowledges more than was submitted | disable immediately; fail-closed (§6.3) already prevented the damage, but the server or the client is lying |
| card free space at the retention floor | spool budget goes to zero automatically (§3.2); no operator action needed |

Rollback is: runtime-disable → node reverts to today's behaviour → the drain catches up →
optionally unlink `/spool/`. **Code rolls back before data**, and the spool holds no data that
the card does not already hold.

---

## 12. Observability

Additive `/status` object (the shape `/status` already uses; no credential, no bytes):

```json
"spool": {"enabled": true, "disabled_reason": null, "fmt": 1, "gen": 3,
          "records": 118, "bytes": 90112, "budget_bytes": 3145728,
          "oldest_seq": 41022, "acked_seq": 41139, "oldest_unacked_age_s": 214,
          "appended": 41140, "evicted": 0, "refused_append": 0,
          "batches_sent": 5142, "batches_ok": 5139, "items_acked": 41139,
          "items_refused_by_server": 1, "dropped_poison": 0,
          "receipt_parse_fail": 0, "torn_tail": 2, "crc_drop": 0,
          "watermark_lost": 0, "last_http_code": 200, "auth_state": "ok"}
```

The same counters ride the heartbeat as **additive** fields (`hear_push_payload.h` is versioned
and additive-safe; `tests/test_firmware_heartbeat_push.py` recomputes the worst-case body size
from the encoders, and any new field must be added inside that budget — the ~160 B Phase 0
growth that silently truncated a 512 B wrapper is the precedent).

Server-side these need no new metric: `producer.spool_backlog` already feeds
`ingest_producer_spool_backlog`, `ingest_ack_gap_items` already measures the acknowledgement
gap, and `ingest_sequence_gaps_total` already detects `batch_sequence` gaps within a `boot_id`.
That is not a coincidence — the frame was designed with a producer spool in mind, and this
document is the producer it was waiting for. The checked-in server-side seam is
`hear/ingest/observability.py`; staged dashboard and alert assets for these series live in
`deploy/observability/`.

Two alert rules that are not obvious:

- `oldest_unacked_age_s` rising while `batches_ok` also rises means the node is draining slower
  than it fills — a backlog that will end in eviction. Alert before the eviction, not after.
- `evicted == 0` **and** `records == 0` on a node with a known event rate is as suspicious as a
  spike. A permanently empty spool on a busy node means the append path is silently failing,
  which is the "zero-result import is a failure, not a quiet success" rule applied on-node.

---

## 13. Implementation gates (later work, in order)

1. `SD_CACHE_SPOOL` retention class exists, with tests (§3.6). **Blocking.**
2. Host-side unit tests for the record frame, the torn-tail scanner, the CRC path, the
   dual-slot watermark and the bounded receipt scanner — as pure functions in a header the
   test suite can compile, the way `sd_retention_policy.h` already is. No hardware needed.
3. TLS trust-store decision made and the CA-bundle build deployed (§9.5). **Blocking.**
4. `/v1/ingest/batches` actually served, with the §7.2 durability ordering of the server spec
   and a replay test that survives a kill between the durable write and the receipt.
5. Bench node: power-cut-during-append and power-cut-during-watermark-write drills, both run,
   not just documented.
6. Canary on nyquist through one full outage-and-reconnect cycle with `loop_max_ms`,
   `heap_min`, `drop_s` and `write_fail` unchanged within noise.
7. Then, and only then, the rest of the order in §11.1.

## 14. Open questions

Recorded, not silently decided. None blocks this document; each blocks a later gate.

1. **Which records are worth spooling.** This design says detections and events, plus exactly
   one coalesced heartbeat (a heartbeat is a liveness claim and a stale one is worth nothing —
   spooling 8,640 of them a day at the 10 s interval would evict real detections to report that
   a node used to be alive). If an operator wants heartbeat *history* offline, that is a
   different feature with a different budget, and it should be argued separately.
2. **Spool budget numbers.** 3 MiB / 3072 records / 10% of free are defensible from §3.2's
   measurements but are not measured *as a spool*. The nyquist canary is what turns them into
   measured numbers.
3. **PUC flash wear budget** (§8.4), owed before `puc-ntp` is in scope.
4. **Per-node versus fleet-wide credential custody** — owned by ADR 0006, still open.
5. **Whether the device should ever send `adapter`.** Today: no (§6.5). If a node ever
   forwards a neighbour's records over LoRa, it becomes a gateway and the answer changes, along
   with the authorization scope it needs.
