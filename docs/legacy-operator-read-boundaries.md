# Legacy operator read boundaries: audit and staged remediation design

What the **currently deployed** read surfaces expose today, which of their defects are real
now (independent of any cutover), and a staged, compatibility-safe remediation that can be
executed without waiting for — or preempting — Phase 6.

> **Status.** Audit and design only. This document changes no endpoint, no manifest, no
> credential, no cluster object, no CronJob and no frozen contract. It does not enable a
> canonical read, does not move a Phase 2–6 gate, and implements nothing. Every behaviour
> described in §2–§4 is what the tree does at the commit this was written against; every
> behaviour in §6 is proposed and unimplemented.

Companions: [phase6-operator-read-audit.md](phase6-operator-read-audit.md) (the read-surface
inventory this narrows to the legacy defects),
[phase6-operator-api-contract.md](phase6-operator-api-contract.md) (the *canonical*-side
counterpart: what the eventual API must be — this document is what the **legacy** surfaces
owe in the meantime), [api-boundaries.md](api-boundaries.md) (the
target northbound contract), [data-governance.md](data-governance.md) (the access, redaction
and audit rules), [worker-packaging.md](worker-packaging.md) (how the annotate code actually
reaches the cluster), [migration-risk-register.md](migration-risk-register.md),
`SECURITY-REVIEW.md` (finding 2, unauthenticated node reads).

## 1. Why this is separate from Phase 6

The Phase 6 audit found five conditions while mapping a future cutover. Four of them are
**true of the running system now** and would still be true if the migration were cancelled:

| # | Condition | True today without any cutover |
|---|---|---|
| L1 | `GET /api/export` is unbounded | yes — one request serialises every human annotation row |
| L2 | No cursor contract on any list read | yes — `/api/queue` has `limit` only |
| L3 | Clip audio and annotation export have no authorization | yes — both are anonymous on a tailnet-exposed Service |
| L4 | Liveness is expressed by absence (TTL expiry, missing file) | yes — no reader can distinguish *expired* from *never observed* |
| L5 | Operator thresholds are invocation inputs, not contracts | yes — `--max-stale-s` and friends change the verdict, not the data |

Phase 6 correctly refuses to fix these *as part of a cutover*: a cutover that also changes
semantics cannot be compared against the thing it replaced. The consequence is the inverse
obligation — these are **legacy defects owed a legacy fix**, on their own schedule, with
their own rollback, and with the explicit property that a later cutover inherits a fixed
surface rather than a documented one. §6 is that plan.

The plan's hard constraint: **no stage may change an answer any current reader depends on
until an operator has decided to change it.** Every stage is therefore additive-by-default
with an explicit, dated flip.

## 2. Boundary map (as deployed)

### 2.1 Annotate service

Code: `tools/hear_annotate/server.py`. Shipped to the cluster **twice over**: the checkout
is the source, and `deploy/k8s/hear-annotate.yaml` carries a byte-identical embedded copy in
the `hear-annotate-code` ConfigMap, regenerated only by
`python3 deploy/k8s/gen_configmap.py hear-annotate-code`
(`deploy/k8s/gen_configmap.py:206-207`) and guarded byte-for-byte by
`tests/test_configmap_sync.py::TestTheEmbeddedConfigMaps`. That guard exists because the
copies once drifted for an entire release: the live pod ran a pre-hardening server
(`worker-packaging.md`, commit `fa5a589`). **Any change in §6 is two artifacts, not one.**

| Route | Code | Auth | Bound | Data class (`data-governance.md` §1) |
|---|---|---|---|---|
| `GET /` (HTML UI) | `:609-611` | none | n/a | — (loads two scripts from `unpkg.com`) |
| `GET /healthz` | `:614` | none | O(1) by construction | `telemetry` |
| `GET /api/queue?limit=` | `:640` | none | `limit` clamped `1..200`, default 25 | `detection` + `labels` (clip keys, node, model tags) |
| `GET /api/audio/{clip_key}` | `:646` | none | one clip, `safe_join`-confined (`:80`), `410` when pruned | **`clip` / `raw_audio`** |
| `POST /api/annotations` | `:668` | none (identity is *attribution only*, `user_from_headers:354`) | body-validated by `AnnotationIn` | `labels` |
| `GET /api/export` | `:678` | none | **none** — `export_rows():345` is `SELECT … ORDER BY id` with no limit | `labels` **with reviewer identity** and free-text `notes` |

### 2.2 Deployment exposure and the tailnet assumption

* `Service/hear-annotate` is `ClusterIP` but annotated `tailscale.com/expose: "true"`
  (`deploy/k8s/hear-annotate.yaml:797-804`). The service is therefore reachable by **every
  device and user the tailnet ACL admits**, and that ACL is not in this repository.
* The docstring calls the service "tailnet-only" (`server.py:2`). That is a *network*
  statement, not an authorization statement. Nothing in the tree records which tailnet
  principals may read clip audio; the assumption "on the tailnet ⇒ entitled to raw audio and
  every reviewer's identity" has never been written down, let alone approved.
* `HEAR_ANNOTATE_TRUSTED_PROXIES` compares the **direct peer** `request.client.host`
  (`server.py:354-362`) before honouring `tailscale-user-login` / `x-webauth-user` /
  `x-forwarded-user`. That is the correct shape (it cannot be spoofed by a client that is not
  the proxy — `tests/test_hear_annotate.py:228`), and it is **unset in the manifest**: the
  Deployment declares only `HEAR_ANNOTATE_DB` (`:757`), so every annotation in the live
  database is attributed `client:<pod-ip>`. There is an identity header path, and it is off.
* No `NetworkPolicy` exists for the namespace, so in-cluster callers reach the service
  directly regardless of tailnet ACLs.
* `HEAR_ANNOTATE_CORS_ORIGINS` is likewise unset, so no cross-origin caller is permitted
  today; a mobile origin is a supported configuration (`tests/…:205`) and would widen the
  same unauthenticated surface to a browser.

### 2.3 Health/liveness readers (for L4 and L5)

* Heartbeat receiver: `POST /api/hear/heartbeat` **is** authenticated — a fleet-wide shared
  token compared in `require_auth` (`tools/hear_heartbeat_receiver.py:1531-1536`) against the
  `X-Hear-Token` header; `GET /healthz` (`:1485`) is not authenticated and reports cache and
  durable state, not per-node liveness. Note the two properties for §6.3: the comparison is a
  plain `!=` (not constant-time, `:1534-1536`), and the refusal to run tokenless lives in the
  **entrypoint** (`_configured_auth_token`, `:85-90`, called at `:1633`) while the request
  path treats `auth_token=None` as auth-off (`:1531-1533`) — a safe arrangement only as long
  as nothing else constructs the server.
* Liveness of a node is the *absence* of `dama:hear:{device_id}` after its 30 s TTL, or the
  absence/staleness of `/pool/heartbeat.json` measured against `--max-stale-s`
  (`tools/hear_drain.py`). No reader can distinguish "expired 40 s ago", "never enrolled" and
  "the reader could not reach the store" — all three render as not-live.
* Node `GET /status`, `/ls`, `/sd`, `/audio` are unauthenticated by construction
  (`SECURITY-REVIEW.md` finding 2). They are **out of scope here**: changing them is a
  firmware release with a fleet rollout and per-node admin tokens (ADR 0006), and that
  sequencing is owned elsewhere. Recorded so this document is not read as covering them.

## 3. Behaviour current consumers actually rely on

A remediation is compatibility-safe only against behaviour that is depended upon. This is
that list, with its source, so §6 can be checked against it rather than against intent.

**The bundled UI** (`INDEX_HTML`, `server.py:543-560`):

1. `fetchMore(50)` calls `/api/queue?limit=50` and reads `data.clips`; it ignores `count` and
   every other key — so **adding keys is safe, renaming `clips` is not**.
2. It **already de-duplicates by `clip_key`** against the clips it holds, and it re-requests
   from the top (`loadQueue`) rather than paging. A cursor is therefore *addable without the
   UI knowing*, and the UI is robust to the same clip appearing twice.
3. It refills whenever fewer than 5 clips remain, so it tolerates short pages but not empty
   ones: an empty page ends the session with "Queue complete!".
4. `GET /api/audio/{clip_key}` is fetched by WaveSurfer as a **plain browser URL** — no
   custom header can be attached without changing the UI. Any token-in-header scheme for
   audio must ship with a UI change in the same artifact, or the player breaks. This single
   fact drives the staging in §6.3.
5. `POST /api/annotations` sends `submission_id` and treats any non-2xx as "Save failed".

**Kubernetes**: the readiness probe is `GET /healthz` with `timeoutSeconds: 2`
(`hear-annotate.yaml:759-772`). The probe is an operator-visible contract; the comment there
records what happened the last time it was treated as an implementation detail (a 1.19 s
corpus parse against a 1 s timeout took the UI down). **No §6 change may add corpus-sized
work, a database scan, or a network call to `/healthz`.**

**Operator/ML workflow**: `docs/ml-lifecycle.md:45-60` names `tools/hear_annotate` as the
source of supervised labels and requires a *human-only, session-aware label manifest* per
training run, with counts by annotator/agreement state. Today that manifest is produced by
hand from `/api/export`'s full array. A bounded export must therefore still be able to
produce a **complete, reproducible** snapshot — bounding the *request* must not bound the
*dataset*.

**No automated in-repo consumer.** `grep` finds no tool, CronJob or test outside
`tools/hear_annotate/` and `tests/test_hear_annotate.py` that calls these routes;
`tools/check_fleet_health.py:846` only reads the `hear-annotate` pod's status as one of five
workload labels. The blast radius of an annotate API change is therefore: the bundled UI, any
operator's curl/browser habit, and the ConfigMap gate. That is small — which is precisely why
this is fixable now rather than after a migration.

**Existing tests that encode the contract** (`tests/test_hear_annotate.py`, 518 lines) — all
must keep passing **unmodified** through §6 stage 0–2:

`test_queue_prioritizes_unannotated_model_disagreement` (queue order), `…_audio_endpoint_streams_normalized_wav`
(+ `X-Gain-Db` / `X-Gain-Bound-By` headers), `…_export_endpoint_returns_only_human_ground_truth_and_no_model_tags`
(`{provenance, annotations, count}` shape), `…_safe_join_rejects_parent_and_symlink_escape`,
`…_untrusted_client_cannot_spoof_proxy_identity_header`, `…_submission_id_is_idempotent_across_concurrent_store_instances`,
`…_reused_submission_id_with_different_payload_is_conflict`, `…_queue_parses_the_corpus_once_across_repeated_requests`,
`…_repeat_queue_requests_stay_far_inside_the_readiness_budget`, `…_queue_cost_does_not_grow_with_the_corpus_once_warm`,
`…_cached_queue_is_identical_to_a_freshly_computed_one`, `…_annotating_a_clip_drops_it_from_the_very_next_queue`,
`…_a_clip_appended_to_the_index_appears_in_the_next_queue`, `…_an_atomically_replaced_tags_file_invalidates_the_cache`,
`…_a_truncated_index_empties_the_queue_rather_than_serving_the_old_one`,
`…_a_missing_corpus_index_is_an_empty_queue_and_a_ready_service`,
`…_cached_index_still_refuses_traversal_and_unlisted_clips`,
`…_concurrent_queue_reads_and_annotations_stay_consistent`, plus
`tests/test_configmap_sync.py::TestTheEmbeddedConfigMaps` (three tests) for the shipped copy.

## 4. Findings

Severity is *current operational/privacy risk*, not migration risk.

| ID | Finding | Severity | Evidence |
|---|---|---|---|
| **L1** | `/api/export` serialises the entire annotations table into one JSON array, unbounded and unauthenticated. Cost grows with the corpus exactly as the old readiness probe did; the response also carries `user_id` for every row and free-text `notes` | **high** | `server.py:345-351`, `:678-681` |
| **L2** | No list read has a cursor, a declared sort key in its response, or a truncation flag. `/api/queue` returns a *filtered, priority-ordered, mutable* view with no way to page past `limit=200` and no way to tell a client the corpus generation it was built from | **medium** | `server.py:640-643`, `queue_rows:437-460` |
| **L3** | `/api/audio/{clip_key}` returns clip WAV bytes and `/api/export` returns reviewer-identified labels, both anonymous, on a Service annotated for tailnet exposure. `data-governance.md` §3 requires server-side authorization per tenant, data class and purpose, and separates `reviewer` from `evidence custodian` | **high** | `server.py:646`, `:678`; `hear-annotate.yaml:804`; governance §3 |
| **L4** | Liveness is absence. Expired, never-seen and unreachable-store are indistinguishable to every current reader, and a deduplicated heartbeat does not re-arm the TTL (register R3), so a healthy rebooting node reads as offline | **medium** | freeze §"Redis keys"; `hear_heartbeat_receiver.py`; `hear_drain.py` staleness check |
| **L5** | Health verdicts depend on invocation flags (`--max-stale-s`, `--unfetched-window-s`, clip deferred/lost thresholds) that are neither recorded in the output nor pinned anywhere; two operators can get two verdicts from identical data | **medium** | `tools/hear_drain.py --check` flags |
| **L6** | Identity is configured off: `HEAR_ANNOTATE_TRUSTED_PROXIES` is unset in the Deployment, so every stored `user_id` is `client:<ip>`. Governance §1 requires labels to be retained *with reviewer identity*; the column exists and is being filled with a network address | **medium** | `hear-annotate.yaml:753-758`; `server.py:354-362` |
| **L7** | No audit record for reads. Governance §9 requires audit events for reads, searches, downloads and exports, and alerting on bulk reads. A full export leaves no trace anywhere | **medium** | governance §9; no logging in either read path |
| **L8** | The UI loads `wavesurfer.js` from `unpkg.com` at runtime (`server.py:546-547`). A tailnet-only service with a public CDN dependency is neither air-gappable nor reproducible, and it is a script-injection path into a page that renders operator-controlled text | **low–medium** (noted, **not** in this plan's scope) | `INDEX_HTML` |

Not findings, recorded so they are not "fixed" by accident: `safe_join` confinement is
correct and tested; the readiness probe is correctly O(1); `CorpusCache` invalidation by
`(mtime_ns, size, inode, device)` is correct and must not be replaced by a TTL; the
annotation table is append-only with a `provenance = 'human'` CHECK and that is the property
`/api/export` exists to preserve.

## 5. Design constraints

1. **Additive first.** New request parameters and new response keys only; no existing key
   changes name, type or meaning in the same release that adds a replacement.
2. **The flip is dated and separate.** Any change of a *default* (bounded export, enforced
   auth) is its own release, after a stated deprecation window, and is reversible by
   configuration alone.
3. **Two artifacts per code change.** Checkout + regenerated `hear-annotate-code` ConfigMap,
   with `tests/test_configmap_sync.py` green. Never hand-edit the embedded copy.
4. **Back up first.** `annotations.sqlite3` lives on the `local-path` `hear-pool` PVC with no
   backup (R6/R7). Before the first apply of any regenerated ConfigMap, snapshot it with the
   **SQLite backup API** — never a raw copy of a live WAL (`worker-packaging.md`).
5. **The probe stays O(1).** No stage may make `/healthz` depend on the corpus, the network
   or an unbounded query.
6. **Nothing here touches a frozen contract.** The 30 s Redis TTL, the `hear.*` schema ids and
   `hear.reconcile.receipt.v1` bytes are unchanged by every stage; §6.4 adds *reporting* of
   expiry, not a new expiry rule.
7. **No Phase 2–6 gate is consumed or advanced.** Stages 0–2 are legacy hygiene and can run
   during Phase 2/3; stage 3 is deliberately sequenced *before* any read cutover, which is the
   position the Phase 6 audit's open question 2 recorded.

## 6. Staged remediation

### 6.0 Stage 0 — observability and evidence (no behaviour change at all)

Add, behind nothing:

* A structured read-audit line per request on `/api/queue`, `/api/audio`, `/api/export`:
  `ts, route, principal (as attributed today), client_ip, request_id, rows_returned or
  bytes_returned, cursor_present, truncated, duration_ms`. **Never** clip bytes, notes, label
  text or a token (governance §9, `api-boundaries.md` "Do not put clip bytes, tokens, or
  secret values in logs"). `X-Request-ID` is honoured if supplied, generated otherwise, and
  echoed.
* A `bulk_read` marker on any export returning more than a declared row count, so the alert
  governance §9 requires has something to alert on.
* Emit `HEAR_ANNOTATE_TRUSTED_PROXIES` and CORS configuration state once at startup, so L6 is
  visible in the pod log rather than only in a manifest diff.

Exit: one week of logs establishes the **real** export size and call frequency — the number
that makes the stage 2 default defensible instead of guessed.

### 6.1 Stage 1 — a stable pagination/cursor contract on `/api/queue`

The queue is not a table; it is a priority-ordered projection over two JSONL files, minus a
mutable annotated-key set (`build_queue:527-540`). A naive offset breaks the moment a clip is
labelled. The contract therefore pins a **snapshot**, not an offset.

**Request (additive):** `limit` (unchanged: default 25, clamp `1..200`), `cursor` (opaque).

**Response (additive keys only):**

```json
{
  "clips": [],
  "count": 0,
  "next_cursor": "opaque-or-null",
  "has_more": false,
  "truncated": false,
  "snapshot": {"generation": "sha256:…", "observed_at": "…Z", "sort_key": "-priority,ts_utc_s,clip_key"},
  "read_contract": {"major": 1, "projection_version": 1}
}
```

`clips` and `count` keep their exact current meaning and position, so every consumer in §3
is unaffected and the existing export/queue tests pass unmodified.

**Cursor semantics.**

* The cursor is **opaque** to clients (`api-boundaries.md`) and encodes: the corpus
  `generation` (a digest of the existing `CorpusCache.fingerprint()` tuple — the cache already
  computes exactly this), the last emitted `(priority, ts_utc_s, clip_key)` triple under the
  already-existing total order `(-priority, ts_utc_s, clip_key)` (`queue_rows:459`), the
  `limit`, and an issue time.
* **Stable sort, already total.** The existing sort is deterministic and tie-broken by
  `clip_key`, so a keyset cursor is exact with no schema change and no new index.
* **Generation change is explicit, never silent.** If the corpus fingerprint moved since the
  cursor was issued, the response is `409` with RFC 9457 `code: CURSOR_GENERATION_CHANGED`
  and a fresh first page's cursor in `detail`; the client restarts from the top. It is never
  a silently reordered page. This is what makes Phase 5's `cursor_divergence` check
  (round-trip stability, no duplicates or gaps across a boundary) decidable on this surface
  instead of undefined.
* **Expiry** at `CURSOR_TTL_S` = 24 h to match `api-boundaries.md`; expired is
  `410 CURSOR_EXPIRED`, distinct from generation change.
* **Annotation drift inside a generation is declared, not hidden.** Clips labelled after the
  snapshot may still appear on a later page. That is the current behaviour of the whole
  surface, the UI already de-duplicates by `clip_key` (§3.2), and the alternative — pinning
  the annotated set too — would re-serve labelled clips, which is the defect `CorpusCache`
  was written to avoid. Declared in `snapshot`, not fixed.
* **Not compared across sides.** A cursor from this surface has no meaning on any canonical
  surface; per Phase 5 §5 the comparator compares concatenations, never tokens.

**Explicitly out of scope of stage 1:** raising the `200` clamp, changing default ordering,
adding filters. The clamp stays; `cursor` is how you get past it.

### 6.2 Stage 2 — bounded export semantics

`/api/export` becomes a **bounded, resumable, reproducible** read in three releases:

| Release | Behaviour | Compatibility |
|---|---|---|
| **2a** | Accept optional `limit` (`1..5000`) and `cursor`; accept `since_id`. Add `next_cursor`, `has_more`, `truncated`, `snapshot`, `read_contract` keys. **Default remains the full array**, plus a `Deprecation` + `Sunset` header and a `bulk_read` audit line | none broken; `{provenance, annotations, count}` unchanged when called as today |
| **2b** (dated flip, ≥ 90 days later per `api-boundaries.md`) | Default becomes `limit = 1000` with `has_more`/`next_cursor`. An explicit `limit=all` is accepted **only** with the evidence-custodian scope from stage 3 and is audited | a caller that ignored the deprecation gets a short page with `has_more: true`, never a silently truncated one |
| **2c** | `limit=all` removed; complete datasets come from the export *job* path (`api-boundaries.md` "large or unbounded searches use Exports"), which writes a manifest with a digest | dataset completeness moves to a manifest, which is what `ml-lifecycle.md` §"export manifest" already requires |

**Cursor key:** `id` (the `AUTOINCREMENT` rowid, already the declared `ORDER BY`,
`export_rows:345-351`). The table is append-only, so a keyset cursor over `id` is
**monotone, gap-tolerant and stable** — an export can be resumed hours later and cannot
re-emit or skip a row. No snapshot pinning is needed and none is claimed; `snapshot.observed_at`
plus `max_id` records the extent so the manifest is reproducible.

**Reproducibility requirement (protects `ml-lifecycle.md`):** a bounded export paged to
exhaustion with a recorded `(since_id, max_id)` pair must be byte-identical to the unbounded
export taken at the same instant. That is a test (§8), not a hope.

### 6.3 Stage 3 — authentication and authorization

Sequenced **after** stages 1–2 for one reason: the bundled UI fetches audio as a plain
browser URL (§3.4), so route authorization and a UI change ship together, and neither should
ride along with a pagination change an operator might want to revert independently.

**Modes.** `HEAR_ANNOTATE_AUTH ∈ {off, log, enforce}`, default `off` — identical behaviour to
today on day one. `log` records what *would* have been denied (the number that makes
`enforce` safe). `enforce` denies. Rollback from `enforce` is one env edit and a pod restart;
no data migration exists to undo.

**Scopes**, mapped to governance §3 roles rather than invented:

| Route | Required scope | Role |
|---|---|---|
| `GET /healthz` | none, permanently | — (it is a probe) |
| `GET /api/queue` | `events:read` | reviewer |
| `GET /api/audio/{clip_key}` | `clips:read` | reviewer |
| `POST /api/annotations` | `events:annotate` | reviewer |
| `GET /api/export` | `exports:read` | model steward |
| `GET /api/export` with `limit=all` / unredacted `user_id` | `exports:create` **and** evidence-custodian | evidence custodian |

**Credential, staged by cost:**

1. **Now-shaped:** a deployment-scoped read token in the `hear-annotate-token` Secret,
   presented as `Authorization: Bearer` or `X-Hear-Token`, compared with
   `hmac.compare_digest` — constant-time, unlike the existing heartbeat comparison, which is
   **not** changed here (its own lane owns it). For the browser, the same token may be set as
   a `HttpOnly`, `Secure`, `SameSite=Strict` cookie by the login step so `<audio>`/WaveSurfer
   URLs keep working; a token in a query string is **forbidden** (it lands in every access
   log).
2. **Target:** the tailnet identity headers already parsed in `user_from_headers` become the
   authentication source of record once `HEAR_ANNOTATE_TRUSTED_PROXIES` is set to the
   tailnet proxy address and the proxy is the **direct peer** (the existing check is
   peer-based and must stay peer-based: `X-Forwarded-For` chains are not parsed and must not
   be, because a chain is client-controlled unless every hop is trusted). Scope mapping then
   comes from a group→scope table in a ConfigMap, versioned in the repo.

**Trusted-proxy hardening that belongs with this stage:** fail closed if
`HEAR_ANNOTATE_AUTH=enforce` while `trusted_proxies` is empty **and** identity headers are
present — that combination means an untrusted hop is feeding identity. Today it is silently
ignored (correctly, for attribution); under enforcement, silence is the wrong answer.

**Network boundary, same stage:** a `NetworkPolicy` admitting only the tailscale proxy pod
(and the kubelet for the probe) to port 8080. Without it, authorization is enforced only at
the HTTP layer of a pod that anything in the cluster can reach.

### 6.4 Stage 4 — explicit absent/unknown liveness

The rule: **absence is never a value.** Every health read returns a verdict with a stated
derivation, and "we do not know" is a first-class answer distinct from "not live".

```json
{"node_id": "…", "liveness": {
  "state": "live | expired | never_observed | unknown",
  "observed_at": "…Z | null", "age_s": 41.2, "ttl_s": 30,
  "source": "redis | durable_sqlite | pool_heartbeat_json",
  "derivation": {"rule": "age_s <= ttl_s", "thresholds_id": "sha256:…"},
  "reader_reached_source": true}}
```

* `expired` — the key/file was found or is known to have existed and is older than the TTL.
* `never_observed` — no record of this node in this source, ever.
* `unknown` — **the reader could not reach the source.** Today this is reported as
  not-live, which manufactures a fleet-wide outage out of a Redis connection error.
* The 30 s TTL is unchanged and remains the frozen value; this stage reports it, and the
  `age_s` that produced the verdict, so the verdict is auditable.
* Register R3 (a deduplicated heartbeat does not re-arm the TTL) is **not** fixed here — it is
  a writer defect. It is recorded as the dated expected-difference the Phase 6 comparator
  needs (P6-R4), and this stage makes it *visible*: `observed_at` will contradict `expired`.

### 6.5 Stage 5 — thresholds become part of the answer (L5)

`--max-stale-s`, `--unfetched-window-s` and the clip deferred/lost thresholds keep their
defaults and their CLI flags. Additively, every JSON health output gains a `thresholds`
block and a `thresholds_id` digest over the resolved set, and the human output gains one
line naming it. Two verdicts computed with different threshold sets then differ *visibly*,
and the Phase 6 run manifest requirement (P6-R3) is satisfiable from the reader's own output
rather than from operator memory. No default changes; no flag is removed.

## 7. Redaction and audit requirements

Applying to every stage that returns label data:

| Requirement | Rule |
|---|---|
| Reviewer identity | `user_id` is returned in full only to evidence-custodian scope. At `exports:read` it is a stable pseudonym — `HMAC-SHA256(deployment_key, user_id)`, truncated — preserving the per-annotator counts `ml-lifecycle.md:60` needs without disclosing who labelled what. `ml-lifecycle.md:196` (pseudonymous IDs in training exports) already asks for this shape |
| Free-text `notes` | operator-entered, unbounded-content, 4096 chars. Never logged, never in an audit record, never in an error body. Returned only with the label rows themselves |
| Clip bytes | never in a log, an audit record, a receipt or an error. Unchanged from today's `Cache-Control: no-store` |
| Coordinates | annotations carry none today. If a future field could, `tools/coord_guard.py` covers the tree only, not the database — a redaction rule in code is required before any such field is added |
| Audit record | append-only, per §6.0's field list, retained per governance §9, alerting on `bulk_read` and on denied requests once `enforce` is on |
| Retention | annotation rows are `labels`-class; this design changes no retention and asserts none. The retention class of the **audit log** itself is an operator decision (§9) |

## 8. Tests the remediation owes

Named so the plan is checkable; none are written by this document.

**Must keep passing unmodified** through stages 0–2: every test listed in §3, plus
`tests/test_configmap_sync.py::TestTheEmbeddedConfigMaps`.

**New, per stage:**

| Stage | Test | Asserts |
|---|---|---|
| 0 | `test_read_audit_line_never_contains_notes_labels_or_bytes` | redaction of the audit path itself |
| 0 | `test_audit_line_is_emitted_once_per_read_with_row_counts` | the evidence exists |
| 1 | `test_queue_without_cursor_is_byte_identical_to_todays_response_shape` | the additive claim, for `clips`/`count` |
| 1 | `test_cursor_round_trips_to_the_same_rows_within_a_generation` | Phase 5 cursor property 1 |
| 1 | `test_paging_to_exhaustion_yields_no_duplicate_and_no_missing_clip_key` | properties 2 and 3 |
| 1 | `test_a_changed_corpus_generation_is_a_409_not_a_reordered_page` | no silent reorder |
| 1 | `test_an_expired_cursor_is_410_and_distinguishable_from_a_generation_change` | distinct failure codes |
| 1 | `test_cursor_paging_does_not_grow_queue_cost_with_the_corpus` | the readiness-budget invariant, reused |
| 1 | `test_healthz_still_does_no_corpus_work_with_pagination_enabled` | constraint 5 |
| 2 | `test_export_default_response_is_unchanged_in_release_2a` | the deprecation window is real |
| 2 | `test_paged_export_concatenation_equals_the_unbounded_export` | reproducibility for `ml-lifecycle` |
| 2 | `test_export_cursor_is_stable_across_appends_and_never_reemits_a_row` | append-only keyset property |
| 2 | `test_export_emits_deprecation_and_sunset_headers_until_the_flip` | the flip is announced |
| 3 | `test_auth_off_is_byte_identical_to_pre_auth_behaviour` | default-off claim |
| 3 | `test_log_mode_denies_nothing_and_records_what_it_would_deny` | safe measurement |
| 3 | `test_enforce_denies_export_without_scope_and_audio_without_scope` | L3 closed |
| 3 | `test_token_comparison_is_constant_time_and_token_never_appears_in_a_log_or_url` | credential hygiene |
| 3 | `test_identity_headers_from_an_untrusted_peer_are_still_ignored_under_enforce` | extends the existing spoof test |
| 3 | `test_enforce_with_empty_trusted_proxies_and_identity_headers_fails_closed` | the new fail-closed rule |
| 3 | `test_probe_path_is_reachable_without_credentials_in_every_mode` | readiness cannot be locked out |
| 4 | `test_unreachable_source_is_unknown_not_offline` | L4's worst case |
| 4 | `test_expired_never_observed_and_unknown_are_three_distinct_states` | absence is not a value |
| 4 | `test_liveness_payload_is_additive_to_todays_health_json` | compatibility |
| 5 | `test_threshold_digest_changes_when_any_threshold_changes` | P6-R3 satisfiable |
| 5 | `test_default_thresholds_produce_the_same_verdict_as_today` | no default changed |

**Migration/rollback tests** (stage 1–3, run against the *pair* of artifacts):
`test_regenerated_annotate_configmap_matches_the_checkout` (existing gate, must be re-run per
stage), `test_a_database_written_by_the_new_server_is_readable_by_the_previous_one` (no schema
change is introduced by any stage — this is the assertion that keeps it true), and
`test_rolling_back_the_configmap_restores_the_previous_response_shape`.

## 9. Rollout and rollback

Per stage, unchanged in form:

1. Back up `annotations.sqlite3` with the SQLite backup API (constraint 4) and verify the
   copy opens and counts rows.
2. Merge the checkout change **and** the regenerated `hear-annotate-code` ConfigMap in one
   commit; CI's `test_configmap_sync.py` is the gate.
3. Apply **only** `deploy/k8s/hear-annotate.yaml` (never `-f deploy/k8s/`); restart the single
   replica; confirm `/healthz` ready within the 2 s probe budget.
4. Soak with the new behaviour **disabled by default** (stages 2a, 3 `off`/`log`); read the
   stage-0 audit lines.
5. Flip a default only as its own dated release.

**Rollback:** for a default flip, revert one environment variable — no data migration exists
to reverse, which is deliberate: no stage changes the SQLite schema, adds a column, or
rewrites a row. For a code change, re-apply the previously generated ConfigMap bytes (they
are in git, stamped with the commit they were generated from) and restart. The annotation
database written by any stage remains readable by the immediately preceding server, and §8
holds a test to that effect.

**Blast radius if a stage goes wrong:** the tailnet labelling UI and one pod. No CronJob, no
node, no ingest path, no Postgres object and no frozen contract is touched by any stage.

## 10. What this does not do

* It does not authenticate the node HTTP surface (`SECURITY-REVIEW.md` finding 2) — firmware
  and fleet rollout, ADR 0006's lane.
* It does not change the heartbeat receiver's token comparison, its TTL, or its auth-optional
  startup — named in §2.3 so the ingest lane can pick them up.
* It does not fix R3, R11 or R14; stage 4 makes R3 *visible*, which is the most a reader may
  do about a writer defect.
* It does not enable, design or schedule a canonical read, and it consumes no Phase 2–6 gate.
  Phases 5 and 6 inherit a surface with a cursor contract, a bounded export and an explicit
  liveness verdict — which is strictly less that their comparators have to declare undefined.
  It also does not specify the canonical API: `phase6-operator-api-contract.md` owns that
  side, and where the two describe the same property (cursor totality and round-trip
  stability, bounded export, explicit `unknown`), this document's stages are the legacy
  surface's route to it, not a second contract.
* It does not remove the `unpkg.com` CDN dependency (L8). Recorded, unscheduled.

## 11. Decisions owed to an operator

Recorded, not decided. Stage 3 cannot be *implemented* past `log` mode without 1–3.

1. **Who may read clip audio and reviewer-identified labels?** Is tailnet membership the
   authorization boundary, or is a scope required inside it? (§6.3 assumes the latter,
   because governance §3 says so, but the tailnet ACL is not in this repository and may
   already encode a decision.)
2. **Is the tailnet identity header the authentication source of record**, or is a
   deployment-scoped bearer token the target rather than the stepping stone?
3. **Enforcement date and deprecation window.** `api-boundaries.md` says ≥ 90 days; this
   deployment has a handful of human users and may reasonably choose shorter — but it must
   choose, and the date belongs in the release notes, not in a comment.
4. **Retention class and destination of the read-audit log** (§7). It records who read what,
   which makes it `identity/access`-class, and governance §3 asks for it to be separated from
   acoustic content.
5. **Pseudonymisation key custody** for `HMAC(deployment_key, user_id)` (§7): if the key is
   lost, historic exports become uncorrelatable; if it leaks, the pseudonym is reversible by
   enumeration over a handful of reviewers.
6. **Export completeness path** (stage 2c): a job-plus-manifest export is the
   `api-boundaries.md` answer, but nothing in this deployment implements export jobs yet, and
   building one is materially larger than the rest of this plan.
