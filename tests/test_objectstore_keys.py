"""Keys are minted from identifiers that already exist, and a minted key is frozen.

Every case here is a way a key could quietly change meaning: a content hash substituted for a
name, a guessed day replacing `unanchored`, a truncated blob id, a class nobody allocated. None of
them would raise at runtime -- they would just publish a second, differently-addressed copy of
evidence that already had an address.
"""
from __future__ import annotations

import pytest

from hear.objectstore import keys as K


def test_a_clip_key_is_never_replaced_by_a_content_hash():
    clip_key = "9f2c" + "0" * 28
    key = K.object_key("clip", ("2026-09-12", "mach"), clip_key)
    assert key == "hear/v1/obj/clip/2026-09-12/mach/" + clip_key
    assert K.sha256_hex(b"any bytes at all") not in key


def test_an_unanchored_row_keys_to_unanchored_and_not_to_a_guessed_day():
    key = K.object_key("clip", ("unanchored", "mach"), "7e1d" + "0" * 28)
    assert "/unanchored/" in key
    assert "2026-" not in key


def test_a_blob_id_is_the_full_digest_and_never_a_truncation():
    digest = K.sha256_hex(b"hello")
    assert K.blob_key(digest).endswith(digest)
    with pytest.raises(ValueError):
        K.blob_key(digest[:32])


def test_no_key_may_be_minted_for_an_unallocated_class():
    # pylib* is 4.7 G of pip-reproducible vendored packages; it has no class, so it cannot be keyed.
    with pytest.raises(ValueError):
        K.object_key("pylib", ("x",), "y")


def test_a_restricted_class_blob_id_is_not_the_plaintext_digest():
    plaintext = K.sha256_hex(b"a silent five second clip")
    restricted = K.restricted_blob_id(b"tenant-index-key", plaintext)
    assert restricted != plaintext
    assert len(restricted) == 64
    # Deterministic per tenant, so dedupe inside the tenant still works.
    assert restricted == K.restricted_blob_id(b"tenant-index-key", plaintext)
    assert restricted != K.restricted_blob_id(b"another-tenant-key", plaintext)


def test_a_task_id_is_derived_from_what_it_is_and_not_from_when_it_ran():
    a = K.task_id("raw", "corpus/raw/mach/x-dets.csv")
    b = K.task_id("raw", "corpus/raw/mach/x-dets.csv")
    assert a == b
    assert a != K.task_id("raw", "corpus/raw/mach/y-dets.csv")
    assert a != K.task_id("record-seg", "corpus/raw/mach/x-dets.csv")
    assert a != K.task_id("raw", "corpus/raw/mach/x-dets.csv", (0, 10))


def test_a_stream_digest_does_not_depend_on_row_order():
    rows = [{"key": "a"}, {"key": "b"}, {"key": "c"}]
    digests = [K.record_digest(r) for r in rows]
    assert K.stream_digest(digests) == K.stream_digest(reversed(digests))


def test_every_class_labelled_sensitive_is_actually_protected():
    """⚠️THE LABELLED-BUT-UNPROTECTED REGRESSION: `tdoa-*` was marked and then left in the clear.

    `SENSITIVITY` labelled `tdoa-arrival-seg` and `tdoa-run` `precise_location` while
    `RESTRICTED_CLASSES` was the literal `{"clip", "raw"}`, so both TDOA classes were addressed by
    the raw sha256 of their plaintext, imported with no key provider at all, deduped across
    tenants by content address, and had that digest copied into ledger rows and quarantine
    records. A TDOA arrival row is short and highly guessable -- a few node ids, a coordinate, a
    microsecond timestamp -- which makes a published digest of it a confirmation oracle for the
    exact coordinate the label exists to protect. The set is now derived from the labels, so the
    two tables cannot drift apart again.
    """
    labelled = {cls for cls, labels in K.SENSITIVITY.items()
                if K.RESTRICTED_SENSITIVITY.intersection(labels)}
    assert labelled == set(K.RESTRICTED_CLASSES)
    assert {"tdoa-arrival-seg", "tdoa-run", "clip", "raw"} <= set(K.RESTRICTED_CLASSES)
    assert set(K.SENSITIVITY) <= set(K.CLASSES)


def test_a_precise_location_class_is_addressed_per_tenant_and_never_by_its_plaintext():
    plaintext = K.sha256_hex(b'{"node":"mach","lat":40.2925221,"lon":-79.1221604}')
    one = K.restricted_blob_id(b"tenant-a-index-key", plaintext)
    two = K.restricted_blob_id(b"tenant-b-index-key", plaintext)
    assert one != plaintext and two != plaintext and one != two
    # And such a class cannot be keyed with the plain algo, which is what kept the digest public.
    assert K.blob_key(one, algo="hmac-sha256", tenant="dama").endswith(one)
    with pytest.raises(ValueError):
        K.blob_key(one, algo="hmac-sha256")
