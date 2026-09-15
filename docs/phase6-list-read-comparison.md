# Phase 6 list, aggregate and export read comparison

**Status: design only.** Nothing here is deployed, scheduled or enabled. No reader, writer,
endpoint, cursor, cluster object, credential or lane mode changes. No query is issued against
any surface. The artifacts are a no-I/O source-of-truth module, a staged receipt contract with
worked fixtures, a falsifiability suite, and this document.

- Source of truth: `hear/verify/list_read.py`
- Generated, staged contract: `docs/phase6-list-read/` (`hear.readcompare.receipt.v1`)
- Generator (drift-gated in CI): `tools/gen_list_read_contracts.py`
- Tests: `tests/test_list_read_receipt_v1.py`
- Decision record: `docs/decisions/0009-phase6-list-read-comparison.md`
- Extends: `docs/phase5-shadow-read-comparator.md` (record grain)
- Discharges: `docs/phase6-operator-read-audit.md` §8.2 (the deferral)
- Answers: `docs/legacy-operator-read-boundaries.md` L1, L2, L5; audit risks P6-R1, P6-R3,
  P6-R5, P6-R6

---

## 1. Position

Phase 5 compares **records**: one row per side, joined on an identity both sides recorded.
Phase 6's read audit stopped there on purpose. Aggregate and list reads have no per-record
identity pair, and `reconcile.correlation_id()` refuses to invent one; inventing one anyway
would defeat the guard that makes the record receipt worth reading. §8.2 of the audit
therefore reserved the name `hear.readcompare.receipt.v1` and deferred the semantics. This
document supplies them.

Three invariants carry over unchanged, by **import** rather than by restatement:

| Invariant | Mechanism |
| --- | --- |
| Legacy answers; the comparator observes | `list_read.READ_AUTHORITY is shadow_read.READ_AUTHORITY`; `assert_shadow_only()` is the same function object |
| Settle, late, backfill, skew, retention and order horizons have one definition | `SETTLE_S`, `LATE_ARRIVAL_S`, `BACKFILL_SETTLE_S`, `SNAPSHOT_SKEW_S`, `RETENTION_EDGE_S`, `ORDER_GRACE_S` imported from `shadow_read` |
| Receipts quote nothing that is not on an allow list | `redact_value()`, `VALUE_ALLOW_PATHS` and the blind comparators imported, then narrowed |

One invariant is new, and it is the whole of this contract:

> **A list answer is a function of a snapshot, a question and a threshold set. If the
> comparator cannot pin all three, it refuses to compare and records the coverage it lost.**

## 2. Why record semantics do not stretch

| Property of the legacy list surfaces | Consequence for a naive list diff | Audit id |
| --- | --- | --- |
| `/api/queue` takes `limit` (clamped 1..200) and nothing else — no cursor, no offset, no order guarantee | The canonical envelope is cursor-paged over a total order; the two answers are not the same shape, so a row-set diff compares two different questions | L2, P6-R1 |
| No snapshot isolation: a live corpus cache plus a live SQLite table | Two honest reads seconds apart differ; reported as loss, this produces a permanently red board out of two correct stores | L2 |
| `/api/export` serialises every annotation row per request | An auditor that reads it twice per run becomes a second copy of the cost defect being retired; the precedent is `/api/queue` at 1.19 s against a 1 s readiness probe taking the annotate service down | L1 |
| Aggregates have no identity and a large disclosure surface | Counts grouped by device-hour are a movement log; by reviewer, a performance review; `min`/`max` over a coordinate is a bounding box | governance |
| `--max-stale-s`, `--unfetched-window-s`, clip deferred/lost thresholds change a verdict without changing a row, and are recorded nowhere | Two sides invoked differently disagree forever, correctly, about nothing | L5, P6-R3 |

## 3. Surfaces and grains

`GRAINS = (page, window, aggregate, export)`.

- **page** — one bounded slice of a list, identified by `page_fingerprint(query, pin_id, index, size)`.
- **window** — the union of the pages compared for one pinned question; where counts and
  membership are judged.
- **aggregate** — a grouped reduction: `count`, `distinct`, `sum`, `min`, `max`, `p50`, `p95`, `p99`.
- **export** — a bounded byte-equivalence proof over an id range, never a dump.

Twelve `LIST_SURFACES` are named: `annotation_queue`, `annotation_export`,
`fleet_health_table`, `health_snapshot`, `clip_index`, `detection_list`, `scene_list`,
`tag_list`, `score_list`, `localization_list`, `audit_list`, `ledger_stat`. Four are
`EXHAUSTIVE_SURFACES` (`health_snapshot`, `ledger_stat`, `audit_list`, `annotation_export`):
small or audit-bearing, so every comparison runs and none is sampled away. The rest are
sampled (§10).

## 4. Pinning: how a cursorless surface is compared without changing it

`PIN_KINDS = (cursor_pin, range_pin, snapshot_id, none)`.

| Side | Pin | Where it comes from |
| --- | --- | --- |
| Canonical list | `cursor_pin` | the opaque cursor the surface already returns |
| A store that offers one | `snapshot_id` | the store |
| **Legacy list (no cursor)** | `range_pin` | a half-open `(since_id, max_id)` plus `as_of`, applied by the **comparator to its own request** |
| Anything else | `none` | refused |

The legacy `range_pin` is the load-bearing move: it makes a cursorless surface comparable
**without adding a cursor to it**. No endpoint, reader, query or response shape changes; the
comparator simply declines to ask an unbounded question.

`pin_ok()` requires both bounds recorded and a pin age ≤ `PIN_TTL_S` (86 400 s, imported from
the canonical `CURSOR_TTL_S`, so one expiry governs both). An unpinned or expired side is
`comparator_fault` — the auditor's defect, never a store's. A *pinned* legacy re-read that
still shifted is `snapshot_drift`: warn, non-blocking, page verdicts withheld, because it is a
known property of a surface this phase may not change.

`pin_id` is a digest of the bounds. It is **never the cursor token**, which a receipt must not
carry.

## 5. Cursors are opaque and side-local

`cursors_comparable()` returns `False` unconditionally, and `cursor_opaque()` exists to be
asserted against. A cursor is never parsed, never re-derived, never durable in a receipt and
never compared across sides: an opaque token from one surface has no meaning on the other, and
diffing the two would invent a contract neither signed.

What *is* compared is behaviour: a cursor round-trip that returns different rows, or paging
that skips or duplicates a row across a boundary, is `cursor_divergence` — critical, blocking,
judged **before** content, because a page set that is not contiguous makes every content
verdict about it an artefact.

## 6. Question identity: query, threshold and page fingerprints

`normalise_query()` produces a canonical form over surface, filters, sort key, tie-break,
`bound_kind` (`half_open` / `closed`), projection and scope; `query_fingerprint()` digests it.
`bound_kind` is inside the fingerprint because an inclusive versus half-open upper bound is the
cheapest way to turn one request into two questions — that case is
`filter_divergence` (critical, blocking).

`normalise_thresholds()` / `threshold_fingerprint()` pin the operator flags that change verdicts
without changing rows (P6-R3). Two sides invoked with different thresholds produce
`threshold_divergence`: the run is **void** — neither a clean run nor a mismatch — and is judged
second, immediately after comparator faults.

`page_fingerprint(query, pin_id, index, size)` is scoped to the pin, so "page 0" of two
different snapshots is never treated as one page.

`projection_digest()` compares field *sets*: one shape difference is one finding, not one per
row.

## 7. Ordering and tie-breakers

`assert_total_order()` refuses a sort key without a unique tie-break. Without one, equal keys
page into different sets and every page verdict becomes unfalsifiable; equal keys ordered
differently is `tie_break_divergence` (critical, blocking), judged before order and content.

With a total order in place, ordering findings are graded by time rather than by shape:

- inversions inside `ORDER_GRACE_S` (300 s) are **`match`** — a gap that closes itself was
  never a defect;
- inversions that outlive it are `order_divergence` (warn);
- a row landing at a page edge while the window is open is `boundary_drift` (warn) — the page
  shifted, the data did not;
- one side stopping at `MAX_PAGES_COMPARED` while the other did not is
  `pagination_divergence` (warn, `reindex_request`), because the two answers then cover
  different extents.

Membership, order and content are three digests — `membership_digest()` (order-independent),
`order_digest()` (ordered), `result_hash()` (content) — because they are three findings with
three different repairs, and one digest over all three makes them indistinguishable.

## 8. Absence, lateness, deletion and retention

Inherited from Phase 5 and re-applied at list grain: "not in this list" is five different facts.

| Fact | Classification | Severity |
| --- | --- | --- |
| Inside `SETTLE_S` (900 s) | `pending` | info, **no receipt written** |
| Settled but arrived within `LATE_ARRIVAL_S` (3 600 s) | `late_arrival` | warn |
| Missing near a declared retention horizon, within `RETENTION_EDGE_S` (3 600 s) | **`match`** (excluded as a retention edge) | info |
| Pruned nowhere near either declared horizon | `retention_divergence` | warn |
| Tombstoned one side, still listed the other | `tombstone_divergence` | **critical, blocking** |

Legacy raw archives have no retention at all and the canonical class is days; treating that
asymmetry as loss is the fastest way to a board nobody reads.

## 9. Bounded export equivalence

`export_equivalence()` never issues the unbounded dump. It proves that **page exhaustion over a
recorded `(since_id, max_id)` range is digest-identical to the bounded export of that range**:

- ordered slices of `EXPORT_SLICE_ROWS = 1 000` rows, compared by `slice_digest()`, so the dump
  is never materialised;
- capped at `MAX_EXPORT_ROWS_COMPARED = 50 000` rows per proof;
- detects row-count mismatch, a manifest whose `rows_declared` disagrees with what was
  streamed, and identical rows in a different order (`export_divergence`, critical, blocking);
- a range that cannot be bounded at all is `export_bound_exceeded` — **warn, non-blocking**,
  with the lost coverage counted in `coverage.excluded_unbounded` rather than silently
  dropped. Refusing to read is a finding, and it is recorded as one.

This is the answer to L1. It also means the comparator's own cost is bounded by construction,
not by a promise about how often it runs.

## 10. Privacy-safe aggregates and receipts

The write-side rules protect *values*; an aggregate needs rules about what may be **grouped**
and **reduced**.

1. **Closed dimensions.** `GROUP_KEY_DIMENSIONS` is nine entries: `site_id`, `device_id`,
   `day`, `hour`, `evidence_class`, `kind`, `state`, `retention_class`, `lane_mode`. Reviewer
   identity and every free-text field are absent, and `assert_group_keys()` raises on anything
   else — an aggregate is not a place to add a dimension quietly.
2. **Denied reductions raise, they are not blinded.** `assert_aggregatable()` rejects any
   deny-pattern path at construction: a `min`/`max` over a coordinate is a bounding box, and
   blinding the number after computing it does not un-compute it.
3. **k-anonymity with the verdict preserved.** A cell whose population is ≥ `MIN_CELL_COUNT`
   (5) is pathed `aggregate.value.<ref>` and its numbers may be quoted. A cell below the floor
   is pathed `aggregate.value_small.<ref>` and its numbers are redacted — but the **finding is
   still reported**. Suppressing the finding would be an auditor hiding its own evidence;
   suppressing the identity is the actual requirement. Population defaults to 0 (suppress)
   when a caller fails to declare it.
4. **Cell keys never appear in a difference path.** `cell_ref()` is a truncated digest, so a
   receipt full of paths is not a movement log with extra steps.
5. **Counts are exact.** `count` and `distinct` are always compared with tolerance 0,
   whatever the caller asks for.
6. **Cells per comparison** are capped at `MAX_AGGREGATE_CELLS = 500`; differences at
   `MAX_DIFFERENCES = 32`; a receipt at `MAX_RECEIPT_BYTES = 8 192`.

`tools/coord_guard.py` makes a real-world coordinate merge-blocking; the fixtures contain
none, and the tests assert the denials rather than trusting them.

## 11. Sampling, coverage and cost

- `coverage_mode()` returns `exhaustive` for the four audit-bearing surfaces and `sampled`
  otherwise.
- `sample_page()` is deterministic on the page fingerprint (default denominator 10), so
  re-running a window compares the same pages and coverage is **computable rather than
  asserted**. It never samples away the first or last page, where boundary defects live.
- `sample_receipt()` (default denominator 50 for clean pages) never samples away a mismatch.
- `coverage()` reports compared pages and rows against what the authoritative surface offered,
  plus `excluded_unpinned` and `excluded_unbounded` — the two ways this comparator loses
  coverage honestly.
- `MAX_PAGES_COMPARED = 20`, `MAX_ROWS_COMPARED = 10 000` per question.
- `within_cost_ceiling()` holds one vCPU-minute per 10 000 compared rows
  (`COST_CEILING_S_PER_10K_ROWS = 60.0`), matching both sibling audits so one cost story covers
  all three.
- `conservation()` requires `compared == pending + match + sum(mismatch classes)`. A run whose
  accounting does not close is **not evidence** and must not be reported as one.

## 12. The closed classification vocabulary

Twenty-three classifications; thirteen block a read cutover. `classify()` applies them in a
fixed and load-bearing order: fault → thresholds → scope → projection → version → question →
pin → snapshot drift → cursor → tie-break → deletion → retention → absence → counts →
membership → aggregate → export → extent → order → boundary drift → values. Anything that
makes the *pair* invalid is judged before anything about its contents, so a broken pair never
produces a content finding.

| Classification | Severity | Gate | Repair intent |
| --- | --- | --- | --- |
| `match` | info | none | none |
| `pending` | info | none | none (no receipt) |
| `late_arrival` | warn | none | none |
| `snapshot_drift` | warn | none | none |
| `boundary_drift` | warn | none | none |
| `order_divergence` | warn | none | none |
| `pagination_divergence` | warn | none | `reindex_request` |
| `retention_divergence` | warn | none | `manual_review` |
| `version_divergence` | warn | none | `manual_review` |
| `export_bound_exceeded` | warn | none | `manual_review` |
| `cursor_divergence` | critical | blocks cutover | `reindex_request` |
| `tie_break_divergence` | critical | blocks cutover | `reindex_request` |
| `count_divergence` | critical | blocks cutover | `manual_review` |
| `membership_divergence` | critical | blocks cutover | `replay_inbox` |
| `aggregate_divergence` | critical | blocks cutover | `manual_review` |
| `export_divergence` | critical | blocks cutover | `manual_review` |
| `filter_divergence` | critical | blocks cutover | `manual_review` |
| `projection_divergence` | critical | blocks cutover | `manual_review` |
| `tombstone_divergence` | critical | blocks cutover | `manual_review` |
| `threshold_divergence` | critical | blocks cutover | `manual_review` |
| `scope_divergence` | critical | blocks cutover | `manual_review` |
| `comparator_fault` | critical | blocks cutover | `manual_review` |
| `unclassified` | critical | blocks cutover | `manual_review` |

`unclassified` exists so that a verdict produced by a newer build is counted, marked
non-actionable and **blocks a cutover**, instead of being silently dropped by an older reader.

## 13. Repair intent boundary

A repair action is an **advisory request a human dequeues**, never an action. The comparator
has no write path into either subject, `disposition.queued` is pinned `false` in the schema,
and `assert_shadow_only()` refuses any mode that is not a shadow mode. A comparator that can
fix what it measures cannot be trusted about what it measured.

## 14. Receipt

`hear.readcompare.receipt.v1`, media type
`application/vnd.dama.hear.readcompare.receipt.v1+json`, major 1, with its own uuid5 namespace
distinct from the record comparator's. `build_receipt()` emits the pair identity (query,
threshold, pin and page fingerprints — never a cursor), the classification, severity, gate
impact, repair intent, bounded differences over redacted paths, coverage, and the contract id.
`receipt_schema_document()` generates the JSON Schema, so the contract cannot stop describing
its source without CI noticing.

**Staged, not published.** Artifacts live under `docs/phase6-list-read/` because publishing a
contract id regenerates `docs/data/phase0-freeze-contracts.v1.json`, and three lanes
regenerating one hashed baseline is a conflict with no meaning. The Phase 4 and Phase 5
receipts promote ahead of this one; the promotion procedure is a move plus a regeneration with
no content change (ADR 0009, "Promotion").

## 15. Fault injection: 32 worked cases

Every classification has at least one worked fixture, and the "does not fire on correct
behaviour" cases are as important as the mismatches. Generated to
`docs/phase6-list-read/fixtures/` and drift-checked by
`tools/gen_list_read_contracts.py --check`.

| Classification | Severity | Gate | Repair | Case | Fixture |
| --- | --- | --- | --- | --- | --- |
| `match` | info | none | `none` | Both sides pinned, same page, same rows, same order. | `match-two-pinned-pages-agree.json` |
| `pending` | info | none | `none` | Canonical list has not caught up with rows minutes old; no receipt written. | `pending-inside-the-settle-horizon.json` |
| `late_arrival` | warn | none | `none` | Canonical page arrived 300 s after the settle horizon. | `late-arrival-after-the-settle-horizon.json` |
| `snapshot_drift` | warn | none | `none` | A pinned legacy re-read returned a different page; row verdicts withheld. | `snapshot-drift-legacy-list-has-no-isolation.json` |
| `boundary_drift` | warn | none | `none` | A row landed at a page edge while the window was open. | `boundary-drift-insert-at-a-page-edge.json` |
| `cursor_divergence` | critical | blocks | `reindex_request` | Same canonical cursor, different rows on re-read. | `cursor-divergence-unstable-round-trip.json` |
| `cursor_divergence` | critical | blocks | `reindex_request` | A row was skipped across a page boundary. | `cursor-divergence-row-skipped-across-a-page-boundary.json` |
| `tie_break_divergence` | critical | blocks | `reindex_request` | Equal `captured_at`, ordered differently: not a total order. | `tie-break-divergence-equal-sort-keys-ordered-differently.json` |
| `order_divergence` | warn | none | `none` | Inversions outlived the order grace window. | `order-divergence-past-the-order-grace-window.json` |
| `match` | info | none | `none` | The same inversions inside the grace window. | `match-inversions-inside-the-order-grace-window.json` |
| `pagination_divergence` | warn | none | `reindex_request` | One side stopped at the page cap. | `pagination-divergence-one-side-truncated.json` |
| `count_divergence` | critical | blocks | `manual_review` | Settled window, different row counts. | `count-divergence-on-a-settled-window.json` |
| `membership_divergence` | critical | blocks | `replay_inbox` | Same count, different rows. | `membership-divergence-same-count-different-rows.json` |
| `aggregate_divergence` | critical | blocks | `manual_review` | A per-device daily count differs; zero tolerance. | `aggregate-divergence-one-cell-differs.json` |
| `match` | info | none | `none` | Aggregates agree; a below-k cell is counted with its key redacted. | `aggregate-small-cell-key-suppressed.json` |
| `aggregate_divergence` | critical | blocks | `manual_review` | A below-k cell diverges: finding kept, both numbers redacted. | `aggregate-divergence-small-cell-values-redacted.json` |
| `match` | info | none | `none` | Page exhaustion equals the bounded export of the same range. | `export-equivalence-bounded-range-matches.json` |
| `export_divergence` | critical | blocks | `manual_review` | Same range and count, different ordered content digest. | `export-divergence-same-rows-different-content.json` |
| `export_bound_exceeded` | warn | none | `manual_review` | Unboundable export refused; coverage loss recorded. | `export-bound-exceeded-comparator-refuses-the-unbounded-dump.json` |
| `filter_divergence` | critical | blocks | `manual_review` | Inclusive versus half-open bound: one request, two questions. | `filter-divergence-inclusive-versus-half-open-bound.json` |
| `projection_divergence` | critical | blocks | `manual_review` | Different compared field sets; one finding, not one per row. | `projection-divergence-different-compared-field-sets.json` |
| `retention_divergence` | warn | none | `manual_review` | Rows pruned nowhere near either declared horizon. | `retention-divergence-pruned-away-from-any-horizon.json` |
| `match` | info | none | `none` | Rows past the legacy raw horizon excluded as a retention edge. | `match-rows-past-the-legacy-retention-horizon.json` |
| `tombstone_divergence` | critical | blocks | `manual_review` | A deletion did not propagate. | `tombstone-divergence-deleted-row-still-listed.json` |
| `threshold_divergence` | critical | blocks | `manual_review` | Two sides invoked with different thresholds; run is void. | `threshold-divergence-two-sides-invoked-differently.json` |
| `scope_divergence` | critical | blocks | `manual_review` | Two answers about different site/device sets. | `scope-divergence-two-sides-answered-for-different-scopes.json` |
| `version_divergence` | warn | none | `manual_review` | Mixed read majors; comparison continues over the intersection. | `version-divergence-mixed-read-majors.json` |
| `comparator_fault` | critical | blocks | `manual_review` | The legacy list could not be pinned. | `comparator-fault-unpinned-legacy-list.json` |
| `comparator_fault` | critical | blocks | `manual_review` | A pin older than its 24 h lifetime. | `comparator-fault-expired-pin.json` |
| `comparator_fault` | critical | blocks | `manual_review` | The two reads are 45 s apart. | `comparator-fault-snapshot-skew.json` |
| `comparator_fault` | critical | blocks | `manual_review` | The legacy surface did not answer. | `comparator-fault-legacy-surface-unavailable.json` |
| `unclassified` | critical | blocks | `manual_review` | One surface published no result digest. | `unclassified-missing-result-digest.json` |

## 16. Rollout and rollback

**Rollout is legacy-authoritative throughout.** Ordered, and each step is falsifiable before
the next:

1. This design lane merges. Nothing runs.
2. Canonical list surfaces gain the cursor and total-order contract specified in
   `docs/phase6-operator-api-contract.md` §5 — a separate lane, not this one.
3. A comparator process is built against this module, reading both sides in shadow. Legacy
   still answers every user.
4. Exhaustive surfaces first (`health_snapshot`, `ledger_stat`, `audit_list`), because they
   are small and their failures are cheap.
5. Sampled list surfaces, with `sample_page()` denominators tuned against the cost ceiling.
6. Bounded export equivalence last, because it depends on a bound `/api/export` does not have
   today.

**Rollback is the absence of a step**: there is no runtime to roll back, no reader moved, no
schedule created, no store written. Abandoning the lane reverts a module, a generator, a
document, a decision record, a test and a staged directory nothing reads. Regeneration after
the revert reproduces the bytes exactly, and CI checks it.

**Promotion gates** — a read cutover may not be proposed until all hold:

1. `gate_blocked()` is empty over a full comparison window.
2. `conservation()` closes on every reported run.
3. Coverage on exhaustive surfaces is 100 %, with `excluded_unpinned` at zero.
4. `excluded_unbounded` is zero, or the residual is an operator-accepted, written exception.
5. Bounded export equivalence passes for every export surface in scope.
6. No `unclassified` in the window.
7. A pre-comparison legacy read-latency baseline exists (it cannot be reconstructed after).
8. Aggregate receipts pass a governance review of the k-anonymity floor.
9. The contract is promoted from staged to published, with no content change.

## 17. Open questions

1. **Receipt retention.** List and aggregate comparisons produce materially more receipts than
   record ones (P6-R6). Size, volume and identity exposure are bounded here; a destruction
   schedule is a `docs/data-governance.md` approval, carried jointly with the identical open
   item in ADR 0005 and ADR 0007.
2. **Is `MIN_CELL_COUNT = 5` the governance-approved floor** for fleet aggregates, and do
   device-level cells need a different rule from site-level ones?
3. **Will `/api/export` acquire a bounded, authorized range parameter** before any list
   comparison runs? If it cannot honour a bound at all, annotation export comparisons stay at
   `export_bound_exceeded` permanently, and that coverage gap is an operator decision.
4. **Canonical list read-latency targets**, still tied to the open site SLO
   (`docs/phase4-https-batch-ingest.md` §16 open question 1,
   `docs/phase6-operator-read-audit.md` §10).

## 18. What this document does not do

It does not add, remove or change an API, reader, writer, cursor, schema in `contracts/`,
cluster object, credential, schedule or lane mode. It does not implement a live comparator, and
nothing in the runtime tree imports `hear/verify/list_read.py`. It issues no query against any
surface, and it publishes no contract id.
