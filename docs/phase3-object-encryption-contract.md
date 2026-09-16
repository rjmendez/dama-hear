# Phase 3 production object encryption and capability contract

The operational specification behind `docs/decisions/0010-phase3-object-encryption-and-capability-contract.md`.

This page specifies what a **production** encryption boundary for the object store has to be. It
implements nothing. No key exists, no KMS is contacted, no dependency is added, no backend is
selected or provisioned, nothing under `/pool` is read, no credential is touched and no cluster
object changes. The signatures below are a *contract shape* for review — they are deliberately
not in `hear/`, because the seam that would hold them (`hear/objectstore/crypto.py`) is in
flight, and because a type that exists is a type someone can wire.

The shipped default remains `NoKeyProvider` + `RefusingCipher`: an importer nobody deliberately
handed key material to cannot seal anything, and quarantines restricted classes as
`key_provider_unavailable`.

## 1. What this replaces, and what it keeps

The scaffold (`hear/objectstore/crypto.py`, PR #220) is a test-cipher pipeline that proves
streaming, per-object keying, keystream separation between body and metadata, tamper evidence
and the absence of a plaintext digest in every published field. It stays as it is. This page is
the production answer it names in its own docstring.

| Property | Scaffold | Production contract |
| --- | --- | --- |
| Cipher | `HmacCtrCipher`, encrypt-then-MAC, `production_ready = False` | Reviewed AEAD (AES-GCM or ChaCha20-Poly1305), framed, AAD-taking |
| Authentication scope | nonce + ciphertext | nonce + ciphertext + **object identity AAD** (§5.3) + trailer over frame count and length |
| Key source | `InMemoryTestKeyProvider(seed)` in-process | KMS/authority: `generate_data_key` / `unwrap` / `index_mac` (§2) |
| Index key | exported into the process (`tenant_index_key`) | never exported; the authority MACs (§2.3) |
| DEK | convergent on the plaintext digest | unique per object generation, authority-generated (§4) |
| Dedupe | ciphertext equality inside a tenant | identity-layer only: keyed blob id + create-if-absent (§4.2) |
| Reader | plaintext yielded, tag checked at end | frame authenticated, *then* plaintext yielded (§6) |
| Metadata | `schema_version` implicit, shape 1 | `hear.objectstore.sealedmeta.v2`, version inside the AAD (§7) |
| Backend trust | D1-D10 asserted in prose | probed, with two probes that must fail (§8) |
| Delete denial | the importer does not call delete | IAM denies it and the probe proves the denial (§9) |
| Rotation | none | rewrap into generation N+1; revocation is crypto-erasure (§10) |

Nothing in this table asks the scaffold to change. Every row is additive, and each one is gated
(§13).

## 2. The key authority

### 2.1 Shape

```python
class KeyAuthority(Protocol):
    """A KMS-shaped authority. It answers questions; it never hands over a long-lived key."""

    name: str
    production_ready: bool

    def capabilities(self) -> "AuthorityCapabilities": ...

    def generate_data_key(self, *, tenant_id: str, data_class: str,
                          aad: bytes) -> "WrappedDataKey":
        """A fresh 256-bit DEK plus its wrapped form. NEVER derived from content (§4)."""

    def unwrap(self, *, key_ref: "KeyRef", wrapped: bytes, aad: bytes) -> bytes:
        """Unwrap under the SAME aad the wrap used; a mismatch is a refusal, not a warning."""

    def index_mac(self, *, tenant_id: str, message: bytes) -> bytes:
        """HMAC under K_tenant_index, computed INSIDE the authority (§2.3)."""

    def describe(self, key_ref: "KeyRef") -> "KeyState":
        """active | rotated | disabled | revoked | unknown — a reader checks before it reads."""
```

```python
@dataclass(frozen=True)
class AuthorityCapabilities:
    generates_data_keys: bool      # required, always
    wraps_with_aad: bool           # required for restricted classes
    macs_without_export: bool      # required for restricted classes (§2.3)
    reports_key_state: bool        # required for revocation to mean anything (§10)
    audits_key_access: bool        # required by data-governance §5
    max_dek_bytes: int
    kek_generations_readable: int  # how many generations a reader may still unwrap under
```

`WrappedDataKey` is `(key: bytes, wrapped: bytes, ref: KeyRef)` — the same shape the scaffold's
`SealedKey` already has, so the seam does not move. `KeyRef` gains one field: `kek_generation:
int`, which the scaffold encodes inside its `key_id` string.

### 2.2 What is forbidden in an implementation

- No key material read from, or written to, a file, an environment variable, a ledger row, a
  metrics label or a log line. The DEK exists for one object's seal and is dropped.
- No key derived from a password, a seed, a hostname, a timestamp or the plaintext.
- No adapter under `hear/`. The authority is injected from the edge; `tools/recoupling_guard.py`
  stays the enforcement.
- No fallback provider. An authority that is unreachable produces `key_provider_unavailable` and
  a quarantined object, never an unsealed publish.

### 2.3 Why `tenant_index_key` is removed

The restricted blob id is `HMAC-SHA256(K_tenant_index, sha256(plaintext))`. The scaffold obtains
`K_tenant_index` by export, which makes the one long-lived per-tenant secret resident in the
importer for the length of a run. Production computes the MAC in the authority
(`index_mac`), so:

- the index key never exists in the importer's address space, in a core dump, or in a heap
  snapshot;
- index-key use is audited separately from object access, which `docs/data-governance.md` §5
  requires and an in-process key makes impossible;
- revoking the index key stops future id minting without touching any object.

The cost is one authority round trip per object. An authority without `macs_without_export` may
serve unrestricted classes only; restricted classes are refused with
`authority_capability_missing`, not degraded.

## 3. Key hierarchy

```
authority root (never leaves the KMS)
└── KEK[tenant, data_class, generation]        rotated on schedule / on compromise (§10)
    ├── K_tenant_index[tenant]                 MAC key for restricted blob ids, never exported
    └── DEK[object, generation]                random, 256-bit, one object, wrapped under KEK
        ├── (key, nonce) for purpose "body"      HKDF-SHA256, RFC 5869
        └── (key, nonce) for purpose "metadata"  independent — never one keystream for two streams
```

The HKDF split is the scaffold's, unchanged and kept: one DEK expands to two independent
`(key, nonce)` pairs bound to tenant, class, key id and cipher name. With a unique DEK per
object, the nonce is a counter — 12 bytes: `purpose(1) || 0x00 0x00 0x00 || frame_index(8, big
endian)` — and no nonce is ever published, because none needs to be.

Nonce safety rests on exactly one obligation: **the authority never returns the same DEK twice**
(gate G-E4). That obligation is testable; nonce randomness spread across a 392 MB object's frames
is not.

## 4. Dedupe versus leakage — the decision

### 4.1 The tradeoff

Convergent sealing (the scaffold's) means equal plaintexts produce equal ciphertexts and equal
wrapped keys inside a tenant. That buys byte-identical replay and cross-object ciphertext
dedupe. It costs a permanent, passive equality oracle for anyone who can list the store — on
`clip` and `raw`, whose entire threat model (`docs/data-governance.md`, key design §8) is that an
operator must not be able to confirm a guessed recording is present. The measured duplicate
fraction in this corpus is ~59 % (`docs/phase3-storage-capacity-model.md`), so the saving is
real and worth stating precisely rather than waving away.

### 4.2 The decision

**Dedupe at the identity layer, not the ciphertext layer.**

1. The importer computes `sha256(plaintext)` during its first streaming pass.
2. The blob id is `index_mac(tenant, plaintext_digest)` for restricted classes, the plaintext
   digest otherwise — deterministic per tenant, so the *same bytes still map to the same key*.
3. The importer probes the blob key with create-if-absent semantics. Present → nothing is
   sealed, nothing is uploaded, the pointer is committed against the existing blob. **All of the
   ~59 % is still saved, and a replay is still free.**
4. Absent → seal with a fresh DEK. Two imports of bytes that were not already present produce
   different ciphertexts, which is precisely the oracle that goes away.

What is lost: byte-identical *re-seal*. Nothing depends on it — verification compares the digest
recorded at ingest against a streamed re-read (D10), not against a re-derived ciphertext, and
published keys are immutable by mechanism (D3), so nothing legitimately writes the same blob
twice.

What is kept, deliberately: intra-tenant **equality of blob ids**. A keyed, per-tenant id still
reveals that two objects in one tenant are identical to someone who can list keys and holds the
index key's outputs. Removing that would require randomised ids and a separate encrypted index,
which loses idempotent resume — the property that makes a re-run free. Across tenants nothing
matches: different KEK, different index key, different ciphertext.

Residual leakage, stated so a reviewer can weigh it: object size (to a frame), frame count,
import timing, class label, sensitivity labels and pointer topology are all clear. Size is the
significant one for ambient audio, and padding is **not** adopted — it would cost storage on the
capacity-bound lane for a partial mitigation of an attacker who already has the store.

## 5. The AEAD frame format

### 5.1 Layout

A sealed stream is a sequence of frames followed by exactly one trailer frame:

```
frame_i   := AEAD_Seal(key, nonce(purpose, i), aad(i), plaintext_chunk_i)
trailer   := AEAD_Seal(key, nonce(purpose, FINAL), aad(FINAL), canonical_json({
                 "frames": n, "plaintext_bytes": total, "purpose": purpose }))
```

- Frame size is fixed per object and recorded in the clear metadata; the last body frame may be
  short. Candidate sizes are 1 MiB and 8 MiB (open question 3 in the record).
- `FINAL` is `2**63 - 1`, so a trailer can never collide with a body frame index.
- No length prefix is invented: the store already provides ranged reads, and the frame size plus
  the frame count in the trailer determine every boundary.

### 5.2 What each frame stops

| Attack | Stopped by |
| --- | --- |
| bit flip in a frame | the frame's own tag |
| frame reordering | `frame_index` in the AAD |
| frame duplication | `frame_index` in the AAD |
| frames spliced from another object | object identity in the AAD (§5.3) |
| truncation | the trailer's frame count; a missing trailer is a refusal |
| trailer replay from a shorter object | the trailer's AAD carries the same object identity |
| object moved onto another pointer | `object_key` in the AAD |
| tenant relabelling | `tenant_id` in the AAD |
| sensitivity downgrade (`raw` → `record`) | `data_class` + `sensitivity` in the AAD |
| schema downgrade | `sealedmeta_version` + `aad_version` in the AAD |
| key-ref substitution | `key_ref` in the AAD, and `unwrap` takes the same AAD |

### 5.3 AAD construction

```python
def frame_aad(header_aad: bytes, purpose: str, frame_index: int, final: bool) -> bytes:
    return b"|".join([header_aad, purpose.encode(),
                      b"%d" % frame_index, b"1" if final else b"0"])

def header_aad(binding: "ObjectBinding") -> bytes:
    """sha256 over the canonical JSON of the binding; canonical_json() is the repo's one form."""
```

```python
@dataclass(frozen=True)
class ObjectBinding:
    aad_version: int          # 1; bumped only by an append-only decision record
    sealedmeta_version: int   # 2
    tenant_id: str
    object_key: str           # hear/v1/obj/<class>/<partition…>/<logical_id>
    blob_id: str
    blob_algo: str            # sha256 | hmac-sha256
    data_class: str           # keys.CLASSES member
    sensitivity: tuple        # keys.SENSITIVITY labels, sorted
    key_provider: str
    key_id: str
    kek_generation: int
    cipher_name: str
```

Every field is already published in the clear, which is the point: the AAD authenticates the
store's own identity layer without adding a single new disclosure. The same `header_aad` is
passed to `generate_data_key` and `unwrap`, so a wrapped key is bound to the object too.

**Ordering constraint.** The blob id is in the AAD, and for restricted classes the blob id is a
MAC of the plaintext digest, so the digest pass must complete before sealing begins. The scaffold
already makes two streaming passes (measure, then seal) and already refuses a source that changed
between them (`source_changed_during_import`); this contract inherits both, and inherits the
prohibition on buffering the object to avoid the second pass.

## 6. The reader constraint

> **No plaintext byte is exposed to any consumer before the authentication that covers it has
> succeeded.**

Mechanically:

1. `open_stream` yields a frame's plaintext only after that frame's tag verifies. A failure
   raises `TamperDetected` before any byte of that frame is yielded — including the first frame,
   so a wrong key produces nothing at all.
2. Already-yielded frames are authenticated, but the *object* is not complete until the trailer
   verifies. Consumers therefore come in two shapes:
   - `read_authenticated_to(sink)` — the default for anything that produces an artifact. Writes
     to a staging path, verifies the trailer, then atomically renames. A failure leaves nothing
     behind (the scaffold's truncated-write fix, applied to the reader).
   - `iter_authenticated_frames(...)` — for a range serve, returns whole frames plus
     `partially_authenticated = True`. For `keys.RESTRICTED_CLASSES` this raises unless the
     caller passes `serve_unverified_range=True`, which is recorded in the read audit with the
     caller's identity.
3. A decrypting reader never writes plaintext to a path a consumer can see before step 2
   completes, never logs a decrypted fragment, and never returns a partial object as a short
   success.
4. A tamper failure quarantines the *object*, not the run: the reason is `authentication_failed`,
   distinct from `blob_missing` and from `key_revoked`.

A framed AEAD is what makes this affordable. The scaffold's single trailing MAC cannot satisfy
(1) without buffering, which is why its docstring reduces the rule to an instruction to the
consumer, and why this contract changes the cipher protocol rather than the consumer's manners.

## 7. Sealed metadata, and its migration

### 7.1 `hear.objectstore.sealedmeta.v2`

Clear sub-document — readable by anyone who can read the object, and deliberately sufficient to
*refuse* an object without opening it:

| Field | Why it is clear |
| --- | --- |
| `schema_version` (=2), `aad_version` | a reader must know the layout before it holds a key |
| `tenant_id`, `data_class`, `sensitivity[]` | refusal by label without decryption |
| `object_key`, `blob_id`, `blob_algo` | the identity layer; already published in keys |
| `cipher`, `kdf`, `frame_bytes`, `frame_count` | needed to locate frame boundaries for a ranged read |
| `ciphertext_bytes`, `ciphertext_digest` | verification without plaintext (D10) |
| `key_ref{provider,key_id,kek_generation,tenant_id,data_class}` | which authority, which generation |
| `wrapped_key_hex` | unwrapping needs the KEK **and** the header AAD |
| `sealed{ciphertext_hex}` | the sealed sub-document itself |
| `supersedes` (optional) | the metadata object this rewrap replaces (§10) |

Sealed sub-document — under the metadata purpose's own key and nonce:

| Field | Why it is sealed |
| --- | --- |
| `plaintext{algo,digest,bytes}` | a plaintext digest is a confirmation oracle; this is its only home |
| `source_ref` | a path names a device, a site and a capture |
| `capture{...}` any 7-decimal coordinate or precise timestamp | `precise_location` |
| `original_name` | filenames leak what the sealing exists to hide |

`plaintext.bytes` is sealed for restricted classes and may be clear otherwise; `frame_count` and
`frame_bytes` bound it to a frame anyway, which is the residual in §4.2.

### 7.2 Version 1 → 2

Version 1 is the scaffold's shape, produced only by a non-production cipher against synthetic
fixtures. Therefore:

- **No ciphertext migration exists, and none may be written.** A v1 object is a test artifact. If
  one is ever found outside `tests/`, it is quarantined as `sealedmeta_nonproduction_cipher`.
- **Readers accept `{1, 2}`** for the length of the transition window and refuse anything else
  with `sealedmeta_version_unknown`. A reader never infers a layout from field presence.
- **Writers emit only `2`** once G-E9 is green, and only `2` may be paired with a
  `production_ready` cipher.
- **A future `3` is additive and allocated, never redefined in place** — the rule ADR 0002 and
  `docs/phase0-freeze-contracts.v1.md` already impose on every contract here.
- Metadata objects are immutable and digest-addressed (`keys.meta_key`), so any change to an
  object's metadata — a rewrap, a re-labelled sensitivity, a schema bump — is a **new metadata
  object plus pointer generation N+1**, with `supersedes` naming the predecessor. There is no
  in-place metadata rewrite anywhere in this design.
- The schema, fixtures and generator live under ADR 0002's layout with a `--check` drift job,
  staged under `docs/phase3-objectstore-crypto/` until promotion, for the same reason ADR 0009
  staged its receipt: publishing a contract id regenerates the hashed freeze baseline, and
  several lanes regenerating one baseline is a conflict with no meaning.

## 8. Backend capability probing

The probe runs at configuration time, before the first object of a run, writes **only** under
`hear/v1/probe/<run_id>/` with synthetic bytes, and records its report in the ledger's
`run_open`. It never touches `/pool`, a published prefix or a staging prefix.

| # | Probe | Demand | Required for | Unmet ⇒ |
| --- | --- | --- | --- | --- |
| P1 | conditional create-if-absent on a pointer | D1 | all | refuse, unless a live fencing lease (D2) is available |
| P2 | fencing lease with monotonic epoch | D2, D8 | all when P1 fails | refuse |
| P3 | **overwrite of a published key is refused** | D3 | all | **refuse — negative probe** |
| P4 | **delete on a published prefix is denied to the importer credential** | D4, §9 | all | **refuse — negative probe** |
| P5 | delete under the staging prefix succeeds | D4 | all | refuse |
| P6 | read-after-write of a new key | D5 | all | refuse |
| P7 | ranged GET returns exactly the requested range | D6, §6 | all | refuse |
| P8 | prefix listing | D7 | GC/audit only | degrade, warn |
| P9 | durable write (atomic rename or equivalent; no truncated object after an interrupted write) | scaffold fix | all | refuse |
| P10 | restrictive object permissions honoured (`0600` or ACL equivalent) | D9 | restricted classes | refuse for restricted classes |
| P11 | case-sensitive key space | keys are case-significant hex + labels | all | refuse |
| P12 | authority reachable, `capabilities()` satisfies §2.1 | §2 | restricted classes | refuse for restricted classes |
| P13 | server-side encryption is **not** relied upon (the store is handed ciphertext only) | audit fact 2 | all | assert in code, not probe |

Rules:

- A refusal is a refusal to **start**, not a per-object quarantine: a store that cannot hold the
  guarantees is not a store this importer talks to.
- There is no `--allow-degraded` for a restricted class. P8 is the only degradable probe.
- P3 and P4 are inverted: the probe passes when the operation **fails**. A backend double that
  permits either is refused (G-E6, G-E7), which is what makes "the importer has no delete" a
  property of the deployment rather than of the client's manners.
- The report is `hear.objectstore.capability.v1`-shaped, recorded, and re-probed on every run —
  IAM drifts, and a probe that ran once is a claim about the past.

## 9. Credentials and delete denial

| Credential | May | May not |
| --- | --- | --- |
| importer | put staged, put immutable (create-if-absent), get, range get, list, acquire lease, delete **under staging only** | delete or overwrite anything published; read another tenant's prefix; unwrap keys for export; call the authority's rotate/revoke |
| reader | get, range get, unwrap for read | put, delete, list beyond its tenant, `index_mac` |
| custodian | delete published objects under a recorded request; rotate; revoke | routine reads; running an import |

- **Delete denial is IAM-enforced.** The store must return an authorization failure for
  importer-issued deletes on published prefixes; P4 proves it each run. A client that simply does
  not call delete provides no evidence and no protection against a future caller.
- Destruction of a published object is a custodian act under `docs/data-governance.md` §6:
  identified tenant/range, reason, requester, approver, deadline, and resolution of every
  replica — including backups, whose restore-tested existence is gate G0.
- Today every job mounts `/pool` read-write (the audit's D9 note). That is a pre-existing
  deployment fact, unchanged by this design and listed as an entry condition, not fixed here.

## 10. Rotation and revocation

**Scheduled KEK rotation (re-wrap, no re-seal).**

1. Custodian creates KEK generation `g+1` in the authority; `g` becomes `rotated` — still
   unwrappable, no longer used for new wraps.
2. A rewrap job, under the custodian credential, for each affected object: unwrap the DEK under
   `g` with the object's header AAD, rewrap under `g+1` with the **same** AAD, write a **new**
   metadata object (`schema_version` unchanged, `key_ref.kek_generation = g+1`,
   `supersedes = <old meta key>`), then commit pointer generation N+1 with
   `expect_generation`. Bodies are never rewritten and never re-read.
3. Rewrap is resumable and idempotent: a generation claim compared on identity rolls forward
   exactly as a republish does (the scaffold's roll-forward fix), and an object already at `g+1`
   is a replay, not a conflict.
4. When coverage is complete and verified, `g` moves to `disabled`. Readers may still be
   configured for `kek_generations_readable` generations back; anything older is
   `key_generation_unreadable`, which is an operational failure, not a silent skip.

**Emergency DEK compromise (re-seal).** A leaked DEK compromises exactly one object. That object
is re-sealed under a fresh DEK, which produces a **new blob** (non-convergent, §4) and pointer
generation N+1; the old blob is destroyed by the custodian under §6, because the old ciphertext
plus the leaked DEK is plaintext and rotation cannot help it.

**Revocation (crypto-erasure).** Disabling KEK `g` with no readable generation makes every
object wrapped under it permanently unreadable.

- It is recorded as a deletion event under §6 **only after** the operator verifies no escrowed
  KEK copy and no plaintext replica remains — including `/pool`, backups, node buffers, exports
  and derived copies. Until that verification, it is "unreadable", which is a weaker claim and
  must be reported as the weaker claim.
- A reader hitting a revoked ref reports `key_revoked`, distinct from
  `key_provider_unavailable` (an outage) and from `blob_missing` (an absence). Conflating them
  makes an erasure look like an incident and an incident look like an erasure.
- Revocation blast radius is the KEK's scope: `(tenant, data_class, generation)`. That scoping is
  the reason KEKs are per class and not per tenant alone.

## 11. Failure vocabulary

Closed set, mapped to quarantine reasons so a run never dies on one object (the scaffold's
`OBJECT_FAILURES` table extends with these):

`key_provider_unavailable` · `authority_capability_missing` · `key_revoked` ·
`key_generation_unreadable` · `authentication_failed` · `aad_mismatch` ·
`sealedmeta_version_unknown` · `sealedmeta_nonproduction_cipher` · `frame_count_mismatch` ·
`trailer_missing` · `source_changed_during_import` · `dek_reuse_detected`
(run-fatal — a repeated DEK invalidates the nonce argument for every object in the run) ·
`capability_probe_failed` (run-fatal, before any object).

## 12. Observability, and what may not be emitted

Counters and labels: run id, object class, sensitivity labels, quarantine reason, frame counts,
ciphertext bytes, authority latency and failure counts, KEK generation in use, probe results.

Never emitted, anywhere, at any level: a plaintext digest for a restricted class, a DEK or any
HKDF output, an unwrapped key, an index-key output that is not already the published blob id, a
source path for a restricted class, a decrypted fragment, or a nonce. `ObjectCrypto.public_digest`
remains the only digest accessor a log line, a ledger row or a quarantine record may use — for a
restricted class, the ciphertext's.

## 13. Gates

G-E1..G-E12 are stated in the decision record and are the merge condition for any code claiming
this contract. Additional notes on how three of them are proven, because they are the ones a
test can fake:

- **G-E1 (AAD matrix)** must mutate the binding field *only*, reusing the identical ciphertext
  bytes, and must assert the specific failure (`aad_mismatch` / `authentication_failed`), not
  "some exception". A positive control — the unmutated binding opening successfully — runs in the
  same test, so a universally-failing `open` cannot pass the matrix.
- **G-E3 (no plaintext before authentication)** is proven with an instrumented sink that records
  every byte handed to it, plus fault injection at the first, a middle and the final frame; the
  assertion is on the sink's *recorded byte count*, not on an exception type. `read_authenticated_to`
  additionally asserts an empty destination directory after a failure.
- **G-E6/G-E7 (probes)** use backend doubles that violate exactly one property each, including
  two doubles that are *too permissive* (delete allowed, overwrite allowed). A probe suite that
  only tests stores that refuse cannot detect a store that permits.

CI placement: the AAD, frame, reader and key-hygiene tests run in the main `tests` job; the
sealed-metadata generator drift check joins the existing `generated headers are current` job; the
recoupling guard (no KMS/cloud SDK inside `hear/`) is already its own merge-blocking job and
covers §2.2 without modification.

## 14. Entry conditions outside this contract

Unchanged by this page and still blocking: **G0** a restore-tested `/pool` backup, **G1** a
backend choice (`docs/object-store-backend.md`), the capacity reservation against the measured
52 GB rather than the reported 240 G (`docs/phase3-storage-capacity-model.md`), and an operator
decision on who holds the custodian credential and which authority is used at all.
