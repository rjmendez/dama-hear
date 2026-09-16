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

import hashlib
import hmac
import inspect
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

#: ⚠️A TEST SEED, PINNED IN A TEST FILE ON PURPOSE. `InMemoryTestKeyProvider` no longer carries a
#: default seed and `synthetic_crypto` makes a random one per process, so a test that needs two
#: runs to converge on one ciphertext has to say which synthetic key material it means. That is
#: the whole point of the change: the only way to get a reproducible key here is to name it in a
#: test, and nothing importable can reach one by accident.
SEED = b"phase3-objectstore-crypto-test-seed"


def _crypto(tenant_id="dama", seed=SEED):
    return C.synthetic_crypto(tenant_id, seed)


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
        C.ObjectCrypto(C.InMemoryTestKeyProvider(SEED), cipher=C.HmacCtrCipher(),
                       allow_test_provider=True)
    assert C.HmacCtrCipher.production_ready is False
    assert C.InMemoryTestKeyProvider.is_test_only is True


# --------------------------------------- the test provider is a test provider


def test_a_test_only_key_provider_is_refused_unless_a_test_asks_for_it_by_name():
    """⚠️THE UNREAD-FLAG REGRESSION. `is_test_only` was documented as checked, and was not.

    The class docstring said `ObjectCrypto` checked the flag; nothing did, so the provider whose
    keys are recomputable by anyone holding this repository could be handed to a production wiring
    and would seal real evidence. Both halves of the boundary are now admitted by name or refused.
    """
    provider = C.InMemoryTestKeyProvider(SEED)
    with pytest.raises(C.KeyUnavailable) as refused:
        C.ObjectCrypto(provider, cipher=C.HmacCtrCipher(), allow_test_cipher=True)
    assert "test-only key provider" in str(refused.value)

    # The refusal comes before anything can be sealed, not after a first object goes out.
    with pytest.raises(C.KeyUnavailable):
        C.ObjectCrypto(provider)  # even paired with the refusing cipher
    assert C.ObjectCrypto(provider, cipher=C.HmacCtrCipher(), allow_test_cipher=True,
                          allow_test_provider=True).can_seal is True


def test_no_synthetic_seed_is_reachable_without_a_caller_choosing_one():
    """⚠️THE DEFAULT-SEED REGRESSION: a key everyone already has is not a key.

    `InMemoryTestKeyProvider()` used to default to a literal in `crypto.py`, so the module shipped
    a complete, working, identical-everywhere key hierarchy that any call site could reach with no
    argument. A seed is now required, is bounded below, and `synthetic_crypto` generates a random
    per-process one rather than reintroducing the constant one level up.
    """
    with pytest.raises(TypeError):
        C.InMemoryTestKeyProvider()
    with pytest.raises(C.KeyUnavailable):
        C.InMemoryTestKeyProvider(b"short")
    with pytest.raises(C.KeyUnavailable):
        C.InMemoryTestKeyProvider("a string is not key material" * 2)

    # No default in either signature: the provider has none at all, and the wiring helper's
    # `None` is "make a random one now", not "use the one in the file".
    seed_param = inspect.signature(C.InMemoryTestKeyProvider).parameters["seed"]
    assert seed_param.default is inspect.Parameter.empty
    assert inspect.signature(C.synthetic_crypto).parameters["seed"].default is None
    # Two unseeded wirings must not agree about anything, which is what a shipped seed destroys.
    a, b = C.synthetic_crypto(), C.synthetic_crypto()
    assert a.provider.tenant_index_key("dama") != b.provider.tenant_index_key("dama")
    assert a.blob_id("clip", "a" * 64)[0] != b.blob_id("clip", "a" * 64)[0]


def test_the_key_provider_writes_no_key_material_anywhere(tmp_path):
    """A provider that persisted a key would leave it in the tree the test owns. None appears."""
    cwd_before = ST.census(str(tmp_path))
    crypto = _crypto()
    key = crypto.provider.data_key("dama", "clip", "a" * 64)
    assert ST.census(str(tmp_path)) == cwd_before
    assert not any(hasattr(crypto.provider, attr) for attr in ("save", "path", "keyfile"))
    assert key.key not in os.environ.get("PATH", "").encode()


# ------------------------------------------------- identity without an oracle


def test_a_restricted_blob_is_addressed_by_its_hmac_id_and_never_by_its_plaintext_digest(
        pool, store, tmp_path):
    crypto = _crypto()
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
    crypto = _crypto()
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
    crypto = _crypto()
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
    report = _importer(store, tmp_path, crypto=_crypto()).run([seg])
    assert report.counters["encrypted"] == 0
    digest = S.digest_source(S.file_chunks(pool["records_path"])).digest
    assert store.keys_under("hear/v1/blob/") == [K.blob_key(digest)]


# --------------------------------------------------------------- confidentiality


def test_the_stored_blob_is_not_the_plaintext_and_opens_back_to_it(pool, store, tmp_path):
    crypto = _crypto()
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
    crypto = _crypto()
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
    report = _importer(store, tmp_path, crypto=_crypto()).run([task])
    assert [q["error_class"] for q in report.quarantined] == ["source_changed_during_import"]
    assert store.keys_under("hear/v1/obj/") == []


# ----------------------------------------------------------------- dedupe


def test_two_identical_restricted_objects_converge_on_one_blob_inside_a_tenant(
        pool, store, tmp_path):
    crypto = _crypto()
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
    crypto = _crypto()
    _importer(store, tmp_path, crypto=crypto).run([_clip_task(pool)])
    blob_key = store.keys_under("hear/v1/blob/")[0]
    first = store.get_range(blob_key)

    # A different run id and a fresh crypto object: nothing is carried over but the key seed.
    report = _importer(store, tmp_path, run=RUN + "-again",
                       crypto=_crypto()).run([_clip_task(pool)])
    assert report.counters["replayed"] == 1
    assert report.counters["quarantined"] == 0
    assert store.get_range(blob_key) == first


def test_two_tenants_do_not_share_a_blob_id_for_identical_bytes(pool, store, tmp_path):
    """Cross-tenant dedupe is a leak, not an optimisation (`docs/data-governance.md` :77-81)."""
    one = _crypto("dama")
    two = _crypto("other")
    _importer(store, tmp_path, crypto=one, tenant_id="dama").run([_clip_task(pool)])
    _importer(store, tmp_path, run=RUN + "-t2", crypto=two,
              tenant_id="other").run([_clip_task(pool)])

    blobs = store.keys_under("hear/v1/blob/")
    assert len(blobs) == 2
    assert len({b.rsplit("/", 1)[-1] for b in blobs}) == 2  # different ids, not just prefixes
    assert one.blob_id("clip", pool["clip_digest"])[0] != two.blob_id("clip", pool["clip_digest"])[0]


# ------------------------------------- domain separation and the nonce oracle


def test_hkdf_matches_the_rfc_5869_test_vector():
    """The derivation is the published one, not something shaped like it."""
    ikm = bytes.fromhex("0b" * 22)
    salt = bytes.fromhex("000102030405060708090a0b0c")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    prk = C.hkdf_extract(salt, ikm)
    assert prk.hex() == "077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5"
    assert C.hkdf_expand(prk, info, 42).hex() == (
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865")


def test_the_body_and_the_metadata_never_share_a_key_or_a_nonce():
    dek = bytes(range(32))
    context = b"in-memory-test|dama|clip|kek-v1|hmac-ctr-etm-sha256"
    body_key, body_nonce = C.derive_stream_material(dek, "body", context)
    meta_key, meta_nonce = C.derive_stream_material(dek, "metadata", context)

    assert body_key != meta_key
    assert body_nonce != meta_nonce
    assert len({body_key, meta_key, body_nonce, meta_nonce}) == 4
    assert dek not in (body_key, meta_key)           # the DEK itself is never a stream key
    # A different key ref or cipher is a different context, so it is a different keystream too.
    other, _ = C.derive_stream_material(dek, "body", context.replace(b"kek-v1", b"kek-v2"))
    assert other != body_key


def test_xoring_the_body_against_the_sealed_metadata_recovers_nothing(pool, store, tmp_path):
    """⚠️THE CTR KEYSTREAM REUSE REGRESSION.

    The sealed metadata used to be sealed with the body's `(key, nonce)`. In a CTR construction
    that publishes `body XOR metadata` to anyone holding both ciphertexts -- and the metadata
    plaintext is a JSON shape an attacker can write out from the schema, so subtracting it yields
    the body in the clear. This test performs that attack and requires it to fail.
    """
    crypto = _crypto()
    _importer(store, tmp_path, crypto=crypto).run([_clip_task(pool)])
    body_ct = store.get_range(store.keys_under("hear/v1/blob/")[0])[:-C.TAG_BYTES]
    meta = json.loads(store.get_range(store.keys_under("hear/v1/meta/")[0]))
    meta_ct = bytes.fromhex(meta["sealed"]["ciphertext_hex"])[:-C.TAG_BYTES]
    plaintext = open(pool["clip_paths"][0], "rb").read()

    # The attacker knows the metadata plaintext exactly: it is a schema-shaped document whose one
    # unknown, the digest, they are trying to confirm. Give them the real thing -- the strongest
    # version of the attack -- and the body must still not fall out.
    known = K.canonical_json({"plaintext": {"algo": "sha256", "digest": pool["clip_digest"],
                                            "bytes": pool["clip_bytes"]}})
    n = min(len(body_ct), len(meta_ct), len(known))
    assert n > 32
    recovered = bytes(a ^ b ^ c for a, b, c in zip(body_ct[:n], meta_ct[:n], known[:n]))
    assert recovered != plaintext[:n]
    assert plaintext[:16] not in recovered


def test_no_published_field_confirms_a_guessed_plaintext(pool, store, tmp_path):
    """⚠️THE CONFIRMATION ORACLE REGRESSION, run as a search rather than a single assertion.

    The nonce used to be `sha256("nonce/" + plaintext_digest + wrapped_key)[:16]`, and both inputs
    were published -- so anyone who could guess the bytes could recompute it and compare, with no
    key at all. Here the attacker *has* the plaintext (the strongest guess there is) and every
    published byte, and must still not be able to reproduce a single published field.
    """
    crypto = _crypto()
    _importer(store, tmp_path, crypto=crypto).run([_clip_task(pool)])
    keys = store.keys_under("hear/")
    published = "\n".join(keys) + "\n" + _all_stored_bytes(store).decode("latin-1")

    guess = open(pool["clip_paths"][0], "rb").read()
    digest = K.sha256_hex(guess)
    assert digest == pool["clip_digest"]
    meta = json.loads(store.get_range(store.keys_under("hear/v1/meta/")[0]))
    wrapped = bytes.fromhex(meta["sealed"]["wrapped_key_hex"])

    # Everything an attacker can compute from the guess plus the public metadata.
    candidates = {
        "plaintext_digest": digest,
        "old_public_nonce": hashlib.sha256(b"nonce/" + digest.encode() + b"/"
                                           + wrapped).digest()[:C.NONCE_BYTES].hex(),
        "digest_of_guess_bytes": hashlib.sha256(guess).hexdigest(),
        "dek_shaped": hashlib.sha256(b"dek/" + digest.encode()).hexdigest(),
        "hmac_of_digest_under_itself": hmac.new(digest.encode(), guess,
                                                hashlib.sha256).hexdigest(),
    }
    for purpose in ("body", "metadata"):
        key, nonce = C.derive_stream_material(hashlib.sha256(digest.encode()).digest(), purpose,
                                              b"in-memory-test|dama|clip|kek-v1|"
                                              + crypto.cipher.name.encode())
        candidates["keyless_%s_key" % purpose] = key.hex()
        candidates["keyless_%s_nonce" % purpose] = nonce.hex()

    for name, value in candidates.items():
        assert value not in published, "a keyless derivation (%s) appears in published data" % name
    assert "nonce_hex" not in meta["sealed"]  # the nonce is not published at all any more

    # The positive control: confirmation is possible *with* the key, which is what makes the
    # negative results above mean something rather than being a property of an unrelated blob.
    sealed_key = crypto.provider.data_key("dama", "clip", digest)
    body_key, body_nonce = C.derive_stream_material(sealed_key.key, "body",
                                                    crypto._context(sealed_key.ref))
    resealed = b"".join(crypto.cipher.seal(body_key, body_nonce, [guess]))
    assert resealed == store.get_range(store.keys_under("hear/v1/blob/")[0])
    assert crypto.blob_id("clip", digest)[0] in "\n".join(keys)


def test_the_wrapped_key_cannot_be_reproduced_without_the_kek(pool):
    """It is convergent on the digest, so it must not be *computable* from the digest."""
    provider = C.InMemoryTestKeyProvider(SEED)
    digest = pool["clip_digest"]
    wrapped = provider.data_key("dama", "clip", digest).wrapped
    assert wrapped != bytes.fromhex(digest[:64])
    assert wrapped != hashlib.sha256(digest.encode()).digest()
    # A provider with a different seed -- an attacker guessing the KEK -- gets different material.
    assert C.InMemoryTestKeyProvider(b"another-seed-entirely").data_key("dama", "clip",
                                                              digest).wrapped != wrapped


# ---------------------------------------------- one DEK unwraps one object


def test_one_disclosed_dek_does_not_unwrap_the_rest_of_its_class():
    """⚠️THE CLASS-WIDE WRAPPING MASK REGRESSION, run as the attack it was.

    The wrapping used to be `dek XOR mask(tenant, class)` with one mask for the whole class, so an
    attacker who learned a single DEK -- one unwrapped object, one debug dump, one reader bug --
    recovered `mask = dek XOR wrapped` and unwrapped **every other object of that class** from the
    published metadata alone. The wrapping is now salted per object, so the leaked-DEK attack
    recovers exactly one object: the one that leaked.
    """
    provider = C.InMemoryTestKeyProvider(SEED)
    victim = provider.data_key("dama", "clip", "a" * 64)
    others = [provider.data_key("dama", "clip", ch * 64) for ch in "bcdef"]

    body = lambda w: w[C.WRAP_SALT_BYTES:C.WRAP_SALT_BYTES + C.KEY_BYTES]
    leaked_mask = bytes(x ^ y for x, y in zip(victim.key, body(victim.wrapped)))
    for other in others:
        forged = bytes(x ^ y for x, y in zip(leaked_mask, body(other.wrapped)))
        assert forged != other.key, "a leaked DEK still unwraps another object of its class"
    # And the salts really are per object rather than one constant with a new name.
    salts = {w.wrapped[:C.WRAP_SALT_BYTES] for w in others + [victim]}
    assert len(salts) == len(others) + 1


def test_a_wrapped_key_round_trips_and_is_refused_under_the_wrong_kek():
    """Unwrapping is authenticated: a wrapping this KEK did not produce is refused, not decoded.

    An unauthenticated XOR unwrap returns 32 bytes of plausible key for *any* input, so a key
    substituted by an attacker or a corrupted metadata document turns into a stream key and fails
    far away -- or does not fail at all, in a reader that treats the result as data.
    """
    provider = C.InMemoryTestKeyProvider(SEED)
    sealed = provider.data_key("dama", "clip", "a" * 64)
    assert provider.open_key(sealed.ref, sealed.wrapped) == sealed.key
    assert len(sealed.wrapped) == C.WRAP_SALT_BYTES + C.KEY_BYTES + C.TAG_BYTES

    flipped = bytearray(sealed.wrapped)
    flipped[C.WRAP_SALT_BYTES] ^= 0x01
    with pytest.raises(C.TamperDetected):
        provider.open_key(sealed.ref, bytes(flipped))
    with pytest.raises(C.TamperDetected):
        provider.open_key(sealed.ref, sealed.wrapped[:-1])
    # Another class under the same tenant is another KEK, so its wrapping does not open here.
    other_class = provider.data_key("dama", "raw", "a" * 64)
    with pytest.raises(C.TamperDetected):
        provider.open_key(sealed.ref, other_class.wrapped)
    with pytest.raises(C.TamperDetected):
        C.InMemoryTestKeyProvider(b"another-seed-entirely").open_key(sealed.ref, sealed.wrapped)


def test_wrapping_is_still_deterministic_so_a_re_seal_is_byte_identical():
    """Per-object must not mean per-run: metadata is digest-addressed and a replay must be a no-op."""
    one = C.InMemoryTestKeyProvider(SEED).data_key("dama", "clip", "a" * 64)
    two = C.InMemoryTestKeyProvider(SEED).data_key("dama", "clip", "a" * 64)
    assert one.wrapped == two.wrapped and one.key == two.key


# --------------------------------------- precise location is restricted data


TDOA_ROW = (b'{"arrival_us":1758000000123456,"lat":40.2925221,"lon":-79.1221604,"node":"mach"}\n')


def _tdoa_task(tmp_path, object_class="tdoa-arrival-seg", name="tdoa.jsonl"):
    path = tmp_path / name
    path.write_bytes(TDOA_ROW)
    return ST.ImportTask(object_class=object_class, logical_id="0",
                         partition=("2026-09-12", "mach"), source_path=str(path),
                         digest_source="computed-at-import")


@pytest.mark.parametrize("object_class", ["tdoa-arrival-seg", "tdoa-run"])
def test_a_tdoa_class_cannot_be_published_without_a_key_provider(store, tmp_path, object_class):
    """The label is `precise_location`, so the refusal is the same one `clip` and `raw` get."""
    task = _tdoa_task(tmp_path, object_class, name="%s.jsonl" % object_class)
    report = _importer(store, tmp_path).run([task])
    assert [q["error_class"] for q in report.quarantined] == ["key_provider_unavailable"]
    assert store.keys_under("hear/v1/obj/") == []
    assert store.keys_under("hear/v1/blob/") == []


def test_a_tdoa_arrival_publishes_no_plaintext_digest_and_no_plaintext_coordinate(store, tmp_path):
    """⚠️THE RAW-DIGEST LEAK, end to end: an arrival row is guessable, so its digest is an oracle."""
    crypto = _crypto()
    task = _tdoa_task(tmp_path)
    report = _importer(store, tmp_path, crypto=crypto).run([task])
    assert report.counters["published"] == 1
    assert report.counters["encrypted"] == 1

    plaintext_digest = K.sha256_hex(TDOA_ROW)
    keys = store.keys_under("hear/")
    stored = _all_stored_bytes(store)
    assert not any(plaintext_digest in k for k in keys)
    assert plaintext_digest.encode() not in stored
    assert b"40.2925221" not in stored                      # the coordinate itself never lands
    assert plaintext_digest not in open(
        str(tmp_path / "work" / RUN / "ledger.jsonl")).read()

    bid, algo = crypto.blob_id("tdoa-arrival-seg", plaintext_digest)
    assert algo == "hmac-sha256"
    assert K.blob_key(bid, algo=algo, tenant="dama") in keys
    meta = json.loads(store.get_range([k for k in keys if "/meta/" in k][0]))
    assert meta["governance"]["sensitivity"] == ["precise_location"]
    assert meta["blob"]["plaintext_digest_location"] == "sealed"
    assert "digest" not in meta["blob"]


def test_two_tenants_do_not_share_a_tdoa_blob_for_identical_arrivals(store, tmp_path):
    """Cross-tenant dedupe on a coordinate is a cross-tenant disclosure that it is the same place."""
    task = _tdoa_task(tmp_path)
    _importer(store, tmp_path, crypto=_crypto("dama"), tenant_id="dama").run([task])
    _importer(store, tmp_path, run=RUN + "-t2", crypto=_crypto("other"),
              tenant_id="other").run([task])
    blobs = store.keys_under("hear/v1/blob/")
    assert len(blobs) == 2
    assert len({b.rsplit("/", 1)[-1] for b in blobs}) == 2


# ------------------------------------------- the default cipher seals nothing


def test_the_default_object_crypto_names_no_cipher_and_can_seal_nothing():
    crypto = C.ObjectCrypto()
    assert isinstance(crypto.cipher, C.RefusingCipher)
    assert crypto.cipher.production_ready is False
    assert crypto.can_seal is False
    with pytest.raises(C.KeyUnavailable):
        b"".join(crypto.cipher.seal(b"k" * 32, b"n" * 16, [b"x"]))
    with pytest.raises(C.KeyUnavailable):
        b"".join(crypto.cipher.open(b"k" * 32, b"n" * 16, [b"x"]))


def test_a_key_provider_alone_does_not_make_an_importer_able_to_encrypt(pool, store, tmp_path):
    """⚠️THE DEFAULT-CIPHER REGRESSION: key material must not be enough to start sealing.

    The default used to construct `HmacCtrCipher` and pass `allow_test_cipher=True` on the
    caller's behalf, so the one guard that keeps a test cipher out of a real run was defeated by
    the constructor that was supposed to enforce it. Now the default cipher refuses, so an operator
    who wires up a KMS and forgets to choose a cipher publishes nothing rather than test-grade
    ciphertext.
    """
    crypto = C.ObjectCrypto(C.InMemoryTestKeyProvider(SEED), tenant_id="dama",
                            allow_test_provider=True)
    assert crypto.can_seal is False
    report = _importer(store, tmp_path, crypto=crypto).run([_clip_task(pool)])
    assert [q["error_class"] for q in report.quarantined] == ["key_provider_unavailable"]
    assert store.keys_under("hear/v1/obj/") == []
    assert store.keys_under("hear/v1/blob/") == []


def test_the_importers_own_default_is_the_refusing_pair(store, tmp_path):
    crypto = _importer(store, tmp_path).crypto
    assert isinstance(crypto.cipher, C.RefusingCipher)
    assert isinstance(crypto.provider, C.NoKeyProvider)
    assert crypto.can_seal is False


def test_the_test_cipher_still_has_to_be_asked_for_by_name():
    with pytest.raises(C.KeyUnavailable):
        C.ObjectCrypto(C.InMemoryTestKeyProvider(SEED), cipher=C.HmacCtrCipher())
    assert _crypto().can_seal is True
    assert _crypto().cipher.production_ready is False
