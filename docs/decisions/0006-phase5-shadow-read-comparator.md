# 0006 - The Phase 5 shadow-read comparator

- Status: proposed
- Date: 2026-09-15
- Scope: how a *future* legacy-versus-canonical **read** comparison is identified, bounded,
  classified and evidenced, before any read cutover. This record changes no reader, no
  writer, no deployment and no credential. It enables nothing: no query is issued, no
  comparator is scheduled, no service is created, no lane mode is changed, and no cluster,
  fleet or production store is touched.
- Companion to: `docs/decisions/0005-phase4-dual-write-observability.md` (the write-side
  audit this reuses), `docs/phase5-shadow-read-comparator.md` (the full specification),
  `docs/phase2-postgres-migration-plan.md` §8 (the shadow-read sketch this generalises), and
  the Phase 3 object key design's read ladder (`off`/`shadow`/`compare`/`prefer`/`only`).
- Enforced by: `tests/test_shadow_read_receipt_v1.py`,
  `tools/gen_shadow_read_contracts.py --check`, CI job `generated headers are current`.

## Problem

`docs/standalone-migration.md` moves reads only after writes, and
`docs/phase2-postgres-migration-plan.md` §8 already sketches a shadow read: count rows per
device, diff a Redis capture, compare a health surface. That sketch is enough for one store
swap and not enough for a read cutover, because it answers the wrong question.

Phase 4 asks *did both writers store the same thing*. The thing that actually gates a
cutover is **would a user have got the same answer**, and those are different questions. A
read is not a row. It is a filter, a window, an order, a page and an instant, and all five
are places two paths diverge while both stores are perfectly healthy:

1. **Two reads are two snapshots.** The legacy and canonical surfaces answer at different
   instants, with different commit latencies behind them. Comparing the leading edge of a
   write stream measures scheduling jitter and reports it as data loss.
2. **"Not returned" is five different facts.** In flight, late, retention-expired,
   tombstoned, lost. The legacy clip rule is a 2 GiB cap and the canonical one is a 30-day
   class; legacy raw archives have no retention at all and the canonical class is 7 days.
   A comparator that treats those as one fact produces a permanently red board out of two
   correct stores, which is the same thing as no board.
3. **Order and pagination are not content.** `docs/api-boundaries.md` gives the canonical
   side opaque cursors over a stable `created_at,id` sort with a 24 h expiry; the legacy
   surfaces have no cursor at all. Two honest readers may page differently and answer the
   same question — but a row duplicated or skipped across a page boundary is real, and a
   comparator that cannot separate the two reports either everything or nothing.
4. **The comparator's own failures look exactly like findings.** A 45-second snapshot skew
   presents as a hundred missing rows. That is the most expensive kind of wrong, because it
   is indistinguishable from the result the exercise exists to produce.
5. **A read comparator is the easiest place in the system to leak.** It handles query
   filters — user input, where `?near_latitude=…` lives — and result bodies, which include
   clip audio and solved coordinates. `tools/coord_guard.py` makes a coordinate in the tree
   merge-blocking; a receipt store full of hashed coordinates is a location database with
   extra steps.

Answering these *after* a shadow read is running means answering them while the evidence is
being produced, which is how a comparison ends up matching whatever the new readers happen
to do.

## Decision

**The comparator audits answers; it never becomes one.** Nine positions, each with a
mechanism rather than a promise:

1. **Legacy stays authoritative and the comparator cannot change that.**
   `READ_AUTHORITY = "legacy"` is a constant with a test and a schema `const`;
   `assert_shadow_only()` refuses to run in the `prefer` and `only` lane modes, because
   those mean a reader already moved and there is nothing left to shadow.
2. **The question is an identity, not an intention.** `query_fingerprint()` is a digest over
   a closed normal form of the read. Both sides record it; a pair whose fingerprints differ
   is `filter_divergence`, judged before any row comparison, because every row verdict
   downstream of a different question is an artefact. An unrecognised filter key is folded
   into the fingerprint rather than dropped — a filter that vanishes from the identity of
   the question lets the two sides be asked different things and still agree.
3. **Row identity is recorded, never re-derived** — the same rule as ADR 0005, for the same
   reason: a comparator that reimplements the identity it audits can make both sides agree
   on a bug.
4. **Nothing inside the settle horizon is compared at all.** 900 s live, 86 400 s for
   drain/import lanes. Exclusions are *counted separately from comparisons*, so coverage is
   computable rather than asserted; a comparator that folds its exclusions into its
   denominator reports 100 % coverage of a window it mostly skipped.
5. **Absence is decomposed, and only one member of it is loss.** `pending`,
   `late_arrival`, retention-edge exclusion, `retention_divergence`, `tombstone_divergence`,
   `missing_canonical_row`, `missing_legacy_row`. Deletion and retention are judged *before*
   absence, because a tombstone and a fired TTL are recorded facts while absence is only the
   absence of evidence.
6. **Order, pagination and cursor stability are separate properties from content.**
   `result_hash()` is order-independent on purpose, so "one late row" and "one re-sorted
   page" stay distinguishable — they have different repairs. Page boundaries are never
   compared; one-sided truncation is, because unequal extent is evidence even though unequal
   page shape is not. Cursors are never compared *across* sides: an opaque token from one
   surface has no meaning on the other.
7. **Comparator competence is judged first, and loudly.** Snapshot skew, an expired cursor,
   an unreachable surface and a projection-version mismatch are `comparator_fault`: critical
   and cutover-blocking, even though they are the auditor's own defect. A gate met by a
   comparator that was not looking is not a gate.
8. **Restricted values are compared blind.** For coordinates, credentials and clip bodies,
   equality is decided in memory and only the verdict is durable — both values are `null`,
   not hashed. A stable digest of a coordinate is still a stable identifier for that
   coordinate. Query filter values go through the same redaction as result values, and clip
   audio is never read at all.
9. **Gate impact is a separate field from repair action.** "Who fixes this" and "may we
   proceed" are different questions, and conflating them is how a blocking finding gets
   closed as a ticket. Nine of the nineteen classes block a read cutover; the rest are
   expected behaviour with recorded reasons and must not, or the gate could never be met and
   the whole exercise becomes theatre.

Repair stays advisory and replay-only, exactly as on the write side:
`none`, `replay_inbox`, `replay_outbox`, `reindex_request`, `manual_review`, with
`disposition.queued` pinned `false` in the schema. A human dequeues.

## Why this reuses the write-side redaction instead of restating it

`READ_VALUE_ALLOW_PATHS` is the **union** of `reconcile.VALUE_ALLOW_PATHS` and the
read-shaped paths this comparator adds, and the deny pattern is
`reconcile.VALUE_DENY_PATTERN` itself, imported rather than copied. A test asserts the
superset relationship.

Two redaction lists in one repository drift, and only one of them gets tested on the day it
matters. Importing means a tightening on the write side is inherited here automatically, and
a divergence cannot be introduced by editing one file. The cost is one import edge from
`hear/verify/` to `hear/ingest/` — acceptable, because it runs in the direction of the
frozen, already-reviewed vocabulary.

## Why the receipt is staged rather than published

The same scheduling reason ADR 0005 records, one step further along. Publishing a contract id
requires regenerating `docs/data/phase0-freeze-contracts.v1.json`, and
`hear.reconcile.receipt.v1` is already staged in front of this one awaiting exactly that
regeneration. Promoting both at once means two lanes regenerating one hashed baseline, which
is a guaranteed conflict in a file where a conflict is meaningless and the resolution is
always "run the generator again".

So the artifacts are produced by a generator, gated for drift in CI, and laid out in exactly
the shape `contracts/` uses. **Promotion is a move plus a regeneration, with no content
change**, and the five steps are listed in `docs/phase5-shadow-read-comparator.md` §16.1. The
Phase 4 receipt promotes first; this one follows in the same shape. Until then the manifest
says `status: staged` with the reason and the source of truth recorded in it.

The same naming trap applies and is handled the same way: the constant is
`RECEIPT_CONTRACT_ID`, not `RECEIPT_SCHEMA_ID`, because `tools/freeze_contracts.py` harvests
`[A-Z_]*SCHEMA[A-Z_]*` assignments as published contract ids, and registering an unpublished
id in the frozen baseline would make the layout rule pass for a contract with no published
artifact. A test asserts the constant stays unharvestable.

## Why this is compatibility-safe

- **No live path changes.** No reader, writer, lane mode, schedule, manifest or credential is
  touched. `hear/verify/shadow_read.py` is imported by a generator and a test file and by
  nothing else — asserted by a test that scans `hear/` and `tools/` for importers.
- **No store, cluster or fleet access.** The module performs no I/O at all, asserted by AST
  rather than by grep. Everything in this lane runs against fixtures on a bare interpreter.
- **Forward.** The receipt is `additionalProperties: true` at every level, so a newer
  comparator's extra field is preserved by an older reader instead of failing validation. An
  unknown classification member is preserved, counted as `unclassified` so the accounting
  still closes, marked non-actionable, and treated as cutover-blocking — because a verdict
  this build cannot interpret is exactly the thing a gate must not step over.
- **Rollback.** Reverting this is reverting a package, a generator, a document, a decision
  record, a test file and a staged artifact directory that no runtime reads. Because the
  artifacts are generated, regeneration after a revert reproduces the previous bytes exactly,
  and CI checks it.
- **Mixed version.** Different read-contract majors between the two surfaces are a supported
  state (`version_divergence`, warn, not blocking) and comparison continues over the
  intersection of known fields. Different *projection* versions are a `comparator_fault`,
  because comparing two different shapes invents findings. Lane modes are per-lane, never
  global, because a staged rollout that can only be rolled back globally is not staged.
- **No new credential.** The comparator authenticates as nothing. It reads the
  `principal_id` and opaque `key_id` each surface already records, and there is no field in
  the receipt a token could occupy.
- **No-dependency baseline.** `hashlib`, `json`, `uuid`, `typing` and
  `hear.ingest.reconcile`. No transport, no cloud SDK, no store client.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Reuse the Phase 4 receipt for read comparisons | It has no place for a query fingerprint, a cursor property, a page count, a retention horizon or a coverage block. Overloading it would either lose those or make every write-side receipt carry fourteen null fields, and the first divergence in a *read* would be recorded as if a writer had failed. |
| Compare whole result bodies by hash | Every additive field and every legal ordering difference becomes a mismatch. That is the comparator people switch off in week two; the projection and the order-independent result hash exist for exactly this reason. |
| Compare at the leading edge, with no settle horizon | The comparator would page on its own scheduling jitter, the operator would mute it, and the one real loss would arrive muted. |
| Treat every "not returned" as missing | Legacy and canonical retention differ by design across four classes. This produces a permanently red board out of two healthy stores. |
| Report snapshot skew as the row differences it causes | A 45-second skew presents as a hundred missing rows — indistinguishable from the real finding. A comparator that blames a store for its own scheduling is one an operator stops believing. |
| Hash coordinates into the receipt instead of comparing blind | A stable hash of a coordinate is a stable identifier for that coordinate. A receipt store full of them is a location database, and `tools/coord_guard.py` exists precisely because that is the leak nobody notices. |
| Let the comparator reindex or backfill what it finds | An auditor with write access to its subject is not an auditor. Replay already exists and is already idempotent; a human deciding to run it is the control. |
| Sample mismatches to control storage | Sampling a failure signal makes the comparator a coin flip. Cost is controlled on the match side and by the per-query caps, neither of which a gate depends on. |
| Gate the cutover on "no mismatches" without qualification | Expected classes — late arrival, legacy redelivery, legal retention difference, mixed majors — would make the gate unmeetable, and an unmeetable gate is one that gets waived. Nine blocking classes are named instead. |
| Build the comparator first and write the semantics from what it found | That is how a comparison ends up matching whatever the new readers happen to do. |

## Consequences

- One stdlib-only package (`hear/verify/`), one generator, one staged artifact directory with
  26 worked fixtures, one specification, this record and one test file. No new dependency, no
  new service, no new credential, no deployment change, no reader change.
- The read cutover acquires a checkable gate: thirteen clauses in
  `docs/phase5-shadow-read-comparator.md` §16.2, each resolving to a counter or a receipt
  query, and an evidence bundle that reproduces because receipt ids are deterministic.
- The comparator's own failure modes (`comparator_fault`, stalled window, implausible zero,
  broken coverage accounting) are first-class findings rather than silence.
- Four questions are recorded rather than decided: the absolute canonical read-latency target
  (stated relative to a measured baseline instead, because the absolute number belongs to the
  same operator decision as the still-open ingest-lag SLO), mismatch-receipt retention
  (carried jointly with ADR 0005, a governance approval and not a design choice),
  retention-horizon parity between the two surfaces, and whether `prefer` → `only` needs its
  own sign-off. This record gates `compare` → `prefer` only, and deliberately does not
  pre-approve the removal of the legacy fallback.
