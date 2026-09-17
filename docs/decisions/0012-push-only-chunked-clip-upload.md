# ADR 0012: push-only chunked clip upload

## Status

Accepted for Phase 4 migration.

## Context

`hear_drain.py` has become an operational debugging tool, not the desired production transport.
The existing firmware event spool is valuable but only transports small JSON events; it cannot
carry a 480,044 byte WAV clip. No-SD nodes also cannot use the SD-backed event spool, but they do
have a PSRAM raw ring that already powers the `/audio` streaming route.

The privacy invariant is stronger than transport convenience: human voice must be purged before a
clip appears in `/pool/corpus/clips` or any tagger-visible index.

## Decision

Add `hear.ingest.clip.v1`, a chunk-addressed HTTPS upload protocol:

- `upload_id` is the existing clip key derived from `(node, boot, sample)`.
- Chunks are idempotent by `(upload_id, chunk_index)` and verified by per-chunk SHA-256.
- Completion assembles bytes in quarantine, verifies whole-clip SHA-256, then runs the privacy
  purge path before any corpus write.
- No-SD nodes upload from `psram_ring`; SD nodes may upload from `sd`. The server treats both
  sources identically.
- Existing batch ingest and legacy heartbeat/event routes remain unchanged.

## Consequences

The no-SD path is live best-effort: a power loss or long outage can lose audio once the PSRAM ring
overwrites it. SD nodes keep their stronger local durability. The server gains bounded staging and
cleanup obligations, but avoids base64-expanding clips into the small event spool and keeps raw
audio out of durable batch refs.
