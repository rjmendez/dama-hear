# Phase 6: the canonical operator API, audited as a contract

What the eventual operator API has to be, expressed as a set of contracts that can be
written and frozen **now**, separated from the set that cannot exist until Phase 2/3/4/5
produce evidence.

> **Status.** Design audit only. This document creates no service, no route, no OpenAPI
> document, no client, no credential and no cluster object. It changes no reader: every
> surface in [phase6-operator-read-audit.md](phase6-operator-read-audit.md) §2 remains
> authoritative for every operator answer. `READ_AUTHORITY` stays `"legacy"`
> (`hear/verify/shadow_read.py:62`) and nothing here proposes moving it. No Postgres, object
> store or secret is provisioned or touched. It moves no frozen contract hash.

Companions: [api-boundaries.md](api-boundaries.md) (the northbound target this audits
against), [phase6-operator-read-audit.md](phase6-operator-read-audit.md) (what an operator
reads today), [phase5-shadow-read-comparator.md](phase5-shadow-read-comparator.md) (how the
two are compared before anything moves), [data-governance.md](data-governance.md) (the
access, redaction and retention rules the API must not weaken),
[decisions/0002-contract-repository-layout.md](decisions/0002-contract-repository-layout.md)
(where a published contract may live).

## 1. Scope and the one rule

**In scope:** the mapping from each desired canonical resource to the legacy input that
would have to answer for it; the auth/RLS/role boundary; redaction and audit obligations;
cursor, ordering, page and error semantics; liveness, freshness and *unknown* semantics;
the clip-metadata/clip-bytes split; a query identity that a Phase 5 receipt can carry
unmodified; compatibility adapters for today's consumers; where an OpenAPI document and its
codegen may live; performance and retention behaviour; mixed-version and read-authority
flags; telemetry; and the rollout/rollback gates and tests a first route would need.

**Explicitly not in scope:** implementing or deploying any API; altering any legacy reader;
provisioning Postgres or object storage; handling any secret; selecting an identity
provider; cutting over read authority; and every item Phase 2, 3, 4 and 5 still owe.

The rule that decides every "now or later" call below:

> **A contract may be written now if, and only if, it constrains a shape.
> A contract may not be written now if it asserts a fact about data that does not exist yet.**

`api-boundaries.md` states the target in prose. Prose is not checkable and does not survive
a reviewer's absence. Turning the checkable parts of it into frozen, generated artifacts is
work that is available today at zero migration risk, because nothing serves them. Turning
the *unchecked* parts into artifacts would publish a promise about a store nobody populates
— which is the P6-R7 trap in a new costume.

---

## 2. Resource map: desired canonical API against the legacy input that must answer for it

Domains abbreviated as in the read audit: **H** health, **D** detections, **Sc** scenes,
**C** clips, **A** annotations, **T** TDoA, **S** scores, **Dur** durable records.

| Canonical resource (`api-boundaries.md`) | Dom | Legacy input that would have to answer | Evidence class (Phase 5) | Canonical side exists? |
|---|---|---|---|---|
| `GET /v1/health/nodes` | H | Redis `dama:hear:{device_id}` (TTL 30 s) + `/pool/heartbeat.json` + `check_fleet_health.py` table | `health` | **partial** — `hear.health_snapshot()` is a *store* health aggregate, not a per-node liveness projection |
| `GET /v1/nodes`, `GET /v1/nodes/{id}` | H | node `GET /status` polled by `fleet.py`, `hear_drain.py` | `heartbeat` | **no** — no node registry table exists; identity is whatever the node reports |
| `GET /v1/events`, `POST /v1/events:search` | D | `/pool/corpus` detection JSONL via `hear/pool.py` | `detection` | **no** — Phase 3 import not run |
| `GET /v1/events/{id}` | D | pool row addressed by `record_uid`/`event_id` | `detection` | **no** |
| scene rows (no named route yet) | Sc | pool scene reader | `scene` | **no** |
| `POST /v1/clips:request`, `GET /v1/clips/{id}` | C | `/pool/corpus/clips/index.jsonl` via `CorpusCache` | `clip_manifest` | **no** — object manifests are Phase 3 |
| `GET /v1/clips/{id}/content` | C | `GET /api/audio/{clip_key}` (`tools/hear_annotate/server.py:645`) | *never compared* | **no** |
| `POST /v1/events/{id}/labels` | A | `POST /api/annotations` → `annotations.sqlite3` | `annotation` | **no** |
| annotation export | A | `GET /api/export` (`server.py:677`), unbounded | `export_manifest` | **no** |
| `GET /v1/tracks`, `POST /v1/localizations` | T | `/pool/corpus/tdoa/{arrivals,runs}` via `hear_tdoa.py` | `localization` | **no** |
| score lane reads | S | `hear.sketch_score.v1` JSONL via `hear_score.py` | `score` | **no** |
| tag lane reads | C, D | `hear.clip_tag.v2` via `hear_tag.py` | `tag` | **no** |
| `GET /v1/models` | — | model files + `hear.tdoa_model_card.v1` | `model_card` | **no** |
| durable ledger read | Dur | `hear_heartbeat_receiver.py` durable SQLite | `ledger_stat`, `audit_record` | **yes (designed)** — `hear.durable_records_operator`, `_audit`, `refused_messages_audit` |
| ingest (write) | — | `POST /api/hear/heartbeat`, `hear.ingest.batch.v1` | n/a | **yes** — `contracts/schemas/hear.ingest.batch.v1.schema.json` |

Three readings of this table, and they are the whole finding:

1. **Exactly one read domain has a canonical side in the tree today, and it is `Dur`.** It
   is expressed in DDL (`deploy/postgres/migrations/0004`, `0006`), has never been read by
   any operator tool, and covers durable records only.
2. **Eleven of the thirteen Phase 5 evidence classes have no canonical producer at all.**
   Designing their routes is free; asserting their response bodies is not, because the body
   is a claim about rows that Phase 3 has not imported.
3. **Two rows have no evidence class on purpose.** `GET /v1/clips/{id}/content` returns
   bytes and is never compared (§8); `POST /v1/ingest/batches` is a write and belongs to
   Phase 4.

### 2.1 Gaps the target API names and the legacy side cannot produce

| Target concept | Legacy reality | Consequence |
|---|---|---|
| `tenant_id` on every object | one tenant, `'default'`, defaulted in DDL (`0001:67`) and nowhere else in the tree | the API must derive tenant from the token from day one even while only one value exists, or retrofitting it is a breaking change |
| `site_id` | `HEAR_SITE_ORIGIN`/`HEAR_SITE_LATLON` env values, not a resource | `normalise_query()` already **requires** a non-empty `site_id` (`shadow_read.py:421`); a site registry is owed before a comparison can even be fingerprinted |
| opaque resource ids | `clip_key`, `record_uid`, `device_id` are all meaningful strings | ids must be opaque **at the API boundary**, mapped from the recorded logical id — never re-derived (Phase 5 rule 3) |
| `ETag` / `If-None-Match` | no reader emits or honours one | additive later; not a blocker |
| saved searches, alert rules, incidents, integrations, exports-as-jobs | nothing in the tree | these are *new product surface*, not a migration. Keep them out of the Phase 6 cutover scope entirely or the cutover never ends |
| geometry versions, calibration runs | `hear.node_positions.v1`, clap-calibration evidence files | a calibration resource cannot be specified before the capture-path bias it records is measured |

---

## 3. Auth, RLS and the role boundary

The target says OAuth 2.0/OIDC with tenant/site claims and 30 named scopes. The tree has
one bearer token (`HEAR_HEARTBEAT_TOKEN`, write-only), one unauthenticated HTTP service, and
four NOLOGIN Postgres group roles.

### 3.1 Three enforcement layers, and which one is authoritative

| Layer | Mechanism in tree | Enforces |
|---|---|---|
| Token scope | none today; `ADR 0006 admin-token-provisioning-policy` is the only scope record | *what operation* a caller may attempt |
| Service-side authorization | none on the annotate surfaces | *which tenant/site/class/purpose* |
| Postgres RLS | `POLICY tenant_isolation` on five tables, `USING (tenant_id = current_setting('hear.tenant_id'))` (`0004:52-69`) | *which rows*, even when the query forgot a `WHERE` |

**RLS is the only one of the three that exists, and it is the only one that survives an
application bug.** The API must therefore run as a member of `hear_durable_reader` with
`hear.tenant_id` set per request from the token claim — not as an owner, and never as
`hear_durable_admin`. `security_invoker = true` on both read views (`0004:82`, `:104`) is
what makes that work: the views are evaluated with the caller's rights, so tenant isolation
still applies *through* them. An API that connected as the view owner would silently defeat
every policy in the migration.

### 3.2 Role mapping

| Governance role (`data-governance.md` §3) | DB role | API scopes it maps to | Status |
|---|---|---|---|
| operator | `hear_durable_reader` | `nodes:read`, `health:read`, `events:read` | role exists, no scope exists |
| reviewer | *none* | `clips:read`, `events:annotate` | **missing on both sides** |
| evidence custodian | *none* | `clips:export`, `exports:create` | **missing** |
| model steward | *none* | `models:read`, `models:publish` | **missing** |
| administrator | `hear_durable_admin` | `admin:tenant` | exists; holds the only `DELETE` |
| auditor | `hear_durable_auditor` | audit read | exists; sees digests, never bodies |

`reviewer` is the consequential gap: it is the role that reads clip audio, and clip audio is
the one class today served with **no authorization at all** (`/api/audio/{clip_key}`). A
canonical `GET /v1/clips/{id}/content` that mirrors that behaviour would launder an existing
exposure into a "migrated" surface.

### 3.3 What this fixes and what it must not

A canonical API that authorizes reads is strictly better than the legacy surfaces it would
replace. That is exactly why it is dangerous as a *justification*: "the new API has auth" is
not a reason to widen exposure before the old surfaces are closed. The read audit's §5
position holds unchanged — **authorize the annotate surfaces first, independently, in the
security lane, before any read-surface work** — because otherwise the cutover inherits the
gap and the gap acquires a migration ticket instead of a fix.

---

## 4. Redaction and auditing

### 4.1 Redaction is already layered; the API is the fourth layer, not the first

| Layer | Mechanism | Removes |
|---|---|---|
| Edge | `data-governance.md` §2; today's firmware emits `gps: {"fix": n}` and no coordinates | coordinates before uplink |
| Store view | `hear.durable_records_operator` strips `{gps,lat}`, `{gps,lon}`, `{gps,alt_m}` (`0004:95`) | coordinates from the operator read |
| Receipt | `REDACTION_PROFILE = "hear.shadowread.redact.v1"`; `blind`/`presence` comparators may never place a value in a receipt **even redacted** (`shadow_read.py:160`) | values from evidence |
| Repository | `tools/coord_guard.py`, CI-gated | coordinates from the tree |

The API adds nothing new here and must not subtract: it reads the redacted view, never the
base table. Its own obligation is the one layer none of the four provides — a **per-field
authorization decision**, so that an `evidence custodian` can obtain a coordinate the
`operator` cannot, with the grant recorded. That is a service-side rule; it is not
expressible in the current DDL and must not be faked by granting the operator role wider
`SELECT`.

The deny-list-first posture is inherited verbatim: an unrecognised field path is
**unquotable by default**. A new firmware field appears in a response body only after
someone classifies it. The alternative — allow by default — is how a `gps.lat` reaches a
dashboard one firmware release after everyone stopped watching.

### 4.2 Auditing

`data-governance.md` §9 requires audit events for reads, searches, downloads, label edits,
exports, grants, holds, deletions and administrative actions, append-only, protected from
the same failure that affects the content. The tree has `hear.durable_records_audit` and
`hear.refused_messages_audit` — custody metadata for *ingest*, not access records for
*reads*. There is no read-audit table anywhere.

So: **the canonical API's first new durable object is an access-audit record, not an event
row.** It must be written before the response is returned, carry actor, tenant, scope,
query fingerprint (§7), result count, classes touched and decision, and never carry the
result. Bulk-read alerting (§9 of governance) is defined over it. Designing this now is
cheap and correct; the record's schema depends on no migrated data.

---

## 5. Cursors, ordering, pagination and errors

This is the largest *shape* surface that can be contracted now, and the one whose absence
Phase 5 has already flagged as a blocker (P6-R1: no cursor exists anywhere today).

### 5.1 The list envelope

`api-boundaries.md` fixes it: `{"items": [], "next_cursor": "opaque", "has_more": true}`,
`limit` default 50 cap 500, cursor encodes the complete stable sort (normally
`created_at,id`), cursor TTL 24 h, cursors opaque to clients. Phase 5 already depends on
three of those as *checked properties* (`shadow_read.py`): `CURSOR_TTL_S = 86_400`,
`MAX_PAGES = 20`, `MAX_ROWS_COMPARED = 10_000`, and `cursor_divergence` is critical and
cutover-blocking.

Six rules the API must obey for a comparison to mean anything, each already implied by the
comparator's classifications:

1. **Total order or no cursor.** The sort key must be unique — `(created_at, id)`, never
   `created_at` alone. A non-unique sort makes duplicates or gaps across a page boundary
   legal, and both are `cursor_divergence`.
2. **Cursor round-trip stability.** Re-reading with the same cursor returns the same rows.
3. **Page size may differ between sides; extent may not.** The comparator compares the
   concatenation, never page boundaries (`§5` of the Phase 5 spec). One-sided truncation at
   the cap is `pagination_divergence` and must be recorded in `sides.*.truncated`.
4. **Cursors are never compared across sides.** An opaque token from one surface has no
   meaning on the other. The API must not accept a foreign cursor, and must refuse an
   expired one with a typed error rather than silently restarting the scan — a silent
   restart is how a "stable" cursor loses rows.
5. **Ordering is producer-local.** A changed `boot_id` makes a node's sequence incomparable,
   the same discontinuity rule `hear_drain.py` already applies.
6. **A mandatory, capped time range on search.** `api-boundaries.md` caps interactive search
   at 31 days; unbounded reads go to Exports. This is the direct remedy for `/api/export`'s
   current unbounded full dump.

### 5.2 Errors

RFC 9457 Problem Details with a stable machine `code`, per `api-boundaries.md`. Two
additions this audit requires, both because of how Phase 5 classifies a side's outcome
(`SIDE_OUTCOMES = ("answered", "refused", "errored", "unavailable")`, `shadow_read.py:133`):

- **A refusal must be typed and durable, not a bare status.** `refused` (a scope denial, an
  out-of-range window) and `errored` (a failed read) classify differently and route to
  different owners. An API that returns `403` with no `code` makes them indistinguishable,
  and a comparator that cannot tell them apart produces `comparator_fault` against a healthy
  store.
- **Unknown is a value, not an error.** See §6.

Reserved codes that follow from constraints already in the tree, and can be frozen now:
`CURSOR_EXPIRED`, `CURSOR_FOREIGN`, `SORT_KEY_NOT_TOTAL`, `TIME_RANGE_REQUIRED`,
`TIME_RANGE_TOO_LARGE`, `PROJECTION_VERSION_UNSUPPORTED`, `READ_MODE_NOT_PERMITTED`,
`TENANT_SCOPE_DENIED`, `CLASS_SCOPE_DENIED`, `RETENTION_EXPIRED`, `SNAPSHOT_SKEW_EXCEEDED`.

---

## 6. Liveness, freshness and the unknown semantics

The read audit's §4 finding is that "healthy" is assembled from five different time models,
and P6-R2 is that liveness is expressed as **absence** — an expired Redis key, a missing
`heartbeat.json`. Absence is not a value a comparator can compare, and it is not a value an
API can return.

### 6.1 The API must return a verdict with its derivation

A `GET /v1/health/nodes` item is not a heartbeat row. It is:

```text
node_id, tenant, site,
liveness        ∈ {live, stale, expired, never_seen, unknown}
observed_at     the instant the *source* observed it
as_of           the instant the API answered
source          which time model produced the verdict
thresholds      the threshold set that produced it
clock_tier      the clock-quality class, not a wall-clock claim
confidence      whether the verdict is derived or asserted
```

Five properties, each forced by something already in the tree:

1. **`expired` is explicit.** Redis TTL expiry must surface as a row with
   `liveness: expired`, not as a missing row. P6-R2 exists precisely because a canonical
   projection that returns an explicit `expired` row structurally mismatches a legacy side
   that returns nothing; the comparator's answer is to compare a *derived verdict*, and the
   API is where that verdict is derived.
2. **`unknown` is distinct from `expired`.** `ROW_STATES` already separates `absent`,
   `tombstoned` and `retention_expired` because "three of these are forms of *not returned*
   and only one of them is loss." Liveness needs the same discipline: a node the API could
   not evaluate is `unknown`, and `unknown` must never be rendered as healthy **or** as
   offline. A dashboard that paints `unknown` green is the failure mode; one that pages on
   it is the other.
3. **Thresholds travel with the answer.** `--max-stale-s`, `--unfetched-window-s` and the
   clip deferred/lost thresholds are invocation inputs today (P6-R3), which means two
   correct readers can legally disagree. The API must pin them into the response, and a
   comparison whose two sides used different thresholds is **void, not a mismatch**.
4. **Unanchored time is a tier, never a value.** A node with no GPS fix reports an instant
   nobody should compare. The `tier` comparator ignores the value and compares the class;
   the API must expose the class so that is possible.
5. **Freshness is a first-class field.** `observed_at` and `as_of` are separate because the
   comparator's `SNAPSHOT_SKEW_S = 5` bound is meaningless without both. An API that returns
   one timestamp has made the skew unmeasurable.

### 6.2 Known legacy defects must be expressible, not silently fixed

R3 (a deduplicated heartbeat does not re-arm the Redis TTL, so a healthy rebooting node
reads offline), R11 (false latched `mic_state: capture-failure`) and R14 (`fix: 6`
dead-reckoning read as a fix) are baked into legacy answers. A canonical API that is simply
*correct* diverges from legacy on every affected node, and the comparator reports that as a
canonical regression unless the difference is declared.

The API's obligation is therefore narrow and specific: **make the derivation visible** so a
dated expected-difference list (P6-R4) can name the defect. The cutover is not the place to
fix R3/R11/R14, and the API is not the place to hide them.

---

## 7. Query identity compatible with Phase 5 receipts

This is the tightest constraint in the document and the most valuable one, because it is
already implemented and frozen in behaviour by 117 tests.

`normalise_query()` (`shadow_read.py:405`) defines the closed normal form of one read
question, and `query_fingerprint()` is SHA-256 over its canonical JSON. The canonical API's
list/search parameters must map **onto that normal form without remainder**:

| Normal-form key | Required API parameter | Notes |
|---|---|---|
| `evidence_class` | route-implied | one of the thirteen; an unlisted class is `comparator_fault`, never a skipped read |
| `site_id` | required, from token claim | `_require_token` rejects empty; a site registry is a prerequisite |
| `device_ids` | repeated filter, **sorted** | sorted in normalisation, so order must not be semantic |
| `time_from`, `time_to` | required, capped | half-open vs inclusive upper bound is the named usual cause of `filter_divergence` — the API must state which, once, in the contract |
| `filters` | sorted map | every filter must be representable as a scalar map entry |
| `sort.field`, `sort.direction` | defaults `captured_at`/`asc` | the API's default must equal this or the fingerprints differ for the same question |
| `page_size` | `limit`, default 50 | must be echoed, since it is folded into the fingerprint |
| `as_of` | snapshot instant | the API must accept **and echo** it, or the two sides read different moments |
| `settle_s` | comparator input | not a client parameter; the API must not reject an unknown key that carries it |
| `projection_version` | explicit, required | different projection versions are `comparator_fault`, not a store defect — so the API must version its projection independently of `/v1` |

Three consequences, stated as API requirements:

- **Unknown keys are folded in, not dropped** (`normal["extra"]`). An API that strips an
  unrecognised query parameter and answers anyway lets the two sides be asked different
  questions and still "agree". The API must either honour a parameter or refuse the request
  with `422` — never ignore it.
- **`as_of` must be a supported parameter.** Without it the canonical side cannot be asked
  the question the legacy side was asked, and `snapshot_skew_ok()` fails every pair.
- **Row identity is never re-derived.** `row_key()` folds the `logical_id` each surface
  *already recorded*. The API's opaque id must be a stable *encoding* of that recorded
  identity, not a new hash computed at response time; a comparator that compares two
  re-derivations can make both sides agree on a bug.

`projection_version` deserves emphasis: it is the field that makes an additive API change
safe to compare. `/v1` versions the *contract*; `projection_version` versions the *shape
that was compared*. Conflating them means every additive field looks like a divergence.

---

## 8. Clip metadata versus clip audio

The single hardest boundary, and the one where the current tree is furthest from the target.

| | Clip **manifest** | Clip **audio** |
|---|---|---|
| Governance class | `detection`/`clip` metadata | `clip` / `raw_audio`, highest restriction |
| Target route | `GET /v1/clips/{id}` | `GET /v1/clips/{id}/content` → **signed URL**, bytes never through the app |
| Legacy today | `clips/index.jsonl` row via `CorpusCache` | `GET /api/audio/{clip_key}` streams WAV bytes through the app, unauthenticated |
| Phase 5 treatment | `clip_manifest`, **restricted**: id, digest, length, retention state | `RESTRICTED_CLASSES` — bytes are **never read by the comparator at all** |
| Retention | `R0-derived` 90 d | `R1-clip` 30 d, `R2-raw` 7 d |

Four rules follow, and none of them needs any migrated data to be written down:

1. **Metadata and bytes are different resources with different scopes.** `clips:read` gets
   the manifest; obtaining bytes is a separate authorization and a separate audit record.
2. **Bytes never flow through the query path.** The control plane authorizes and returns a
   short-lived signed URL. This is not only a performance choice: it is what keeps clip
   bytes out of request logs, out of the comparator and out of the receipt store.
3. **The comparator never sees a clip.** A clip is compared as its manifest. `blind` and
   `presence` may not place a value in a receipt **even redacted**, because a redacted hash
   of a coordinate is still a stable identifier for a coordinate — and the same is true of
   an audio digest used as a join key across an access boundary.
4. **Retention divergence is expected here and must be declared.** The legacy clip rule is a
   2 GiB cap, not an age; the canonical rule is `R1-clip` at 30 days. The comparator compares
   only inside the **intersection** of the two declared windows. The API must expose the
   retention class and horizon per clip so that intersection is computable, and must return
   `410`/`RETENTION_EXPIRED` for a pruned clip — which is exactly what the legacy route
   already does (`server.py:655`), the one place legacy behaviour is already correct and
   should be preserved verbatim.

---

## 9. Compatibility adapters

The point of an adapter here is not elegance; it is that the operator answers in
[phase6-operator-read-audit.md](phase6-operator-read-audit.md) §3.3 are the **gate evidence
for Phases 2, 3 and 4**. If a read cutover breaks `bridge_soak_evidence.py` or
`hear_drain.py --check`, the evidence for the other phases becomes unreadable mid-migration.

| Consumer | Adapter shape | Risk |
|---|---|---|
| `hear_drain.py --check`, `--json` | none — drain is a *node* reader and a pool writer. It must never be pointed at the API (P6-R5: a second fetcher competes with capture) | high if ignored |
| `check_fleet_health.py` | thin client behind existing flags, emitting the same table and exit codes; `--source legacy\|canonical` defaulting to `legacy` | medium |
| `fleet.py` | none — polls nodes directly; out of API scope | n/a |
| `bridge_soak_evidence.py` | none during the migration. It is gate evidence for Phase 2; changing its reader invalidates the soak | **do not touch** |
| CronJob `-check` lanes | unchanged. They read pool files; the pool is still written | low |
| annotate UI | the one consumer that would genuinely move, and it is last (stage 6) because it needs the reviewer role to exist first | high |
| `dama-gotchi` | already bounded by `api-boundaries.md` and enforced by `tools/recoupling_guard.py` — no in-repo AWS SDK consumer, merge-blocking | enforced |

**Adapter rule:** an adapter translates a *shape*; it never re-derives a *verdict*. A
`check_fleet_health.py` adapter that recomputed `ONLINE`/`DEGRADED`/`OFFLINE` from canonical
rows using its own thresholds would be a second implementation of the health semantics, and
the two would drift silently. It must read the verdict and the threshold set the API
returned (§6.1) and render them.

---

## 10. Where OpenAPI, JSON Schema and codegen live

ADR 0002 §5 already decided this, and the decision holds unchanged: **`proto/`, `openapi/`
and `codegen/` are not created now — an empty directory is a promise, not a contract. They
arrive with the first service that needs them.**

What this audit adds is the shape of the arrival, so the lane that creates them has no
choices left to get wrong:

```text
contracts/
  schemas/    hear.operatorquery.v1.schema.json      generated
              hear.operatorpage.v1.schema.json       generated
              hear.operatorproblem.v1.schema.json    generated
  fixtures/   hear.operatorquery.v1/*.json + manifest.json
  openapi/    dama-hear-operator.v1.yaml             generated, arrives with the service
  codegen/    arrives with the first non-Python client
```

Six constraints, every one of them an existing CI gate rather than a new one:

1. **Source of truth is the owning Python module, never the artifact** (ADR 0002 rule 3). An
   OpenAPI document generated from a live FastAPI app is a description of an implementation;
   a contract must constrain the implementation, so the field table comes first.
2. **`contracts/` is generated-only.** No hand-edited YAML or JSON (rule 1/7).
3. **Every artifact has a `tools/gen_*.py` and that generator runs in CI**, or
   `check_contract_layout.py` rejects it.
4. **Every contract id is named by a decision record**, uniquely numbered (rule 6 — see the
   note in §14.3).
5. **Every fixture declares its expected outcome in `manifest.json`**, so a cross-language
   client can assert without importing Python.
6. **Staging before publishing.** Publishing a contract id regenerates
   `docs/data/phase0-freeze-contracts.v1.json`, and two lanes regenerating one hashed
   baseline is a guaranteed meaningless conflict. The Phase 4 reconcile receipt is staged
   first, the Phase 5 shadow-read receipt behind it; an operator-API contract stages behind
   **both** and is promoted by a `git mv` plus a regeneration with no content change.

One further constraint specific to this lane: the contract id constant must **not** match
`[A-Z_]*SCHEMA[A-Z_]*`, because `tools/freeze_contracts.py` harvests that pattern as a
published contract id. Phase 5 already hit this and named its constant `RECEIPT_CONTRACT_ID`
with a test pinning the reason. An operator-API module must do the same.

---

## 11. Performance and retention behaviour

### 11.1 Performance

The tree contains one hard-won lesson and one O(1) design, and they point the same way.

`GET /api/queue` re-parsed the entire corpus index on every request; at 1 878 clips that was
1.19 s of honest work against a 1 s readiness probe, and it took the annotate service down.
The remedy was a health endpoint that is **O(1) in the corpus by construction** — it opens
the database and `stat`s the sources, and never builds a queue (`server.py:613-637`). On the
canonical side, `hear.health_snapshot()` is documented as "O(1) counters plus
partial-index-backed pending metrics; safe to call from a 10 s readiness probe".

Four API requirements follow:

1. **Health and readiness are O(1) in the data.** Never a count over rows, never a parse of
   a growing file.
2. **Every list read is bounded at the contract, not by convention.** `limit` capped at 500,
   a mandatory capped time range, `MAX_PAGES`/`MAX_ROWS_COMPARED` honoured by the comparator
   on top.
3. **Unbounded work is a job, not a request.** Exports return `job_id`/`status_url`. This
   replaces `/api/export`'s full dump with something that has a cost bound and an audit
   record.
4. **A latency baseline must be captured *before* any lane is enabled.** G6 states the gate
   relative to the recorded legacy baseline, deliberately, because the absolute p99 belongs
   to the same still-open operator decision as the ingest-lag SLO. A baseline not captured
   before the comparator runs cannot be reconstructed afterwards.

### 11.2 Retention

Retention is a governance contract the API surfaces; it is not an API policy.

| Class | Default max | Canonical enforcement | Legacy reality |
|---|---|---|---|
| `R0-derived` | 90 d | `enforce_retention()` refuses > 90 d with a named reason (`0003:112`) | pool files, no age rule |
| `R1-clip` | 30 d | Phase 3 key design | 2 GiB cap, not an age |
| `R2-raw` | 7 d | Phase 3 key design | raw archives, no retention |
| `R3-evidence` | until release | append-only | — |
| `R4-audit` | 1 y | `enforce_refusal_retention()` | — |

Requirements: retention clocks run on **capture time, not upload time**; every resource
exposes its retention class and horizon so the comparator can compute the intersection; a
pruned row is `retention_expired`, never a 404 (`ROW_STATES` separates them because only one
of them is loss); and the API never extends retention — a legal hold is a separate, audited
action with its own release authority. Receipt retention itself is an **open governance
decision**, carried identically by ADR 0005 and the Phase 5 spec, and this document does not
resolve it either.

---

## 12. Mixed-version behaviour and read-authority flags

### 12.1 The flags do not exist

`HEAR_OBJECTSTORE_MODE_{DRAIN,TAG,SCORE,TDOA,ANNOTATE}` is named once, in the Phase 5 spec
§7. It appears nowhere else in the repository: not in `tools/`, not in `hear/`, not in
`deploy/`. The per-lane read ladder `("off", "shadow", "compare", "prefer", "only")` exists
only as a tuple in `hear/verify/shadow_read.py:67`.

This is the correct state and should be preserved until the Phase 5 gate. The finding is
that it must be recorded, because a reader could reasonably infer from the spec that a
mechanism exists. **It does not.** Selecting a feature-flag mechanism *to be used* is
explicitly out of scope for both this document and the read audit.

### 12.2 Properties any future flag must have

Derived from the Phase 5 invariants, not invented here:

1. **Per-lane, never global.** A staged rollout that can only be rolled back globally is not
   staged.
2. **`prefer` and `only` are refused by the comparator** (`assert_shadow_only()`). Those two
   mean a reader already moved; there is nothing left to shadow.
3. **The mode is recorded in every receipt** (`lane_mode` label) — a comparison whose mode is
   unknown is not evidence.
4. **`READ_AUTHORITY = "legacy"` is a constant, a schema `const` and a test**, not a config
   value. A build that flipped it would be a read cutover disguised as a comparator change.
   An API must not be able to change read authority by configuration.
5. **A mixed-version fleet is a supported state, not an error.** Different read-contract
   majors between the two surfaces are `version_divergence` (warn, not cutover-blocking) and
   comparison continues over the intersection of fields both majors define. Different
   *projection* versions are `comparator_fault`. The API must expose both numbers separately
   for that distinction to be makeable.
6. **Additive within a major, always.** `additionalProperties: true` at every level; new
   optional fields and new enum members are compatible; a new major is a new file, never an
   edit.

---

## 13. Telemetry and observability

Phase 4 and Phase 5 already define two metric families with a shared discipline. The API
adds a third, and inherits the discipline rather than restating it.

Labels: `site`, `tenant`, `route`, `evidence_class`, `code`, `scope_outcome`, `lane_mode`.
**Never a token, never a filter value, never a coordinate, never a clip id.**

| Metric | Type | Use |
|---|---|---|
| `operator_api_requests_total{route,code}` | counter | denominator; `code` is the stable problem code, never the message |
| `operator_api_latency_seconds{route}` | histogram | the G6 input, compared against the legacy baseline |
| `operator_api_denied_total{scope_outcome}` | counter | `refused` vs `errored` kept apart, matching `SIDE_OUTCOMES` |
| `operator_api_page_truncated_total{route}` | counter | one-sided truncation is `pagination_divergence`, so it must be visible before a comparison |
| `operator_api_cursor_rejected_total{reason}` | counter | expired / foreign / unstable |
| `operator_api_unknown_liveness` | gauge | nodes the API could not evaluate; an `unknown` count that is not a metric becomes a silently green dashboard |
| `operator_api_audit_writes_total` | counter | must track requests; a divergence means reads happened unaudited |
| `operator_api_bulk_read_total{tenant}` | counter | governance §9 bulk-read alerting |
| `operator_api_retention_excluded_total{class}` | counter | rows excluded by horizon, kept out of the coverage denominator |

Two inherited rules that matter more than the list:

- **Alert on implausible zeros, not only on spikes.** A surface that silently stopped
  produces a perfect dashboard.
- **No alert on legacy read health.** Legacy is authoritative and unchanged; a new page
  against an unchanged path is a failure mode introduced by the act of observing it. A
  legacy surface that cannot answer routes to the *comparator's* owner.

Tracing: `X-Request-ID` caller-supplied or server-generated, logged with tenant and resource
ids and latency. The query fingerprint (§7) is the natural correlation key between a request
log, an audit record and a Phase 5 receipt — and it is safe to log, because it is a hash of
a normalised question, not of an answer.

---

## 14. Rollout, rollback, gates and tests

### 14.1 What can be built now, and what cannot

**Buildable now — constrains a shape, asserts no fact about migrated data:**

| # | Artifact | Why it is safe |
|---|---|---|
| B1 | This audit | design only |
| B2 | A *staged* `hear.operatorquery.v1` normal form + fingerprint, generated from a stdlib-only module | it is the same normal form `normalise_query()` already implements; publishing it lets a future client and the comparator agree by construction |
| B3 | A *staged* list-envelope + cursor-property schema (`hear.operatorpage.v1`) | `{items,next_cursor,has_more}` plus the six §5.1 rules; no row content |
| B4 | A *staged* Problem Details profile with the §5.2 reserved `code` vocabulary | error codes are a shape |
| B5 | A read **projection spec per surface** (P6-R1 exit gate) | required before any comparison can be classified |
| B6 | A dated **expected-difference list** for R3/R11/R14 (P6-R4 exit gate) | a list of known defects, not a claim about data |
| B7 | The **access-audit record** schema (§4.2) | depends on no migrated data |
| B8 | A scope → governance-role → DB-role mapping table (§3.2) | documentation of a boundary that already exists in DDL |
| B9 | Fault-injection **test cases** for cursor instability, skew, expired cursor, unknown liveness, threshold mismatch | fixtures, no runtime |

**Blocked, and on what:**

| # | Item | Blocked on | Why it cannot be faked |
|---|---|---|---|
| X1 | Any live route, service, deployment or credential | Phase 2 + 3 + 4 + 5 gates | a served answer is a cutover |
| X2 | Response **bodies** for `events`, `scenes`, `clips`, `tags`, `scores`, `localizations`, `annotations` | **Phase 3** import (11 of 13 evidence classes have no canonical producer) | a body is a claim about rows that do not exist |
| X3 | `GET /v1/health/nodes` canonical projection | **Phase 2** (R1–R3 fixed, 14-day window re-run from the fixed build, R16 discharged) | the liveness verdict is derived from the durable path being trustworthy |
| X4 | Clip content route and signed URLs | **Phase 3** object store + the reviewer role + annotate authorization | §3.3 |
| X5 | Any published (not staged) contract id | the Phase 4 receipt promotion, then Phase 5's | one hashed baseline, two lanes |
| X6 | Latency SLO numbers | the still-open operator p99 ingest-lag decision | §11.1 |
| X7 | Receipt/audit retention schedule | data-governance approval | §11.2 |
| X8 | A feature-flag mechanism *to be used* | Phase 5 gate | §12.1 |
| X9 | Site registry and tenant model beyond `'default'` | product decision | `site_id` is required by `normalise_query()` |
| X10 | Alert rules, incidents, saved searches, integrations | not a migration at all | §2.1 |

### 14.2 Gates for a first canonical route

Phase 6 entry is unchanged (read audit §9): Phases 2, 3, 4 and 5 gates all met. On top,
before a *first route serves a real operator answer*:

| # | Gate |
|---|---|
| A1 | Phase 5 G1–G13 met for that evidence class, on one window, per lane |
| A2 | A read projection spec exists for the surface and is the one the comparator used |
| A3 | The legacy annotate surfaces are authorized — independently, in the security lane, **before** this |
| A4 | An access-audit record is written for every read, verified by test, and its count tracks request count |
| A5 | Latency baseline captured **before** the lane was enabled; canonical p99 ≤ 2× it (G6) |
| A6 | Cursor stability, no-duplicate and no-gap properties green under fault injection |
| A7 | `unknown` liveness is rendered as neither healthy nor offline, verified by test |
| A8 | Rollback rehearsed: lane to `off` and back, with **no legacy reader change** |
| A9 | Every consumer in §9 still works, including `bridge_soak_evidence.py` untouched |
| A10 | Operator sign-off recorded against a bundle id, not a screenshot |

### 14.3 Rollback

There is nothing to roll back from this document — it reverts to a deleted file that no
runtime reads. For the staged contracts (B2–B4, B7): regeneration after a revert reproduces
the previous bytes exactly, and CI checks it. For a future first route, rollback is the
per-lane mode returned to `off` plus the Phase 2/3 rollback tables, both of which already
exist and both of which keep the legacy surface live and complete.

**Note on contract-layout hygiene.** ADR 0002 rule 6 requires decision records to be
uniquely numbered, enforced by `tools/check_contract_layout.py` in the CI job *contract
layout and frozen baseline*. Two independently merged lanes (PR #222 and PR #224) each
landed a record numbered `0006`, which is why that job is failing on `main` at the time of
this audit. This has now happened twice in this repository — the earlier occurrence was a
pair of `0002` records. A number allocated in a branch is not allocated in the repository,
and a single-maintainer repository cannot solve that with review routing. Either the
checker's guidance should recommend allocating the number at merge time, or records should
be named by date rather than sequence. Recorded here because the next contract this lane
adds will allocate a number the same way.

---

## 15. Open questions owed to an operator

Carried forward, not decided:

1. The p99 ingest-lag SLO, and with it the canonical read-latency target (ingest spec §16.1,
   Phase 5 §18.1, ADR 0005). Nothing downstream can state an absolute freshness number until
   this is answered.
2. Receipt and access-audit retention (governance approval; Phase 5 §18.2 carries the
   identical item).
3. Retention-horizon parity between legacy and canonical, or a documented approved
   difference (Phase 5 G11, §18.3).
4. Whether the annotate surfaces are authorized **before** or **as part of** read-surface
   work. This audit's position is unchanged from the read audit: **before, and
   independently**.
5. The tenant and site model. One tenant exists (`'default'`) and no site registry exists,
   yet `site_id` is a required input to the query fingerprint. Someone must decide whether
   sites are a resource or a configuration value before B2 can be generated.
6. Whether Phase 6 scope includes the *new product surface* in `api-boundaries.md` (alerts,
   incidents, saved searches, integrations, exports-as-jobs) or explicitly defers it. This
   audit's position: defer, or the cutover never terminates.
