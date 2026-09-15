"""Key and digest arithmetic for the object store. Pure functions, no I/O, no state.

⚠️A KEY MINTED HERE IS FROZEN THE MOMENT IT IS PUBLISHED. `v1` is a key-schema generation, not a
version number to bump: `docs/api-boundaries.md` and `docs/phase0-freeze-contracts.v1.md` both say
meanings are allocated, never redefined in place. A key that meant blob X in v1 means blob X
forever; a fix is a v2 generation with both readable.

⚠️IDENTITY IS THE NAME, NOT THE CONTENT -- at L1. `object_key` takes `pool.key`, `clip_key`,
`tag_key` or an archive path as its `logical_id` and never recomputes one. `hear/clips.py` states
the reason and it still holds: a content hash would collide two clips of the same silence and drop
a real timestamped event. The content hash lives one layer below, at `blob_key`, where two
identical clips *should* share one blob.

The canonicalisation is the repository's existing one -- `json.dumps(row, sort_keys=True)` as in
`hear/pool.py`, plus the compact separators `tools/freeze_contracts.py` already uses -- because a
second canonical JSON in the same repository is a second answer to "what are these bytes".
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, Optional

KEY_SCHEMA_VERSION = 1
PREFIX = "hear/v1"

#: The class tokens of the key design's per-class map. A class not in here has no key, which is
#: how `pylib*` (4.7 G of pip-reproducible vendored packages) is excluded at the key layer rather
#: than by a skip somewhere in a loop.
CLASSES = frozenset({
    "raw", "record", "record-seg", "scene-seg", "clip", "clip-index-seg", "tags-seg",
    "score-seg", "model-card", "tdoa-arrival-seg", "tdoa-run", "model-file", "model-set",
    "ledger-seg", "sketch-corpus", "annotations",
})

#: Classes whose blob id is HMAC-derived rather than the plaintext digest, because a plaintext
#: digest of guessable content is a confirmation oracle (key design §8).
RESTRICTED_CLASSES = frozenset({"clip", "raw"})

#: The governance labels that travel with an object in the CLEAR part of its metadata. The label
#: is clear precisely so a reader can refuse an object without opening it; the thing the label
#: describes -- a 7-decimal coordinate, an ambient recording -- is inside the sealed sub-document
#: and appears in no key, index or log line (key design §8, `docs/data-governance.md`).
SENSITIVITY = {
    "clip": ("ambient_audio",),
    "raw": ("ambient_audio", "precise_location"),
    "tdoa-arrival-seg": ("precise_location",),
    "tdoa-run": ("precise_location",),
}


def pointer_generation_prefix(object_key: str) -> str:
    """Where the immutable per-generation records of one pointer live, beside it and not under it."""
    if "/obj/" not in object_key:
        raise ValueError("not an object pointer key: %r" % (object_key,))
    return object_key.replace("/obj/", "/ptrgen/", 1) + "/"


def canonical_json(doc: Dict[str, Any]) -> bytes:
    """The one canonical serialisation: sorted keys, compact separators, UTF-8."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def record_digest(row: Dict[str, Any]) -> str:
    """The stable identity of one row, independent of the file it happens to sit in."""
    return sha256_hex(canonical_json(row))


def stream_digest(record_digests: Iterable[str]) -> str:
    """The identity of a segment: its record SET, not its byte image.

    Order-independent by construction. The drain re-archives whole tails and the pool dedupes at
    the record layer, so an order-dependent digest would flip the identity of a segment every time
    an overlapping tail was re-ingested -- which the inventory measured happening 3183 times. It is
    also what makes a multi-member `.jsonl.gz` addressable at all: `gzip.open(path, "at")` makes
    the byte image a function of append history, i.e. a timestamp, not an identity.
    """
    return sha256_hex(b"\x1f".join(sorted(d.encode("ascii") for d in record_digests)))


def blob_key(digest: str, *, algo: str = "sha256", tenant: Optional[str] = None) -> str:
    """`hear/v1/blob/<algo>/[tenant/]<h0:2>/<h2:4>/<h>` -- full 64 hex, never truncated.

    The 32-hex truncation is fine for `pool.key`/`clip_key`/`tag_key`, which are names; a content
    address that also decides byte identity gets no truncation. The fan-out is derived from the
    digest and is never parsed for meaning.
    """
    if algo not in ("sha256", "hmac-sha256"):
        raise ValueError("unknown blob digest algo: %r" % (algo,))
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("a blob id is 64 lowercase hex characters")
    if algo == "hmac-sha256" and not tenant:
        raise ValueError("a restricted-class blob id is per tenant")
    parts = [PREFIX, "blob", algo]
    if tenant:
        parts.append(tenant)
    parts += [digest[:2], digest[2:6], digest]
    return "/".join(parts)


def restricted_blob_id(tenant_index_key: bytes, plaintext_digest: str) -> str:
    """`HMAC-SHA256(K_tenant_index, plaintext_digest)` for `ambient_audio`/`precise_location`.

    Deterministic per tenant, so dedupe still works inside a tenant and deliberately does not work
    across tenants -- which `docs/data-governance.md` requires anyway. The plaintext digest itself
    lives inside the encrypted metadata and never in a key, a log line or an index.
    """
    import hmac

    return hmac.new(tenant_index_key, plaintext_digest.encode("ascii"), hashlib.sha256).hexdigest()


def object_key(object_class: str, partition: Iterable[str], logical_id: str) -> str:
    """`hear/v1/obj/<class>/<partition…>/<logical_id>`; `logical_id` is passed through verbatim.

    `partition` carries the *existing* partition, including the literal `unanchored`
    (`hear/pool.py`, `hear/clips.py`). A guessed date here would assert a capture time the row
    itself refuses to assert.
    """
    if object_class not in CLASSES:
        raise ValueError("no key may be minted for class %r" % (object_class,))
    if not logical_id:
        raise ValueError("a logical id is required and is never derived from content")
    parts = [PREFIX, "obj", object_class]
    for p in partition:
        if not p or "/" in p:
            raise ValueError("bad partition component: %r" % (p,))
        parts.append(p)
    parts.append(logical_id)
    return "/".join(parts)


def meta_key(object_class: str, logical_id: str, meta_digest: str) -> str:
    """Metadata is itself blob-addressed, so "which metadata did this run publish" has an answer."""
    return "%s/meta/%s/%s/%s.json" % (PREFIX, object_class, logical_id, meta_digest)


def staging_key(run_id: str, staging_id: str) -> str:
    return "%s/staging/%s/%s" % (PREFIX, run_id, staging_id)


def quarantine_key(run_id: str, task_id: str) -> str:
    return "%s/quarantine/%s/%s.json" % (PREFIX, run_id, task_id)


def task_id(object_class: str, source_ref: str, byte_range: Optional[Iterable[int]] = None) -> str:
    """The deterministic id of one unit of import work: `(class, source_ref, byte_range)`.

    ⚠️DERIVED, NEVER ALLOCATED. Resume replays the local run ledger and treats every `published`
    task as done. If a task id depended on run order, a wall clock, a uuid or a dict iteration
    order, a resume would re-import the whole manifest and a re-run would stop being free -- which
    is the difference between "resumable" and "restartable".
    """
    rng = list(byte_range) if byte_range is not None else []
    if rng and len(rng) != 2:
        raise ValueError("a byte range is [start, end)")
    payload = {"class": object_class, "source_ref": source_ref, "byte_range": rng}
    return sha256_hex(canonical_json(payload))[:32]
