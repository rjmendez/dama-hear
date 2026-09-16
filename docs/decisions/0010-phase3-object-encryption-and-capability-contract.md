# 0010 - Phase 3 production object encryption and backend capability contract

- Status: proposed
- Date: 2026-09-15
- Scope: what a **production** encryption boundary for the object store must be, before any key
  provider, cipher or backend is chosen. This record adds no dependency, contacts no KMS,
  implements no cryptography, selects no backend, provisions nothing, reads nothing under
  `/pool`, touches no credential and changes no cluster object. It enables nothing: the shipped
  default stays the refusing pair, and satisfying this contract is a *later* act that has to
  pass the gates in §G below.
- Companion to: `hear/objectstore/crypto.py` (the injected `KeyProvider`/`Cipher` seam and the
  `HmacCtrCipher` **test double** this record is the production answer to),
  `docs/object-store-backend.md` (the capability audit and the D1-D10 demands),
  `docs/phase3-object-encryption-contract.md` (the operational specification behind this
  record), `docs/data-governance.md` §5-§6, `docs/decisions/0002-contract-repository-layout.md`
  (where `hear.objectstore.sealedmeta.v2` must eventually live),
  `docs/decisions/0006-admin-token-provisioning-policy.md` (custody precedent).
- Enforced by: nothing yet, and that is the point — §G lists the tests and CI jobs that must
  exist and be green before any code may claim this contract is satisfied.

## Problem

The scaffold proved the *pipeline*: streaming, per-object keying, tamper evidence, no plaintext
digest in any published field, a default that refuses. It deliberately did not produce a
production encryption boundary, and it says so in its own docstrings. Six things are still
missing, and every one of them is a property a reviewer would otherwise have to take on trust
the first time real evidence is sealed:

1. **There is no key authority.** `InMemoryTestKeyProvider` derives every key from a seed
   passed to a constructor. `docs/data-governance.md` §5 requires keys held outside application
   data, audited separately from object access, rotated on a schedule and on compromise, and
   revocable when a node or account is retired. None of those verbs has an interface to attach
   to today: the protocol exposes `tenant_index_key()`, which **exports raw key material into
   the process**, and `data_key()`, whose only wrapping evidence is a hex blob the same process
   produced.
2. **Nothing binds a ciphertext to the object it belongs to.** The test double authenticates
   the nonce and the ciphertext and nothing else. A ciphertext moved onto another object's
   pointer, re-labelled with another tenant's metadata, relabelled from `raw` to `record`, or
   replayed under an older schema version, still opens and still verifies. Confidentiality
   without context binding means the store's own identity layer — object key, blob id, tenant,
   sensitivity class, schema version — is unauthenticated metadata.
3. **The DEK is convergent on the plaintext digest, forever.** That was the right call for a
   scaffold whose replay story is "re-seal and expect the same bytes". Carried into production
   it publishes, permanently and inside every tenant, the fact that two objects are equal — on
   exactly the two classes (`clip`, `raw`) for which the design already refuses to publish a
   plaintext digest, for exactly the same reason.
4. **A reader can see plaintext that was never authenticated.** `HmacCtrCipher.open` yields
   decrypted chunks and raises `TamperDetected` at the *end* of the stream. Its own docstring
   says a consumer must not act on early bytes. "Must not" is not a mechanism, and the first
   consumer that is not a whole-stream digest — a range serve, an operator download, a model
   load — will act on them.
5. **No backend is probed for the guarantees the contract demands.** D1-D10 are demands on a
   store that does not exist yet, so a future adapter can assert them in a README and be
   believed. The audit records this gap explicitly ("no capability probe exists, because there
   is no store to probe").
6. **Deletion is denied by a docstring.** D4 says the importer credential has no
   `delete_object`; today's enforcement is that the importer does not call it, on a deployment
   where every job mounts `/pool` read-write.

## Decision

**Sealing is a KMS operation over a framed AEAD, every frame is bound to the object's published
identity, dedupe happens at the identity layer and never at the ciphertext layer, a reader emits
no plaintext byte it has not already authenticated, and a backend that cannot prove the
guarantees it is handed is refused rather than trusted.** Ten positions:

1. **A key provider is an authority, not a key store.** The production protocol is
   `generate_data_key` / `unwrap` / `index_mac` / `describe` / `capabilities`. Raw key material
   only ever appears as a per-object DEK returned by `generate_data_key`, held for one object's
   seal and dropped. `tenant_index_key()` — raw export — is **removed from the production
   protocol**; the restricted blob id becomes `index_mac(tenant, plaintext_digest)`, computed by
   the authority, so the index key never exists in the importer's address space. A provider that
   cannot MAC is a degraded provider (§4 of the specification) and may not serve restricted
   classes.
2. **One DEK per object generation, generated by the authority, never derived from content.**
   Uniqueness is a provider obligation and a tested one. It is what makes a counter nonce safe,
   and it is what ends convergent encryption.
3. **Dedupe moves up a layer, and keeps working.** The blob id stays
   `HMAC-SHA256(K_tenant_index, sha256(plaintext))` — now computed inside the authority — so an
   already-present blob is still recognised *before* anything is sealed, and a replayed import
   still writes nothing. What changes is that two imports of bytes that are *not* already
   present produce different ciphertexts. Idempotency was never a property of the ciphertext; it
   was a property of create-if-absent plus a deterministic id.
4. **Every frame carries AAD covering the object's whole published identity**: AAD schema
   version, sealed-metadata schema version, tenant, logical object key, blob id and blob algo,
   key ref (provider, key id, KEK generation), sensitivity labels, data class, cipher name,
   purpose (`body` or `metadata`), frame index and final-frame flag. A trailer frame
   additionally authenticates the frame count and the total plaintext length. Moving,
   relabelling, reclassifying, downgrading or truncating an object breaks decryption rather than
   being detected later by an auditor who may not run.
5. **No plaintext is exposed before it is authenticated.** The reader's unit is a frame: tag
   verified, then plaintext yielded, never the other order. A consumer that needs object-level
   integrity gets `read_authenticated_to(sink)`, which commits by atomic rename only after the
   trailer verifies. A range read returns whole frames and is labelled
   `partially_authenticated`; for `RESTRICTED_CLASSES` that label is refused at the API boundary
   unless the caller passes an explicit, logged `serve_unverified_range=True`.
6. **A backend is probed, not trusted.** A capability probe writes synthetic bytes under
   `hear/v1/probe/` only and answers thirteen questions, including the two that must come back
   *negative*: the importer credential must be **denied** delete on a published prefix, and an
   overwrite of a published key must be **refused**. Unmet required capability is a refusal to
   start; there is no `--allow-degraded` path for restricted classes.
7. **Delete denial is an IAM property.** A client that does not call delete is not a control.
   The importer credential must be denied by the store; the probe asserts the denial;
   destruction is a custodian act with a recorded request under `docs/data-governance.md` §6.
8. **Rotation re-wraps, it does not re-seal.** A KEK generation bump rewraps DEKs into a new,
   immutable metadata object published as pointer generation N+1 (the machinery the scaffold's
   `ptrgen` already provides); the body is untouched, because re-sealing petabyte-shaped
   evidence to rotate a KEK is how rotation stops happening. Only DEK compromise forces a
   re-seal, which is a new blob and a new generation. **Revocation is crypto-erasure** and is
   treated as deletion under §6 only after the operator verifies no escrowed copy remains.
9. **Sealed metadata is versioned, and the version is inside the AAD.** `schema_version: 2` is
   the first production shape; `1` is the scaffold's and is only ever produced by a
   non-production cipher. Readers accept `{1, 2}` during the transition window and writers emit
   only `2`. Metadata is immutable and digest-addressed, so a migration is a new metadata object
   plus a generation, never an in-place rewrite.
10. **Nothing above is active until §G is green.** The default stays `NoKeyProvider` +
    `RefusingCipher`. A cipher may not carry `production_ready = True` until the gates pass, and
    the test double's flag and name are not changed by this record.

## Alternatives rejected

- **Keep convergent DEKs in production.** It makes equal plaintexts visibly equal inside a
  tenant forever, on the two classes whose entire threat model is "can an operator confirm a
  guess". The scaffold's reason — byte-identical replays — is satisfied by create-if-absent on a
  keyed blob id, which already exists.
- **Server-side encryption (SSE-KMS).** The store would hold plaintext. `docs/object-store-backend.md`
  already rejects this for the restricted classes; it is restated here so a backend choice
  cannot quietly reintroduce it.
- **Export the tenant index key to the importer.** It converts a KMS into a key file with extra
  steps, and it puts the one long-lived secret in the process most likely to core-dump.
- **Whole-object AEAD (single tag).** It requires either buffering the object or exposing
  unauthenticated plaintext. A 392 MB model rules out the first; §4 above rules out the second.
- **Per-object random nonces stored in metadata.** Storage is not the problem — the scaffold
  already proved a *published* nonce can be an oracle. With a unique DEK per object, a counter
  nonce is safe and carries no field to publish.
- **Trust an adapter's documented guarantees.** D1-D10 are exactly the properties whose absence
  is silent: a lost update, a permitted overwrite, an undenied delete. Every one of them is
  cheap to probe and catastrophic to assume.
- **Add a KMS SDK now to make this concrete.** The recoupling boundary forbids a cloud SDK in
  `hear/`, and an interface that is written against one vendor's semantics is a vendor choice
  disguised as a refactor. The provider is abstract; the adapter lives outside `hear/`.
- **Build the AEAD from the standard library.** The repository's zero-dependency test story is
  worth a test double, not production evidence. A reviewed AES-GCM / ChaCha20-Poly1305
  implementation is a dependency decision (§G8), taken deliberately or not at all.

## Consequences

**Forward.** The scaffold's seam survives unchanged: `ObjectCrypto` still takes a provider and a
cipher, and the production pair drops into the same constructor arguments. What changes is the
provider protocol (an authority, not a key store), the cipher protocol (framed, AAD-taking), and
the metadata schema (`2`). Those are protocol changes, which is why this record exists **before**
an adapter, not after — the same argument PR #220 made for changing the `Backend` Protocol while
it had one implementation.

**Compatibility with the in-flight scaffold.** Nothing here asks PR #220 to change. The test
double keeps `production_ready = False`, the default stays refusing, convergent sealing remains
correct for a scaffold whose fixtures are synthetic, and `schema_version: 1` remains the shape
the test cipher produces. This record is the answer to that PR's own open item ("the production
answer is a reviewed AEAD behind a KMS, injected through the same protocol") and to the audit's
("no capability probe exists").

**Mixed-version.** A reader that encounters a `schema_version` it does not know refuses the
object and quarantines it as `sealedmeta_version_unknown`; it never guesses a layout. A reader
given a `key_ref` naming a KEK generation the authority has revoked reports
`key_revoked` — distinct from `key_provider_unavailable`, because one is an infrastructure
outage and the other is a deliberate erasure.

**Rollback.** There is nothing to roll back: no key exists, no object is sealed under this
contract, no backend is selected. Abandoning the lane reverts two documents and an index row.

**Open, and deliberately not decided here.**

1. **Which key authority.** A cloud KMS, a self-hosted HSM-backed service, or an offline
   custodian-held root with an online wrapping service. G1 (backend choice) and the custody
   question in ADR 0006 both feed it, and it is an operator decision with a cost.
2. **Which AEAD, and therefore which dependency.** AES-GCM with hardware acceleration versus
   ChaCha20-Poly1305 on a node with unknown AES-NI availability is a measurement nobody has
   taken on this hardware.
3. **The frame size.** 1 MiB and 8 MiB trade tag overhead against range-read granularity and
   reader memory; the capacity model's 24 GiB bounded shadow makes the overhead visible but not
   decisive.
4. **Whether `record`/`annotations` join `RESTRICTED_CLASSES`.** Human annotation text is not
   ambient audio, but it is not obviously safe to leave under a plaintext digest either. That is
   a `docs/data-governance.md` classification decision, not a cryptographic one.
5. **Escrow.** Crypto-erasure as a deletion mechanism is only honest if no escrowed copy of the
   KEK exists; whether this deployment wants an escrow at all is an operator decision with a
   direct, opposite consequence for §6 deletion claims.
6. **G0 and G1 are unchanged and still block everything.** A restore-tested `/pool` backup and a
   backend choice come first; this record neither satisfies nor bypasses them.

## G. Gates before this contract may be called satisfied

Nothing below exists yet. Each is a test or a CI job someone has to write, and the contract is
unsatisfied until all of them are green. The specification expands each one.

| Gate | What must be proven |
| --- | --- |
| G-E1 | **AAD negative matrix.** For each of object key, blob id, tenant, data class, sensitivity labels, key ref, cipher name, purpose, AAD version and sealed-metadata schema version: altering it alone makes `open` raise, with the ciphertext byte-identical. Ten cases, no exceptions. |
| G-E2 | **Frame integrity.** Reorder, drop, duplicate, splice-from-another-object, truncate-before-trailer and trailer-only-missing each raise, and a truncated stream is never reported as a short but valid object. |
| G-E3 | **No plaintext before authentication.** An instrumented sink asserts zero plaintext bytes emitted for any frame whose tag fails, and `read_authenticated_to` leaves no file behind when the trailer fails. Verified by fault injection at the first, a middle and the last frame. |
| G-E4 | **DEK uniqueness and non-convergence.** Sealing identical bytes twice yields different ciphertexts and different wrapped keys; a provider returning a repeated DEK is rejected at the seam. |
| G-E5 | **No key material at rest.** The scaffold's existing search — every key, every stored byte, every ledger row, every quarantine record — extended to the probe prefix, the capability report and the rotation records, plus a keyless-derivation oracle search with a key-holding positive control. |
| G-E6 | **Capability probe.** Each of the thirteen probes has a test with a backend double that fails exactly that property, and the importer refuses to start; the two negative probes (delete denied, overwrite refused) fail *open* — a store that permits them is refused. |
| G-E7 | **Delete denial is external.** A double whose IAM permits delete is refused even though the client never calls it; the probe records the denial as evidence in `run_open`. |
| G-E8 | **Rotation and revocation.** Rewrap produces generation N+1, the body object is not rewritten, old and new metadata both open until revocation, and a revoked KEK yields `key_revoked` and quarantine rather than a partial read or a crash. |
| G-E9 | **Sealed metadata contract.** `hear.objectstore.sealedmeta.v2` schema, fixtures and generator exist under ADR 0002's layout, with a `--check` drift job in CI and an unknown-version refusal test. |
| G-E10 | **Clear/sealed split.** A property test asserts no sensitive field (plaintext digest, plaintext length for restricted classes, source path, coordinates) appears anywhere outside the sealed sub-document, across metadata, keys, ledger, index, counters, logs and probe output. |
| G-E11 | **The default is still refusing.** The shipped default provider and cipher refuse; a cipher claiming `production_ready = True` requires a named reviewed implementation and a passing G-E1..G-E5; the test double cannot be reached outside `tests/`. |
| G-E12 | **No recoupling, no surprise dependency.** `tools/recoupling_guard.py` stays green (no KMS or cloud SDK in `hear/`); adding the AEAD dependency updates both pinned requirement sets and is reviewed as a dependency decision, not as part of a feature PR. |
