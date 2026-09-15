# hear-drain / hear-score / hear-tag / hear-heartbeat / hear-mqtt-bridge — pooled ingest and live telemetry

Runs in k3s (`namespace: dama`), **not** on a workstation: the pool has to keep being fed while
nobody is logged in, and a laptop is not that.

⚠️**Two independent bundles. Apply one bundle's files by NAME — never `-f deploy/k8s/`.** A
ConfigMap and a CronJob are each written WHOLE, so a directory apply pushes whatever state every
file in it happens to be in. The live `hear-drain` CronJob has already been found carrying an
argument the checked-in file lacked; a directory apply would have deleted it silently.

```
# hear-drain
python3 deploy/k8s/gen_configmap.py > deploy/k8s/hear-drain-code.yaml
kubectl apply -f deploy/k8s/hear-drain-code.yaml -f deploy/k8s/hear-drain.yaml

# hear-score
python3 deploy/k8s/gen_configmap.py hear-score-code > deploy/k8s/hear-score-code.yaml
kubectl -n dama get configmap hear-score-code -o yaml     # NotFound on a first apply
kubectl apply -f deploy/k8s/hear-score-code.yaml -f deploy/k8s/hear-score.yaml
kubectl -n dama create job --from=cronjob/hear-score hear-score-manual-1   # then watch it

# hear-tag  ⚠️SHIPS SUSPENDED. Do NOT unsuspend before the Phase-3 gate in
#           docs/acoustic-stack.md §0.4 -- >=300 stored clips over >=3 days, >=30 of them
#           LISTENED TO by a human and written up in docs/clip-calibration-<date>.md, and
#           --max-silence-frac set from that measured distribution.
python3 deploy/k8s/gen_configmap.py hear-tag-code > deploy/k8s/hear-tag-code.yaml
kubectl apply -f deploy/k8s/hear-tag-code.yaml -f deploy/k8s/hear-tag.yaml

# hear-heartbeat  ⚠️THIS IS STILL THE APPLIED PATH, AND IT IS ALSO THE ROLLBACK PATH.
#                 deploy/k8s/hear-heartbeat.proposed.yaml is the Phase 1.5 immutable-image
#                 cutover for this workload -- proposed, not applied, and unappliable until
#                 its digest is published and recorded. See the section below.
python3 deploy/k8s/gen_configmap.py hear-heartbeat-code > deploy/k8s/hear-heartbeat-code.yaml
kubectl apply -f deploy/k8s/hear-heartbeat-code.yaml -f deploy/k8s/hear-heartbeat.yaml

# hear-mqtt-bridge
python3 deploy/k8s/gen_configmap.py hear-mqtt-bridge-code > deploy/k8s/hear-mqtt-bridge-code.yaml
kubectl apply -f deploy/k8s/hear-mqtt-bridge-code.yaml -f deploy/k8s/hear-mqtt-bridge.yaml

# hear-annotate  ⚠️REGENERATED IN PLACE, NOT REDIRECTED. Its ConfigMap lives INSIDE the
#                workload manifest -- one file carrying ConfigMap + Deployment + Service --
#                so the generator rewrites that one document and leaves the rest alone.
#                Redirecting stdout into the file would truncate the other two documents,
#                which is why this command has no `>`.
#
#                ⚠️`/app/server.py` IS A subPath MOUNT, so the kubelet never propagates a
#                ConfigMap update into the running pod: an apply with no restart is a no-op to
#                the process. Restart by SCALING, never by rolling: one RWO PVC, one SQLite
#                writer, and `maxSurge: 25%` would start the new pod before the old one exits.
#
#                  kubectl scale deploy -n dama hear-annotate --replicas=0   # wait for deletion
#                  kubectl apply -f deploy/k8s/hear-annotate.yaml
#                  kubectl scale deploy -n dama hear-annotate --replicas=1
#
#                This apply also carries the Deployment document, so it resets any live
#                readiness patch. Readiness is `GET /healthz` -- a bounded check of the
#                annotation store -- because the old `GET /api/queue?limit=1` probe measured a
#                full corpus parse and took the pod NotReady as the corpus grew.
python3 deploy/k8s/gen_configmap.py hear-annotate-code
kubectl apply -f deploy/k8s/hear-annotate.yaml

# hear-tdoa  ⚠️--server-side ON THE CODE BUNDLE, AND IT IS NOT A STYLE PREFERENCE.
#            A plain apply is REJECTED by the API server, not merely discouraged:
#              The ConfigMap "hear-tdoa-code" is invalid: metadata.annotations:
#              Too long: may not be more than 262144 bytes
#            Client-side apply stores the whole submitted object in
#            kubectl.kubernetes.io/last-applied-configuration, and this bundle carries the
#            entire solve stack -- 478,902 B serialised, 1.8x that 256 KiB annotation cap
#            (and still inside the 1 MiB object cap, which server-side apply does NOT lift).
#            Each generated -code.yaml states its own mode on line 1 and in the
#            dama-hear/apply-mode annotation; gen_configmap.py prints the command on stderr.
#            The MANIFEST is small and applies normally.
python3 deploy/k8s/gen_configmap.py hear-tdoa-code > deploy/k8s/hear-tdoa-code.yaml
kubectl apply --server-side -f deploy/k8s/hear-tdoa-code.yaml
kubectl apply -f deploy/k8s/hear-tdoa.yaml
```

⚠️**A bundle that grows past 256 KiB changes how it must be applied, silently.** `hear-drain-code`
is at 236,037 B — 90% of the cap — so one more module in its import closure moves it across and
the apply that has always worked starts failing. That is why the mode is computed per bundle by
`gen_configmap.apply_mode()` from the SERIALISED OBJECT and written into the file, rather than
being remembered here.

| object | what |
|---|---|
| `hear-pool` PVC | the corpus (`/pool/corpus`), the raw archive (`/pool/corpus/raw`), the scores (`/pool/corpus/scores`), and a cached numpy (`/pool/pylib`) |
| `hear-drain` CronJob | every 15 min: fetch, archive, ingest |
| `hear-drain-check` CronJob | hourly: fails if a sensor's last SUCCESS is stale |
| `hear-score` CronJob | 4x/hour: score every unscored pooled sketch, count every refusal |
| `hear-score-check` CronJob | hourly: fails if scoring is not flowing |
| `hear-tag` CronJob | **suspended.** 2x/hour: YAMNet over the collected clips, counting every refusal by reason. Touches the PVC and never a node — the ESP32 serves one client at a time |
| `hear-tag-check` CronJob | **suspended.** hourly: fails if tagging is not flowing, or if the pinned model sha256 stopped verifying |
| `hear-heartbeat-state` PVC | the phase-0 durable heartbeat/event SQLite outbox (`/state/*.sqlite3`) for the direct HTTP receiver |
| `hear-heartbeat` Deployment | token-gated HTTP receiver: durable append, then Redis cache update; rollback is explicit via `HEAR_DURABLE_STORE=none` + removing the state PVC mount |
| `hear-mqtt-bridge-state` PVC | the phase-0 durable heartbeat/event SQLite outbox for the MQTT/AWS relay path |
| `hear-mqtt-bridge` Deployment | MQTT relay consumer: durable append, then Redis cache update; same Redis contract, same rollback switch |

The PVC is declared **once**, in `hear-drain.yaml`. `hear-score.yaml` and `hear-tag.yaml` mount
it and declare no storage of their own — two manifests claiming one PVC is how they come to
disagree about its size. The tag lane installs into `/pool/pylib-tag`, a **separate** target dir
from the drain's `/pool/pylib`, so a tagger dependency cannot break the collector that feeds it;
the 16 MB YAMNet weights live at `/pool/models/yamnet` and are verified against a sha256 pinned
in `tools/hear_tag.py` before anything is tagged.

## hear-score — the consumer that was missing

`modules/supersonic/classify.score_sketch` was trained, versioned and shipped with **zero callers
outside its own tests**. Every model in `modules/supersonic/` had been fitted, exported with its
AUC, and never once applied to a byte the fleet produced. The consequence was not wrong scores —
it was that nobody could say what any detection *was*.

⚠️**It scores the POOL, not a live topic.** `dama-sketch-corpus` is connected and subscribed to
`dama/+/acoustic_sketch` and has been logging "0 messages": the phone port is not in a release
and the hugbot emitter is an unmerged PR. A live-MQTT scorer would starve on day one. The phone
lane is nonetheless already wired *through this store* — the corpus worker lands JSONL on the PVC
and `hear-drain --phone-corpus` ingests it into the same pool — so it degrades to zero input by
construction, and `by_source` reports `phone: 0` as a number rather than as an absence.

⚠️**Refusals are the product, not an error path.** `score_sketch` refuses rather than pads, and a
scorer that swallowed those would report a quiet period from a corpus it could not read. Measured
on one real 1038-record pool: **955 refused (92.0 %), every one on the legacy `nyquist` layout** —
and split by day, 0 % scorable in one partition and 100 % in the next. Refusals are therefore
counted by reason, by node **and by day partition**; a scalar describes neither of those.

⚠️**Refusal is a step function, so there is no threshold to tune.** `valid_bands` holds at 15 down
to fs 13678 Hz and is 14 below it — 0 % refused right up to 100 % refused. `--check` fails on a
`node|day|reason` bucket that has **never been seen before**, not on a percentage. The first run
is exempt: it establishes the census.

⚠️**Health does not look at the scores.** A normal period is P at the floor: one measured
population scored 437/437 non-zero with exactly 1 above 0.5, another peaked at 1.2e-5 over 83
rows. A gate asking "did anything score high" fires on a correct result. The gates are throughput,
refusals, torn lines, the accounting invariant and staleness; the class distribution is printed
under `OBSERVATION, not a gate`.

⚠️**An empty read is a failure, not a pass.** Point `--pool` at `/pool` instead of `/pool/corpus`
and every growth-conditioned gate passes vacuously on 0 records. `--check` fails outright on
`records_seen == 0` and on a stale heartbeat, independently of what the pool looks like — the
shape `hear_drain.check()`'s `if not sensors: return 1` already established.

### What a score row says, and what it does not

`/pool/corpus/scores/<day>/<source>.jsonl`, one `hear.sketch_score.v1` row per pool record, plus
`scores/model_card.json` (the prose provenance, once) and `state/score_heartbeat.json`. Each row
carries `p`, **`z`** and `z_terms{bias, level, shape}` — P saturates and z does not, and the
inter-node level offset is additive only in z. Rows are ~1.3 kB (≈0.7 GB/yr at the measured
62.8 records/h); the card is what keeps the prose off every row.

Every row, **scored or refused**, carries the model's identity and its AUC — never bare:

| field | why it is there |
|---|---|
| `auc_nested_grouped_cv` | copied from the model file, never retyped |
| `auc_measured_at_fs_hz: 48000` | ⚠️the pool is **16 kHz**; that figure is a 48 kHz one |
| `auc_cross_rate_48k_to_16k: 0.9473` | what this repo measured for *exactly* this operation (`hear/corpus.py:283-285`) |
| `auc_is_optimistic: true` | CV grouped by 3 s against a measured 22.4 s decorrelation lag |
| `claim.level_calibrated: false` | `ref_db` is not SPL; a float-vs-int16 producer is 90.31 dB = 53.8 logits out |
| `claim.comparable_across_nodes: false` | measured 28.4 dB between two identical nodes = 16.9 logits, vs a 7.37 dB p0.1–p0.9 range |
| `claim.prior_applied: false` | the corpus is 43.9 % shots; the card carries the offset to correct it |
| `claim.evidence_of_absence: false` | no true positive has ever been scored on the field population, so there is **no measured detection rate** for it |

## Two stores, on purpose

`dets.csv` rows exist only where the impulse gate fired. `scene.csv` carries a row every
~1.024 s regardless, and that is the corpus this project is actually about — dama-hear is not a
gunshot project, and a pool built only from gated events structurally cannot represent the
ambient world. They are stored separately so `records` keeps meaning "gated events" and every
ratio taken from it stays true.

| store | path | cadence | reader |
|---|---|---|---|
| sketches | `records/<day>/<source>.jsonl` | on gate | `Pool.records()` -> `hear.corpus.Record` |
| scene | `scene/<day>/<node>.jsonl.gz` | ~1.024 s | `Pool.scene()`, `Pool.scene_matrix()` |

⚠️**Scene is fetched by tail, not whole.** It grows without bound (11 MB seen, 2-4 min to pull),
so each run takes the last `SCENE_TAIL_BYTES` — about 2.3 hours of rows. The overlap costs
nothing because ingest is content-addressed, and the leading fragment that a byte-range fetch
always starts with is dropped AND counted as `partial_first_line`. A gap longer than that window
is real loss, which is what `hear-drain-check` exists to make visible.

Measured: **112 B/row compressed**, so about **19 MB/day** for two nodes — roughly 8 months in
the 5 Gi PVC.

## Reading the pool

```
kubectl exec -n dama <pod> -- sh -lc \
  'PYTHONPATH=/pool/pylib:/app python /app/tools/hear_drain.py --pool /pool/corpus --stats'
```

`hear.pool.Pool.records()` returns `hear.corpus.Record`, so `feature_matrix`, `aligned_matrix`
and the solvers read the pool with no adapter.

## Things that have already gone wrong here

- **The drain's exit status is not a health signal.** It succeeds whenever HTTP succeeds, so a
  node that stopped detecting stays green forever. `hear-drain-check` is the thing to alert on.
- **A server dry-run does not catch a LimitRange.** `kubectl apply --dry-run=server` passed a
  CronJob whose pods the namespace then refused (`minimum cpu usage per Container is 100m`),
  because the limit applies at POD creation. Create one job from the CronJob and watch it run.
- **The ConfigMap is written whole.** Regenerate it with `gen_configmap.py`; never edit a key in
  the cluster. The generator refuses to ship an incomplete closure and stamps both the source
  commit and a content digest into the object's annotations. The digest is the authoritative
  bundle identity because it remains valid when GitHub squash-merges a branch.
- **The import audit did not audit imports.** It resolved only `hear/<name>.py`, from three
  `ImportFrom` shapes, with no `ast.Import` branch at all — so `from modules.supersonic import
  classify` passed it silently and a workload importing the classifier would have generated
  cleanly and raised `ModuleNotFoundError` only in the cluster, which is the exact failure the
  generator's docstring claims cannot happen.
- **And no import audit can see a model file.** `classify.FLEET_SKETCH_MODEL` is an
  `os.path.join` against `__file__`, not an import: with the model dropped from the file list the
  old `check()` passed without complaint and the entry point then died on `FileNotFoundError`.
  Bundles now declare `data` files and the audit resolves every `.json` a shipped module opens
  against its own directory. ⚠️That `__file__`-relative path also **pins the mount layout** — the
  model must be a sibling `subPath` of `classify.py` or the default points at nothing.
- **Nothing in `tests/` compared a code ConfigMap against the checkout**, which is why the
  `hear-drain` drift went unnoticed until review. `tests/test_hear_score.py` now regenerates both
  bundles and compares the `data` mapping — ⚠️`data` only: the commit annotation is
  `git rev-parse --short HEAD` plus a `-dirty` flag, so it changes on every commit and flips in
  any tree with uncommitted work, i.e. in the exact state a developer runs pytest in.
- ⚠️**A ConfigMap that was not in `BUNDLES` was in no guard at all.** `hear-annotate`'s
  `server.py` was hand-embedded in its manifest, so every test above walked past it: they
  iterate `BUNDLES`. It drifted at commit `fa5a589` — `tools/hear_annotate/server.py` gained
  path-traversal, proxy-trust, submission-idempotency and WAV-frame hardening, and the embedded
  copy did not — so the pod that owns `annotations.sqlite3`, the only human-labelled ground
  truth in the system, ran the pre-hardening code while the checkout, the tests and every
  reviewer read the hardened one. It is now generated from the checkout
  (`EMBEDDED_BUNDLES`) and guarded by `tests/test_configmap_sync.py::TestTheEmbeddedConfigMaps`,
  which requires the sources to reproduce the committed manifest **byte for byte**.
  ⚠️**Applying it is a deliberate act, not a formality**: the regenerated ConfigMap carries the
  hardened server, which adds the `submission_id` column (additive `ALTER TABLE`, existing rows
  untouched) and stops trusting `tailscale-user-*` / `x-webauth-user` headers from clients that
  are not listed in `HEAR_ANNOTATE_TRUSTED_PROXIES` — unset today, so attribution becomes
  `client:<ip>` until the proxy address is configured. Back up `annotations.sqlite3` with the
  SQLite backup API (never a raw copy of a live WAL) before applying.

- **`/pool/pylib` is shared between the two workloads.** A guard that only tests whether the
  numpy directory exists means whichever workload reaches an empty PVC first decides the version
  and the other silently uses what it finds — both pins then read as discipline while enforcing
  nothing. `hear-score` asserts the version and prints the resolved one.
  ⚠️This, and the runtime `pip install` that produces it, is what
  [`deploy/images`](../images/README.md) exists to remove: digest-pinned base images whose
  dependencies are resolved once, at build time, from a hash-locked closure. Nothing in this
  directory has been migrated yet — the images are the substrate, not a cut-over.
- ⚠️**`deploy/k8s/hear-drain.yaml` on this branch is behind the cluster.** The live `hear-drain`
  carries `--phone-corpus /pool/sketch_corpus` (a cross-repo contract with `dama-sketch-corpus`'s
  `SKETCH_CORPUS_OUT_DIR`) and the live `hear-drain-check` captures `rc=$?`; the checked-in file
  has neither. Applying it would delete the phone leg and revert the staleness gate to one that
  always exits 0. Apply hear-score's two files by name and leave hear-drain's alone.

## Phase 1.5 — the immutable-image pilot (`hear-heartbeat`)

`deploy/k8s/hear-heartbeat.proposed.yaml` is the same workload delivered as an image instead of a
ConfigMap mount plus a `pip install` at pod start. It is **not applied**, and `kubectl apply` of
it fails today on purpose: its `image:` is the all-zero digest sentinel, because a pull request
publishes nothing and the real digest is recorded only after `main` builds it
(`deploy/images/service/digests.txt`).

⚠️**`hear-heartbeat.yaml` stays as it is, and stays applied.** The two files are the cutover and
its rollback. `tests/test_service_images.py` parses both and fails if they differ by anything
other than the image, the entrypoint and the `code`/`deps` volumes — `hostNetwork`, `hostPort`
5051, the Service, all nine env vars including both `secretKeyRef`s, both probes, resources,
`strategy: Recreate`, the `state` PVC mount and the root uid are identical by assertion.

⚠️**ConfigMap `hear-heartbeat-code` is not deleted by the cutover.** It stays applied and
unreferenced, stays generated by `gen_configmap.py`, and stays covered by the sync test. It is
retired in step 9 of `docs/worker-packaging.md`, never in a cutover.

⚠️**The container keeps running as root.** `/state/heartbeat-receiver.sqlite3` and its WAL were
created by a root pod and `local-path` volumes get no `fsGroup` ownership management, so adopting
the base image's uid 65532 here would be a write failure on a PVC after cutover. That is a
separate, per-volume change.

The full procedure — digest recording, pre-pull, apply, the ten pieces of live acceptance
evidence gate 1 requires, and the rollback — is in
[`deploy/images/service/README.md`](../images/service/README.md). Rollback, for reference, is one
command against a file this repository never stopped carrying:

```sh
kubectl -n dama apply -f deploy/k8s/hear-heartbeat.yaml    # or: kubectl -n dama rollout undo deployment/hear-heartbeat
kubectl -n dama rollout status deploy/hear-heartbeat --timeout=180s
```

## Phase-0 durable heartbeat/event outbox

`tools/hear_heartbeat_receiver.py` now has a pluggable durability boundary. The manifests above
set `HEAR_DURABLE_STORE=sqlite` and mount a dedicated RWO PVC at `/state`, so each accepted
heartbeat/event is committed to SQLite WAL storage before Redis is touched. Redis remains the
compatibility cache for current consumers, and startup replays any rows whose cache publish never
recorded a success.

Rollback is explicit: set `HEAR_DURABLE_STORE=none` and remove the `/state` PVC mount to restore
the previous Redis-only behavior. PostgreSQL is the next step once a shared dependency and DDL
ownership are ready: keep the same `DurableRecordStore` seam, move `durable_records` and
`cache_attempts` there, and leave the Redis contract unchanged during the cut-over.

### Refused messages are quarantined, not dropped

Validation used to happen entirely *before* persistence, so a message the receiver did not
understand left nothing behind but a log line and an in-memory counter (migration risk register
R4). Anything that identifies itself as hear telemetry and is then refused now gets a row in
`refused_messages` (body as received, telemetry path, source, reason, receipt time) in the same
SQLite file, kept apart from `durable_records` and never published to Redis — a quarantined
message must never be mistaken for an accepted one. `/healthz` reports the quarantine under its
own `refusals` key rather than inside `durable_store`, because `durable_store` is the contract
every durable backend has to implement key for key.

The quarantine is a **bounded ring**, not an append-only ledger: it accepts input that by
definition failed validation, so any publisher reachable on the topic can mint unbounded distinct
rejected bodies, and age alone is not a capacity bound on a 5Gi volume shared with accepted
telemetry.

| Knob | Default | What it bounds |
|---|---|---|
| `HEAR_REFUSAL_MAX_BODY_CHARS` | `8192` | characters stored per refusal |
| `HEAR_REFUSAL_MAX_ROWS` | `5000` | rows in the quarantine |
| `HEAR_REFUSAL_MAX_BYTES` | `16777216` | total quarantined body bytes |
| `HEAR_REFUSAL_RATE_LIMIT` | `30` | refusals stored per source per window (`0` disables) |
| `HEAR_REFUSAL_RATE_WINDOW_S` | `60` | length of that window |
| `HEAR_DURABLE_RETENTION_DAYS` | `30` | age sweep, shared with acknowledged records |

The caps are enforced inside the same transaction as the insert, and eviction always takes the
oldest row of whichever `(source, device)` holds the most — so a flooding publisher erases its
own history first and cannot evict another node's evidence. Nothing on that path can reach
`durable_records` or `cache_attempts`: a refusal flood can cost older *refusals*, never accepted
telemetry, and never an ingest failure. The rate limiter is in memory, ahead of the disk, so a
flood does not become one synchronous fsync per hostile message.

Two things are deliberately *not* quarantined: unauthenticated requests, and foreign or
undecodable traffic on the shared `dama/+/telemetry` topic. Neither is recoverable hear data, and
storing either would let an unrelated (or hostile) publisher write to the telemetry volume.

The Postgres generation keeps all of this (`deploy/postgres/migrations/0006_hear_durable_refusals.sql`):
same bounded ring, counters in `hear.durable_counters`, `hear.refusal_health_snapshot()`, tenant
RLS, and an audit view that omits the quarantined body.

### Legacy `hear/event` bodies

Firmware older than the clock-state rollout emits `hear/event` with **no `time` block at all**
while its heartbeats carry `"time":{"valid":<bool>}`. Those events are now canonicalized instead
of refused: the block is reconstructed from the `ts` the same firmware derived from the same
`time_valid` flag, and marked `"source":"legacy_pre_clock_state_firmware"` so an inferred block
is distinguishable from one a node actually sent. A node that has no wall clock is detected by
its `uptime_s * 1000 + 1` fallback timestamp, so the upstream ingest's restamp of a null `ts`
cannot be laundered into a valid wall-clock time. A `time` block that is present but malformed,
or a legacy event whose `ts` is neither a string nor null, is still refused (and quarantined).
