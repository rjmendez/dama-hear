"""NOT PRODUCTION. A repository-local test scaffold for the Phase 3 object-store import lane.

⚠️NOTHING HERE TALKS TO A REAL OBJECT STORE, AND NOTHING HERE MAY BE RUN AGAINST `/pool`.
No backend has been selected (import plan §20 G1) and `/pool` still has no backup (G0), so the
only thing this package is allowed to be is a place where the import rules can be *proved* on
synthetic fixtures before any byte of evidence is read. The one backend implemented here,
`LocalDirBackend`, writes into a throwaway directory a test owns.

WHAT IS PROVEN HERE, AND WHY EACH ONE IS HERE:

* **Task identity is derived, not allocated** (`keys.task_id`) -- resume replays a local ledger
  instead of listing a bucket, so a task id that changed between runs would silently re-import
  everything. Re-running a finished run must write zero bytes.
* **The digest compared on readback is the one recorded at ingest** (`ledger.jsonl`/`index.jsonl`),
  never one computed from the same read. A digest computed from the bytes you just wrote proves
  nothing about the bytes you read.
* **The pointer commit is the only observable step** and it is conditional: absent -> committed,
  present with the same blob -> idempotent replay, present with a different blob -> conflict,
  quarantine, halt that class. There is no half-published object.
* **A lease has a fencing epoch.** A resumed-from-the-dead importer holding a stale lease is
  refused at commit rather than trusted because it still has an object in memory.
* **A failure quarantines one object, not the run** -- except the three that must halt (pointer
  conflict, model pin mismatch, gate failure).
* **The source is opened read-only and is never written**, including no `fsync`, no `utimes`, no
  rename, no `.tmp` sweep. Tests census the source tree before and after.
* **No payload is ever held whole** (`streaming.py`). A payload is a *factory of chunk
  iterators*; digests accumulate as the chunks go past and nothing but a digest and a byte count
  survives a stream. The largest object in scope is the 392 MB perch model and the importer is a
  guest on a pod near its memory limit, so a `body = fh.read()` is a correctness bug here, not a
  performance one -- `tests/test_objectstore_streaming.py` holds it to a measured memory ceiling.
* **A restricted class is encrypted or it is not imported** (`crypto.py`). The default key provider
  refuses every question *and the default cipher has no algorithm*, so an importer nobody
  deliberately handed both halves to quarantines `clip`/`raw` as `key_provider_unavailable` and
  keeps going -- key material alone is not enough to start sealing. Nothing in this package creates,
  derives or stores a key, and the one cipher here declares itself not production ready. With a
  provider injected, the blob id is `HMAC-SHA256(K_tenant_index, plaintext digest)` -- a plaintext
  digest in a key, an index, a ledger row or a log line is a confirmation oracle for guessable
  content, so it exists in exactly one place: inside the sealed metadata sub-document.
* **Nothing published can confirm a guessed plaintext, and no two streams share a keystream.**
  Every key and nonce is an RFC 5869 HKDF expansion of the secret data key under a distinct label,
  so the body and the sealed metadata never share one -- a shared CTR keystream between them hands
  out `body XOR metadata`, and the metadata is a guessable shape. Nonces are derived, never
  published: a nonce computed from public inputs is an oracle that needs no key at all.
* **A commit interrupted between its generation claim and its pointer write rolls forward.** The
  claim is the CAS and it lands first, so a writer that dies in between leaves a claim with no
  pointer; reading that as another writer's work would wedge the object for every future run.
  A claim naming the same object, blob and predecessor is finished rather than refused.
* **A lease is won atomically, or it is not won.** Acquisition is an exclusive create on an
  epoch-named record, so two racers cannot both be handed the same epoch -- an epoch two writers
  share fences neither of them. `LocalDirBackend(conditional_put=False)` models the store that has
  no conditional put, including the update it really loses, so the fallback primitive is tested
  rather than assumed.
* **Exactly one object is written twice**: the open tail of a live stream, republished as a new
  pointer generation under an if-generation-matches commit, at most once per (class, partition)
  per run. Generations are immutable records; `superseded_by` is derived from the existence of the
  next one, never written back onto a published object.

The rules come from `files/phase3-key-design/PHASE3-OBJECT-KEY-DESIGN.md` and
`files/phase3-import-plan/PHASE3-OBJECT-IMPORT-PLAN.md`. Nothing here imports `hear.pool`,
`hear.clips` or `hear.tags`: the object store takes `pool.key`/`clip_key`/`tag_key` as *inputs*
and must never re-derive an identifier those modules already wrote into rows that exist.
"""
