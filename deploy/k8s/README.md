# hear-drain / hear-score — pooled sketch ingestion and scoring

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
```

| object | what |
|---|---|
| `hear-pool` PVC | the corpus (`/pool/corpus`), the raw archive (`/pool/corpus/raw`), the scores (`/pool/corpus/scores`), and a cached numpy (`/pool/pylib`) |
| `hear-drain` CronJob | every 15 min: fetch, archive, ingest |
| `hear-drain-check` CronJob | hourly: fails if a sensor's last SUCCESS is stale |
| `hear-score` CronJob | 4x/hour: score every unscored pooled sketch, count every refusal |
| `hear-score-check` CronJob | hourly: fails if scoring is not flowing |

The PVC is declared **once**, in `hear-drain.yaml`. `hear-score.yaml` mounts it and declares no
storage of its own — two manifests claiming one PVC is how they come to disagree about its size.

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
scorer that swallowed those would report a quiet night from a corpus it could not read. Measured
on one real 1038-record pool: **955 refused (92.0 %), every one on the legacy `nyquist` layout** —
and split by day, 0 % scorable in one partition and 100 % in the next. Refusals are therefore
counted by reason, by node **and by day partition**; a scalar describes neither of those.

⚠️**Refusal is a step function, so there is no threshold to tune.** `valid_bands` holds at 15 down
to fs 13678 Hz and is 14 below it — 0 % refused right up to 100 % refused. `--check` fails on a
`node|day|reason` bucket that has **never been seen before**, not on a percentage. The first run
is exempt: it establishes the census.

⚠️**Health does not look at the scores.** A normal night is P at the floor: one measured
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
| `claim.evidence_of_absence: false` | no true positive has ever been scored on the night population, so there is **no measured detection rate** for it |

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
  the cluster. The generator refuses to ship an incomplete closure and stamps the commit it was
  built from into the object's annotations.
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
- **`/pool/pylib` is shared between the two workloads.** A guard that only tests whether the
  numpy directory exists means whichever workload reaches an empty PVC first decides the version
  and the other silently uses what it finds — both pins then read as discipline while enforcing
  nothing. `hear-score` asserts the version and prints the resolved one.
- ⚠️**`deploy/k8s/hear-drain.yaml` on this branch is behind the cluster.** The live `hear-drain`
  carries `--phone-corpus /pool/sketch_corpus` (a cross-repo contract with `dama-sketch-corpus`'s
  `SKETCH_CORPUS_OUT_DIR`) and the live `hear-drain-check` captures `rc=$?`; the checked-in file
  has neither. Applying it would delete the phone leg and revert the staleness gate to one that
  always exits 0. Apply hear-score's two files by name and leave hear-drain's alone.
