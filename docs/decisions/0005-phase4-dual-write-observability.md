# 0005 - Dual-write observability and reconciliation for Phase 4

- Status: proposed
- Date: 2026-09-15
- Scope: how a *future* legacy + HTTPS dual-write is observed, compared, classified and
  evidenced. This record changes no writer, no reader, no deployment and no credential. It
  enables nothing: dual-write is not turned on here, no comparator is scheduled, no service
  is created, and no telemetry credential is issued.
- Companion to: `docs/decisions/0004-phase4-https-batch-ingest-adapter.md` (the transport
  decision) and `docs/phase4-https-batch-ingest.md` §12-§13, which name this work and
  deliberately leave the receipt, the classification vocabulary and the dashboards to it.
- Enforced by: `tests/test_reconcile_receipt_v1.py`,
  `tools/gen_reconcile_contracts.py --check`, CI job `generated headers are current`.

## Problem

`docs/standalone-migration.md` stage 2 is "dual-write, old readers authoritative". The batch
ingest spec defines the counters that stage emits and states its exit gate — *seven days
with zero unexplained differences* — but nothing in the tree can currently answer the three
questions that gate asks:

1. **Which legacy record is which canonical record?** The two paths do not share an
   identity. `tools/hear_heartbeat_receiver.py` derives a `record_uid` as
   `sha256("v2" | telemetry_path | device_id | idempotency_key)`, or a whole-body hash when
   the node minted no key. `hear/ingest/envelope.py` derives an `event_id` as a UUIDv5 over
   ADR 0001's closed identity tuple. Neither is computable from the other, and neither may
   be changed: one is live, the other is frozen for the life of the v1 major.
2. **What counts as a difference?** Two honest writers with no GPS fix will disagree on a
   wall-clock instant. Two honest writers *will* disagree on arrival order. A reconciler
   that calls either a mismatch produces a permanent red dashboard, which is the same thing
   as no dashboard.
3. **Where does the evidence go, and what may it contain?** `tools/coord_guard.py` makes a
   real-world coordinate in the tree merge-blocking, and `docs/data-governance.md` classes
   telemetry as R0-derived. A mismatch store that quotes both sides of every differing field
   is the most obvious way to reintroduce a coordinate, a token or a clip body into a place
   nobody is guarding.

Answering these *after* dual-write is enabled means answering them while the evidence is
being produced, which is how a comparison ends up matching whatever the new path happens to
do.

## Decision

**Reconciliation is an audit, not a participant.** The comparator reads both sides, emits
durable receipts, and has no authority to change either side. Concretely:

1. **The join key is recorded, never re-derived.** The dual-writer writes the
   `(legacy_uid, event_id)` pair in the same transaction as the canonical write. The
   comparator joins on it. `correlation_id()` is `"<side>:<sha256>"` over
   `(contract, side, site_id, device_id, telemetry_path, anchor)`, and **legacy owns the key
   whenever a legacy row exists** — an authoritative record must not change identity when
   the claimant appears or disappears. A pair that cannot be joined is an explicit
   `legacy_orphan`/`canonical_orphan` binding, never a dropped row and never a guess.
2. **The classification vocabulary is closed and total.** Fifteen members, one severity and
   one repair action each, and `conservation()` must close:
   `compared == pending + match + Σ(mismatch classes)`. There is no "other" bucket, because
   an "other" bucket is where a real loss goes to be ignored. An unknown member from a newer
   comparator is preserved and marked non-actionable — never coerced to `match`.
3. **Absence inside the grace window is `pending`, not loss.** 300 s for live paths,
   86 400 s for drain/import backfill. Without this the reconciler pages on its own
   scheduling jitter, the operator mutes it, and the one real loss arrives muted.
4. **Undecidable is louder than wrong.** `unclassified` is critical severity. A comparator
   that cannot classify has stopped being evidence, and a quiet unclassified bucket is how a
   dashboard goes green over a hole.
5. **Receipts carry hashes, paths and reason codes — never payloads.** Redaction is
   deny-list-first over an allow list: a field value is quoted only if its path is
   explicitly allowed *and* does not match the location/credential/body deny pattern. An
   unknown path defaults to redacted, because an allow list defaults to hidden and a deny
   list defaults to leaked.
6. **Repair is replay-only, and the comparator never queues it.** `repair_action` is one of
   `none`, `replay_inbox`, `replay_outbox`, `manual_review`. There is no action that edits,
   deletes or re-derives a row on either side, and every emitted receipt has
   `disposition.queued = false`. A human dequeues.
7. **Mismatches are never sampled; matches are sampled deterministically.** Sampling a
   failure signal makes the reconciler a coin flip. Audit samples are keyed on the
   correlation id, so a re-run of a window produces the same audit set and coverage is
   computable rather than assumed.

## Why the receipt is staged rather than published

`hear.reconcile.receipt.v1` is generated into `docs/phase4-dual-write-reconciliation/`, not
`contracts/`. That is a scheduling decision, not a disagreement with decision 0002.

Publishing a contract id requires its source of truth to be recorded in
`docs/data/phase0-freeze-contracts.v1.json` (rule 1), which means regenerating the frozen
baseline and `docs/phase0-freeze-contracts.v1.md`. The in-flight HTTPS batch ingest lane is
already regenerating both files for two new contract ids. Two lanes regenerating one hashed
baseline is a guaranteed conflict in a file where a conflict is meaningless — the resolution
is always "run the generator again" — and resolving it repeatedly on a rebasing branch is
how a hash gets hand-edited.

So the artifacts are produced by a generator, gated for drift in CI, and laid out in exactly
the shape `contracts/` uses. **Promotion is a move plus a regeneration, with no content
change:**

1. `git mv docs/phase4-dual-write-reconciliation/hear.reconcile.receipt.v1.schema.json contracts/schemas/`
2. `git mv docs/phase4-dual-write-reconciliation/fixtures contracts/fixtures/hear.reconcile.receipt.v1`
3. Point `OUT_DIR` in `tools/gen_reconcile_contracts.py` at `contracts/`.
4. Regenerate the freeze baseline (`tools/freeze_contracts.py --format json|markdown`).
5. `python3 tools/check_contract_layout.py` — this record already satisfies rule 2 by naming
   the id, and the generator already satisfies rule 3 by being wired into CI.

Until then the receipt is a design artifact, and the manifest says so: `status: staged`,
with the reason and the source of truth recorded in it.

One consequence is visible in the code. `hear/ingest/reconcile.py` names the id
`RECEIPT_CONTRACT_ID`, not `RECEIPT_SCHEMA_ID`, because `tools/freeze_contracts.py` harvests
`[A-Z_]*SCHEMA[A-Z_]*` assignments as published contract ids — the same trap both tools
already document about their own constants. Registering an unpublished id in the frozen
baseline would make rule 1 pass for a contract with no published artifact, which is the
inverse of the check's purpose. `tests/test_reconcile_receipt_v1.py` asserts the constant
stays unharvestable.

## Why this is compatibility-safe

- **No live path changes.** The legacy receiver, the MQTT bridge, the drain, the pool
  readers and the firmware are untouched, and remain authoritative. Nothing in this record
  is reachable from any running process: `hear/ingest/reconcile.py` is imported by tests and
  a generator, and by nothing else.
- **Forward.** The receipt object is `additionalProperties: true` at every level, so a newer
  comparator's extra field is preserved by an older reader instead of failing validation.
  An unknown classification member is preserved and held back from automated action.
- **Rollback.** Rolling this back is reverting a module, a generator, a document and a
  staged artifact directory that no runtime reads. Because the artifacts are generated,
  regeneration after a revert reproduces the previous bytes exactly, and CI checks it.
- **Mixed version.** The comparator compares *recorded* identities. A fleet where some nodes
  emit legacy bodies and some emit canonical envelopes produces `bound` pairs for the
  dual-written ones and explicit orphan bindings for the rest — neither description is
  deleted to make room for the other.
- **No-dependency baseline.** `hear/ingest/reconcile.py` imports `hashlib`, `json`, `re`,
  `uuid` and `typing`. `tools/check_contract_layout.py` rule 5 is satisfied, and the module
  performs no I/O at all — asserted by AST, not by grep.
- **No new credential.** The comparator authenticates as nothing. It reads the
  `principal_id` and opaque `key_id` each writer already recorded, and there is no field in
  the receipt a token could occupy.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Re-derive `event_id` from legacy rows at comparison time | The comparator would be reimplementing the thing it audits. A bug in the derivation would make both sides agree and hide the very defect it exists to find. |
| Compare whole stored rows by hash | Every additive field becomes a mismatch. That is the reconciler people turn off in week two; the compared projection is explicit for exactly this reason. |
| Let the comparator auto-repair `missing_canonical` | An auditor with write access to its subject is not an auditor. Replay exists and is already durable; a human deciding to run it is the control. |
| Sample mismatches to control storage | Storage is cheap and evidence is not. Cost is controlled by sampling *matches* and by capping receipt size, which costs nothing that a gate depends on. |
| Alert on legacy health from the comparator | Legacy is authoritative and unchanged. A new page against an unchanged path is a new failure mode introduced by observing it. |
| Wait for the ingest lane to merge, then publish under `contracts/` | The design is needed to review the ingest lane, not after it. Staging costs one directory move; delay costs the review. |

## Consequences

- One stdlib-only module, one generator, one staged artifact directory, one document and one
  test file. No new dependency, no new service, no new credential, no deployment change.
- The Phase 4 exit gate in `docs/phase4-https-batch-ingest.md` §13.3 becomes checkable: each
  clause maps to a counter and a receipt query in `docs/phase4-dual-write-observability.md`.
- The comparator's own failure modes are now first-class findings (`unclassified`, stalled
  window, implausible zero) rather than silence.
- Two open questions are recorded rather than decided: the numeric site SLO for p99 ingest
  lag (already open in §16 of the ingest spec, and the freshness SLO here is stated relative
  to it), and mismatch-receipt retention, which is a data-governance approval and not a
  design choice.
