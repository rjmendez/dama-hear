# hear-drain — pooled sketch ingestion

Runs in k3s (`namespace: dama`), **not** on a workstation: the pool has to keep being fed while
nobody is logged in, and a laptop is not that.

```
python3 deploy/k8s/gen_configmap.py > deploy/k8s/hear-drain-code.yaml
kubectl apply -f deploy/k8s/hear-drain-code.yaml -f deploy/k8s/hear-drain.yaml
```

| object | what |
|---|---|
| `hear-pool` PVC | the corpus (`/pool/corpus`), the raw archive (`/pool/corpus/raw`), and a cached numpy (`/pool/pylib`) |
| `hear-drain` CronJob | every 15 min: fetch, archive, ingest |
| `hear-drain-check` CronJob | hourly: fails if a sensor's last SUCCESS is stale |

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
  the cluster. The generator refuses to ship an incomplete import closure and stamps the commit
  it was built from into the object's annotations.
