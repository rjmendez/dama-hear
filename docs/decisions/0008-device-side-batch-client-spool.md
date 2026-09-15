# 0008 - Device-side batch client and uplink spool

- Status: proposed — **design only; nothing is implemented, built, flashed or enabled**
- Date: 2026-09-15
- Scope: where a node holds an uplink backlog, what bounds it, how a spooled record is
  identified and ordered, how batches are acknowledged and abandoned, what survives a power
  cut, which credential the client sits behind, and how the whole thing is turned off. This
  record modifies **no firmware file**, produces no image, flashes no node, changes no cluster
  object, creates/prints/stores no credential, and cuts over no transport. Device behaviour is
  unchanged by it.
- Companion to: `docs/decisions/0004-phase4-https-batch-ingest-adapter.md` (the frame this
  produces), `docs/phase4-https-batch-ingest.md` §1/§7.3 (which deferred exactly this work),
  `docs/decisions/0001-hear-ingest-v1-envelope-and-codec.md` (item identity), and
  `docs/decisions/0006-admin-token-provisioning-policy.md` (credential custody, still open), and
  `docs/fleet-release-rollout-sequence.md` §4/§6/§12 (the merged fleet order this defers to, and
  the measured fact that only three nodes have a card).
- Full specification: `docs/phase4-device-batch-spool.md`.
- Enforced by: nothing yet, deliberately. §Implementation gates in the specification names the
  tests each future change owes, starting with a host-compilable record-frame header.

## Problem

`hear.ingest.batch.v1` was designed with a producer spool in mind — the frame carries
`producer.boot_id`, `producer.batch_sequence` and `producer.spool_backlog`, the receipt carries
`ack_through_index` as a contiguous durable prefix, and `ingest_producer_spool_backlog` and
`ingest_ack_gap_items` already exist to read them. No producer fills them. The only device
uplink is `push_post_json()`: one record per HTTPS request, and a **failed event push is
dropped** (`hear_node.ino:2749`; the heartbeat is rescheduled with backoff, the event is not).
A node offline for an hour loses an hour of event pushes, and recovery depends entirely on
`hear_drain.py` pulling `dets.csv` later.

The naive fix — "add a queue to the firmware" — is the one this fleet has already been punished
for. Four in-tree facts bound it:

1. **Card capacity is per node and wildly unequal.** nyquist runs on a Raspberry Pi boot card
   whose FAT partition is 40 MB, half of it kernel images (`hear_node.ino:4603`), while another
   node's card is 30 GiB (`hear_node.ino:2519`). `scene.csv` alone costs ~0.8 MB/h.
2. **RAM is not spare.** gold has run in production with `psramFound()==false`, no raw ring,
   `heap_min` at 92 B and `loop_max_ms` past 1.4 s (`board_profiles.py`,
   `REDESIGN-LESSONS.md` §1.8); kasami's bus mode has never been scanned.
3. **The push path is stack-critical.** Correctly sizing the push buffers put ~2.7 kB live in
   the same frame as an mbedtls handshake on an 8 kB loop stack, and nyquist panicked twice on
   real hardware until the buffers were made `static` (`hear_node.ino:2612-2626`).
4. **A silent drop is the recurring defect.** Issue #104 exists because the MQTT bridge refuses
   before anything durable is written.

So the decision is not "spool or not". It is: what is the spool allowed to cost, and what is it
allowed to lose.

## Decision

1. **The spool is on the SD card, at `/spool/`, and nowhere else.** NVS is 20 KiB of wear-limited
   flash holding credentials — it holds no record and not even the acknowledgement watermark.
   Repartitioning flash for a data region is a USB reflash of a deployed node, which
   `standalone-migration.md` forbids as a routine act.
2. **It is bounded by measured free space, not by a compiled constant promise:**
   `min(3 MiB, 10% of free-at-boot)` and 3072 records, whichever binds first; **zero** budget
   below the existing 8 MiB retention floor.
3. **It is not authoritative, and that is what licenses its bounds.** Every record it carries is
   also a `dets.csv` row that `hear_drain.py` recovers through `cursor-v1`, raw-before-parse.
   Eviction costs latency, not data. Oldest-first, counted, never silent.
4. **Capture always wins.** The row reaches the card before its copy reaches the spool; an
   append that cannot get headroom is refused and counted, never satisfied by evicting a clip,
   a detection row or a scene row.
5. **Segments are append-only and payloads are immutable.** A record is never rewritten — not to
   mark it sent, and *never* to back-fill a timestamp once a PPS anchor arrives. Back-filling
   would manufacture an arrival and change an identity-tuple input. Space is reclaimed by
   unlinking a whole segment at or below the watermark.
6. **Two watermark slots, alternating, each CRC'd with a generation counter.** POSIX
   temp-then-rename is not available on FATFS through the Arduino `SD` library, so durability
   comes from never having one writable copy. The worst case is resending an acknowledged
   batch, which the server reports as `duplicate` — a success.
7. **Identity is the item's `event_id`; the spool's `seq` is ordering only,** scoped by
   `boot_id`. A `boot_id` change makes sequences incomparable, matching the drain's existing
   cursor-discontinuity rule.
8. **The device batch is 8 items, not the server's 64, and the receipt is why.** The client must
   read `ack_through_index`, so it must hold a receipt in a bounded static buffer alongside
   mbedtls on an 8 kB stack. 4 KiB of receipt is 8 items' worth. The receipt scanner is bounded
   and **fails closed**: an unparseable receipt acknowledges nothing.
9. **Freeing is contiguous-prefix only, in the order receipt → watermark → unlink.** A `refused`
   item inside the acknowledged prefix is freed (it is durable server-side as a refusal), and a
   frame the server will never accept is isolated by halving and then dropped as poison rather
   than head-of-line-blocking a node's backlog.
10. **Backoff is the existing `push_backoff_ms()`** — exponential to 60 s with full jitter — and
    the batch client and the legacy push are mutually exclusive in time, so exactly one
    `WiFiClientSecure` is ever alive.
11. **The credential is the existing per-device NVS `ptoken`, never `HEAR_ADMIN_TOKEN`.** No
    credential ⇒ spool, do not send, report `auth_state`. Nothing token-shaped reaches
    `/status`, a log, a heartbeat field, a metric label, a record or a filename.
12. **Two hard prerequisites, both blocking:** a `SD_CACHE_SPOOL` retention class for `/spool/`
    (today `seg-*.spl` falls into `UNKNOWN_ROLLING` and the generic pruner would delete spool
    segments out from under the spool's own accounting), and the CA-bundle trust migration of
    `phase4-https-batch-ingest.md` §11.2 (a spool pointed at an untrusted endpoint does not fail
    loudly — it fills, then evicts, and the fleet looks quiet).
13. **Compile-time off (`HEAR_SPOOL=0`), then runtime-off, then one node at a time:** bench →
    nyquist (worst card, already the release canary) → mach → rankine (highest rate, only after
    its USB recovery). That is the card-bearing subset of the merged fleet order in
    `docs/fleet-release-rollout-sequence.md` §4, in that document's order; where the two touch,
    the release sequence wins. **`gold`, `kasami` and `ageev` are ring-only (`sd: false`,
    `fleet-release-rollout-sequence.md` §12) and therefore cannot spool at all** — on them the
    boot check disables the spool with `no_card` and the drain keeps covering them through
    card-independent `cursor-v1`. Spool enablement never shares a change window with a tag
    rollout.
14. **Fallback is today's behaviour, always running:** the legacy single-record HTTPS push is
    never disabled by the spool, `hear_drain.py` stays authoritative, the card stays the capture
    authority. There is no device-side MQTT client and none is added; `hear_mqtt_bridge.py` is a
    server-side legacy path and is untouched.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Spool in NVS | 20 KiB, wear-limited, and it is the credential store. A backlog there is a few dozen records and a shortened flash life. |
| A new flash data partition | Repartitioning a deployed node is a USB reflash — the act the migration document exists to avoid. |
| Make the spool authoritative and stop pulling `dets.csv` | Removes the property that makes bounded eviction safe, and replaces a tested pull path with an untested push path in the same change. |
| Rewrite records in place to mark them sent | FAT + `SD` has no atomic in-place update; a torn rewrite silently mutates an event that already has an `event_id`. |
| Back-fill `observed_at` when a PPS anchor arrives | Manufactures an arrival and changes an identity-tuple input. The server refuses to invent a time; so does the node. |
| Use the server's full 64-item batch | A 64-item receipt does not fit a bounded static buffer on an 8 kB stack shared with mbedtls, and the receipt is the only thing that can free spool space. |
| Spool heartbeats | 8,640/day at the 10 s interval would evict real detections to report that a node used to be alive. One coalesced latest heartbeat only. |
| Spool clips or scene rows | 480,044 B per clip against a 65,536 B item limit; 0.8 MB/h of scene. Both stay on the drain. |
| Retry a `400`/`413`/`422` frame forever | An infinite loop over a body the server has already durably refused, blocking the whole backlog behind it. |
| Treat an unparseable receipt as "all acknowledged" | The one failure direction in this design that deletes real data. Fails closed instead. |

## Consequences

- **Nothing changes today.** A node built without `HEAR_SPOOL` is byte-for-byte today's
  firmware, and every failure mode in the specification degrades to exactly that.
- **The spool's reach is half the fleet, and that is worth saying plainly.** Three of six nodes
  are ring-only, so the spool is an improvement for `nyquist`/`mach`/`rankine` and a no-op
  elsewhere. That is an argument for keeping it small and firmly non-authoritative, not an
  argument for fitting cards — which is a hardware decision, made elsewhere, on other grounds.
- Two firmware prerequisites are now named and ordered (retention class, trust bundle), so
  neither is discovered on a node.
- One correction to `docs/phase4-https-batch-ingest.md` §2 is recorded rather than silently
  carried: `HEAR_PUSH_WRAP_BATCH` defaults to **1**, so nodes already emit a one-item
  batch-shaped body. What is absent is the receipt/spool loop, not the wrapper.
- The server side needs **no new metric**: `producer.spool_backlog`, `ingest_ack_gap_items` and
  `ingest_sequence_gaps_total` were designed for this producer.
- Rollback is a runtime flag; the spool holds nothing the card does not already hold, so code
  rolls back before data and no data rolls back at all.
- Open, and owned elsewhere: credential custody (ADR 0006), the p99 ingest-lag SLO and
  quarantine retention (`phase4-https-batch-ingest.md` §16), the PUC flash-wear budget, and the
  spool budget numbers themselves, which the nyquist canary is what turns from defensible into
  measured.
