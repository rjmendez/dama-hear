# Phase 4: spool open questions operator memo

- Status: proposed
- Date: 2026-09-16
- Audience: operators deciding whether and how to proceed from `docs/phase4-device-batch-spool.md` §14
- Scope: decision support only; this memo does **not** change firmware, backend behavior or ADR 0006

This memo restates the five open questions in `docs/phase4-device-batch-spool.md` §14 and
separates the **recommended default** from the **genuine operator decision this memo does not
make**.

## 1. Which records are worth spooling

### Recommended default

Spool **detections and events**, plus **exactly one coalesced heartbeat**.

That is still the best default in the source document's own terms:

- detections and events are the records whose late delivery still has operational value;
- a heartbeat is only a liveness claim, so stale heartbeat history is weak evidence compared with
  delayed detections;
- spooling every 10-second heartbeat would consume budget quickly and can evict the records that
  matter more.

If operators later need offline heartbeat history, that should be treated as a separate feature
with its own storage budget and retention argument, not quietly folded into the spool.

### Operator decision this memo does not make

Whether the fleet wants an **additional offline heartbeat-history feature** despite the eviction
risk and extra storage cost. This memo recommends **not** broadening the spool unless that need is
stated explicitly and budgeted separately.

## 2. Spool budget numbers

### Recommended default

Use the current planning numbers as the working default:

- `SPOOL_MAX_BYTES = 3 MiB`
- `SPOOL_MAX_RECORDS = 3072`
- spool cap = `min(3 MiB, 10% of free-at-boot)`
- disable the spool below the existing 8 MiB retention floor

These are reasonable **planning defaults** because they follow `docs/phase4-device-batch-spool.md`
§3.2's measured card-capacity discussion and preserve the rule that capture space wins over spool
space.

### Operator decision this memo does not make

Whether those numbers are good enough to accept as **real operating limits** after canary evidence
exists.

This memo does **not** claim they are measured spool numbers today. Per §14 and §13 gate 6, they
remain provisional until the nyquist canary measures the spool as a spool. No canary data is
invented here.

## 3. PUC flash wear budget

### Recommended default

Keep the PUC **out of spool scope** for now, exactly as `docs/phase4-device-batch-spool.md` §8.4
states. That also means `puc-ntp` stays blocked on this question.

The current record is clear: PUC uses 26 MB of `ffat` flash, not an SD card; the tree has no wear
budget for that flash, no mapped SD pins and no routed PPS. A write-heavy spool should not be
assumed safe there without measurement.

### Operator decision this memo does not make

Whether the PUC should ever host a spool after dedicated wear analysis exists.

Before that decision, someone would need to measure at least:

- expected write/erase cadence for the proposed segment and watermark pattern;
- the flash part's erase-cycle endurance and any wear-levelling behavior actually available;
- whether the intended retention window fits inside that wear budget with margin;
- whether PUC timing and storage constraints still hold once NTP/PPS work is in scope.

Until that analysis exists, the safe default is **do not include PUC**.

## 4. Per-node versus fleet-wide credential custody

### Recommended default

**Defer entirely to ADR 0006**:
`docs/decisions/0006-admin-token-provisioning-policy.md`.

This memo does not re-decide, narrow, reinterpret or supersede ADR 0006's still-open D1 operator
choice. `docs/phase4-device-batch-spool.md` §9 already says the spool only requires "per-device,
rotatable without a reflash" and inherits the custody mechanics from that ADR.

### Operator decision this memo does not make

The D1 choice itself: **per-node** versus **fleet-wide** custody. ADR 0006 owns that decision of
record, including threat model, blast radius and rollout consequences. Operators should resolve it
there, not here.

## 5. Whether the device should ever send `adapter`

### Recommended default

Keep the current answer: **no**, the device should not send `adapter` in this design.

Per `docs/phase4-device-batch-spool.md` §6.5, the node is the record origin, not an adapter. Not
sending `adapter` keeps the payload honest about that role and avoids expanding authorization and
trust assumptions unnecessarily.

### Operator decision this memo does not make

Whether the architecture should change so a node becomes a **gateway** for another node's records
(for example over LoRa or Meshtastic).

That is the condition that would change the answer. If a node starts forwarding neighbour records,
then:

- `adapter` becomes semantically correct rather than misleading;
- the system needs an explicit origin-versus-gateway authorization model;
- the current per-origin trust boundary in §9 would need a new design review.

Until that gateway role exists, the default should remain **do not send `adapter`**.

## Bottom line

Recommended defaults today:

1. spool detections/events plus one coalesced heartbeat;
2. keep 3 MiB / 3072 records / 10% free as provisional planning defaults only;
3. keep PUC out of scope until wear analysis exists;
4. defer credential-custody choice entirely to ADR 0006;
5. do not send `adapter` unless a node truly becomes a gateway for another node's records.
