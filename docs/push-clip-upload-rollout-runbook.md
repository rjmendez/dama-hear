# Runbook — rolling out push-only chunked clip upload (`hear.ingest.clip.v1`)

What to check before flashing any node, how to roll it out one node at a time, how to tell it
worked, and how to put it back. The wire contract is [`phase4-push-clip-upload.md`](phase4-push-clip-upload.md);
the routing decision is [`phase4-clip-upload-routing.md`](phase4-clip-upload-routing.md). This
document is only the operational procedure.

## 0. Preconditions — already true in production as of this writing

| # | Precondition | How it was verified |
|---|---|---|
| P1 | `hear-heartbeat`'s live ConfigMap/Deployment ship `hear/ingest/clipupload.py` and mount it at `/app/hear/ingest/clipupload.py` | `deploy/k8s/gen_configmap.py hear-heartbeat-code` regenerated, applied server-side, pod rolled out `Running 1/1` |
| P2 | `hear-mqtt-bridge` ships the same fragment (it imports the same receiver module) | same regen/apply; note `hear-mqtt-bridge` was already down before this change on an unrelated missing `hear-mqtt-bridge-tls` secret — not caused by, and not fixed by, this work |
| P3 | A public, TLS-terminated origin exists for the clip routes | Cloudflare Tunnel `hear-clip-ingest` → `http://hear-heartbeat.dama.svc.cluster.local:5051`, DNS CNAME `hear-clip.floppydicks.net`, tunnel `status: healthy` |
| P4 | AWS API Gateway serves `/v1/ingest/clips` and `/v1/ingest/clips/{proxy+}` on the fielded custom domain, without touching the existing `/ingest/batch` route | `terraform apply` of only `module.ingest_api.{aws_apigatewayv2_integration,aws_apigatewayv2_route}.hear_clip_*` (targeted apply — the full plan carries unrelated pre-existing drift in other modules that this change does not touch and this runbook does not authorize applying) |
| P5 | The fleet's push credential has `clip:write` alongside its existing `ingest:write` | `hear-batch-credentials` secret, principal `dama-fleet-push`, updated in place; `hear-heartbeat` restarted to pick it up |
| P6 | The full protocol (init → chunk → complete → promote) works end to end through the real path | Live proof: a synthetic no-speech clip pushed through AWS → Cloudflare Tunnel → k3s with a throwaway test credential, scored by the real Silero VAD, promoted to `/pool/corpus/clips/unanchored/<node>/...`, then the test clip and its index row were removed and the test credential revoked |

P6 is not a standing fixture — it was a one-time proof performed by hand and cleaned up
afterward. Re-running it (rather than trusting `tests/test_clip_upload_contract.py` and
`tests/test_hear_heartbeat_receiver.py`, which cover the same paths hermetically) is only
warranted after a change to the routing chain itself, not for ordinary firmware rollout.

## 1. What is *not* yet true

* No fielded node runs firmware with the chunked live-upload path. `firmware/hear_node/hear_node.ino`
  compiles clean for both fielded board variants (`XIAO_ESP32S3`, generic `esp32s3`) and passes
  its host tests, but has not been flashed. The SD-backed spool/drain path is untouched and still
  the only thing running on any node today.
* `hear-drain`'s pull cron is still the only way clips reach the pool until nodes are flashed.

## 2. Rollout — one node, then the fleet

1. Pick one low-traffic node as the canary (not `rankine` — it is the one being actively
   soak-tested/disrupted for other diagnostics right now).
2. Flash it with the build containing this change. Confirm over MQTT/heartbeat that it reports
   the new firmware version.
3. Watch its `/status` endpoint (`hear-mqtt-bridge` or direct node poll, per existing heartbeat
   tooling) for the new `clip_push` counters:
   `{ok, fail, init_fail, chunk_fail, complete_fail, ring_lost, attempts, last_code}`.
   `ok` incrementing with `fail`/`*_fail` at zero over a full boot cycle is the signal to proceed.
4. Cross-check server side: `hear-heartbeat`'s durable-store `pending_records` and the pool's
   `clips/index.jsonl` should show new rows with `"clip_why": "push-upload"` and `"record_key":
   "clip-upload:<upload_id>"` for that node, at a rate matching its detection cadence.
5. Let the canary run at least one full day/night cycle (day and night acoustic conditions
   differ) before flashing the rest of the fleet in small batches, watching the same two signals
   after each batch.
6. Once the whole fleet is flashed and quiet on `clip_push.fail`/`*_fail` for a few days, `hear-drain`'s
   cron can be considered for removal on its own schedule — that is a separate decision, not part
   of this rollout, and this document does not authorize it.

## 3. Rollback

Rollback is per-node and does not touch the server or AWS/Cloudflare infrastructure, which is
additive and harmless to leave in place with no traffic:

* **Single misbehaving node:** re-flash it with the prior firmware build. The SD-backed spool/drain
  path was never modified or removed, so this is a plain revert with no server-side coordination
  needed.
* **Fleet-wide problem:** revoke the push path without touching firmware, by removing `clip:write`
  from the `dama-fleet-push` credential in the `hear-batch-credentials` secret and restarting
  `hear-heartbeat`. Firmware will get `403 forbidden` on `clip_init` and its own `clip_push_fail`
  counter will climb, but nothing crashes — `clip_pump()` only ever falls back to `CLIP_NOCARD`
  when there is neither SD nor ring, so a `403` is a push failure, not a firmware fault, and the
  SD-backed path keeps working unaffected on nodes that have an SD card.
* **Server-side regression:** `hear-heartbeat`'s rollback path is unchanged from
  [`hear-heartbeat-cutover-runbook.md`](hear-heartbeat-cutover-runbook.md) — this change shipped
  through the existing ConfigMap-mount path (P1/P2 above), not the proposed image cutover, so
  reverting is `git revert` the commit that added `hear_ingest_clipupload.py` to
  `HEARTBEAT_CODE`/`MQTT_BRIDGE_CODE`, regenerate both ConfigMaps, `kubectl apply --server-side`,
  and roll back to the previous ConfigMap generation if a faster revert is needed than a new
  regen/apply cycle.
* **Infra teardown (last resort, should not be needed):** delete the `hear-clip-tunnel` Deployment/
  Secret in the `dama` namespace, the Cloudflare Tunnel `hear-clip-ingest` and its DNS record, and
  the four `module.ingest_api.*.hear_clip_*` Terraform resources (`terraform destroy -target=...`
  for exactly those four, mirroring the targeted apply in P4). The existing `/ingest/batch` route
  and `api.botnet.floppydicks.net` custom domain are never touched by any of this.
