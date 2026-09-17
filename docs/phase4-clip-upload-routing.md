# Phase 4: making `hear.ingest.clip.v1` fleet-reachable

Companion to [`phase4-push-clip-upload.md`](phase4-push-clip-upload.md). That document is the
wire contract; this document is the routing decision for how a fielded node actually reaches
`POST /v1/ingest/clips` and friends without reflashing and without reusing the incompatible
Android/gotchi Lambda semantics on `POST /ingest/batch`.

## The constraint that decides this

Firmware pushes over `WiFiClientSecure` to `push_host_runtime()` (`prov.push_host`, provisionable
via NVS with no reflash, defaulting to `HEAR_PUSH_HOST` = `api.botnet.floppydicks.net`), and
validates the TLS chain against `HEAR_PUSH_CA_CERT` in `firmware/hear_node/hear_push_ca.h` — which
is **compiled in** as Amazon Root CA 1, with `HEAR_PUSH_TLS_INSECURE` defaulting to `0` in every
fielded v0.1.10 build. There is no NVS-provisionable escape from that pin.

That rules out the two tempting shortcuts:

- **Point `prov.push_host` straight at a home-LAN address.** `hear-heartbeat` serves plain HTTP on
  hostPort 5051 today; even a TLS-fronted LAN endpoint would present a cert that does not chain to
  Amazon Root CA 1 unless it is itself issued through ACM. A fielded node's handshake fails closed
  (as designed — Alert 3), and getting a different trust anchor onto the fleet needs a reflash,
  which this task is explicitly trying to avoid.
- **Reuse AWS's existing `POST /ingest/batch`.** That route is `AWS_PROXY` into
  `aws/lambda/batch_ingest/handler.py` (dama-gotchi), which expects
  `{"device_id": ..., "messages": [...]}`, fans out to SQS + S3, and returns 202 with no synchronous
  per-chunk result. `hear.ingest.clip.v1` is a synchronous, stateful, chunked upload: a chunk `PUT`
  must get back `409 chunk_conflict` on a bytes mismatch, `GET` must return current status, and
  `complete` must not return until purge/promotion has run. Async fan-out cannot produce any of
  that, independent of the schema mismatch the task description already flags.

## Recommendation

Keep `hear.ingest.clip.v1` on the **same** custom domain the fleet already trusts
(`api.botnet.floppydicks.net`, ACM-issued, chains to Amazon Root CA 1), so no fielded node needs a
new CA or a new host. Add new, additive routes to the existing `aws_apigatewayv2_api.ingest` HTTP
API in `dama-gotchi/aws/modules/ingest_api`:

```
POST /v1/ingest/clips              → HTTP_PROXY → <origin>/v1/ingest/clips
ANY  /v1/ingest/clips/{proxy+}      → HTTP_PROXY → <origin>/v1/ingest/clips/{proxy}
```

`HTTP_PROXY` (not `AWS_PROXY`) is a plain request/response passthrough: no Lambda, no SQS, no S3,
no Android batch schema anywhere in the path. The origin is whatever already implements the
contract in `hear/ingest/clipupload.py` — i.e. `hear-heartbeat` once `push-clip-server-api`
implements the routes server-side. `POST /ingest/batch` and `POST /v1/ingest/batches` are
untouched: same Lambda, same integration, same route, zero edits.

This is implemented, **inert by default**, in
`dama-gotchi/aws/modules/ingest_api/{main.tf,variables.tf,outputs.tf}`, following the repo's own
`api_enroll_secret`-style convention: every new resource is `count`-gated on a new
`hear_clip_origin_url` variable defaulting to `""`, so merging it changes nothing until an operator
sets the variable and runs `terraform apply`. `terraform validate` passes against this module in
isolation (verified in this task).

## What is still a blocker, and why it cannot be faked here

1. **`hear_clip_origin_url` has no value yet.** It must be the base URL of an
   already-TLS-terminated, internet-reachable origin for `hear-heartbeat`'s clip routes — e.g. a
   Cloudflare Tunnel hostname, or a DDNS name behind a router port-forward with its own valid cert.
   Provisioning that origin is a network/router/DNS operation outside this repo and outside
   anything Terraform here can create. **Action needed from an operator with router/DNS access.**
2. **`terraform apply` needs live AWS credentials** this environment does not have and should not
   be given. The exact change to apply, once (1) is done, is: set `hear_clip_origin_url` in the
   `ingest_api` module block in `dama-gotchi/aws/main.tf`, then
   `terraform plan`/`apply` inside `dama-gotchi/aws`. **Action needed from whoever holds the AWS
   credentials.**
3. **The origin itself must exist and pass privacy purge.** `push-clip-server-api` (implementing
   `/v1/ingest/clips*` against `hear/ingest/clipupload.py` inside `tools/hear_heartbeat_receiver.py`)
   is a separate, already-tracked, in-progress workstream. Routing changes here have nothing to
   forward to until that lands and is deployed via `deploy/k8s/hear-heartbeat.yaml` (do **not**
   apply `deploy/k8s/hear-mqtt-bridge-code.yaml` for this — the MQTT bridge is the old async
   telemetry relay and is not part of this synchronous path at all).
4. **DNS/Cloudflare change.** `aws_apigatewayv2_domain_name.ingest`'s API mapping was created
   manually outside Terraform ("created manually due to domain name conflicts" — see the commented
   `aws_apigatewayv2_api_mapping` in `main.tf`), so the new routes ride the existing mapping with no
   DNS change required — confirm this holds before applying, since it is not Terraform-enforced.

## Validation performed

- Read `firmware/hear_node/hear_node.ino`, `hear_prov.h`, `hear_push_ca.h` to confirm the TLS/CA
  and host-provisioning constraints above.
- Read `dama-gotchi/aws/modules/ingest_api/main.tf`, `aws/lambda/batch_ingest/handler.py`,
  `aws/oxalis/dama-sqs-consumer.py`, and `deploy/k8s/hear-mqtt-bridge.yaml` to confirm the existing
  Android relay is async, one-way, and schema-incompatible.
- `terraform fmt -check -diff` and `terraform init -backend=false` / `terraform validate` against
  a scratch copy of `dama-gotchi/aws/modules/ingest_api` (with `aws/lambda/*` alongside it) —
  passed. No `terraform plan`/`apply` was run; none of dama-gotchi's live state was touched.
- `git status` confirms only `aws/modules/ingest_api/{main.tf,outputs.tf,variables.tf}` changed in
  `dama-gotchi`; the pre-existing dirty files on `fix/rtcm-accounting-eskf-vertical`
  (`AcousticEnvRecorder.java`, `test_bridge_sync.py`, the new adversarial test) are untouched.
