# 0009 - Phase 6 list, aggregate and export read comparison

- Status: proposed
- Date: 2026-09-15
- Scope: how a *future* legacy-versus-canonical comparison of **list, aggregate and export**
  reads is pinned, identified, bounded, classified and evidenced, before any read cutover.
  This record changes no reader, no writer, no API, no cursor, no deployment, no cluster
  object and no credential. It enables nothing: no query is issued, no comparator is
  scheduled, no lane mode is changed, no endpoint is added, and nothing is deployed.
- Companion to: `docs/decisions/0007-phase5-shadow-read-comparator.md` (the record-level
  comparator this extends and imports from), `docs/phase6-operator-read-audit.md` §8.2 (the
  deferral this discharges), `docs/phase6-operator-api-contract.md` §5 (the canonical cursor,
  ordering and pagination contract this assumes and does not create),
  `docs/legacy-operator-read-boundaries.md` (L1 unbounded export, L2 no cursor contract, L5
  unpinned thresholds), `docs/api-boundaries.md`, `docs/data-governance.md`.
- Enforced by: `tests/test_list_read_receipt_v1.py`,
  `tools/gen_list_read_contracts.py --check`, CI job `generated headers are current`.

## Problem

Phase 5's comparator compares **records**: one row on each side, joined on an identity both
sides already recorded. That covers the thirteen record-shaped evidence classes and stops
exactly where the Phase 6 read audit said it would (§8.2): aggregate and list reads
(`/api/queue`, the fleet-health table, `health_snapshot()`, `/api/export`) have **no
per-record identity pair**, and `reconcile.correlation_id()` raises rather than inventing one.
Synthesising a correlation key for an aggregate would defeat the guard that makes the record
receipt trustworthy, so the audit reserved a separately allocated contract instead and
deferred the semantics. This record supplies them.

Five properties of the legacy list surfaces make "the same answer" undefined today, and every
one of them is a defect in something this phase may not change:

1. **No cursor anywhere (P6-R1, L2).** `/api/queue` takes a `limit` clamped to `1..200` and
   nothing else. A canonical reader with opaque cursor pagination returns a *different shape*,
   so a naive list diff compares two answers that were never about the same rows.
2. **No snapshot isolation.** A legacy list is re-read against a live corpus cache and a live
   SQLite table. Two reads seconds apart legitimately differ. A comparator that reports that
   as loss produces a permanently red board out of two healthy stores, and a permanently red
   board is muted by week three.
3. **`/api/export` is unbounded (L1).** One request serialises every human annotation row.
   An auditor that reproduces that read twice per run has become a second copy of the cost
   defect it exists to retire. The recorded precedent is
   `/api/queue` re-parsing the whole corpus index on every request, 1.19 s against a 1 s
   readiness probe, which took the annotate service down
   (`docs/phase6-operator-api-contract.md` §11.1).
4. **Aggregates have no identity and enormous disclosure surface.** A count grouped by device
   and hour is a movement log; grouped by reviewer it is a performance review; a `min`/`max`
   over a coordinate is a bounding box. The write-side redaction rules protect *values* in a
   receipt and say nothing about what a group-by may be computed over.
5. **Verdicts depend on invocation flags (P6-R3, L5).** `--max-stale-s`,
   `--unfetched-window-s` and the clip deferred/lost thresholds change a health answer without
   changing a row, and they are recorded nowhere.

Deciding these after a list comparator is running means deciding them while the evidence is
being produced, which is how a comparison ends up ratifying whatever the new readers happen to
do.

## Decision

**A list answer is a function of a snapshot, a question and a threshold set. If the comparator
cannot pin all three, it refuses to compare and records the coverage it lost.** Nine
positions, each with a mechanism rather than a promise, all in `hear/verify/list_read.py`:

1. **Legacy stays authoritative, and the constant is imported, not restated.**
   `list_read.READ_AUTHORITY is shadow_read.READ_AUTHORITY`, and `assert_shadow_only()` is the
   same function object. Two copies of "who answers" drift, and the wrong copy is always the
   untested one.
2. **No pin, no comparison.** Each side declares a `pin_kind`: the canonical side's opaque
   `cursor_pin`, a store-level `snapshot_id`, or — for the cursorless legacy surfaces — a
   `range_pin`, a half-open `(since_id, max_id)` bound plus an `as_of` that the *comparator*
   puts on its own request. **No legacy reader, endpoint or cursor is changed to obtain it.**
   An unpinned or expired side is `comparator_fault`, and a pinned legacy re-read that shifted
   anyway is `snapshot_drift`: warn, never cutover-blocking, because it is a property of a
   surface nobody is changing in this phase.
3. **A cursor is opaque and side-local.** Never parsed, never re-derived, never durable in a
   receipt, and never compared across sides — `cursors_comparable()` returns `False`
   unconditionally. Page identity comes from `page_fingerprint(query, pin_id, index, size)`,
   which is scoped to a pin, so page 0 of two snapshots is never treated as one page.
4. **A total order or no comparison.** `assert_total_order()` requires a unique tie-break
   column. Ties ordered differently are `tie_break_divergence`, judged before content, because
   unstable ties page into different sets and make every page-level verdict unfalsifiable.
5. **Unbounded work is refused, not audited.** `export_equivalence()` proves that page
   exhaustion over a recorded `(since_id, max_id)` range is digest-identical to the bounded
   export of that range, in ordered slices of ≤ 1 000 rows, capped at 50 000 rows per proof.
   An unboundable range is `export_bound_exceeded` — warn, non-blocking, with the lost
   coverage counted in `coverage.excluded_unbounded`. The comparator never issues the
   unbounded dump.
6. **Counts, not people.** Group keys are a closed dimension list that excludes reviewer
   identity and every free-text field; an aggregate over a denied path raises at construction
   rather than being blinded; a cell below `MIN_CELL_COUNT = 5` keeps its verdict and loses its
   key and its numbers; and a cell key never appears in a difference path — an opaque
   `cell_ref()` digest does.
7. **Thresholds are pinned into the question.** The threshold set is folded into the query's
   normal form and published as `threshold_fingerprint`. Two sides invoked differently produce
   `threshold_divergence`: the run is **void**, which is neither a clean run nor a mismatch.
8. **Membership, order and content are three separate digests.** "Which rows", "in what order"
   and "with what values" are three findings with three repairs, and one digest over all three
   makes them indistinguishable.
9. **The comparator is an auditor.** No I/O at all in the module (asserted by AST), no node
   polling (P6-R5), no write path into either subject, `disposition.queued` pinned `false` in
   the schema, and repair actions that are advisory requests a human dequeues.

The receipt is `hear.readcompare.receipt.v1` — the id the Phase 6 audit reserved — **staged**
under `docs/phase6-list-read/`, generated from the module, drift-gated in CI, with 32 worked
fixtures covering all 23 classifications.

## Alternatives rejected

- **Extend `hear.shadowread.receipt.v1`.** Its bytes are frozen and its grains are record-
  shaped. A list finding has no row key, an aggregate has no identity pair, and an export has
  neither; forcing them in would either loosen a frozen contract or require synthesising the
  identity the write-side guard exists to prevent. ADR 0002 says a new shape is a new
  contract.
- **Give the legacy list a cursor first.** That is an API change to the authoritative surface
  during the phase whose entire premise is that the authoritative surface does not move. The
  comparator bounds its own request instead.
- **Compare the unbounded export directly.** It reproduces a recorded cost defect twice per
  run against a service that an unbounded read has already taken down once.
- **Sample aggregates.** Sampling the only number anybody reads is not cost control.
- **Suppress small-cell findings entirely.** That is an auditor hiding its own evidence. The
  finding is kept and the identity is dropped.
- **Compare cursors across sides.** An opaque token from one surface has no meaning on the
  other; diffing them invents a contract neither side signed.

## Consequences

**Forward.** A future comparator has a closed vocabulary, a pinning rule, a page identity, an
export equivalence proof, an aggregate privacy rule and thirteen cutover-blocking classes to
gate on. The staged artifacts are drift-gated, so the contract cannot quietly stop describing
its source.

**Mixed-version.** Different read-contract majors are `version_divergence` (warn) and a
supported state; different requested projection *versions* are `comparator_fault`; different
projection *shapes* are `projection_divergence`; an unknown classification from a newer build
is counted as `unclassified`, marked non-actionable, and **blocks a cutover**.

**Rollback.** Nothing to roll back at runtime: no reader moved, no schedule exists, no store
is written. Abandoning the lane reverts a module, a generator, a document, this record, a test
and a staged artifact directory that no runtime reads; regeneration after the revert reproduces
the bytes exactly, and CI checks it.

**Promotion.** `hear.readcompare.receipt.v1` is staged, not published, because publishing a
contract id regenerates `docs/data/phase0-freeze-contracts.v1.json` and three lanes
regenerating one hashed baseline is a conflict with no meaning. The Phase 4 and Phase 5
receipts are promoted ahead of it. Promotion is a move plus a regeneration, with **no content
change**:

1. `git mv docs/phase6-list-read/hear.readcompare.receipt.v1.schema.json contracts/schemas/`
2. `git mv docs/phase6-list-read/fixtures contracts/fixtures/hear.readcompare.receipt.v1`
3. Point `OUT_DIR` in `tools/gen_list_read_contracts.py` at `contracts/`.
4. Regenerate the freeze baseline (`tools/freeze_contracts.py --format json|markdown`).
5. `python3 tools/check_contract_layout.py`.

**Open, and deliberately not decided here.**

1. **Receipt retention.** Aggregate and list comparisons produce materially more receipts than
   record ones (P6-R6). This design bounds size, volume and identity exposure; it sets no
   destruction schedule, which is a `docs/data-governance.md` approval carried jointly with the
   identical open item in ADR 0005 and ADR 0007.
2. **Whether the k-anonymity floor of 5 is the governance-approved number** for aggregate cells
   over fleet data, or whether device-level cells need a different rule from site-level ones.
3. **Whether `/api/export` acquires a bounded, authorized range parameter before any list
   comparison runs.** This record assumes only that the comparator may *bound its own request*;
   if the legacy surface cannot honour a bound at all, annotation export comparisons stay at
   `export_bound_exceeded` and that coverage gap is an operator decision, not a design one.
4. **Canonical list read-latency targets**, which remain tied to the still-open ingest-lag SLO
   (`docs/phase4-https-batch-ingest.md` §16 open question 1, and
   `docs/phase6-operator-read-audit.md` §10).
