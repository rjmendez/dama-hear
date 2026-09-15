"""The encryption boundary: what it refuses, what it hides, and what it still lets dedupe do.

⚠️NOTHING HERE CREATES A REAL KEY. Every key in this file comes from `InMemoryTestKeyProvider`,
whose material is derived from a literal test seed, lives in one object, and is never written to
disk, an env var or a KMS. The first test is the one that matters most operationally: an importer
that nobody deliberately handed key material to **cannot publish a restricted class at all**.

⚠️THE PRIVACY CLAIM IS A SEARCH, NOT AN ASSERTION ABOUT ONE FIELD. A plaintext digest of guessable
content is a confirmation oracle (key design §8), so the test greps every key, every stored byte
and every ledger row for it. Asserting "the digest is not in the metadata dict" would pass while
the same digest sat in the object key, which is exactly where it used to be.
"""
from __future__ import annotations

import json
import os

import pytest

from hear.objectstore import backend as B
from hear.objectstore import crypto as C
from hear.objectstore import keys as K
from hear.objectstore import stage as ST
from hear.objectstore import streaming as S
from tests import objectstore_mini_pool as MINI

RUN = "2026-09-16T0000Z-crypto"


@pytest.fixture()
def pool(tmp_path):
    return MINI.build(str(tmp_path / "pool"))


@pytest.fixture()
def store(tmp_path):
    return B.LocalDirBackend(str(tmp_path / "store"))


def _importer(store, tmp_path, run=RUN, crypto=None, tenant_id="dama"):
    return ST.Importer(store, str(tmp_path / "work" / run / "ledger.jsonl"), run,
                       tenant_id=tenant_id, git_commit="0000000", crypto=crypto)


def _clip_task(pool, path=None, clip_key="9f2c" + "0" * 28, day="2026-09-12"):
    return ST.ImportTask(object_class="clip", logical_id=clip_key, partition=(day, "mach"),
                         source_path=path or pool["clip_paths"][0],
                         expected_digest=pool["clip_digest"], expected_bytes=pool["clip_bytes"],
                         digest_source="index.jsonl")


def _all_stored_bytes(store):
    return b"".join(store.get_range(k) for k in store.keys_under("hear/"))


# ------------------------------------------------------- the default refuses


def test_the_default_importer_cannot_publish_a_restricted_class(pool, store, tmp_path):
    """No key provider is configured, so `clip` is refused -- not published in the clear."""
    report = _importer(store, tmp_path).run([_clip_task(pool)])
    assert [q["error_class"] for q in report.quarantined] == ["key_provider_unavailable"]
    assert report.counters["published"] == 0
    assert report.exit_code == 1
    assert store.keys_under("hear/v1/obj/") == []
    assert store.keys_under("hear/v1/blob/") == []


def test_a_refused_restricted_class_does_not_stop_an_unrestricted_one(pool, store, tmp_path):
    seg = ST.ImportTask(object_class="record-seg", logical_id="0", partition=("2026-09-12", "mach"),
                        source_path=pool["records_path"], digest_source="computed-at-import")
    report = _importer(store, tmp_path).run([_clip_task(pool), seg])
    assert report.counters["quarantined"] == 1
    assert report.counters["published"] == 1
    assert report.outcome == "partial"


def test_no_production_cipher_ships_in_this_repository():
    with pytest.raises(C.KeyUnavailable):
        C.ObjectCrypto(C.InMemoryTestKeyProvider(), cipher=C.HmacCtrCipher())
    assert C.HmacCtrCipher.production_ready is False
    assert C.InMemoryTestKeyProvider.is_test_only is True


def test_the_key_provider_writes_no_key_material_anywhere(tmp_path):
    """A provider that persisted a key would leave it in the tree the test owns. None appears."""
    cwd_before = ST.census(str(tmp_path))
    crypto = C.synthetic_crypto()
    key = crypto.provider.data_key("dama", "clip", "a" * 64)
    assert ST.census(str(tmp_path)) == cwd_before
    assert not any(hasattr(crypto.provider, attr) for attr in ("save", "path", "keyfile"))
    assert key.key not in os.environ.get("PATH", "").encode()


# ------------------------------------------------- identity without an oracle


def test_a_restricted_blob_is_addressed_by_its_hmac_id_and_never_by_its_plaintext_digest(
        pool, store, tmp_path):
    crypto = C.synthetic_crypto()
    report = _importer(store, tmp_path, crypto=crypto).run([_clip_task(pool)])
    assert report.counters["published"] == 1
    assert report.counters["encrypted"] == 1

    plaintext_digest = pool["clip_digest"]
    expected_id, algo = crypto.blob_id("clip", plaintext_digest)
    assert algo == "hmac-sha256" and expected_id != plaintext_digest

    keys = store.keys_under("hear/")
    assert any(K.blob_key(expected_id, algo=algo, tenant="dama") == k for k in keys)
    # The oracle test: the plaintext digest is in no key and in no stored byte, anywhere.
    assert not any(plaintext_digest in k for k in keys)
    assert plaintext_digest.encode() not in _all_stored_bytes(store)


def test_the_plaintext_digest_reaches_no_ledger_row_and_no_quarantine_record(
        pool, store, tmp_path, monkeypatch):
    crypto = C.synthetic_crypto()
    ledger_path = str(tmp_path / "work" / RUN / "ledger.jsonl")
    _importer(store, tmp_path, crypto=crypto).run([_clip_task(pool)])
    assert pool["clip_digest"] not in open(ledger_path).read()

    # And on the failure path, where a careless "expected: <digest>" is the usual leak.
    monkeypatch.setattr(store, "iter_range",
                        lambda key, offset=0, length=None, **kw: iter([b"nope"]))
    run2 = RUN + "-b"
    report = _importer(store, tmp_path, run=run2, crypto=crypto).run([_clip_task(pool)])
    doc = report.quarantined[0]
    assert doc["digests_withheld"] == "restricted_class"
    assert pool["clip_digest"] not in json.dumps(doc)
    assert pool["clip_digest"] not in open(str(tmp_path / "work" / run2 / "ledger.jsonl")).read()


def test_the_clear_metadata_carries_the_label_and_the_sealed_document_carries_the_secret(
        pool, store, tmp_path):
    crypto = C.synthetic_crypto()
    _importer(store, tmp_path, crypto=crypto).run([_clip_task(pool)])
    mkey = [k for k in store.keys_under("hear/v1/meta/")][0]
    meta = json.loads(store.get_range(mkey))

    assert meta["governance"]["sensitivity"] == ["ambient_audio"]
    assert meta["blob"]["algo"] == "hmac-sha256"
    assert "digest" not in meta["blob"]
    assert meta["blob"]["plaintext_digest_location"] == "sealed"
    assert meta["encryption"]["boundary"] == "client-side"
    assert pool["clip_digest"] not in json.dumps(meta)

    # The sealed sub-document opens only with the key, and then it holds the plaintext digest.
    sealed = C.SealedObject(blob_id=meta["blob"]["blob_id"], blob_algo="hmac-sha256",
                            tenant_id="dama", plaintext_digest="", plaintext_bytes=0,
                            ciphertext_bytes=0, ciphertext_digest="",
                            key_ref=C.KeyRef(**meta["encryption"]["key_ref"]),
                            cipher_name=meta["encryption"]["cipher"],
                            sealed_metadata=meta["sealed"])
    assert crypto.open_metadata(sealed)["plaintext"]["digest"] == pool["clip_digest"]


def test_an_unrestricted_class_keeps_its_plaintext_content_address(pool, store, tmp_path):
    """Encryption is per class, not global: a record segment is still addressed by sha256."""
    seg = ST.ImportTask(object_class="record-seg", logical_id="0", partition=("2026-09-12", "mach"),
                        source_path=pool["records_path"], digest_source="computed-at-import")
    report = _importer(store, tmp_path, crypto=C.synthetic_crypto()).run([seg])
    assert report.counters["encrypted"] == 0
    digest = S.digest_source(S.file_chunks(pool["records_path"])).digest
    assert store.keys_under("hear/v1/blob/") == [K.blob_key(digest)]


# --------------------------------------------------------------- confidentiality


def test_the_stored_blob_is_not_the_plaintext_and_opens_back_to_it(pool, store, tmp_path):
    crypto = C.synthetic_crypto()
    _importer(store, tmp_path, crypto=crypto).run([_clip_task(pool)])
    blob_key = [k for k in store.keys_under("hear/v1/blob/")][0]
    ciphertext = store.get_range(blob_key)
    plaintext = open(pool["clip_paths"][0], "rb").read()

    assert ciphertext != plaintext
    assert plaintext[:16] not in ciphertext  # not even the RIFF header survives in the clear
    assert len(ciphertext) == len(plaintext) + C.TAG_BYTES

    mkey = [k for k in store.keys_under("hear/v1/meta/")][0]
    meta = json.loads(store.get_range(mkey))
    sealed = C.SealedObject(blob_id=meta["blob"]["blob_id"], blob_algo="hmac-sha256",
                            tenant_id="dama", plaintext_digest="", plaintext_bytes=0,
                            ciphertext_bytes=len(ciphertext), ciphertext_digest="",
                            key_ref=C.KeyRef(**meta["encryption"]["key_ref"]),
                            cipher_name=meta["encryption"]["cipher"],
                            sealed_metadata=meta["sealed"])
    opened = crypto.open_stream(sealed, store.range_source(blob_key))
    assert b"".join(opened()) == plaintext


def test_a_tampered_ciphertext_does_not_open(pool, store, tmp_path):
    crypto = C.synthetic_crypto()
    _importer(store, tmp_path, crypto=crypto).run([_clip_task(pool)])
    blob_key = [k for k in store.keys_under("hear/v1/blob/")][0]
    body = bytearray(store.get_range(blob_key))
    body[4] ^= 0xFF
    store.corrupt(blob_key, bytes(body))

    mkey = [k for k in store.keys_under("hear/v1/meta/")][0]
    meta = json.loads(store.get_range(mkey))
    sealed = C.SealedObject(blob_id=meta["blob"]["blob_id"], blob_algo="hmac-sha256",
                            tenant_id="dama", plaintext_digest="", plaintext_bytes=0,
                            ciphertext_bytes=0, ciphertext_digest="",
                            key_ref=C.KeyRef(**meta["encryption"]["key_ref"]),
                            cipher_name=meta["encryption"]["cipher"],
                            sealed_metadata=meta["sealed"])
    with pytest.raises(C.TamperDetected):
        b"".join(crypto.open_stream(sealed, store.range_source(blob_key))())


def test_a_source_that_changes_between_the_digest_pass_and_the_seal_pass_is_refused(
        store, tmp_path):
    """Convergent sealing reads twice; a source that moved in between must not be published."""
    state = {"n": 0}

    def shifting():
        state["n"] += 1
        return iter([b"first read" if state["n"] == 1 else b"second read"])

    task = ST.ImportTask(object_class="clip", logical_id="9f2c" + "0" * 28,
                         partition=("2026-09-12", "mach"), chunks=shifting,
                         digest_source="computed-at-import")
    report = _importer(store, tmp_path, crypto=C.synthetic_crypto()).run([task])
    assert [q["error_class"] for q in report.quarantined] == ["source_changed_during_import"]
    assert store.keys_under("hear/v1/obj/") == []


# ----------------------------------------------------------------- dedupe


def test_two_identical_restricted_objects_converge_on_one_blob_inside_a_tenant(
        pool, store, tmp_path):
    crypto = C.synthetic_crypto()
    a, b = pool["clip_paths"]
    report = _importer(store, tmp_path, crypto=crypto).run([
        _clip_task(pool, a, "9f2c" + "0" * 28, "2026-09-12"),
        _clip_task(pool, b, "7e1d" + "0" * 28, "unanchored"),
    ])
    assert report.counters["published"] == 2
    assert report.counters["deduped"] == 1
    assert len(store.keys_under("hear/v1/blob/")) == 1
    assert len(store.keys_under("hear/v1/obj/")) == 2


def test_a_rerun_of_a_restricted_object_reseals_to_the_same_bytes(pool, store, tmp_path):
    """Non-convergent sealing would make every re-run look like a corrupt blob."""
    crypto = C.synthetic_crypto()
    _importer(store, tmp_path, crypto=crypto).run([_clip_task(pool)])
    blob_key = store.keys_under("hear/v1/blob/")[0]
    first = store.get_range(blob_key)

    # A different run id and a fresh crypto object: nothing is carried over but the key seed.
    report = _importer(store, tmp_path, run=RUN + "-again",
                       crypto=C.synthetic_crypto()).run([_clip_task(pool)])
    assert report.counters["replayed"] == 1
    assert report.counters["quarantined"] == 0
    assert store.get_range(blob_key) == first


def test_two_tenants_do_not_share_a_blob_id_for_identical_bytes(pool, store, tmp_path):
    """Cross-tenant dedupe is a leak, not an optimisation (`docs/data-governance.md` :77-81)."""
    one = C.synthetic_crypto("dama")
    two = C.synthetic_crypto("other")
    _importer(store, tmp_path, crypto=one, tenant_id="dama").run([_clip_task(pool)])
    _importer(store, tmp_path, run=RUN + "-t2", crypto=two,
              tenant_id="other").run([_clip_task(pool)])

    blobs = store.keys_under("hear/v1/blob/")
    assert len(blobs) == 2
    assert len({b.rsplit("/", 1)[-1] for b in blobs}) == 2  # different ids, not just prefixes
    assert one.blob_id("clip", pool["clip_digest"])[0] != two.blob_id("clip", pool["clip_digest"])[0]
