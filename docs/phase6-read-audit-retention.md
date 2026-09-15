# Phase 6: retention and minimisation for the read-audit and receipt surfaces

- Status: **design only, and deliberately decision-shaped.** This document creates no table,
  no migration, no route, no credential and no bucket. It changes no reader, enables no API,
  and moves no frozen hash. It allocates **no ADR number** — numbering is done once, at
  merge, by whoever ratifies it (this repository has already collided on ADR numbers twice;
  see `docs/phase6-operator-api-contract.md` §13).
- Scope: the four record families Phase 6 would create or inherit that are made of *who
  looked at what* and *where two surfaces disagreed* — **access-audit records**, **read
  receipts**, the **refusal quarantine** and **mismatch receipts** — plus the aggregates,
  exports and keys that hang off them.
- Position: Phase 3 evidence includes `ambient_audio` and `precise_location`
  (`hear/objectstore/keys.py:38`). An audit trail over that evidence is not metadata; it is
  a second, searchable index into it. Phase 6 must not solve "there is no read audit"
  (`docs/phase6-operator-api-contract.md` §4.2) by creating an unbounded sensitive log.
- Companions read rather than duplicated: `docs/data-governance.md` (the policy this
  implements — classes §1, zones §2, roles §3, retention §4, keys §5, deletion/holds §6,
  custody §7, exports §8, audit §9), `docs/phase6-operator-api-contract.md` §4.2 and §11.2,
  `docs/phase5-shadow-read-comparator.md` §9/§9.1/§10 and open item 18.2,
  `docs/legacy-operator-read-boundaries.md` §6.0/§7/§9, `docs/durable-postgres-schema.md`
  §7.1/§8, `docs/decisions/0005-phase4-dual-write-observability.md`.

**What this document decides:** the shape — classification, field rules, hash boundaries,
storage and role layout, bounds, tiering mechanics, hold/erasure mechanics, export and
aggregation rules, key custody model, observability, compliance evidence, and a reversible
migration order. **What it does not decide:** any destruction *period* that governance
reserves to an operator. Every such period appears in §14 as a named choice with its
consequence, a default the design will run on if nobody chooses, and the evidence needed to
choose. A default that ships unreviewed is not a decision; a default that is written down
with its owner and its review date is a decision someone can overturn.

---

## 1. The problem, stated precisely

Three facts collide.

1. **There is no read audit anywhere in the tree.** `hear.durable_records_audit`
   (`0004:104`) and `hear.refused_messages_audit` (`0006:370`) are *ingest custody* views,
   not access records. `docs/data-governance.md` §9 requires audit events for reads,
   searches and downloads; `docs/legacy-operator-read-boundaries.md` L7 rates the absence
   medium on a tailnet-exposed service.
2. **The record that fixes it is itself sensitive.** An access-audit row says *this actor,
   at this time, retrieved these rows about this site*. Governance §1 classes it
   `identity/access` and §3 asks for it to be held separately from acoustic content. A read
   audit over clip and location evidence is a behavioural record of people *and* a
   selection index over restricted material.
3. **Its volume is driven by an adversary-adjacent input.** Reads are unbounded in
   frequency; `/api/export` is a full dump with no cursor
   (`docs/legacy-operator-read-boundaries.md` L1). The refusal quarantine already proved the
   generalisation — it accepts input that failed validation, so it is a *bounded ring*, not a
   ledger (`0006:20-28`). The same reasoning applies to anything an unauthenticated or
   semi-trusted caller can cause to be written.

So the design rule for all four families is one sentence: **an audit record is retained
because it answers a question someone will actually ask, at the smallest field set that
answers it, for the shortest period in which it will be asked, in a store that cannot be
used as a search index over the evidence it describes.**

### 1.1 Capacity is a real constraint, not a rhetorical one

`docs/object-store-backend.md` measured the truth behind `df`: `/pool`, every PVC, the k3s
datastore, the backup budget and the image store share **~52 GB of real headroom** on a
sparse VHDX. The outbox alone is ~51,840 records/day at ~639 B. An audit family with no
byte cap competes with the backup plan for that 52 GB, which means an unbounded audit log
degrades into an availability incident before it degrades into a privacy incident. Every
family below therefore carries **three** bounds — age, rows and bytes — not one.

---

## 2. The four families, and what each is for

| # | Family | Produced by | The question it exists to answer | Governance class |
|---|---|---|---|---|
| F1 | **Access audit** | the canonical operator API (and stage 0 of the legacy annotate remediation) | who read what, when, under what scope, and what did the system decide | `identity/access` |
| F2 | **Read receipts** (`match`/audit receipts, `hear.shadowread.receipt.v1`) | the shadow-read comparator, sampled | was the comparator *looking*, and is coverage computable rather than asserted | `evidence` (derived) |
| F3 | **Refusal quarantine** (`hear.refused_messages`) | ingest validation | what was rejected, by whom-ish, and why | `evidence` (untrusted input) |
| F4 | **Mismatch receipts** (terminal findings: `missing_canonical_row`, `tombstone_divergence`, `identity_divergence`, …) | the comparator, never sampled | the gate review, the deletion incident, the authentication incident | `evidence`, and the three critical classes are **incident evidence** |

They are separated because their *destruction schedules have different owners*. F1 is a
privacy liability whose value decays in weeks. F4's three critical classes are the evidence
of a governance failure and their value *increases* until the incident closes. F2 is
disposable statistics. F3 is a bounded ring whose only job is to be present when someone asks
"why is that node failing validation". Fusing them into one "audit log" with one number is
how the 1-year `R4-audit` ceiling gets applied to a location-adjacent behavioural log, or a
7-day sweep gets applied to a deletion incident. Both are wrong and both are one config line.

### 2.1 A classification defect this design surfaces

`docs/phase6-operator-api-contract.md` §11.2 lists `enforce_refusal_retention()` as the
enforcement of `R4-audit` (1 y). The function defaults to **30 days** and *raises* above
**90**, citing the `R0-derived` ceiling (`0006:257-280`). One of the two is wrong. This
design says the **code is right and the table is wrong**: the quarantine holds *rejected
telemetry bodies*, which is derived operational data of the same class as the telemetry
itself, not an audit record of an access. The correction belongs in that document's table,
not in the DDL. It is recorded here as finding **RT-1** rather than fixed here, because that
file is owned by an open lane.

---

## 3. Data classification and the per-family retention model

Classes are `docs/data-governance.md` §4. Where a family needs a horizon that class does not
supply, the model **splits the record**, rather than stretching the class.

| Family | Class | Default horizon (this design runs on it absent a decision) | Ceiling that may not be exceeded without §14 approval | Clock |
|---|---|---:|---:|---|
| F1 access audit — **hot tier**, full fields | `identity/access` → `R4-audit` | **90 d** | 1 y | the access instant (`decided_at`) |
| F1 access audit — **cold tier**, minimised | `identity/access` → `R4-audit` | **1 y** | 1 y | same |
| F1 — access to a **restricted class** (`clip` bytes, `raw`, coordinates) | `identity/access`, hold-eligible | **1 y** (never tiered below actor-pseudonym + object id) | 1 y, extendable only by a hold | same |
| F2 read receipts (match/audit) | derived evidence | **14 d**, or the close of the gate window that cites them, whichever is later | 90 d (`R0-derived`) | receipt `window.as_of` |
| F3 refusal quarantine | derived evidence | **30 d** *and* 5,000 rows *and* 16 MiB, whichever binds first (`0006`) | 90 d | `received_at` |
| F4 mismatch receipts — warn classes | derived evidence | **90 d** | 90 d | `window.as_of` |
| F4 mismatch receipts — the nine cutover-blocking classes | evidence | **until the gate decision they inform is recorded, + 90 d** | — (hold-governed) | `window.as_of` |
| F4 — `tombstone_divergence`, `identity_divergence`, `missing_canonical_row` | **incident evidence** → `R3-evidence` | **until the incident is closed and released by the custodian** | — | `window.as_of` |
| Derived aggregates (§9) | `telemetry` | indefinite **only** if they satisfy §9's non-reidentification rules | — | bucket end |

Three rules bind the table together:

1. **Retention clocks run on the event's own time, not on write time.** Governance §4 says
   capture time for content; the analogue for an access record is the instant of the access,
   which is also the only value a restore can preserve. A restore is an audited event and
   never resets a clock (§6 of governance, restated in §7 below).
2. **A record may move to a shorter-lived or more minimised state, never to a longer one.**
   Tiering is a one-way lattice. There is no code path that lengthens a horizon; extension is
   a **hold** (§7), which is a separate object with its own release authority.
3. **The blocking-class exception is scoped to the finding, not to the family.** "Keep it
   until the gate closes" applied to every receipt is how an evidence store becomes a lake.
   `shadow_read` already separates gate impact from repair action; the retention model
   reuses that split verbatim rather than inventing a second one.

---

## 4. The access-audit record: fields, and the fields that must never exist

`docs/phase6-operator-api-contract.md` §4.2 fixes the obligation — written before the
response is returned, carrying actor, tenant, scope, query fingerprint, result count, classes
touched and decision, *never* the result. This section makes that a field list with a
retention tier per field, because "never carry the result" is not enforceable against a
field nobody classified.

### 4.1 Retained fields

| Field | Form | Hot (0 – 90 d) | Cold (90 d – 1 y) | Why it is retained at all |
|---|---|---|---|---|
| `audit_id` | uuid5 over (contract ⋮ request_id ⋮ decided_at) — deterministic, as receipts already are | keep | keep | idempotent write; a retried request does not double-count a read |
| `decided_at` | RFC 3339, UTC | keep | truncate to the **hour** | the retention clock and the incident timeline |
| `tenant_id` | as stored everywhere else | keep | keep | governance §3 mandates it on every audit event |
| `actor_ref` | `HMAC-SHA256(K_actor_pseudonym[epoch], principal)`, 128-bit prefix, prefixed with the key epoch | keep | keep | correlation without a name (§5) |
| `actor_name` | the raw principal | keep | **dropped** | the only field that can name a person; it is the first thing to go |
| `actor_role` | the governance role in force (§3 of governance) | keep | keep | "did a reviewer do this, or an administrator" survives pseudonymisation |
| `auth_method`, `grant_ref` | enum; the id of the time-bounded grant, if any | keep | keep | a cross-tenant or custodian read must be attributable to its grant |
| `route`, `operation` | the contracted operation id, not a raw path | keep | keep | raw paths carry ids and filter values |
| `query_fingerprint` | the comparator's fingerprint (§7 of the API contract), **keyed** (§5) | keep | keep | groups repeat reads without storing the query |
| `evidence_classes` | the closed class set touched | keep | keep | the only way to answer "who has seen restricted material" |
| `object_refs` | ids/keys of restricted objects actually returned, **capped at 32** with `refs_dropped` | keep (restricted only) | keep | a clip disclosure must be enumerable during an incident |
| `decision` | `allowed` / `refused` / `errored` / `partial`, with the reserved problem `code` | keep | keep | refusals are the security signal, not the noise |
| `rows_returned`, `bytes_returned`, `truncated`, `cursor_present` | integers/booleans | keep | keep | bulk-read alerting (governance §9) |
| `bulk_read` | boolean, set above the declared threshold | keep | keep | the alert predicate |
| `duration_ms`, `read_contract`, `projection_version`, `as_of` | scalars | keep | drop all but `read_contract` | comparability and mixed-version debugging |
| `client_net_ref` | `HMAC(K_actor_pseudonym[epoch], client_ip)`; the raw IP is retained **only** for 7 d in the hot tier and only if §14-D5 chooses to retain it at all | 7 d then drop | absent | source attribution during an active incident, nothing else |
| `request_id` | correlation id, echoed to the caller | keep | drop | joins an audit row to a log line during triage |
| `hold_ref`, `retention_class`, `expires_at` | the record's own governance state | keep | keep | a record that cannot state its own horizon cannot be reconciled |

`expires_at` is **stored, not computed at read time**. A horizon that exists only in a cron
argument is a horizon nobody can audit and one flag can silently extend.

### 4.2 Fields that must never be retained, anywhere in F1–F4

This is a deny list and it is enforced the way the repository already enforces this class of
rule: by pattern, with unknown paths defaulting to hidden
(`hear/ingest/reconcile.py:228`, `hear/verify/shadow_read.py:333`, §9.1 of the Phase 5 spec).

| Never retained | Because |
|---|---|
| Clip or raw **audio bytes**, any transform of them (spectrogram, embedding, hash of the bytes as an index key) | governance §4: raw audio is never copied into logs, crash reports or message payloads. A digest of a clip stored next to its access record makes the audit store a content index over restricted media |
| **Coordinates** in any form — value, rounded value, geohash, or digest | `tools/coord_guard.py`, and the Phase 5 rule verbatim: *"not even a hash — a stable digest of a coordinate is still a stable identifier for that coordinate, and a receipt store full of them is a location database with extra steps"* (§9.1). The `blind` comparator exists for this |
| **Free-text annotation `notes`** | `docs/legacy-operator-read-boundaries.md` §7: never logged, never in an audit record, never in an error body. Operator-entered free text is unclassifiable by construction |
| **Label text and label rows** | the audit record indexes a read; it is not a second copy of the labels |
| **Tokens, bearer credentials, secrets, DSNs, `path`** | `api-boundaries.md`; `docs/durable-postgres-schema.md` §7.1 (the DSN carries a password and must never reach `/healthz` or a log line) |
| **Raw filter values** from the query string | a query is user input: `?near_latitude=…` reintroduces a coordinate through the one field nobody classified (Phase 5 §9.1) |
| **Result bodies**, row payloads, or any `payload`/`body`/`raw` path | §4.2 of the API contract: the record carries the *count*, never the result |
| **A second copy of the tenant's content under an audit name** | governance §3: audit is separated from acoustic content *where practical*; this design says here it is practical, so it is required |

An unrecognised field path is **not writable to an audit record**. Adding a field to the
audit contract is a contract change with a test, exactly as adding a quotable path to the
reconciler is today.

### 4.3 Write-path rule and the fail-open/fail-closed question

The record is written **before** the response is returned, in the same transaction boundary
as the authorization decision where one exists. Two consequences:

- `operator_api_audit_writes_total` must track request count; a divergence means reads
  happened unaudited (this metric is already named in the API contract, §9 of it).
- If the audit store is unavailable, the API either refuses the read (**fail-closed**) or
  serves it and increments an unaudited-read counter (**fail-open**). This is a genuine
  operator choice — §14-D6 — and the design's position is: **fail-closed for restricted
  classes, fail-open with a paging alert for everything else.** Failing closed on a health
  dashboard turns an audit outage into an operational outage; failing open on clip audio
  turns it into an undetectable disclosure.

---

## 5. Hash and pseudonym boundaries

A hash is a minimisation control *only* when the pre-image space is large and the key is
held apart from the data. This deployment has "a handful of human users"
(`docs/legacy-operator-read-boundaries.md` §9), which makes an unkeyed hash of a reviewer id
reversible by enumeration in milliseconds. The boundary is therefore drawn by *purpose*, and
there are exactly four treatments.

| Treatment | Used for | Construction | Property required |
|---|---|---|---|
| **Plain digest** | content identity that is already public inside the tenant: `payload_sha256`, segment digests, receipt digests | `sha256` over canonical JSON (`hear/objectstore/keys.py`) | must be reproducible by a third party checking evidence |
| **Keyed pseudonym** | actor identity, client network identity, query fingerprint | `HMAC-SHA256(K[epoch], value)`, truncated to 128 bits, **epoch-prefixed** | stable inside an epoch, unlinkable across epochs, not enumerable without the key |
| **Restricted blob id** | `ambient_audio` / `precise_location` object references | `HMAC-SHA256(K_tenant_index, plaintext_digest)` (`hear/objectstore/keys.py:88`) | per tenant; dedupe works inside a tenant and deliberately does not work across tenants |
| **Blind** | coordinates, credentials, clip bodies | compared in memory; **only the verdict is durable**, both values `null`, `blind: true` | no durable representation of the value exists at all — not a value, not a hash |

Rules that follow, and that the audit contract must encode:

1. **An unkeyed hash of a low-cardinality identifier is prohibited.** Reviewer ids, device
   ids used as person-proxies and client IPs are all low-cardinality here. Where such a
   value must be correlatable, it is a keyed pseudonym; where it must not, it is blind.
2. **Every pseudonym carries its key epoch in-band** (`a1:` prefix). Without it, a rotation
   silently produces two populations that look like one, and the *only* fix after the fact is
   to keep the old key forever — which is the opposite of what rotation is for.
3. **Rotation is a deliberate unlinkability event.** Rotating `K_actor_pseudonym` makes
   pre-rotation and post-rotation rows uncorrelatable *by design*. §14-D7 chooses the period;
   the design default is **annual, aligned to the F1 cold horizon**, so the key needed to
   correlate the oldest live rows is retired at the same moment those rows are.
4. **Losing a pseudonym key is not a deletion event** unless the operator verifies no usable
   replica remains (governance §5). It *is* a correlatability loss, and the audit store must
   keep functioning: rows stay readable, they simply stop joining across the epoch boundary.
5. **The query fingerprint is keyed, not plain.** An unkeyed fingerprint over a query
   containing a site, a device and a window is a dictionary attack away from being the query
   itself.
6. **No hash may be used as a lookup key into restricted content from the audit store.** The
   audit store may name an object (`object_refs`) but must hold no key material and no
   capability to fetch it; obtaining bytes is a separate authorization and a separate audit
   record (API contract §8).

---

## 6. Retention mechanics: tiering, bounds, and what "expired" means

### 6.1 Three bounds, evaluated independently

Every family is bounded by **age**, **rows** and **bytes**, per tenant. The precedent is
`hear.enforce_refusal_bounds()` (`0006`), whose eviction always takes the oldest row of
whichever `(source, device_id)` holds the most, so a flooding publisher erases its own
history first. F1 inherits that shape with the grouping key `(actor_ref, operation)`: a
single runaway client cannot evict another actor's records, which is precisely the attack an
unbounded-but-capped audit log invites.

Byte bounds are not a nicety. §1.1 is the reason: the store shares ~52 GB with the backup
budget.

### 6.2 Tiering is field-level minimisation, not a move to cheap disk

At the hot→cold boundary a row is **rewritten in place with fields removed** (§4.1), not
archived. The transition is itself an audited administrative action and is irreversible.
Three properties are required:

- the minimisation is **append-only-compatible**: it is expressed as a versioned
  `minimisation_applied` marker plus a nulling of the named columns, so an auditor can see
  *that* a row was minimised and *by which policy version*, and cannot be fooled into reading
  a minimised row as a complete one;
- a row **under hold is never minimised** (§7);
- minimisation runs **dry-run by default**, as both existing retention functions do
  (`0003`, `0006`), so the destructive form is always something someone typed.

### 6.3 Expiry is a state, not a 404

Reused verbatim from the comparator's `ROW_STATES`: a pruned row is `retention_expired`, and
an API that cannot distinguish it from "never existed" makes loss indistinguishable from
policy. The audit store must be able to answer *"there was a record here and it aged out
under policy P at time T"* without retaining the record — which it does by retaining the
**per-(tenant, day, family) coverage counter** described in §9, never a tombstone per row.

### 6.4 Failure is quarantined, never silently retained

Governance §4: a failed deletion, unknown timestamp or unresolved tenant goes to a
**quarantined exception queue**, not into indefinite silent retention. The exception queue is
itself bounded and alerted, and a row in it is excluded from the coverage denominator so its
existence cannot be hidden by a healthy-looking ratio.

---

## 7. Holds and erasure

### 7.1 Holds

A hold is a first-class object, per governance §6: issuer, scope, authority, start time,
review date, release authority. Applied to this design:

- a hold names a **scope expression** over the audit/receipt families — e.g. `(tenant,
  window, evidence_class, object_ref)` — never "all audit";
- **new records matching a live hold inherit it at write time**, which is why `hold_ref` is a
  stored field and not a join against a policy table evaluated at prune time;
- holds **suspend deletion and minimisation**; on release, the original clock resumes — a
  hold is never a retention extension;
- a hold on *content* implies a hold on the F1 records that reference it. The converse is not
  true. This is the only automatic propagation in the model, and it exists because releasing
  clip evidence while destroying the record of who accessed it defeats the custody chain
  (governance §7).

### 7.2 Erasure

Erasure of *content* is already specified (governance §6). What is new here is erasure that
touches the audit trail itself, and it is the one place where two policies genuinely conflict:
minimisation says destroy, integrity says an audit trail is append-only.

The resolution is structural, and it is the design's core proposal:

1. **The audit record holds no personal content**, only references and a keyed actor
   pseudonym (§4). Erasing the *subject* content therefore does not require erasing the audit
   row — the row's remaining payload is counts, classes and decisions.
2. **Erasing an actor** (a departed reviewer exercising a rights request, say) is executed as
   **epoch retirement plus key destruction for that actor's pseudonym mapping**, not as a
   DELETE across the ledger. The rows remain, correct and countable; the link to the person
   is destroyed because the key that made it is destroyed. This is the only erasure mechanism
   that is compatible with an append-only store.
3. **Where a DELETE is genuinely required**, it is performed by the administrator role only,
   under a recorded deletion request (tenant, scope, reason, requester, approver, deadline),
   and it emits a **signed deletion receipt** and increments a per-(tenant, day, family)
   erasure counter. The counter is what makes the gap in the ledger *explained* rather than
   suspicious.
4. **A deletion receipt is not itself deletable** under the same request, and it carries no
   field that the deletion removed. A receipt naming what it erased re-creates the record.
5. **A periodic reconciler finds orphaned copies** across indexes, caches, exports and
   backups (governance §6). Backups are the hard case and this design does not pretend
   otherwise: see §14-D8 — crypto-shredding via per-tenant-per-class keys is the only
   mechanism in this repository that makes backup erasure tractable, and the encryption it
   depends on **does not exist yet** (`docs/object-store-backend.md` §126).

### 7.3 What erasure must never do

Reuse or delete a **key epoch identifier**, renumber `audit_id`s, rewrite counters, or
"repair" a conservation equation so the books balance. A gap with a receipt is evidence; a
balanced ledger with a rewritten past is not.

---

## 8. Storage, isolation, roles and RLS

### 8.1 A separate schema, not a table in `hear`

Governance §3 asks that identity/access data be separated from acoustic content where
practical. Concretely: **`hear_audit`**, a schema of its own, with its own roles, its own
grants and its own backup namespace, reachable from the API by a **write-only** role.

| Role | May | May not |
|---|---|---|
| `hear_audit_writer` (the API) | `INSERT` only, on the F1 table | `SELECT`, `UPDATE`, `DELETE` — a service that can read its own audit trail can shape it |
| `hear_audit_reader` (auditor, per governance §3) | `SELECT` on the minimised view | see `actor_name`, `client_ip`, or any restricted `object_ref` without an explicit grant |
| `hear_audit_custodian` (evidence custodian) | `SELECT` unminimised under a recorded grant; issue/release holds | `DELETE` |
| `hear_audit_admin` | run retention, minimisation and recorded deletions | `INSERT` (so retention cannot forge evidence) |

This is the same posture `0004` already takes for the outbox — deletion confined to the
administrator, so a compromised or buggy writer cannot erase the ledger it writes to — with
one addition: **the writer cannot read**. The outbox writer needs to read; an audit writer
never does.

### 8.2 RLS and views

Row-level security on every table with the `tenant_isolation` policy driven by the
`hear.tenant_id` session setting, and every view `security_invoker` so RLS applies through it
(`0004:62,82-85`). A caller who forgets a `WHERE` clause still cannot read another tenant's
rows. Two audit-specific views:

- `hear_audit.access_records_minimised` — the cold field set, granted to the auditor role,
  which is what a routine review reads;
- `hear_audit.access_records_restricted` — unminimised, granted to the custodian role only,
  and **every SELECT against it is itself an F1 record**. Auditing access to the audit log is
  not paranoia; governance §5 already requires key access to be audited separately from
  object access, and this is the same argument.

### 8.3 Append-only enforcement

Enforced three ways because one is a config line: no `UPDATE`/`DELETE` grant to any role but
admin; a trigger that rejects `UPDATE` except the single `minimisation_applied` transition;
and a **periodic chain digest** over `(audit_id, decided_at)` ordered runs, written to the
evidence store, so a silent out-of-band deletion is detectable after the fact. The chain
digest is cheap — it is a digest over identifiers, not content — and it is the only mechanism
here that survives an administrator who is the threat.

### 8.4 Blast-radius separation from the content store

Governance §9: audit records must be *protected from the same failure that affects the
content*. Whether `hear_audit` is a separate database, a separate instance, or a separate
schema in the same instance is §14-D9 — with the stated consequence that a separate schema
shares one `PITR` timeline and one disk, and that the phase-3 capacity finding (§1.1) makes a
second instance a real cost on this hardware, not a free "best practice".

---

## 9. Export and aggregation

### 9.1 Export

An export **of audit data** is an export like any other and carries the manifest governance
§8 requires: schema version, export id, tenant, purpose, requester, approver, object ids,
intervals, classes, redaction versions, hashes, hold state, key references, and the list of
excluded objects with reasons. Three audit-specific additions:

1. An audit export is **minimised by default** — the cold field set — and the unminimised
   form requires the custodian role and a recorded purpose.
2. An audit export is **time-limited and destruction-confirmed**, and the export itself
   generates an F1 record. An export of the access log that leaves no access record is a
   copy of the whole liability with no trail.
3. An export **may not be re-derived into a search index** over content: the manifest states
   the permitted use, and the recipient's destruction confirmation is recorded.

### 9.2 Aggregation is the mechanism that lets retention be short

The reason a 90-day hot horizon is defensible is that the *operational* questions — is usage
growing, is anyone bulk-reading, is the refusal rate rising — are answered by aggregates that
outlive the rows. Aggregates are `telemetry` class and may be retained indefinitely **only
if** all four hold:

- the bucket is `(tenant, day, operation, evidence_class, decision)` or coarser, **never**
  keyed by object, and never by raw actor;
- an actor dimension, where present, is the **keyed pseudonym**, and the bucket is suppressed
  below a minimum cell count (§14-D10 chooses it; the design default is **k = 5**, and with a
  handful of reviewers this will suppress most actor-level cells, which is the correct
  outcome rather than a bug);
- no free-text, no object id, no coordinate, no query value ever enters an aggregate;
- the aggregate is **computed forward from rows that still exist** and is never
  back-filled from a restore — otherwise a restore silently resurrects a minimised
  population.

Additionally, the **per-(tenant, day, family) coverage and erasure counters** of §6.3/§7.2
are retained with the aggregates, because they are what make an aged-out or erased period
*explained* rather than merely empty.

---

## 10. Key custody

Four key roles, and they are not interchangeable.

| Key | Purpose | Custody | Rotation | Loss means |
|---|---|---|---|---|
| `K_actor_pseudonym[epoch]` | F1 actor and network pseudonyms; the annotate export pseudonym already specified in `docs/legacy-operator-read-boundaries.md` §7 | outside the audit database; the audit DB role must not be able to read it | annual, aligned to the F1 cold horizon (§5) | historic rows stop correlating; **this is also the erasure mechanism** (§7.2) |
| `K_query_fingerprint` | keyed query fingerprints | as above, may be the same custody domain, **never the same key** | with the actor key | fingerprints stop grouping; no content is lost |
| `K_tenant_index` | restricted blob ids (`hear/objectstore/keys.py:88`) | per tenant, Phase 3 key design | per the Phase 3 design, not here | dedupe and object addressing break; this key is *not* an audit key and must not be reused as one |
| Tenant/class KEK | content encryption, the precondition for crypto-shredding backups | Phase 3 | Phase 3 | **does not exist yet** — §14-D8 depends on it |

Custody rules: keys live outside application data (governance §5); **key access is audited
separately from object access**; no key is ever written into a receipt, a manifest, a metric
label or a log line; and the audit store holds key *epoch identifiers* only. Reusing
`K_tenant_index` as the actor pseudonym key would make a reviewer's pseudonym and a
restricted object's id derivable from one secret — that is the single worst reachable mistake
in this design, and it is named here so it is refusable in review.

---

## 11. Observability

Metrics, with labels restricted to `tenant`, `family`, `operation`, `evidence_class`,
`decision`, `severity` — **never** an actor, a token, a filter value or a coordinate
(Phase 5 §11.1's rule, inherited).

| Metric | Type | Use |
|---|---|---|
| `audit_records_written_total{family,decision}` | counter | must track request count; divergence = unaudited reads |
| `audit_unaudited_reads_total` | counter | fail-open events (§4.3); a non-zero value is a paging condition on restricted classes |
| `audit_store_rows{family}` / `audit_store_bytes{family}` | gauge | the three-bound enforcement, live, against §1.1 |
| `audit_retention_runs_total{family,action}` | counter | `would_delete` vs `deleted` vs `skipped`, so a dry-run-forever cron is visible |
| `audit_minimised_total{family,policy_version}` | counter | tiering actually happened |
| `audit_expired_total{family}` / `audit_evicted_total{family,reason}` | counter | age-based vs bound-based destruction, kept apart — eviction under bound pressure is a capacity alert, not a retention success |
| `audit_exception_queue_depth` | gauge | §6.4; alert on any sustained non-zero |
| `audit_holds_active{family}` | gauge | a hold that outlives its review date is the failure governance §6 warns about |
| `audit_erasures_total{family,reason}` | counter | the explained-gap counter |
| `audit_bulk_reads_total{operation}` | counter | the governance §9 alert predicate |
| `audit_chain_digest_age_seconds` | gauge | §8.3; alert on **age**, not value |
| `audit_key_epoch_age_days{key}` | gauge | rotation is due, or overdue |

Alerts, following the Phase 5 discipline of alerting on **implausible zeros** as well as
spikes: writes-to-requests divergence (page); any unaudited restricted read (page); exception
queue non-empty for two runs (page); `audit_records_written_total == 0` while the API served
reads (page); bytes above 80 % of the bound (ticket); eviction under bound pressure (ticket);
hold past its review date (ticket); key epoch past rotation (ticket).

None of these may reproduce content: a metric label is the most commonly forgotten exfil
path in this whole design, which is why the label set is closed above.

---

## 12. Evidence that the policy is being followed

A retention policy with no evidence is a paragraph. Each item below is producible by this
repository's existing machinery — generators, checkers, CI jobs and receipts — not by a new
compliance system.

| # | Evidence | Produced by | Proves |
|---|---|---|---|
| E1 | The **field contract** for F1, generated from one source module, with fixtures | the pattern `tools/gen_shadow_read_contracts.py` already establishes | the field list is a contract, not a docstring |
| E2 | A test that the deny list (§4.2) rejects notes, bytes, coordinates, tokens and unknown paths, including *newly added* upstream fields | test, mirroring `test_shadow_read_receipt_v1.py` | unknown-defaults-hidden actually holds |
| E3 | A test that the writer role has no `SELECT`, and admin has no `INSERT` | the file-level DDL guards `tests/test_hear_durable_pg_schema.py` already uses | §8.1 is real, not aspirational |
| E4 | A **dry-run retention report** per family per run: would_delete counts, bounds headroom, exception queue | the `(action, row_count, reason)` return shape `enforce_retention()` already uses | destruction is predicted before it happens |
| E5 | `audit_records_written_total` vs API request count, per day | metrics | no unaudited reads (gate A4 of the API contract) |
| E6 | Chain digest continuity over the ledger | §8.3 | no out-of-band deletion |
| E7 | Hold register with issuer, scope, authority, review date, release | §7.1 | no hold has become a silent retention extension |
| E8 | Signed deletion receipts + erasure counters, reconciled against gaps | §7.2 | every gap is explained |
| E9 | Key epoch register and rotation record | §10 | pseudonyms are unlinkable across epochs as claimed |
| E10 | An annual **minimisation review**: sampled rows at each tier compared against the contract | operator process | the tiering is producing the field set it claims |
| E11 | The aggregate suppression check (k-threshold) run against live buckets | §9.2 | aggregates are not a re-identification path |
| E12 | A restore drill that preserves `decided_at`, tenant, hold state and clocks, and resets nothing | governance §6 | restore is an audited event, not a clock reset |

Gates, in the style the other Phase 5/6 documents use — all must hold before F1 is enabled in
`enforce` mode anywhere:

| Gate | Condition |
|---|---|
| **RA1** | The F1 field contract exists, is generated, and is drift-gated in CI |
| **RA2** | E2 and E3 pass on the build that would be deployed |
| **RA3** | Three bounds configured per family, with headroom measured against §1.1's real 52 GB, not `df` |
| **RA4** | Retention and minimisation jobs scheduled, dry-run-first, with E4 reports retained |
| **RA5** | Hold and deletion-receipt mechanisms exist before the first restricted-class read is served |
| **RA6** | Pseudonym key custody is outside the audit database, with a rotation record |
| **RA7** | Every §14 decision is recorded — chosen or explicitly deferred with an owner and a review date |
| **RA8** | One erasure drill and one restore drill executed, evidenced by E8 and E12 |

---

## 13. Migration and rollback

Additive at every step; nothing below alters an existing table, reader or contract.

| Step | Action | Reversal |
|---|---|---|
| S0 | **This document.** No object created | delete the file |
| S1 | Generated F1 field contract + deny-list tests, importable by nothing at runtime (asserted by test, as `hear/verify/` already is) | revert |
| S2 | Migration `0007_hear_audit_baseline.sql` — **new schema** `hear_audit`, F1 table, RLS, four roles, two views. Applied to an empty database; **no writer configured** | `rollback/0007_…_down.sql` drops the schema; nothing else references it |
| S3 | Migration `0008_hear_audit_retention.sql` — bounds, minimisation, retention, exception queue, counters, chain digest. Dry-run by default | drop the functions; the table remains inert |
| S4 | Hold/erasure objects + signed deletion receipts | as S3 |
| S5 | The API (when it exists) writes F1 in `log` mode, fail-open, restricted classes not yet served | one env var |
| S6 | Enable bounds + retention in enforcing mode after ≥ 30 d of dry-run reports | back to dry-run; no data is recoverable *after* a real prune, which is why S6 follows 30 days of predictions |
| S7 | Restricted-class reads served, fail-closed (§4.3), only after RA1–RA8 | withdraw the scope; the records already written stay |

Two irreversibility notes stated plainly, because a rollback table that implies otherwise is
worse than none:

- **Minimisation and pruning are not reversible.** S6 is the point of no return for the
  affected rows; the 30-day dry-run window before it exists for exactly that reason.
- **Key destruction is not reversible**, and that is the point (§7.2). A rollback plan that
  quietly retains a retired epoch key defeats the erasure it was used to perform.

Migration numbers `0007`/`0008` are **provisional**: `deploy/postgres/migrations/` currently
ends at `0006`, and whoever lands first takes the number. The ADR-collision precedent (§13 of
the API contract) applies to migrations too; the sequence is allocated at merge, not at
design time.

---

## 14. The decisions this design does not make

Each is a parameter, a scheduled job argument or a policy record — not a redesign. Each has a
default this design runs on, so nothing is silently undefined, and each names the evidence
that should decide it.

| # | Decision | Default if unchosen | What it costs to get wrong | Evidence needed |
|---|---|---|---|---|
| **D1** | **F1 hot horizon** (full fields) | 90 d | too long: a behavioural record of named people over restricted evidence. Too short: incident investigation loses its source data | one week of stage-0 logs (`legacy-operator-read-boundaries.md` §6.0) gives real read frequency and export size |
| **D2** | **F1 cold horizon** (minimised) | 1 y, the `R4-audit` ceiling | above the ceiling requires written purpose, approver and review date | the operator's own obligation set |
| **D3** | **Mismatch-receipt destruction schedule** for the three critical classes — the item carried open by ADR 0005 and Phase 5 §18.2 | until incident close + custodian release | a deletion incident whose evidence expired mid-investigation | expected incident duration; the gate-window length (14 d) is a floor, not an answer |
| **D4** | **Refusal quarantine class**: `R0-derived` (as coded) or `R4-audit` (as the API contract's table says) — finding **RT-1**, §2.1 | `R0-derived`, 30 d, as the DDL enforces | a 1-year retention of arbitrary attacker-supplied bodies | whether refusals are ever used as security evidence after 30 d |
| **D5** | **Raw client IP**: retain 7 d, or never retain | retain 7 d, hot tier only | never retaining removes the only source-attribution signal during an active incident; retaining makes F1 a network-behaviour log | site/network policy, and whether the tailnet already records this outside the repo |
| **D6** | **Fail-open vs fail-closed** on audit-store unavailability (§4.3) | closed for restricted classes, open + page for the rest | closed everywhere = an audit outage is an outage; open everywhere = undetectable disclosure | the availability requirement of the health surfaces |
| **D7** | **Pseudonym key rotation period** | annual, aligned to D2 | too fast: ordinary investigations stop correlating. Too slow: one key compromise de-anonymises the whole history | investigation lookback needs |
| **D8** | **Backup erasure strategy** — crypto-shredding vs backup expiry vs documented non-erasure of backups | documented non-erasure, with the expiry period stated, **because the encryption crypto-shredding requires does not exist** (`object-store-backend.md` §126) | claiming erasure that backups silently defeat | Phase 3 encryption landing; backup retention period |
| **D9** | **Audit store blast-radius separation**: separate instance / separate database / separate schema (§8.4) | separate **schema**, given the ~52 GB real headroom | shared failure domain contradicts governance §9 | the capacity and backup decisions already open in Phase 2 O1 |
| **D10** | **Aggregate suppression threshold** k (§9.2) | 5 | too low re-identifies with a handful of reviewers; too high makes per-actor review impossible | actual number of distinct actors |
| **D11** | **Bulk-read threshold** (rows/bytes that set `bulk_read`) | to be set from D1's week of logs; **not guessed here** | a threshold above real usage never fires; below it, the alert is noise | the same stage-0 evidence as D1 |
| **D12** | **Who may read the audit log** — auditor role only, or operator too (governance §3 names `auditor` explicitly) | auditor + custodian only | a read audit readable by everyone it observes is not an audit | the role assignment the deployment actually has |

Deliberately **not** decided, and not defaulted: any question of applicable law. This
document maps controls; it does not assert a legal basis, a lawful-basis category, a
data-subject-rights process or a jurisdiction. `docs/data-governance.md` is explicit that
these are operational defaults and not legal advice, and this design inherits that limit
rather than quietly exceeding it.

---

## 15. What this document is not

- Not an API. No route, no OpenAPI document, no client, no credential.
- Not a schema change. No migration file is added; `0007`/`0008` above are proposals with
  provisional numbers.
- Not a policy ratification. §14 is the list of things an operator still owes, and RA7 makes
  recording them a gate.
- Not a replacement for the Phase 5 comparator's own privacy rules. §9.1 of that document
  remains the source of truth for receipt redaction; this document adds *retention* to it and
  contradicts none of it.
- Not a claim that any of this is deployed. `READ_AUTHORITY` is still `legacy`, no reader has
  moved, and no Phase 2–5 gate is consumed or advanced by anything written here.
