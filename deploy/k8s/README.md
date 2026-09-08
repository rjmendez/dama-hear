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
