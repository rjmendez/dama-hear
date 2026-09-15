# Phase 7 prerequisite: Redis lifecycle and consumer evidence (design only)

Status: **design only, authorizes nothing.** Written 2026-09-15 against `main` from a fresh
clone, by reading the repository and the already-published read-only measurements in
[phase7-retirement-inventory.md](phase7-retirement-inventory.md). No live command was issued
for this document: no key was read, written, expired or deleted; no `CONFIG SET`, no
`CLIENT KILL`, no keyspace notification was enabled; no deployment, manifest, image, cloud
resource or credential was inspected, rotated or changed.

This document does **not** retire, replace or deprecate anything. It defines the *evidence*
that would have to exist, and the *lifecycle* that would have to be run, before anyone may
propose that hear stops relying on `dama:hear:*` in the shared `infra/audit-redis`. It is the
prerequisite the inventory names in its §6 "Unknowns that must be closed before any removal":

> no `CLIENT LIST`/keyspace-notification census of who actually touches `dama:hear:*` was
> taken; no audit of operator scripts outside the repository; no confirmation that the
> AWS-side Lambda/SQS resources have no second subscriber.

Phase 7 remains blocked behind Phases 2–6 ([standalone-migration.md](standalone-migration.md)
§"Staged migration"). Nothing here shortens that ordering; the census lane below is the only
part that can run *early*, because it is read-only and touches no read path.

---

## 1. What is actually at stake

From the inventory's live measurement (2026-09-15), restated because every rule below depends
on it:

| Fact | Consequence for this plan |
|---|---|
| `infra/audit-redis` is **shared**: 43 260 keys in `db0`, of which `dama:hear:*` is **10**; dominant tenant `dama:consensus:*` | hear is a minority tenant. Every hear-side action must be prefix-scoped, and no hear-side action may change instance-wide configuration |
| `maxmemory-policy=allkeys-lru` | `dama:hear:*` is **evictable regardless of TTL**. "The key is present" is not a durability statement, and never was |
| `dama:hear:devices` (set) and `dama:hear:event:{device_id}` (string) carry **TTL `-1`** | two unbounded growth surfaces whose only bound today is another tenant's LRU pressure |
| `dama:hear:events` is a stream with `maxlen 1024`, observed `XLEN=305` | already lossy; it can never be conservation evidence for anything |
| `dama:hear:{device_id}` has TTL 30 s and its expiry **is** the liveness answer | absence is a value here. Any replacement must express expiry explicitly, per P6-R2 |
| Single `--requirepass`, no per-tenant ACL | hear cannot be *technically* prevented from touching foreign keys, so the prefix discipline must be enforced in tooling and review |
| Writers: `tools/hear_heartbeat_receiver.py:920-922`, `:936-937`, `tools/hear_mqtt_bridge.py` | two writers, both hear-owned. The *write* side is knowable from the tree; the *read* side is not |

The asymmetry in the last row is the whole problem. Writers are in-tree and auditable.
Readers may be a `redis-cli` in someone's shell history, a dashboard, a cron on a laptop, or
an AWS-side subscriber nobody in this repository can see. **A retirement proof is a proof
about readers**, and hear currently has none.

## 2. Non-negotiable safety envelope

Everything defined in this document is constrained by these rules. They are stated first so
that any later step which appears to require breaking one is thereby refused, not negotiated.

1. **Read-only on a shared instance.** Permitted commands are exactly the allow-list already
   implemented and reviewed in `tools/bridge_soak_evidence.py:105`
   (`DBSIZE`, `INFO`, `EXISTS`, `XLEN`, `TTL`, `TYPE`, `SCARD`, `STRLEN`, `MEMORY`, `PING`,
   `XINFO`), plus `SCAN` with a `MATCH dama:hear:*` cursor and `OBJECT FREQ`/`IDLETIME`.
   `KEYS` stays excluded (O(N) on a live multi-tenant cache).
2. **No client-affecting command, ever, in this lane.** `CONFIG SET`, `CLIENT KILL`,
   `CLIENT NO-EVICT`, `CLIENT PAUSE`, `ACL SETUSER`, `SUBSCRIBE` on a pattern that requires
   enabling `notify-keyspace-events`, `MONITOR`, `DEBUG`, `FLUSHDB`/`FLUSHALL`, `SWAPDB`,
   `RENAME`, `MIGRATE`, `XDEL`, `SREM`, `DEL`, `EXPIRE`, `PERSIST`. `MONITOR` is singled out:
   it is nominally read-only and is nonetheless forbidden, because it degrades the instance
   for all eleven foreign workloads and streams other tenants' payloads to the operator.
3. **Keyspace notifications are a configuration change.** `notify-keyspace-events` is an
   instance-wide `CONFIG SET`. It is therefore **not available** to this lane and must not be
   assumed by any design below. If it is ever wanted, it is an `infra`-owned change request
   with its own review, and §3.3 gives the alternative that does not need it.
4. **`CLIENT LIST` is admin-scoped and privacy-bearing.** It returns every tenant's client
   addresses and command state, not just hear's. It is available **only** under §3.2's
   consent-and-redaction protocol, and never from an unattended job.
5. **Prefix discipline.** Every scan, count and report is `dama:hear:`-scoped. A tool that can
   emit a key name outside that prefix is a defect, and §10 makes it a test.
6. **No cloud mutation, no credential read.** The AWS boundary work in §3.4 is a request for
   evidence from its owner, executed by that owner with their own read-only role. Nothing in
   this repository acquires, stores or exercises an AWS credential.
7. **Nothing in this lane writes to a read path.** Census output is a file in an operator's
   evidence directory. No operator surface, alert or gate consumes it until §8's gates say so.

## 3. Owner and consumer census

The census answers one question: **who reads `dama:hear:*`, from where, how often, and who
owns them?** It has four independent methods because no single one is sufficient, and the
inventory's negative result ("only two workloads reference the prefix in a spec") is
absence-of-evidence, not evidence-of-absence.

### 3.1 Method A — in-tree and manifest sweep (repeatable, offline)

Deterministic, cheap, and the only method that can run in CI.

| Source | What is swept | Why |
|---|---|---|
| This repository at a named commit | `dama:hear`, `audit-redis`, `REDIS_PASS`, `redis-cli`, `redis.Redis`, `StrictRedis`, `aioredis`, `HEAR_EVENT_STREAM_KEY` | finds in-tree readers and the env knobs that redirect them |
| Rendered `deploy/k8s/**` manifests | env, args, ConfigMap bodies, CronJob command lines | code-bearing ConfigMaps mean a reader can exist in a manifest without existing as a file |
| Every namespace's workload specs (`kubectl get -o json`, read verbs only) | same token set | catches foreign workloads; this is the sweep the inventory already ran once |
| Sibling repositories named by the inventory (dama-gotchi, oxalis/agents, infra) | same token set, per repository, by their owners | hear cannot read foreign repositories; the owner returns a signed statement instead |

Output: a dated **consumer manifest** (§5) with one row per candidate reader, each row
carrying `source_kind` ∈ {repo, configmap, workload-spec, foreign-repo-attestation}, the
commit or object generation it was read from, and the owner it is attributed to.

Known limit, recorded rather than hidden: a sweep of specs cannot see a reader that builds the
key name dynamically, reads through a generic Redis dashboard, or runs outside the cluster.
Methods B–D exist because of this limit.

### 3.2 Method B — live client census (consent-gated, redacted, attended)

Purpose: observe *connected clients* over a window long enough to catch periodic readers.

Protocol:

1. **Consent first.** `infra` owns the instance; the census is requested in writing, with the
   exact command list, the window, the redaction profile and the retention period. No run
   without a recorded approver.
2. **Sampled, not streamed.** `CLIENT LIST` at a low fixed cadence (design default: once per
   minute) for a declared window (design default: 14 days, to cover weekly and fortnightly
   jobs; a shorter window is allowed and is recorded as reduced coverage, never as absence).
3. **Redact at capture, not at publication**, reusing the receiver/soak precedent
   (`tools/bridge_soak_evidence.py` rules 3: name-based secret redaction, high-precision
   decimal blunting). The redaction profile for a client row is deny-list-first:
   - client address → keep the **network class** and a stable salted pseudonym
     (`HMAC(salt, addr)`, salt kept out of the receipt), never the raw address;
   - `name`, `lib-name`, `lib-ver`, `user` → kept, these are the attribution signal;
   - `cmd`, `argv-mem`, `age`, `idle`, `db` → kept as counters;
   - everything else, including any command argument, → dropped.
   A receipt that would contain an unredacted address is refused, not sanitized after the
   fact.
4. **Attribution is a join, not a guess.** A pseudonymous client is only ever resolved to an
   owner by matching it against Method A's manifest (pod IP class + client name + library
   version). An unmatched client is reported as **`unattributed`** — a first-class outcome
   that blocks every gate in §8 until it is closed by its owner, never by inference.

Coverage arithmetic is part of the receipt: window length, sample count, samples lost, and the
longest observed gap. A census with a four-hour hole cannot prove anything about an hourly job.

### 3.3 Method C — access telemetry without configuration change

Because §2.3 forbids keyspace notifications, read-frequency evidence comes from per-key
metadata that is already maintained by the server:

| Signal | Command | What it proves | What it does not prove |
|---|---|---|---|
| Idle time | `OBJECT IDLETIME dama:hear:<key>` | an upper bound on "time since last access" — a *small* idle time on a key hear does not write is direct evidence of a foreign reader | nothing about who, and it is reset by hear's own writes |
| Access frequency | `OBJECT FREQ` | a per-key access counter — **but it requires an LFU `maxmemory-policy`**. The measured instance is `allkeys-lru`, so this signal is *not collectable here* and the receipt records it as such. Switching the policy to obtain it is an instance-wide change affecting eleven foreign workloads and is refused | anything, on this instance |
| Keyspace hits/misses | `INFO stats` | instance-wide only | nothing hear-scoped; usable only as a denominator |
| Command mix | `INFO commandstats` | instance-wide read/write mix | shared with 43 000 foreign keys |

The honest conclusion, stated here so no later reader mistakes it: **under `allkeys-lru` with
no ACL and no notifications, the server cannot attribute a read to a tenant.** `IDLETIME` on
the two keys hear rarely rewrites (`dama:hear:event:{device_id}`, `dama:hear:devices`) is the
only per-key read signal available, and it is a bound, not a count. This is a finding about
the instance, not a gap in the method, and it is why §8 requires a *behavioural* proof
(quiet-period + dual-read) rather than a telemetry proof.

### 3.4 Method D — the AWS Lambda/SQS subscriber boundary

The inventory records the chain as API Gateway → Lambda → SQS → Oxalis SQS →
`dama-sqs-consumer.py` → local MQTT, and records that **no in-repo consumer imports an AWS
SDK** (`tools/recoupling_guard.py:73-75` makes such an import merge-blocking). Hear therefore
cannot and must not enumerate the cloud side itself. What it can do is state the evidence it
requires, so the owner can produce it with their own read-only role:

| Question | Read-only evidence requested | Acceptable form |
|---|---|---|
| Does any queue have a second consumer? | `GetQueueAttributes` (`ApproximateNumberOfMessages*`, `RedrivePolicy`), `ListQueues`, `ListDeadLetterSourceQueues` | dated export, attributed to a named role |
| Does any Lambda read from it? | `ListEventSourceMappings` per function, `ListFunctions` filtered to the account/region in use | dated export |
| Who else subscribes to the topic/route? | SNS `ListSubscriptionsByTopic` if a topic is in the path; API Gateway stage/route export | dated export |
| Is there an undocumented consumer that only appears when messages flow? | CloudTrail `ReceiveMessage`/`DeleteMessage` principals over the same 14-day window | aggregated principal list, no payloads |
| Does the queue retain a backlog that a cutover would strand? | queue age/retention attributes, DLQ depth | dated export |

Rules: no payload leaves the cloud account into this repository; principals are recorded as
role/function names, never as credentials or account-identifying secrets; the export is
attached to the consumer manifest as a `foreign-attestation` row with its author and date.
**Absence of a second subscriber must be asserted by the account owner in writing.** Hear
never infers it, and the inventory's §6 unknown stays open until that assertion exists.

### 3.5 Method E — operator automation and ad-hoc clients

The hardest population: scripts on operator machines, runbook copy-paste, dashboards.

| Surface | How it is closed | Evidence |
|---|---|---|
| Runbooks in this repository | already enumerated: `docs/durable-outbox-failure-drill-runbook.md`, `deploy/images/service/README.md` evidence step 6, `tools/bridge_soak_evidence.py` | manifest rows, in-tree |
| Operator scripts outside the repository | a dated declaration round: every operator with cluster or NodePort `30379` access confirms in writing what they run against `dama:hear:*`, on what schedule | one signed row per operator; **silence is `unattributed`, not "none"** |
| Ad-hoc `redis-cli` / `kubectl exec` | announced contract-change notice period (design default: 30 days) before any semantic change, published where the runbooks live | notice, with acknowledgements |
| Dashboards / exporters | declared by their owner, including any generic Redis exporter that scrapes the whole keyspace | manifest row |
| NodePort `30379` exposure | recorded as an unauthenticated-adjacent reachability fact: anything on the node network can be a reader | a reachability statement in the manifest, not an inventory of hosts |

## 4. Authoritative versus cache classification

No key may enter a lifecycle rule until it is classified. The classification is the contract
between the writer and every reader, and it is what makes "you may lose this" reviewable.

Classes:

- **A — authoritative.** Loss changes a durable answer. Redis is not permitted to hold class
  A once a canonical store exists; today it effectively does, and that is the defect.
- **C — cache (derivable).** Loss costs latency only; the value can be recomputed from the
  canonical store within a stated recovery bound.
- **S — signal (perishable).** The value *is* a time-bounded assertion (liveness). Loss is
  indistinguishable from the assertion expiring, so the reader must already tolerate it.
- **U — unclassified.** Anything not yet decided. Blocks every gate.

Proposed classification of the measured keyspace, with the property each claim rests on:

| Key | Today's de facto class | Target class after Phase 6 | Derivable from | Recovery bound if lost |
|---|---|---|---|---|
| `dama:hear:{device_id}` (TTL 30 s) | **A** (it is the liveness answer) | **S** | canonical health projection over durable heartbeat rows | next heartbeat interval, plus the projection's settle horizon |
| `dama:hear:latest` | **A** (fleet-global last heartbeat) | **C** | `MAX(received_at)` over durable rows | one query |
| `dama:hear:devices` (set, TTL `-1`) | **A** (de facto device registry, and it contains `test-verify`) | **C**, and *not* the registry — the canonical device registry is authoritative | canonical registry | one query |
| `dama:hear:event:{device_id}` (TTL `-1`) | **A** (last event per device) | **C** | last durable event row per device | one query |
| `dama:hear:events` (stream, `maxlen 1024`) | **A**-shaped but **already lossy** | **C**, explicitly "recent tail, lossy by construction" | durable event log | bounded by the canonical retention window |

Two rules fall directly out of the table:

1. **A class-C or class-S value may be evicted, expired or flushed at any time without
   incident.** If any reader's behaviour contradicts that, the reader is the thing that must
   change, and the change is recorded in the manifest before the lifecycle proceeds.
2. **`allkeys-lru` means every hear key is already treated as class C by the server.** The
   classification above does not *introduce* risk; it documents a risk that has been live and
   unacknowledged, which is precisely the postmortem's misleading-signal pattern
   ([REDESIGN-LESSONS.md](REDESIGN-LESSONS.md)).

## 5. The consumer manifest and the evidence ledger

Two artefacts, both dated, both append-only, both produced by an operator and reviewed:

**`hear.redisconsumer.manifest`** — one row per known or suspected reader:
`consumer_id`, `owner`, `owner_contact_role` (a role, never a person's private contact),
`discovery_method` (A–E), `source_ref` (commit / object generation / attestation id),
`keys_touched` (prefix-scoped patterns only), `access_kind` (read / write / both),
`cadence_observed`, `criticality_declared_by_owner`, `replacement_answer` (which canonical
surface serves them after the change), `status` ∈ {attributed, unattributed, retired,
disputed}, `first_seen`, `last_confirmed`.

**`hear.rediscensus.receipt`** — one row per census run (staged, **not** frozen; it must not
enter `docs/data/phase0-freeze-contracts.v1.json` until §8's promotion gate, exactly as the
Phase 4/5 receipts are staged before promotion): `run_id`, window start/end, method, sample
count, samples lost, longest gap, per-key `TYPE`/`TTL`/`SCARD`/`XLEN`/`STRLEN`/`IDLETIME`,
distinct pseudonymous clients, `unattributed_count`, the redaction profile version, the
allow-list version, the approver, and the tool build. A receipt missing any of these is
**void**, not a negative result — the same rule Phase 6 §8.4 applies to comparison runs.

Rejected as evidence by construction: anything derived from `dama:hear:events` (lossy), any
run whose allow-list or redaction profile differs from the recorded version, any run with an
unexplained sample gap longer than the shortest cadence it claims to cover.

## 6. Lifecycle rules: TTL, eviction and bounded semantics

These are the **target** rules. They are design statements; applying any of them is a code and
deployment change that this document does not authorize and that cannot land before Phase 6.

### 6.1 TTL policy

| Key | Today | Target rule | Rationale |
|---|---|---|---|
| `dama:hear:{device_id}` | TTL 30 s (`HEAR_HEARTBEAT_TTL_S`), frozen contract value | unchanged **until** the canonical projection is trusted; then the TTL stops being the answer and becomes a cache hint | the TTL is a frozen contract (`tools/freeze_contracts.py:395-401`); changing it is a contract change, not a tuning knob |
| `dama:hear:latest` | none | TTL ≥ 2× the longest expected fleet-wide heartbeat gap, refreshed on write | a stale fleet-global "latest" is worse than no value |
| `dama:hear:devices` | none, unbounded | replaced by a bounded structure (§6.3); if kept as a cache, TTL on the whole key with full rewrite on refresh, never per-member accretion | a set that only ever grows is a registry pretending to be a cache; `test-verify` is the proof |
| `dama:hear:event:{device_id}` | none, unbounded | TTL sized to the operator-visible "recent event" window, refreshed on write | one key per device forever is unbounded in device count, not in time |
| `dama:hear:events` | `maxlen 1024`, no TTL | keep `maxlen` and add an explicit "lossy tail" label in every reader-facing doc; never a source of counts | it already silently discards; the only defect is that it looks authoritative |

**Every TTL is a maximum staleness, never a liveness proof.** Any reader that infers "the node
is up" from key presence must be listed in the manifest with a replacement answer before the
TTL rules change.

### 6.2 Eviction policy

Hear does not own `maxmemory-policy` and must not request a change to it for its own benefit:
it is shared with eleven foreign workloads. The hear-side rules are therefore behavioural:

1. **Design for eviction at any moment.** Every hear reader must be correct when a key is
   absent for a reason other than expiry.
2. **Measure the exposure, do not assume it.** `INFO memory` / `evicted_keys` /
   `maxmemory_policy` are recorded in every census receipt so that the first eviction event in
   the instance's history is visible as a dated fact (`evicted_keys=0` today).
3. **Never mistake eviction for a signal.** An evicted `dama:hear:{device_id}` is
   indistinguishable from a dead node *and that is unfixable within Redis*. It is the primary
   technical argument for moving the liveness answer to the canonical store, and the
   quantified version belongs in the risk register rather than in a tuning ticket.
4. **A tenant-scoped ACL is the prerequisite for hear ever deleting a key safely** (inventory
   §1.2, security lane). Until it exists, any hear-side deletion is one typo from a
   multi-tenant incident, so §8's preconditions require it before *removal*, not before
   *replacement*.

### 6.3 Bounded replacement for the unbounded event and list semantics

Three unbounded semantics need a bounded replacement before their keys can be retired:

| Legacy semantic | Bounded replacement | Boundedness proof |
|---|---|---|
| `dama:hear:devices` as "every device ever seen" | canonical **device registry** with enrolment and decommission states, plus a derived cache key holding only *currently enrolled* devices, rewritten whole on change | cardinality ≤ enrolled fleet size, asserted per refresh; a member that is not in the registry is a defect, not a device |
| `dama:hear:event:{device_id}` as "last event, forever" | canonical event rows + a per-device cache with TTL (§6.1); readers that want history query canonical with an explicit time range | key count ≤ enrolled devices; byte size ≤ devices × max envelope size, both asserted in the receipt |
| `dama:hear:events` as "the event log" | canonical durable event log for counts, conservation and replay; the stream keeps `maxlen` and is documented as a **best-effort recent tail** | unchanged `maxlen`; the point is that nothing is allowed to *count* it |

Removing a member from the device set is, today, indistinguishable from a decommission
(inventory §1.2). The bounded replacement fixes that by making *the reason* a stored field:
the registry records why a device left. This is a precondition for the set's retirement, not
an optional nicety — without it, retirement destroys information.

## 7. Dual-read, shadow and reconciliation

The sequence below is the only path from "Redis answers" to "Redis may be removed". Each stage
is reversible by configuration, and no stage deletes anything.

| Stage | What runs | What is proved | Reversal |
|---|---|---|---|
| **0. Census** (may start now; read-only) | §3 methods A–E; receipts per §5 | who reads the keys; `unattributed_count` trending to zero | stop the job |
| **1. Canonical projection exists** | a health/liveness projection over durable rows, computed but wired to nothing | the projection is computable and its settle horizon is stated | delete the job |
| **2. Shadow compare** | the Phase 5 comparator ([phase5-shadow-read-comparator.md](phase5-shadow-read-comparator.md)) applied to the liveness/last-event/device-list projections; compare a **derived verdict**, not key presence (P6-R2) | agreement for a full window including a node reboot loop (R3), a quiet node, and a simulated Redis outage | disable the comparator; it feeds no operator surface |
| **3. Dual-read** | operator surfaces read canonical **and** Redis; canonical is displayed, Redis is displayed alongside as the legacy column; disagreements are receipted | operators see both answers, and any behavioural dependence on the legacy column becomes visible | a single flag returns the display to Redis-first |
| **4. Canonical-primary, Redis mirror** | canonical answers; hear keeps **writing** the keys as a compatibility mirror | external readers keep working with no change; hear no longer *relies* on Redis | flip reads back; the mirror never stopped |
| **5. Quiet period** | mirror still written, canonical serving; census continues | no consumer outside hear reads the keys for a full declared window | nothing to reverse |
| **6. Write stop (still no delete)** | hear stops writing the keys; keys are left to expire or sit | the last hear dependency is gone; a forgotten reader now fails **visibly and reversibly** | resume writing; state re-materializes within one heartbeat interval |
| **7. Removal** | prefix-scoped deletion under an approved retention/destruction record | the keyspace is clean | **none** — this is the one irreversible step, and it is last |

Reconciliation evidence required at stages 2–5, all of it durable (never from the lossy
stream), and all of it reusing the vocabulary already defined for the write and read
comparators rather than inventing a third:

1. **Conservation.** For the event lane: `input = accepted + duplicate + refused`, closing
   over the window ([phase4-dual-write-observability.md](phase4-dual-write-observability.md)
   §6.1). Note the open blocker: **R4 (refusal rows discarded before durable write) makes
   conservation unclosable today**, so stage 2 cannot pass until R4 lands. That is a fact
   about ordering, not a reason to weaken the rule.
2. **Per-device liveness agreement.** For every device, for every sample, the canonical
   verdict and the Redis-derived verdict agree, or the disagreement is classified against a
   dated expected-difference list (R3-class defects are *expected*, not regressions — P6-R4).
3. **Registry agreement.** Canonical enrolled set ≡ `dama:hear:devices` minus a dated,
   itemized exception list (`test-verify` traced to its creator, `nyquist`/`rankine` explained
   as set members with no live key).
4. **Last-event agreement.** Per device, canonical last event and `dama:hear:event:{id}`
   identify the same event by `event_id`, within the declared skew.
5. **Outage drill.** A deliberate loss of Redis (drill, not an outage in production traffic:
   the rehearsed procedure is `docs/durable-outbox-failure-drill-runbook.md`, which already
   carries the `FLUSHDB`-never rule) changes **no durable result**, and the canonical answers
   are unaffected.

Declared window defaults, to be confirmed by an operator, not by this document: 14 days for
the census window, 14 days of stage-2 agreement, 14 days of stage-3 dual-read with zero
operator escalations, and 30 days of stage-5 quiet period. The quiet period is longest on
purpose: it is the only defence against a monthly job.

## 8. Preconditions and gates

### 8.1 Entry gates for the census lane (stage 0) — the only lane that may start early

1. `infra` has approved the command allow-list, window and redaction profile in writing (§2,
   §3.2).
2. The census tool exists, is prefix-scoped, and its refusal behaviour is tested (§10).
3. A retention decision for census receipts exists (§9).
4. Nothing in the lane writes to an operator surface (§2.7).

### 8.2 Preconditions for *replacement* (stages 1–4)

1. Phases 2–6 closed in order; Phase 6 read cutover has served canonical reads for a full
   compatibility window (inventory §1.2).
2. R1–R3 fixed and re-soaked; **R4 closed**, or conservation cannot be computed at all.
3. Census `unattributed_count = 0` for the whole census window, with every attributed
   consumer carrying a `replacement_answer`.
4. The AWS boundary attestation (§3.4) exists and asserts the subscriber set.
5. Every key has a class (§4) and no key is class `U`.
6. Bounded replacements (§6.3) exist in the canonical store, including the decommission-reason
   field.

### 8.3 Preconditions for *write stop* (stage 6)

7. Stages 2–5 evidence complete, with the declared windows actually elapsed (a shortened
   window is a shortened claim, and is recorded as such).
8. A 30-day contract-change notice has been published and acknowledged (§3.5).
9. The rollback path in §8.5 has been *exercised*, not merely written.

### 8.4 Preconditions for *removal* (stage 7) — cache removal proper

10. A separately approved **retention/destruction record** exists per
    [data-governance.md](data-governance.md); this document is not one and does not request
    one (inventory §7).
11. A tenant-scoped **ACL** exists so a hear-side deletion cannot reach a foreign prefix
    (§6.2.4).
12. The deletion is prefix-scoped, enumerated key-by-key from a receipt, and executed as a
    reviewed, itemized list — never a pattern-matched sweep, never `FLUSHDB`.
13. The removal is in its **own release**, with no code cutover in the same release
    (inventory §9).
14. The census shows zero reads attributable to any non-hear consumer for the full quiet
    period, and the write stop has been in effect for that period without an escalation.

### 8.5 Rollback

| Stage | Rollback | Time to restore | Data loss |
|---|---|---|---|
| 0–2 | stop a job | immediate | none |
| 3 | flip the display flag to Redis-first | one config apply | none |
| 4 | flip reads back to Redis; the mirror never stopped being written | one config apply | none |
| 5 | as stage 4 | one config apply | none |
| 6 | resume writing the keys | one pod cycle; state re-materializes within one heartbeat TTL | none — the keys were always derived |
| 7 | **no rollback** | — | the keys are gone; only the canonical store can answer |

Rollback is configuration-first at every reversible stage, which matches
`standalone-migration.md` §Rollback. The asymmetry at stage 7 is the reason it is last and the
reason it needs its own approval.

Observable rollback triggers (any one, at any stage): a consumer appears that is not in the
manifest; `unattributed_count` rises; the canonical projection disagrees with Redis outside
the expected-difference list; conservation stops closing; an operator escalation references a
missing Redis answer; the instance records its first `evicted_keys > 0` during a comparison
window (the window is then void, not failed).

## 9. Privacy and retention for the evidence itself

The census produces a new evidence class, so it needs its own governance answer rather than
inheriting one by omission:

| Item | Rule |
|---|---|
| Client addresses | never stored raw; salted pseudonym plus network class only (§3.2). The salt is stored separately from the receipts and is rotated per census campaign |
| Key **values** | never captured. The census reads `TYPE`/`TTL`/`STRLEN`/`SCARD`/`XLEN`/`IDLETIME` — sizes and shapes, not bodies. Heartbeat bodies contain node identity and position-bearing fields, so a census that read values would create a new privacy surface to prove a cleanup |
| Free text (log lines, errors) | high-precision decimals blunted at capture, per `tools/bridge_soak_evidence.py` rule 3, because receipts get pasted into issues where `tools/coord_guard.py` is not watching |
| Credentials | `REDIS_PASS` and every `PASS/TOKEN/SECRET/KEY/CRED/AUTH`-named value redacted by name; the instance's plaintext `--requirepass` argument is never reproduced |
| AWS exports | principals as role/function names; no payloads, no account secrets (§3.4) |
| Receipt retention | a declared class with an expiry (design default: retain for the migration, expire one release after Phase 7 closes), approved before the first run — the same open question Phase 6 §10.1 raises for comparison receipts |
| Operator declarations | a role and a statement, not personal contact details |
| Legal hold | if a hold is in force, receipts are held with everything else and no expiry runs; holds are a separately audited action and are not exercised here |

## 10. Tests and receipts

Every rule above that can be violated by a tool must be enforced by a test rather than by
review. These are the tests a future implementation PR must bring with it; **none of them is
added by this docs-only change.**

| # | Test | Asserts |
|---|---|---|
| 1 | census allow-list | the tool refuses any Redis command outside §2.1's list, including `MONITOR`, `CONFIG`, `KEYS`, `SUBSCRIBE`, `DEL`, `EXPIRE`, `FLUSHDB` — asserted per command name, table-driven |
| 2 | prefix confinement | any scan/report path given a non-`dama:hear:` pattern raises; a key name outside the prefix can never reach a receipt |
| 3 | kubectl read-verb confinement | mirrors `bridge_soak_evidence.assert_read_only`: `apply`/`patch`/`delete`/`scale`/`rollout`/`cp` refused |
| 4 | redaction | a synthetic `CLIENT LIST` row containing an address, a password-named field and a high-precision decimal produces a receipt with none of them; a fixture asserts the pseudonym is stable and the salt is absent |
| 5 | receipt completeness | a receipt missing window/sample-count/gap/allow-list-version/redaction-version/approver is **void**, and the tool says "void", not "clean" |
| 6 | coverage arithmetic | a census whose longest gap exceeds the cadence it claims to cover cannot report that cadence as covered |
| 7 | unattributed is blocking | a manifest with any `unattributed` row fails the gate evaluation, and the failure message names the rows |
| 8 | classification completeness | every key pattern in `docs/data/phase0-freeze-contracts.v1.json` §Redis keys has a class in §4, and class `U` fails |
| 9 | boundedness | the bounded replacements assert cardinality ≤ enrolled fleet and byte size ≤ devices × max envelope, with a fixture that grows the device set and fails |
| 10 | no-count-from-the-stream | any code path attempting to derive a count or a conservation term from `dama:hear:events` fails the test that names it lossy |
| 11 | freeze compatibility | the census receipt is **staged**, absent from the frozen baseline, and `tools/freeze_contracts.py --check` stays green; promotion is a separate, reviewed change |
| 12 | gate ordering | stage N's gate evaluator refuses to pass when stage N−1's evidence is missing, absent, or has a shorter window than declared |

Receipts, not assertions, are the deliverable of each stage: a census run, a comparison run, a
dual-read window, a quiet period and a drill each produce a dated, redacted artefact that a
reviewer can read without cluster access. A stage with no receipt did not happen.

## 11. Open questions owed to an operator

Recorded, not decided:

1. The four window lengths in §7 (census, compare, dual-read, quiet). This document proposes
   14/14/14/30 days; the quiet period is the one that actually bounds the risk of a forgotten
   monthly reader.
2. Census receipt retention class and expiry (§9), which needs a governance owner.
3. Whether the liveness *contract* (`HEAR_HEARTBEAT_TTL_S = 30`, frozen) is restated in
   canonical terms before or after the read cutover — it is a frozen contract value, so it is
   an ADR, not a tuning change.
4. Whether `dama:hear:latest` survives at all as a cache, or is simply dropped at stage 4
   because a single global "last heartbeat" has no canonical meaning in a per-device model.
5. Who owns `test-verify` in `dama:hear:devices`, which must be traced before the set can be
   replaced without destroying information.
6. Whether the AWS attestation (§3.4) is obtained once or refreshed per campaign; a one-time
   attestation ages badly against a cloud account hear cannot see.
7. Whether an `infra`-owned tenant ACL is pursued now as a security-lane item (it is the
   precondition for §8.4.11, and it is on a different clock from the migration).

## 12. What this document explicitly does not do

- It retires, deletes, expires, disables, scales, migrates or cuts over **nothing**.
- It issues no Redis command, client-affecting or otherwise, and enables no notification.
- It changes no manifest, image, schedule, env var, secret, firmware or cloud resource.
- It reads no credential and creates none.
- It requests no retention or destruction record, and asserts no legal-hold state.
- It does not promote a receipt into the frozen contract baseline.
- It does not shorten, reorder or conditionally waive the Phase 2–6 ordering, and it does not
  claim that any of the gates above is currently satisfiable: **R4 alone blocks the first
  reconciliation stage.**
