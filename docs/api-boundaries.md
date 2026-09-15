# DAMA Hear API boundaries

This is the northbound contract for the acoustic sensing platform. Services own durable
resources and invariants; clients do not reach into databases, brokers, or worker queues.
`dama-gotchi` is **not** a platform dependency. It may provide an Android UI, an edge
collector, or a compatibility adapter using these APIs, but every service must operate
without it.

## Service map

| Service | Owns | Northbound API | Events it publishes |
|---|---|---|---|
| **Enrollment & Configuration** | Tenants, sites, nodes, credentials, desired config, rollout state | REST; gRPC for device provisioning | `node.enrolled`, `config.changed`, `config.rollout.*` |
| **Ingest** | Authenticated telemetry envelopes, deduplication, ingest offsets, quarantine | HTTPS batch REST; MQTT only as an edge transport; internal gRPC | `observation.received`, `observation.rejected` |
| **Fleet Health** | Heartbeats, liveness, clock quality, link quality, firmware/config compliance | REST query; SSE for live dashboards | `health.degraded`, `node.offline`, `clock.quality.changed` |
| **Event Query/Search** | Canonical detections, labels, filters, saved searches, result cursors | REST; async export jobs | `event.created`, `event.updated`, `label.changed` |
| **Clips & Audio** | Clip manifests, object references, range reads, retention/legal holds | REST control plane; signed object URLs for bytes | `clip.ready`, `clip.expired` |
| **Calibration & Geometry** | Calibration runs, sensor response, node poses, geometry versions, validity | REST; gRPC for solver workers | `calibration.completed`, `geometry.activated` |
| **Localization & Tracks** | Localization jobs/results, tracks, uncertainty and provenance | REST; gRPC for synchronous solve requests | `localization.completed`, `track.updated` |
| **Models** | Model artifacts, manifests, compatibility, deployment channels and rollout | REST; signed artifact download | `model.published`, `model.rollout.*` |
| **Alerts** | Rules, deduplication, incidents, acknowledgements, notification delivery | REST; webhook delivery | `alert.opened`, `alert.updated`, `incident.*` |
| **Exports** | Export specifications, snapshots, manifests, delivery destinations and status | REST; signed downloads | `export.completed`, `export.failed` |
| **Integrations** | External destinations, credentials references, subscriptions and delivery state | REST; outbound webhooks/event sinks | `integration.delivery.*` |

Services may be deployed together initially, but the ownership and API boundaries remain
separate. The event bus is for durable facts and asynchronous work, not for hiding a
request/response dependency.

## Resource and endpoint conventions

All HTTP endpoints are under `/v1`. Resource identifiers are opaque, URL-safe IDs. Tenant
and site are derived from the token; accepting them from the body is forbidden unless the
caller has an explicit cross-tenant administrative scope.

Representative resources:

```text
POST   /v1/nodes/enrollment-tokens
POST   /v1/nodes/{node_id}/enroll
GET    /v1/nodes
GET    /v1/nodes/{node_id}
PATCH  /v1/nodes/{node_id}/desired-config
POST   /v1/ingest/batches

GET    /v1/health/nodes
GET    /v1/events
POST   /v1/events:search
GET    /v1/events/{event_id}
POST   /v1/events/{event_id}/labels

POST   /v1/clips:request
GET    /v1/clips/{clip_id}
GET    /v1/clips/{clip_id}/content

POST   /v1/calibrations
GET    /v1/calibrations/{calibration_id}
POST   /v1/geometries/{geometry_id}:activate
POST   /v1/localizations
GET    /v1/localizations/{localization_id}
GET    /v1/tracks

GET    /v1/models
POST   /v1/models
POST   /v1/model-rollouts

GET    /v1/alert-rules
POST   /v1/alert-rules
GET    /v1/incidents
POST   /v1/incidents/{incident_id}:acknowledge

POST   /v1/exports
GET    /v1/exports/{export_id}
GET    /v1/integrations
POST   /v1/integrations
```

`POST ...:search`, `:request`, `:activate`, `:acknowledge`, and similar commands are
explicit command resources. They return a command/job representation rather than pretending
that a potentially long operation is synchronous.

## Protocol choices

* **REST/JSON** is the public control and query API: operators, web clients, automation,
  integrations, and `dama-gotchi` adapters use it.
* **gRPC/protobuf** is for bounded internal calls where typed contracts and deadlines matter:
  ingest validation, geometry/localization solver calls, health aggregation, and model
  compatibility checks. It is not required of external clients.
* **HTTPS signed URLs** serve clip/audio and model bytes. The API authorizes access and
  returns a short-lived URL; large bodies do not flow through application workers.
* **Events** use CloudEvents 1.0 over the existing broker/stream. Event subjects are
  resource IDs, event IDs are globally unique, and consumers must be at-least-once safe.
  Suggested types include `dama.event.created.v1` and `dama.localization.completed.v1`.
  MQTT remains a node-side transport only; it is not the northbound API.

Every async response includes `job_id`, `status_url`, `created_at`, and a stable
`operation_id`. Clients can poll with `GET` or subscribe to the corresponding event type.

## Auth and authorization

Use OAuth 2.0/OIDC access tokens for humans and services, with mTLS-bound credentials for
device identities. Tokens carry tenant/site claims and least-privilege scopes:

```text
nodes:read              nodes:enroll             nodes:configure
ingest:write            health:read              events:read
events:annotate         clips:read               clips:export
calibration:read        calibration:write        geometry:activate
localization:read       localization:run         tracks:read
models:read             models:publish           models:deploy
alerts:read             alerts:manage            incidents:ack
exports:create          exports:read              integrations:manage
admin:tenant
```

Scopes are necessary but not sufficient: enforce tenant, site, node, and data-retention
boundaries in the service. Device tokens get only `ingest:write`, `health:write`, and
`nodes:read` for their own node. Secrets for integrations are write-only references to a
secret manager and never appear in GET responses or events.

## Query, pagination, and filtering

List endpoints use cursor pagination, never offset pagination:

```json
{
  "items": [],
  "next_cursor": "opaque",
  "has_more": true
}
```

`limit` defaults to 50 and is capped at 500. Cursors encode the complete stable sort
(normally `created_at,id`) and expire after 24 hours. Clients must treat cursors as opaque.
Event search accepts bounded `from`, `to`, `site_id`, `node_ids`, `kinds`, `labels`,
`min_confidence`, `geometry_version`, and `track_id`; a time range is mandatory and capped
by endpoint (for example 31 days for interactive search). Large or unbounded searches use
Exports. Return `ETag` for individual resources and support `If-None-Match`.

## Idempotency, consistency, and concurrency

All mutating POST commands accept `Idempotency-Key` (required for ingest batches, enrollment,
clip requests, calibration, localization, exports, and rollouts). Scope the key to
`tenant + principal + route`, retain the result for at least 24 hours, and return the exact
original status/body on retry. Reusing a key with a different request body is `409`.

Ingest deduplicates on the producer's `(node_id, event_id)` and rejects timestamp or schema
replays that conflict. Resource mutations support `If-Match: "<etag>"`; stale writes return
`412`. Reads are strongly consistent for a single resource after a successful mutation and
eventually consistent for cross-service search and fleet aggregates. Each response exposes
`observed_at`/`source_version` where freshness affects interpretation.

## Versioning and compatibility

The major version is in the path (`/v1`) and protobuf package (`dama.*.v1`). Additive fields
are compatible; clients must ignore unknown JSON fields and protobuf fields. Never change
the meaning or type of an existing field. Breaking changes require `/v2`, a new protobuf
package, migration notes, and a deprecation window of at least 90 days. Event type versions
are immutable; publish a new event type for a breaking payload change. Advertise supported
versions and sunset dates from `/v1/version`.

Schemas are published as OpenAPI and protobuf descriptors from CI. Contract tests cover
auth scope enforcement, idempotent retries, cursor stability, event envelopes, and error
mapping before a service is released.

## Error semantics and observability

Use RFC 9457 Problem Details for REST:

```json
{
  "type": "https://api.dama.example/problems/invalid-argument",
  "title": "Invalid argument",
  "status": 422,
  "code": "EVENT_TIME_RANGE_TOO_LARGE",
  "detail": "The requested range exceeds the interactive search limit.",
  "instance": "req_01...",
  "retryable": false,
  "field_errors": [{"field": "to", "reason": "range_exceeded"}]
}
```

Stable `code` values, not human text, drive client behavior. Map common cases to `400`
(malformed), `401` (missing/invalid token), `403` (scope or tenant denial), `404` (resource
not visible), `409` (state/idempotency conflict), `412` (ETag conflict), `422` (valid JSON,
invalid domain value), `429` (rate limit), and `5xx` (server/upstream failure). `429` and
transient `5xx` include `Retry-After`; never retry `401`, `403`, `404`, `409`, `412`, or
non-retryable `422` automatically. gRPC uses the equivalent canonical status codes and
includes the same `code`, request ID, and retry metadata.

Every request has a caller-supplied or server-generated `X-Request-ID`, is logged with
tenant/resource IDs and latency, and emits metrics for rate limits, retries, freshness,
queue age, and error code. Do not put clip bytes, tokens, or secret values in logs.

## Boundary rule for `dama-gotchi`

The Android project may implement a thin `DamaHearClient` or an adapter translating legacy
MQTT/telemetry into `/v1/ingest/batches`. It must not own enrollment records, fleet truth,
event indexes, geometry, localization state, model registry, alert rules, or export data.
Replacing it with curl, a web UI, a Python client, or another edge application must leave
the platform functional. Legacy gotchi endpoints, topic names, and payloads are compatibility
inputs only and are translated at the edge; they are not canonical northbound contracts.
