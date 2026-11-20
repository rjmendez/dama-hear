# BMA fleet metadata sender (opt-in)

This standalone sender is never started by imports or services. It reads two
local, atomically replaced JSON files, converts with `convert_fleet`, and
explicitly invoked polls post metadata only, never raw audio. No deployment,
endpoint or authentication configuration has been verified. This is not a
field-validated DOA result.

The host-fitted `source_time_wall_s` and `ts_ms` are hub wall/display time,
not PTP/GPS or precision-fusion timestamps (`clock_precise: false`).
Bearings carry `evidence: unvalidated`. See Loci findings
`e3578b2e-e01d-4654-b988-20152e15f9f8` and
`fa44495d-e11c-4b25-91e8-ac988bfbeb00`.

## Operation

Supply `BMA_AWS_URL` and `BMA_AWS_TOKEN` through a protected environment, not
command-line arguments or logs. URL must be HTTPS with path `/ingest/batch`
or a single stage prefix such as `/prod/ingest/batch`. Stage names are limited
to 1–128 ASCII letters, digits, underscores or hyphens; encoded characters,
multiple prefixes, userinfo, query and fragment are rejected. The token is sent
as a bearer token. TLS uses
the system trust store and hostname validation; redirects are not followed.
Validate the actual endpoint, auth, and downstream receiver independently
before enabling anything. Example invocation after securely configuring env:

```
python tools\bma_aws_sender.py --fleet C:\local\fleet.json --origin C:\local\origin.json --polls 10 --interval 1 --max-age 5 --timeout 5
```

For a receiver configured with `--site-id yard-a`, explicitly add
`--site-id yard-a` to the sender invocation. The exact, case-sensitive value
is included in every message as `site_id`; omission preserves the original
wire shape and is refused by a site-filtering receiver. A site ID is 1–64
ASCII characters, starts with a letter or digit and otherwise contains only
letters, digits, `_`, `.`, `:` or `-`. It is an operator-supplied label, not
authentication, proof of location, or inferred from the origin.

The origin file is the strict WGS84/true-north object expected by
`hear.ingest.bma_fleet`. Fleet and origin must match. Both feed and observation
wall times must be fresh and not in the future; malformed JSON, duplicate keys,
nonfinite values, invalid origins, poses and rays are refused. Each file is
limited to 1 MiB, the fleet to 256 units and 512 rays, and each encoded
request to 64 KiB. Polls are 1–1000,
interval 0.1–60 s, timeout 0.1–30 s and max age 0.1–3600 s. `--max-attempts`
is 1–16 and defaults to 4. At most that many HTTP requests are attempted per
poll; failed requests rotate to the back so they cannot starve other pending
messages. The timeout is per socket operation, not a strict end-to-end request
or poll deadline; DNS, multiple socket operations and slow responses can take
longer. Freshness is checked at the start of the poll.

The canonical `rjmendez/dama-gotchi` Terraform
`aws/modules/ingest_api/main.tf` defines a `$default` stage and
`POST /ingest/batch`, and `outputs.tf` exposes the unprefixed URL. It does not
establish that `/prod` is deployed. AWS HTTP APIs support named stages in
invoke URLs ([AWS stages documentation](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-stages.html));
the optional prefix only permits an independently verified named stage.

Each request has exactly one message to isolate units and partial outcomes:
`{"device_id":"bma-<unit_id>","messages":[...]}`. The message has the same
device ID, integer `ts_ms`, `telemetry_path: "bma/bearing"`,
`schema: "bma.bearing.v1"`, and the entire unchanged converter observation.
Deduplication uses `(origin_id, det_id)` in memory, while the observation
retains those stable IDs on every retry. A key is retired only after its
original source timestamp is strictly older than the freshness window and it
is no longer pending. The process tracks the maximum observed poll wall time:
if time moves backward, it reports `clock_rollback` and neither reads nor
sends until the clock catches up. A forward clock jump can therefore pause
the sender for a long time after correction; restart loses dedup state.
This assumes an identity's source timestamp is immutable. A reused or
retimestamped detection ID can be accepted again after retirement; bounded
in-memory state cannot prevent arbitrary identity reuse.

Each poll prints JSON counters only. `refused` counts converter refusals;
`input_error` reports a rejected input file/shape; `capacity_dropped` counts
observations not queued; `expired` counts pending observations dropped once too
old; `unconfirmed` counts failed or ambiguous attempts; `deferred` counts queued
records not attempted due to the poll limit; `pending` includes both.
`clock_rollback` reports a fail-closed poll. `not_enqueued` counts
accepted but not enqueued records; `quarantined` reports quarantine. A 202 is
trusted only when its integer accepted, quarantined and enqueued counts are
consistent for that one message. It does not prove downstream delivery. A 503,
other status, timeout, malformed response or redirect remains unconfirmed and
is retried on a subsequent poll while fresh, with identical bytes. A 503 can
already have partially enqueued: retries can duplicate, irrespective of any
server idempotency hash. There is no exactly-once guarantee.

At most 256 records are pending and 4096 still-fresh identities remembered per
process; capacity refusal is reported rather than evicting eligible identities.
At high enough rates the bounded window can still fill; observations rejected
for capacity can disappear before a later poll. No disk
spool or persistent deduplication exists: process exit, crashes, overwritten
feed snapshots, expiry, quarantine, capacity and receiver failures may lose
data. Restarting may resend records. The exit code is 1 if any poll reported
loss, refusal, quarantine, ambiguous outcome or remaining pending work, 2 for
invalid configuration, otherwise 0. Monitor every poll, not merely the last.
