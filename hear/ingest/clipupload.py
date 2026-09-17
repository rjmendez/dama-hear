"""Canonical `hear.ingest.clip.v1` chunked WAV upload contract: push-only clip ingestion.

This is the *wire contract* for moving one 5.0 s clip (480,044 B nominal) from a node to the
pool over HTTPS, in bounded chunks, without `tools/hear_drain.py` ever pulling it. It is a
contract, not an adapter: nothing here opens a socket, names a host, touches the filesystem,
Redis, SQLite or PostgreSQL, scores audio, or deletes anything. The operational specification
is `docs/phase4-push-clip-upload.md` and the decision record is
`docs/decisions/0012-push-only-chunked-clip-upload.md`.

Five rules carry the weight, and each has a test in `tests/test_clip_upload_contract.py`:

1. **Identity is the name, not the bytes.** `upload_id == hear.clips.clip_key(node, boot,
   sample)`, so a retry after reboot, a re-init from a different session, or a duplicate
   submission converge on one upload instead of minting a second copy of the same audio.
   The body `sha256` is an integrity field, checked at `complete`, never identity.
2. **A chunk is addressed, not appended.** `(upload_id, chunk_index)` names the byte range
   exactly, so a `PUT` is idempotent, resumable and order-free. Re-`PUT`ting the same index
   with different bytes is a conflict, never an overwrite.
3. **No SD card is required.** The source of record is declared (`psram_ring` or `sd`) and
   the server treats both identically; a no-SD node streams the window straight out of its
   PSRAM raw ring. Durability of the node-side copy is the node's business and is
   best-effort by construction (`UPLOAD_SOURCES`).
4. **Nothing is promoted before it is scored.** `complete` assembles into a quarantine root
   that is not the corpus and is not tagger-visible, then scores for human speech. Promotion
   into the corpus and purge-with-receipt are the *only* two exits, and `NOT_SCORED`
   fails closed to purge (`terminal_state_for_verdict`).
5. **A purged clip is terminal for its `clip_key`.** Re-initialising a purged upload is
   refused with `clip_key_purged`, so the push lane cannot become a loop that re-transports
   destroyed speech — the same rule `docs/silero-vad-privacy-contract.md` §6 puts on
   `hear-drain`.
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, Mapping, Optional, Tuple

CLIP_UPLOAD_SCHEMA_ID = "hear.ingest.clip.v1"
CLIP_UPLOAD_SCHEMA_MAJOR = 1
"""The only `upload_schema_version` this contract defines. A larger major is refused whole."""

# --------------------------------------------------------------------------- routes

INIT_ROUTE = "/v1/ingest/clips"
UPLOAD_ROUTE_TEMPLATE = "/v1/ingest/clips/{upload_id}"
CHUNK_ROUTE_TEMPLATE = "/v1/ingest/clips/{upload_id}/chunks/{chunk_index}"
COMPLETE_ROUTE_TEMPLATE = "/v1/ingest/clips/{upload_id}/complete"
ABORT_ROUTE_TEMPLATE = "/v1/ingest/clips/{upload_id}"

#: method -> route template, the closed set of this contract's request lines.
ROUTES: Tuple[Tuple[str, str], ...] = (
    ("POST", INIT_ROUTE),
    ("PUT", CHUNK_ROUTE_TEMPLATE),
    ("POST", COMPLETE_ROUTE_TEMPLATE),
    ("GET", UPLOAD_ROUTE_TEMPLATE),
    ("DELETE", ABORT_ROUTE_TEMPLATE),
)

INIT_MEDIA_TYPE = "application/vnd.dama.hear.ingest.clip-init.v1+json"
COMPLETE_MEDIA_TYPE = "application/vnd.dama.hear.ingest.clip-complete.v1+json"
STATUS_MEDIA_TYPE = "application/vnd.dama.hear.ingest.clip-status.v1+json"
CHUNK_MEDIA_TYPE = "application/octet-stream"

#: Per-chunk integrity, hex lowercase sha256 of the chunk body. Required on every chunk PUT:
#: a truncated TLS write that still reports its own length is the failure this catches.
CHUNK_DIGEST_HEADER = "X-Hear-Chunk-SHA256"
#: Echoed request correlation id, per docs/api-boundaries.md.
REQUEST_ID_HEADER = "X-Request-ID"
IDEMPOTENCY_HEADER = "Idempotency-Key"

# --------------------------------------------------------------------------- limits

#: One chunk on the wire. 32 KiB is four times the device spool batch body (8192 B) and still
#: fits an ESP32 TLS write loop without heap pressure; 480,044 B is then 15 chunks.
MAX_CHUNK_BYTES = 32_768
#: Every chunk but the last is exactly this size, so an index is an offset and nothing has to
#: be reconstructed from arrival order.
CHUNK_BYTES = 32_768
#: Hard bound on one assembled clip. Above the 480,044 B nominal with room for a longer
#: window; a declared `clip_bytes` above it is refused at init, before a byte is accepted.
MAX_CLIP_BYTES = 524_288
#: ceil(MAX_CLIP_BYTES / CHUNK_BYTES). An index outside [0, MAX_CHUNKS) is refused.
MAX_CHUNKS = 16
#: A clip the firmware actually writes today: 5.0 s, 48 kHz, 16-bit mono, 44-byte header.
NOMINAL_CLIP_BYTES = 480_044
#: An upload that stops mid-flight is swept, staging and all, after this long.
UPLOAD_TTL_S = 3_600
#: Concurrent unfinished uploads one device may hold. Above it, init is 429.
MAX_OPEN_UPLOADS_PER_DEVICE = 4

LIMITS: Dict[str, int] = {
    "chunk_bytes": CHUNK_BYTES,
    "max_chunk_bytes": MAX_CHUNK_BYTES,
    "max_clip_bytes": MAX_CLIP_BYTES,
    "max_chunks": MAX_CHUNKS,
    "nominal_clip_bytes": NOMINAL_CLIP_BYTES,
    "upload_ttl_s": UPLOAD_TTL_S,
    "max_open_uploads_per_device": MAX_OPEN_UPLOADS_PER_DEVICE,
}

#: Where the node read the window from. Declared, never inferred: a no-SD node
#: (`gold`, `ageev`, `kasami`) has only `psram_ring`, and the server must not read an absent
#: card as an absent clip.
UPLOAD_SOURCES: Tuple[str, ...] = ("psram_ring", "sd")

#: Credential scopes. `ingest:write` alone does not authorize audio upload.
REQUIRED_SCOPES: Tuple[str, ...] = ("ingest:write", "clip:write")

# --------------------------------------------------------------------------- state machine

#: Non-terminal.
STATE_OPEN = "open"
STATE_RECEIVING = "receiving"
STATE_ASSEMBLING = "assembling"
STATE_SCORING = "scoring"
#: Terminal.
STATE_PROMOTED = "promoted"
STATE_PURGED = "purged"
STATE_REFUSED = "refused"
STATE_EXPIRED = "expired"
STATE_ABORTED = "aborted"

STATES: Tuple[str, ...] = (
    STATE_OPEN, STATE_RECEIVING, STATE_ASSEMBLING, STATE_SCORING,
    STATE_PROMOTED, STATE_PURGED, STATE_REFUSED, STATE_EXPIRED, STATE_ABORTED,
)
TERMINAL_STATES: Tuple[str, ...] = (
    STATE_PROMOTED, STATE_PURGED, STATE_REFUSED, STATE_EXPIRED, STATE_ABORTED,
)
#: A terminal state the node may treat as "stop holding this window, and never re-init it".
#: `refused` and `expired` are terminal for the *upload*; `purged` is terminal for the
#: `clip_key` itself (silero-vad-privacy-contract.md §6).
CLIP_KEY_TERMINAL_STATES: Tuple[str, ...] = (STATE_PROMOTED, STATE_PURGED)

TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    STATE_OPEN: (STATE_RECEIVING, STATE_ASSEMBLING, STATE_EXPIRED, STATE_ABORTED, STATE_REFUSED),
    STATE_RECEIVING: (STATE_RECEIVING, STATE_ASSEMBLING, STATE_EXPIRED, STATE_ABORTED,
                      STATE_REFUSED),
    STATE_ASSEMBLING: (STATE_SCORING, STATE_REFUSED, STATE_EXPIRED, STATE_ABORTED),
    # No edge from `scoring` to `promoted` that does not pass a verdict, and no edge back to
    # `receiving`: audio already in quarantine is never re-opened for more bytes.
    STATE_SCORING: (STATE_PROMOTED, STATE_PURGED),
    STATE_PROMOTED: (),
    STATE_PURGED: (),
    STATE_REFUSED: (),
    STATE_EXPIRED: (),
    STATE_ABORTED: (),
}

#: VAD verdict (`docs/silero-vad-privacy-contract.md` §5) -> the only state it may produce.
#: `NOT_SCORED` fails closed: an unscored clip is purged, never promoted, never held.
VERDICT_TERMINAL_STATE: Dict[str, str] = {
    "NO_SPEECH": STATE_PROMOTED,
    "SPEECH_DETECTED": STATE_PURGED,
    "NOT_SCORED": STATE_PURGED,
}

#: Closed refusal vocabulary. A refusal is a durable, attributable result, not a log line.
REFUSAL_REASONS: Tuple[str, ...] = (
    "upload_schema_version_unsupported",
    "clip_bytes_invalid",
    "chunk_bytes_invalid",
    "chunk_index_out_of_range",
    "chunk_length_mismatch",
    "chunk_digest_mismatch",
    "chunk_conflict",
    "clip_name_invalid",
    "clip_key_mismatch",
    "device_identity_mismatch",
    "upload_source_unknown",
    "clip_bytes_incomplete",
    "clip_sha256_mismatch",
    "clip_not_riff",
    "clip_header_rate_unsupported",
    "clip_key_purged",
    "clip_key_already_stored",
    "upload_expired",
    "credential_missing",
    "scope_missing",
    "quota_exceeded",
    "store_unavailable",
)

# --------------------------------------------------------------------------- identity

_HEX32_RE = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def upload_id(node: str, boot: str, sample: int) -> str:
    """The upload's identity, byte-identical to `hear.clips.clip_key(node, boot, sample)`.

    Kept here rather than imported so this contract module stays dependency-free; the two are
    asserted equal by test, which is the only place the duplication is allowed to live.
    """
    h = hashlib.sha256()
    for part in ("clip", node, boot, str(sample)):
        h.update(part.encode())
        h.update(b"\x1f")
    return h.hexdigest()[:32]


def init_idempotency_key(device_id: str, upload: str) -> str:
    """`Idempotency-Key` for init. Derived from identity, so a retry is the same key."""
    return f"{device_id}.clipinit.{upload}"


def complete_idempotency_key(device_id: str, upload: str, sha256_hex: str) -> str:
    """`Idempotency-Key` for complete. Carries the body hash: a completion claiming different
    bytes for the same upload is a `409`, not a silent second assembly."""
    return f"{device_id}.clipdone.{upload}.{sha256_hex[:16]}"


def chunk_count(clip_bytes: int, chunk_bytes: int = CHUNK_BYTES) -> int:
    """How many chunks a clip of `clip_bytes` is cut into. Raises on an unusable declaration."""
    if chunk_bytes <= 0 or chunk_bytes > MAX_CHUNK_BYTES:
        raise ValueError("chunk_bytes_invalid")
    if clip_bytes <= 0 or clip_bytes > MAX_CLIP_BYTES:
        raise ValueError("clip_bytes_invalid")
    return (clip_bytes + chunk_bytes - 1) // chunk_bytes


def chunk_span(chunk_index: int, clip_bytes: int, chunk_bytes: int = CHUNK_BYTES) -> Tuple[int, int]:
    """Byte range `[start, end)` of one chunk. An index is an offset, never an arrival order."""
    total = chunk_count(clip_bytes, chunk_bytes)
    if chunk_index < 0 or chunk_index >= total:
        raise ValueError("chunk_index_out_of_range")
    start = chunk_index * chunk_bytes
    return start, min(start + chunk_bytes, clip_bytes)


def expected_chunk_bytes(chunk_index: int, clip_bytes: int,
                         chunk_bytes: int = CHUNK_BYTES) -> int:
    """Exact `Content-Length` this chunk must carry. Every chunk but the last is full."""
    start, end = chunk_span(chunk_index, clip_bytes, chunk_bytes)
    return end - start


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def transition_allowed(current: str, nxt: str) -> bool:
    """Whether the state machine has this edge. An unknown state is not an edge to anywhere."""
    return nxt in TRANSITIONS.get(current, ())


def terminal_state_for_verdict(verdict: str) -> str:
    """The one state a VAD verdict may produce. An unknown verdict fails closed to `purged`."""
    return VERDICT_TERMINAL_STATE.get(verdict, STATE_PURGED)


def validate_init(body: Mapping[str, object], *,
                  credential_device_id: Optional[str]) -> Optional[str]:
    """Refusal reason for an init frame, or `None` when the frame is admissible.

    Field-shape only: it does not touch storage, does not decide whether the `clip_key` is
    already terminal (that needs the index), and never repairs a field.
    """
    if credential_device_id is None:
        return "credential_missing"
    if body.get("upload_schema_version") != CLIP_UPLOAD_SCHEMA_MAJOR:
        return "upload_schema_version_unsupported"
    device_id = body.get("device_id")
    if not isinstance(device_id, str) or device_id != credential_device_id:
        return "device_identity_mismatch"
    if body.get("upload_source") not in UPLOAD_SOURCES:
        return "upload_source_unknown"
    clip_bytes = body.get("clip_bytes")
    if not isinstance(clip_bytes, int) or isinstance(clip_bytes, bool):
        return "clip_bytes_invalid"
    chunk = body.get("chunk_bytes", CHUNK_BYTES)
    if not isinstance(chunk, int) or isinstance(chunk, bool):
        return "chunk_bytes_invalid"
    try:
        chunk_count(clip_bytes, chunk)
    except ValueError as exc:
        return str(exc)
    for field in ("node", "boot", "clip_basename"):
        value = body.get(field)
        if not isinstance(value, str) or not value:
            return "clip_name_invalid"
    sample = body.get("sample")
    if not isinstance(sample, int) or isinstance(sample, bool) or sample < 0:
        return "clip_name_invalid"
    declared = body.get("upload_id")
    if not isinstance(declared, str) or not _HEX32_RE.match(declared):
        return "clip_key_mismatch"
    if declared != upload_id(str(body["node"]), str(body["boot"]), sample):
        return "clip_key_mismatch"
    return None


def validate_complete(body: Mapping[str, object], *, bytes_received: int,
                      chunks_received: int, expected_chunks: int,
                      clip_bytes: int) -> Optional[str]:
    """Refusal reason for a complete frame, or `None` when assembly may begin."""
    if body.get("upload_schema_version") != CLIP_UPLOAD_SCHEMA_MAJOR:
        return "upload_schema_version_unsupported"
    sha = body.get("sha256")
    if not isinstance(sha, str) or not _SHA256_RE.match(sha):
        return "clip_sha256_mismatch"
    declared = body.get("clip_bytes")
    if declared != clip_bytes:
        return "clip_bytes_invalid"
    if chunks_received != expected_chunks or bytes_received != clip_bytes:
        return "clip_bytes_incomplete"
    return None
