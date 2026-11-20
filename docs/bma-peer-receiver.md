# Optional DAMA BMA metadata peer

`hear/ingest/bma_peer.py` and `tools/bma_peer_receiver.py` provide a separate,
manual, opt-in subscriber for BMA bearing metadata. They do not modify or feed
the hear MQTT bridge/drain, the AWS forwarder, deployed infrastructure,
TDoA, localization, or fusion. Stored records remain metadata for DAMA users to
inspect; this receiver performs no event matching or inference.

The sender contract is the generic MQTT JSON record:

* `device_id`: `bma-` followed by the converter's `unit_id`;
* `ts_ms`: `int(observation.source_time_wall_s * 1000)`;
* `telemetry_path`: `bma/bearing`;
* `schema`: `bma.bearing.v1`;
* `observation`: the full `convert_fleet` observation including identity,
  origin, surveyed pose, bearing, uncertainty, source wall time, and the
  literal labels `clock_precise: false` and `evidence: "unvalidated"`.
* Optional `site_id` is checked against `--site-id` when configured. Without
  that option a supplied site ID is retained as a claim, not independently
  authenticated.

The DAMA consumer routes records to `dama/<normalized-device-id>/telemetry`.
The subscriber uses the broad `dama/+/telemetry` MQTT filter because the broker
also carries non-BMA devices. It silently acknowledges unrelated messages
without treating them as BMA errors. For BMA records, the consumer's canonical
normalizer lowercases and replaces spaces and underscores with hyphens; this
subscriber applies that exact rule to topic/body comparison, then independently
requires the body ID to equal `bma-<observation.unit_id>`. In particular,
`bma-bma_west` correctly routes on `dama/bma-bma-west/telemetry`.
It rejects other schemas/paths, conflicting origins or identities, site
mismatches, claims of precise clocks or validated evidence, duplicate JSON
keys, malformed fields, nonfinite numbers and payloads above the configured
message limit. `source_time_wall_s` and `ts_ms` are display-time labels, not
synchronized arrival times or evidence of clock accuracy. BMA bearings remain
unvalidated; no TDoA/fusion route exists here.
Pose `source` and `surveyed_utc` follow the converter's nonempty-string rule:
surrounding whitespace is allowed and preserved as supplied, never trimmed.

The canonical AWS batch-ingest handler adds `idempotency_key`,
`batch_idempotency_key` and `ingest_trace_id`; the forwarder normalizes a
separate `ts` field. The receiver validates these fields and preserves them in
stored JSON. Replay identity uses only original sender content, excluding
`ts` and those AWS-added delivery fields, so a new trace ID does not turn a
replay into a conflict. The AWS forwarder documents its Oxalis SQS forwarding
as best-effort: an AWS HTTP 202 is not confirmation that MQTT publication or
local persistence succeeded.

## Manual use

The `run` subcommand requires the explicit `--enable` opt-in. For QoS 1
deliveries it uses manual acknowledgements: a valid record is acknowledged
only after its SQLite transaction commits; deterministic invalid messages are refused and
acknowledged, unrelated non-BMA telemetry is silently acknowledged without
parsing/logging it as a refusal. A capacity or storage error is left
unacknowledged and causes a disconnect; the supervisor reconnects using
exponential backoff capped at 30 seconds so the broker can redeliver the
unacknowledged record. A QoS 1 subscription does not upgrade a QoS 0 publication.
Publish BMA records at QoS 1 for broker redelivery to be possible. Valid QoS 0
records are still stored when possible, without an MQTT acknowledgement. If
storage fails, the receiver logs the loss and stays connected: reconnecting
would not recover that QoS 0 delivery.

`clean_session=False` and a stable client ID
(`dama-bma-peer` by default) request a persistent MQTT session. Keep that ID
unchanged and exclusive to one receiver instance. Redelivery across process
restarts depends on the broker retaining persistent sessions; behavior across
broker restarts depends on its persistence configuration and is not guaranteed
by this client. If the broker does not persist the session, reconnect alone
cannot guarantee redelivery. MQTT/session reliability does not establish
end-to-end AWS delivery. The optional `paho-mqtt` package is imported only for
`run`; it must support manual acknowledgement. A subscription is considered
ready only after SUBACK grants QoS 1; refusal, a QoS 0 downgrade, or a failed
subscribe request disconnects and stops the receiver explicitly rather than
running without its manual-ack delivery assumption. Paho versions exposing the
callback API selector use API v2; compatible older versions use their legacy
callback signatures. No dependency manifest is changed.

For a local-only broker, start the dedicated subscriber with an explicit DB:

```powershell
python tools\bma_peer_receiver.py run --enable --mqtt-host 127.0.0.1 --mqtt-port 1883 --db .\state\bma-peer.sqlite3
```

Non-loopback hosts are refused unless `--ca-file` names a readable CA bundle
that loads successfully. Certificate-chain and hostname verification remain
enabled; there is no insecure TLS switch. To pin an expected site, add
`--site-id <site-token>`. The receiver does not start automatically and this
example does not imply a deployed broker or validated BMA result.

Finite defaults are 10,000 rows, 64 MiB of serialized records plus dedup
fingerprints, 64 KiB per incoming message and 30 days of retention. Override
them with `--max-records`, `--max-storage-bytes`, `--max-message-bytes`, and
`--retention-s`. An insert that would exceed either row or byte capacity is
predictably rejected; expired rows are pruned transactionally before new
inserts. Replays with the same `(origin_id, device_id, det_id)` and identical
content return `duplicate`; changed content under that identity is rejected,
never overwritten. Keep the DB on local durable storage and back it up using
normal SQLite-safe procedures.

Inspect the newest locally retained observations and their usage:

```powershell
python tools\bma_peer_receiver.py inspect --db .\state\bma-peer.sqlite3 --limit 100
```

Inspection emits JSON and labels its interpretation; it does not contact a
broker or change the hear pipeline. All automated tests use synthetic messages,
temporary local SQLite files and fake MQTT/TLS dependencies only. They are
software-contract tests, not field validation or proof of AWS/MQTT delivery.
