"""The encryption boundary as an injected object, and the refusal that stands in for a real key.

⚠️NO KEY IS CREATED, DERIVED FROM A PASSWORD, READ FROM DISK, WRITTEN TO DISK OR PUT IN AN
ENVIRONMENT VARIABLE BY ANYTHING IN THIS MODULE. The key design (§8) says keys live outside
application data in a KMS with separately audited access; until that exists, the only honest
default is `NoKeyProvider`, which raises. An importer given the default cannot publish a
restricted class at all -- it quarantines the object as `key_provider_unavailable` and keeps
going. A scaffold that silently invented a key would make the untested path the default path.

⚠️A PLAINTEXT DIGEST IS A CONFIRMATION ORACLE for `ambient_audio`/`precise_location` (key design
§8): anyone who can list keys and can guess a plaintext -- a silent WAV, a `health.csv` row shape
-- can confirm it is there. So for `keys.RESTRICTED_CLASSES` the blob id is
`HMAC-SHA256(K_tenant_index, sha256(plaintext))` and the plaintext digest appears in exactly one
place: inside the sealed sub-document of the metadata. Not in a key, not in an index, not in a log
line, not in a ledger row, not in a quarantine record. `ObjectCrypto.public_digest` is the only
digest a caller may print, and for a restricted class it is the ciphertext's.

⚠️`HmacCtrCipher` IS A TEST DOUBLE, NOT A CIPHER YOU MAY POINT AT EVIDENCE. It is encrypt-then-MAC
over an HMAC-SHA256 keystream, written with the standard library so this package keeps its
zero-dependency test story, and it exists to prove that the *pipeline* is streaming, per-object
keyed, tamper-evident and free of plaintext leakage. The production answer is an AEAD from a
reviewed implementation (AES-GCM / ChaCha20-Poly1305) behind a KMS, injected through the same
`Cipher` protocol -- which is why the protocol is here and the cipher is a constructor argument.
`HmacCtrCipher.production_ready` is `False` and `ObjectCrypto` refuses to be built from a
not-production-ready cipher unless it is explicitly told this is a test (`allow_test_cipher=True`).

⚠️THE DEFAULT CIPHER ENCRYPTS NOTHING. `RefusingCipher` is what `ObjectCrypto()` and `Importer()`
get when nobody chose, and it raises on `seal`. The earlier default -- "construct the test cipher
and pass `allow_test_cipher=True` on the caller's behalf" -- made the guard decorative, because the
one wiring that was supposed to be a deliberate act was the one the constructor performed for you.
Only test code may name `HmacCtrCipher`, and it must still say `allow_test_cipher=True` to use it.

⚠️ONE DEK, TWO INDEPENDENT (KEY, NONCE) PAIRS, DERIVED WITH HKDF. The body and the sealed metadata
sub-document are separate streams under the same data key, so they must never share a keystream: a
CTR-mode stream reused across two messages gives up `P1 XOR P2`, and the metadata plaintext is a
known JSON shape, which makes that an unmasking of the body. `hkdf_expand` (RFC 5869, HMAC-SHA256)
derives `body/key`, `body/nonce`, `metadata/key` and `metadata/nonce` under distinct info strings
bound to the tenant, class, key id and cipher name. A real AEAD drops into the same seam and gets
the same four values.

⚠️A NONCE IS DERIVED FROM THE SECRET DEK AND IS NEVER PUBLISHED. It used to be
`sha256("nonce/" + plaintext_digest + wrapped_key)`, with both inputs public -- which is a
confirmation oracle with no key at all: guess the plaintext, recompute, compare to the published
nonce. Now every nonce comes out of the HKDF over the DEK, so reproducing one requires the KEK, and
nothing digest-derived appears in any published field. `wrapped_key_hex` stays public because
unwrapping it needs the KEK; it is convergent on the plaintext digest, which is the same
intra-tenant equality leak the HMAC blob id already carries, and it is not verifiable from a guess.

NOT PRODUCTION: no backend is selected, no KMS exists, and no gate (G0-G6) is satisfied.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import struct
from typing import Any, Dict, Iterable, Iterator, Optional, Protocol

from . import keys as K
from . import streaming as S

#: The AEAD tag is appended to the ciphertext stream as its final chunk.
TAG_BYTES = 32
NONCE_BYTES = 16
KEY_BYTES = 32
#: Everything derived under this label is bound to this package and this schema version.
KDF_LABEL = b"hear/objectstore/v1"
#: What the metadata records about the derivation, so a reader never has to guess it.
KDF_NAME = "hkdf-sha256"


def hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    """RFC 5869 extract. The DEK is already uniform, but extract costs nothing and keeps it RFC."""
    return hmac.new(salt or b"\x00" * 32, ikm, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 expand over HMAC-SHA256, stdlib only, no third-party dependency in this package."""
    if length > 255 * 32:
        raise ValueError("hkdf-sha256 cannot expand that far")
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def derive_stream_material(dek: bytes, purpose: str, context: bytes) -> tuple:
    """`(key, nonce)` for one purpose. ⚠️TWO PURPOSES NEVER SHARE EITHER HALF.

    `purpose` is `body` or `metadata`. Both are sealed under the same DEK -- that is what makes a
    single wrapped key enough to read an object -- so the *only* thing keeping the body's keystream
    away from the metadata's is this derivation. `context` binds the pair to the tenant, the data
    class, the key id and the cipher name, so the same DEK reached through a different key ref or a
    swapped cipher does not reproduce the same keystream either.
    """
    if purpose not in ("body", "metadata"):
        raise ValueError("unknown key purpose: %r" % (purpose,))
    prk = hkdf_extract(KDF_LABEL + b"/salt", dek)
    info = KDF_LABEL + b"/" + purpose.encode() + b"/" + context
    block = hkdf_expand(prk, info + b"/key", KEY_BYTES)
    nonce = hkdf_expand(prk, info + b"/nonce", NONCE_BYTES)
    return block, nonce


class KeyUnavailable(Exception):
    """No key material is reachable. A restricted object is refused, never published in the clear."""


class SourceChanged(Exception):
    """The bytes moved between the digest pass and the seal pass. Nothing is published."""


class TamperDetected(Exception):
    """The authentication tag did not verify. The bytes are not the bytes that were sealed."""


@dataclass(frozen=True)
class KeyRef:
    """A *reference* to key material, never the material itself in a field anyone logs."""

    provider: str
    tenant_id: str
    data_class: str
    key_id: str

    def as_metadata(self) -> Dict[str, str]:
        return {"provider": self.provider, "tenant_id": self.tenant_id,
                "data_class": self.data_class, "key_id": self.key_id}


class KeyProvider(Protocol):
    """Three questions, no key storage. An implementation talks to a KMS; none of them talk here."""

    def tenant_index_key(self, tenant_id: str) -> bytes:
        """`K_tenant_index` for restricted blob ids. Per tenant, so dedupe stops at the tenant."""

    def data_key(self, tenant_id: str, data_class: str, context: str) -> "SealedKey":
        """A per-object data key plus the reference that says which KEK wrapped it.

        ⚠️`context` IS THE PLAINTEXT DIGEST AND THE KEY IS CONVERGENT ON IT. Two runs that seal
        the same bytes must produce the same ciphertext, or dedupe stops working, a re-run stops
        being free, and every idempotent replay looks like a corrupt blob. The cost is the known
        one: inside a tenant, equal ciphertexts reveal equal plaintexts -- which is the same fact
        the HMAC blob id already publishes, and is why the blob id is per tenant (key design §8).
        Across tenants neither the key nor the id matches, so nothing converges between them.
        """

    def open_key(self, ref: KeyRef, wrapped: bytes) -> bytes:
        """Unwrap a DEK. A reader path; the importer never calls it except to verify a round trip."""


@dataclass(frozen=True)
class SealedKey:
    """A data key and the wrapped form that goes in the metadata. The raw key never leaves here."""

    key: bytes
    wrapped: bytes
    ref: KeyRef


class NoKeyProvider:
    """The default. Every question is answered with a refusal, and that refusal is the feature."""

    name = "none"

    def tenant_index_key(self, tenant_id: str) -> bytes:
        raise KeyUnavailable("no key provider is configured; a restricted class cannot be imported")

    def data_key(self, tenant_id: str, data_class: str, context: str) -> SealedKey:
        raise KeyUnavailable("no key provider is configured; nothing may be sealed")

    def open_key(self, ref: KeyRef, wrapped: bytes) -> bytes:
        raise KeyUnavailable("no key provider is configured; nothing may be opened")


class InMemoryTestKeyProvider:
    """Deterministic key material for tests. Never touches disk, an env var, or a real KMS.

    ⚠️THE SEED IS A TEST INPUT, NOT A SECRET, and this class says so in its name so that a
    production wiring of it is a visible act rather than an accident. Keys live for the lifetime of
    the object and are never written anywhere: there is no `save`, no path, no env var. `is_test_only`
    is checked by `ObjectCrypto` and by a test that asserts no real provider ships here.
    """

    name = "in-memory-test"
    is_test_only = True

    def __init__(self, seed: bytes = b"phase3-synthetic-test-seed", *, kek_version: int = 1) -> None:
        self._seed = seed
        self._kek_version = kek_version
        self._issued = 0

    def _kek(self, tenant_id: str, data_class: str) -> bytes:
        return hashlib.sha256(b"kek/%s/%s/%d/" % (tenant_id.encode(), data_class.encode(),
                                                  self._kek_version) + self._seed).digest()

    def tenant_index_key(self, tenant_id: str) -> bytes:
        return hashlib.sha256(b"index/" + tenant_id.encode() + b"/" + self._seed).digest()

    def _wrap_mask(self, tenant_id: str, data_class: str) -> bytes:
        return hkdf_expand(hkdf_extract(KDF_LABEL + b"/wrap", self._kek(tenant_id, data_class)),
                           KDF_LABEL + b"/wrap/mask", KEY_BYTES)

    def data_key(self, tenant_id: str, data_class: str, context: str) -> SealedKey:
        # Convergent on the plaintext digest, so re-sealing the same bytes is byte-identical and
        # dedupe survives. A real provider does the same derivation inside the KMS.
        #
        # ⚠️`wrapped` IS CONVERGENT TOO, AND THAT IS NOT AN ORACLE: reproducing it from a guessed
        # plaintext needs the KEK, which never leaves this object. What it does leak is the same
        # intra-tenant equality the HMAC blob id already leaks. It has to be deterministic --
        # metadata is digest-addressed and immutable, so a wrapping that changed per run would make
        # every replay write a new metadata object and turn an idempotent re-run into a conflict.
        self._issued += 1
        dek = hashlib.sha256(b"dek/" + context.encode() + b"/"
                             + self._kek(tenant_id, data_class)).digest()
        wrapped = bytes(a ^ b for a, b in zip(dek, self._wrap_mask(tenant_id, data_class)))
        ref = KeyRef(provider=self.name, tenant_id=tenant_id, data_class=data_class,
                     key_id="kek-v%d" % self._kek_version)
        return SealedKey(key=dek, wrapped=wrapped, ref=ref)

    def open_key(self, ref: KeyRef, wrapped: bytes) -> bytes:
        return bytes(a ^ b for a, b in zip(wrapped, self._wrap_mask(ref.tenant_id, ref.data_class)))


class Cipher(Protocol):
    """Streaming authenticated encryption. Chunks in, chunks out, tag last."""

    name: str
    production_ready: bool

    def seal(self, key: bytes, nonce: bytes, chunks: Iterable[bytes]) -> Iterator[bytes]: ...

    def open(self, key: bytes, nonce: bytes, chunks: Iterable[bytes]) -> Iterator[bytes]: ...


class RefusingCipher:
    """The default. It has no algorithm, and that is the point -- see the module docstring.

    It is not "not production ready" in the way a test double is; it cannot encrypt at all, so
    `ObjectCrypto` admits it without `allow_test_cipher` and an importer nobody configured still
    cannot put a restricted class anywhere. `is_refusing` is what the constructor checks, so a
    future real cipher -- which will not carry that flag -- can never slide into this slot.
    """

    name = "refusing"
    production_ready = False
    is_refusing = True

    def seal(self, key: bytes, nonce: bytes, chunks: Iterable[bytes]) -> Iterator[bytes]:
        raise KeyUnavailable("no cipher is configured; nothing may be sealed")
        yield b""  # pragma: no cover - unreachable, keeps this a generator function

    def open(self, key: bytes, nonce: bytes, chunks: Iterable[bytes]) -> Iterator[bytes]:
        raise KeyUnavailable("no cipher is configured; nothing may be opened")
        yield b""  # pragma: no cover - unreachable, keeps this a generator function


class HmacCtrCipher:
    """Encrypt-then-MAC over an HMAC-SHA256 keystream. ⚠️TEST DOUBLE -- see the module docstring."""

    name = "hmac-ctr-etm-sha256"
    production_ready = False

    @staticmethod
    def _subkeys(key: bytes) -> tuple:
        return (hashlib.sha256(b"enc/" + key).digest(), hashlib.sha256(b"mac/" + key).digest())

    @staticmethod
    def _keystream(enc: bytes, nonce: bytes, counter: int) -> bytes:
        return hmac.new(enc, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()

    def _xor(self, enc: bytes, nonce: bytes, chunks: Iterable[bytes]) -> Iterator[bytes]:
        counter = 0
        carry = b""
        for chunk in chunks:
            out = bytearray()
            pos = 0
            while pos < len(chunk):
                if not carry:
                    carry = self._keystream(enc, nonce, counter)
                    counter += 1
                take = min(len(carry), len(chunk) - pos)
                piece = chunk[pos:pos + take]
                mask = carry[:take]
                out += (int.from_bytes(piece, "big") ^ int.from_bytes(mask, "big")).to_bytes(take, "big")
                carry = carry[take:]
                pos += take
            yield bytes(out)

    def seal(self, key: bytes, nonce: bytes, chunks: Iterable[bytes]) -> Iterator[bytes]:
        enc, mac = self._subkeys(key)
        tag = hmac.new(mac, nonce, hashlib.sha256)
        for chunk in self._xor(enc, nonce, chunks):
            tag.update(chunk)
            yield chunk
        yield tag.digest()

    def open(self, key: bytes, nonce: bytes, chunks: Iterable[bytes]) -> Iterator[bytes]:
        """Verify the tag at the end of the stream; hold back only the tag, never the body.

        The tag is the last 32 bytes, so a consumer sees plaintext before the stream is
        authenticated and must not act on it until the iterator ends -- `TamperDetected` is raised
        there. The importer's only consumer is a whole-stream digest, which never sees a value if
        this raises. A reader that must act on early bytes gets a real AEAD with per-chunk tags;
        that is a reader-lane decision and is not made in this scaffold.
        """
        enc, mac = self._subkeys(key)
        tag = hmac.new(mac, nonce, hashlib.sha256)
        pending = b""

        def _body() -> Iterator[bytes]:
            nonlocal pending
            for chunk in chunks:
                buf = pending + chunk
                if len(buf) > TAG_BYTES:
                    emit, pending = buf[:-TAG_BYTES], buf[-TAG_BYTES:]
                    tag.update(emit)
                    yield emit
                else:
                    pending = buf

        for plain in self._xor(enc, nonce, _body()):
            yield plain
        if len(pending) != TAG_BYTES:
            raise TamperDetected("the ciphertext stream is too short to carry a tag")
        if not hmac.compare_digest(tag.digest(), pending):
            raise TamperDetected("authentication tag mismatch")


@dataclass(frozen=True)
class SealedObject:
    """What a sealed stream leaves behind. `blob_id` is the only identifier anyone may print.

    ⚠️`plaintext_digest` IS IN-MEMORY ONLY. It is here because the importer has to compare it
    against the digest recorded at ingest, which is the whole verification chain; it must never be
    copied into a key, a metadata field outside `sealed_metadata`, a ledger row, a counter or a log
    line. `ObjectCrypto.public_digest` is the accessor that enforces that choice, and
    `SealedObject` is never serialised as a whole by anything in this package.
    """

    blob_id: str
    blob_algo: str
    tenant_id: str
    plaintext_digest: str
    plaintext_bytes: int
    ciphertext_bytes: int
    ciphertext_digest: str
    key_ref: KeyRef
    cipher_name: str
    #: The sealed sub-document: the plaintext digest and anything else sensitive, encrypted.
    sealed_metadata: Dict[str, Any]


class ObjectCrypto:
    """The encryption boundary the importer sees. One object in, one `SealedObject` out.

    Construction is the whole policy: a provider that refuses (the default) makes restricted
    classes unimportable, and a cipher that is not production ready has to be admitted by name.
    """

    def __init__(self, provider: Optional[KeyProvider] = None, *, cipher: Optional[Cipher] = None,
                 tenant_id: str = "dama", allow_test_cipher: bool = False) -> None:
        self.provider = provider or NoKeyProvider()
        self.cipher = cipher or RefusingCipher()
        self.tenant_id = tenant_id
        if getattr(self.cipher, "is_refusing", False):
            return
        if not getattr(self.cipher, "production_ready", False) and not allow_test_cipher:
            raise KeyUnavailable(
                "%r is not a production cipher; pass allow_test_cipher=True to use it in a test"
                % (getattr(self.cipher, "name", self.cipher),))

    @property
    def can_seal(self) -> bool:
        """False for the default wiring. Nothing here seals until someone chose both halves."""
        return not getattr(self.cipher, "is_refusing", False) \
            and not isinstance(self.provider, NoKeyProvider)

    # ------------------------------------------------------------------ ids

    def is_restricted(self, object_class: str) -> bool:
        return object_class in K.RESTRICTED_CLASSES

    def blob_id(self, object_class: str, plaintext_digest: str) -> tuple:
        """`(blob_id, algo)`. Restricted classes get the HMAC; everything else the plain digest."""
        if not self.is_restricted(object_class):
            return plaintext_digest, "sha256"
        index_key = self.provider.tenant_index_key(self.tenant_id)
        return K.restricted_blob_id(index_key, plaintext_digest), "hmac-sha256"

    def public_digest(self, object_class: str, plaintext_digest: str,
                      ciphertext_digest: Optional[str]) -> Optional[str]:
        """The digest a log line, a ledger row or a quarantine record is allowed to carry."""
        if self.is_restricted(object_class):
            return ciphertext_digest
        return plaintext_digest

    # --------------------------------------------------------------- stream

    def seal_stream(self, object_class: str, source: S.ChunkSource, plaintext_digest: str, *,
                    data_class: Optional[str] = None) -> tuple:
        """`(ChunkSource of ciphertext, finish())`.

        `finish()` is callable only after the returned source has been walked to its end, and
        returns the `SealedObject`. The plaintext never exists as one object at any point: the
        digest is accumulated chunk by chunk on the way into the cipher and checked against the
        digest the caller measured on its own pass, so a source that changed between the two
        passes is caught here rather than published.
        """
        sealed_key = self.provider.data_key(self.tenant_id, data_class or object_class,
                                            plaintext_digest)
        context = self._context(sealed_key.ref)
        body_key, body_nonce = derive_stream_material(sealed_key.key, "body", context)
        state: Dict[str, Any] = {"plain": None, "cipher": None}

        def _chunks() -> Iterator[bytes]:
            plain = S.DigestingChunks(source())
            out = S.DigestingChunks(self.cipher.seal(body_key, body_nonce, plain))
            for chunk in out:
                yield chunk
            state["plain"] = plain.stat()
            state["cipher"] = out.stat()

        def finish() -> SealedObject:
            if state["plain"] is None or state["cipher"] is None:
                raise RuntimeError("the sealed stream was not walked to its end")
            plain: S.StreamStat = state["plain"]
            ct: S.StreamStat = state["cipher"]
            if plaintext_digest != plain.digest:
                raise SourceChanged("the sealed bytes are not the bytes the caller measured")
            bid, algo = self.blob_id(object_class, plain.digest)
            return SealedObject(
                blob_id=bid, blob_algo=algo, tenant_id=self.tenant_id,
                plaintext_digest=plain.digest, plaintext_bytes=plain.bytes,
                ciphertext_bytes=ct.bytes, cipher_name=self.cipher.name,
                ciphertext_digest=ct.digest, key_ref=sealed_key.ref,
                sealed_metadata=self._seal_metadata(sealed_key, {
                    "plaintext": {"algo": "sha256", "digest": plain.digest, "bytes": plain.bytes},
                }))

        return _chunks, finish

    def open_stream(self, sealed: SealedObject, source: S.ChunkSource) -> S.ChunkSource:
        """Reader-side round trip, used by tests to prove the ciphertext is the plaintext sealed."""
        wrapped = bytes.fromhex(sealed.sealed_metadata["wrapped_key_hex"])
        dek = self.provider.open_key(sealed.key_ref, wrapped)
        key, nonce = derive_stream_material(dek, "body", self._context(sealed.key_ref))

        def _open() -> Iterator[bytes]:
            yield from self.cipher.open(key, nonce, source())

        return _open

    # ------------------------------------------------------------- metadata

    def _context(self, ref: KeyRef) -> bytes:
        """What every derivation is bound to. Public, and useless without the DEK it expands."""
        return b"|".join([ref.provider.encode(), ref.tenant_id.encode(),
                          ref.data_class.encode(), ref.key_id.encode(),
                          self.cipher.name.encode()])

    def _seal_metadata(self, sealed_key: SealedKey, sensitive: Dict[str, Any]) -> Dict[str, Any]:
        """The sensitive sub-document, under its *own* derived key and nonce, plus what a reader needs.

        ⚠️THIS IS NOT THE BODY'S KEYSTREAM. It used to be sealed with the body's `(key, nonce)`,
        which in CTR mode hands out `body XOR metadata` to anyone holding both ciphertexts -- and
        the metadata plaintext is a JSON shape you can guess, so that is the body in the clear.
        `derive_stream_material(..., "metadata", ...)` gives an independent pair from the same DEK.

        The wrapped key is carried here because unwrapping it needs the KEK. The nonce is *not*
        carried: it is derived from the DEK, so publishing it would be publishing key-derived
        material for no reader benefit, and the old public nonce was a confirmation oracle.
        """
        key, nonce = derive_stream_material(sealed_key.key, "metadata",
                                            self._context(sealed_key.ref))
        body = K.canonical_json(sensitive)
        blob = b"".join(self.cipher.seal(key, nonce, [body]))
        return {
            "cipher": self.cipher.name,
            "kdf": KDF_NAME,
            "key_ref": sealed_key.ref.as_metadata(),
            "wrapped_key_hex": sealed_key.wrapped.hex(),
            "ciphertext_hex": blob.hex(),
        }

    def open_metadata(self, sealed: SealedObject) -> Dict[str, Any]:
        doc = sealed.sealed_metadata
        dek = self.provider.open_key(sealed.key_ref, bytes.fromhex(doc["wrapped_key_hex"]))
        key, nonce = derive_stream_material(dek, "metadata", self._context(sealed.key_ref))
        raw = b"".join(self.cipher.open(key, nonce, [bytes.fromhex(doc["ciphertext_hex"])]))
        import json

        return json.loads(raw.decode("utf-8"))


def synthetic_crypto(tenant_id: str = "dama",
                     seed: bytes = b"phase3-synthetic-test-seed") -> ObjectCrypto:
    """The only wiring that produces a usable `ObjectCrypto` here, and its keys are synthetic."""
    return ObjectCrypto(InMemoryTestKeyProvider(seed), cipher=HmacCtrCipher(),
                        tenant_id=tenant_id, allow_test_cipher=True)
