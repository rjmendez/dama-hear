# Phase 4: push-only clip upload

Wire contract: `hear.ingest.clip.v1`, implemented in `hear/ingest/clipupload.py` and mirrored for
firmware in `firmware/hear_node/clip_upload.h`. Decision record:
[`decisions/0012-push-only-chunked-clip-upload.md`](decisions/0012-push-only-chunked-clip-upload.md).

## Scope

This is an additive HTTPS upload path for one hear-node WAV clip. It replaces neither
`/v1/ingest/batches` nor legacy heartbeat/event routes. The narrow goal is to let a node push
the same 5 s, 48 kHz, 16-bit mono clip that `hear_drain.py` used to pull, while preserving the
privacy rule from `silero-vad-privacy-contract.md`: no uploaded audio becomes visible under
`/pool/corpus/clips`, taggers, indexes, or long-lived raw refs until VAD scoring has completed.

No-SD nodes declare `psram_ring` as their upload source and stream the clip window directly from
the PSRAM raw ring. SD nodes may declare `sd` and upload a card-backed clip. Server semantics are
identical for both sources; node-side durability is deliberately not inferred from the source.

## Routes

All routes require TLS, an authenticated bearer credential with `ingest:write` and `clip:write`,
`Content-Length`, `X-Request-ID` echo when supplied, and RFC 9457 problem details for request
errors. A device credential may upload only its own `device_id`.

```
POST /v1/ingest/clips
PUT /v1/ingest/clips/{upload_id}/chunks/{chunk_index}
POST /v1/ingest/clips/{upload_id}/complete
GET /v1/ingest/clips/{upload_id}
DELETE /v1/ingest/clips/{upload_id}
```

Media types:

- init: `application/vnd.dama.hear.ingest.clip-init.v1+json`
- chunk: `application/octet-stream`
- complete: `application/vnd.dama.hear.ingest.clip-complete.v1+json`
- status: `application/vnd.dama.hear.ingest.clip-status.v1+json`

Each chunk `PUT` carries `X-Hear-Chunk-SHA256`, the lowercase hex SHA-256 digest of the exact
request body.

## Init frame

```json
{
  "upload_schema_version": 1,
  "device_id": "gold",
  "node": "gold",
  "boot": "a1b2c3",
  "sample": 4242,
  "clip_basename": "gold-a1b2c3-0000004242.wav",
  "clip_bytes": 480044,
  "chunk_bytes": 32768,
  "upload_source": "psram_ring",
  "upload_id": "32 lowercase hex chars"
}
```

`upload_id` is the existing `hear.clips.clip_key(node, boot, sample)`, not a hash of the audio
bytes. `Idempotency-Key` for init is derived from `(device_id, upload_id)`, so retries converge
on the same open upload.

## Chunking

`chunk_index` is an offset, not arrival order. With `chunk_bytes = 32768`, chunk `i` owns byte
range `[i * chunk_bytes, min((i + 1) * chunk_bytes, clip_bytes))`. Every chunk except the last
must be exactly `chunk_bytes`; the last must be exactly the remaining byte count. Re-sending the
same chunk bytes is a duplicate success; re-sending the same index with different bytes is
`409 chunk_conflict`.

Limits are fixed by the contract:

| Limit | Value |
|---|---:|
| chunk bytes | 32768 |
| max chunk bytes | 32768 |
| max clip bytes | 524288 |
| max chunks | 16 |
| nominal clip bytes | 480044 |
| upload TTL | 3600 s |
| max open uploads per device | 4 |

## Complete frame

```json
{
  "upload_schema_version": 1,
  "clip_bytes": 480044,
  "sha256": "64 lowercase hex chars"
}
```

Completion is accepted only after every addressed byte has arrived and the assembled quarantine
file's SHA-256 matches `sha256`. Completion is idempotent by `(device_id, upload_id, sha256)`.

## State machine

Non-terminal states are `open`, `receiving`, `assembling`, and `scoring`. Terminal states are
`promoted`, `purged`, `refused`, `expired`, and `aborted`.

Allowed transitions:

| From | To |
|---|---|
| `open` | `receiving`, `assembling`, `expired`, `aborted`, `refused` |
| `receiving` | `receiving`, `assembling`, `expired`, `aborted`, `refused` |
| `assembling` | `scoring`, `refused`, `expired`, `aborted` |
| `scoring` | `promoted`, `purged` |
| `promoted` | none |
| `purged` | none |
| `refused` | none |
| `expired` | none |
| `aborted` | none |

There is no transition from received bytes directly to `promoted`, and no transition from
`scoring` back to `receiving`.

## Privacy and storage invariant

Chunks and assembled WAVs live only in upload staging/quarantine. The completion path invokes the
same privacy purge semantics as batch clip promotion: `NO_SPEECH` is the only verdict that may
write to `/pool/corpus/clips`; `SPEECH_DETECTED`, `NOT_SCORED`, an unknown verdict, a scorer
failure, or an incomplete upload all end in `purged` or `refused` with staging removed or left
only as bounded refusal evidence without audio exposure.

A `purged` clip key is terminal. A later init for the same `upload_id` is refused with
`clip_key_purged`, preventing a node retry loop from re-uploading human speech after the system
has destroyed it.

## Mixed-version behavior

`POST /v1/ingest/batches` remains the metadata/event spool endpoint for v0.1.10 firmware. The
clip upload routes are additive and may return `404`/`503` to new firmware without changing old
batch receipt semantics. New firmware must surface missing routes and upload failures in
`/status` rather than silently falling back to `hear_drain.py`.
