# Object-store backend: capability audit and the conditions on a choice

Phase 3 ships a backend *contract* — `hear/objectstore/backend.py`, ten calls, one local adapter —
and deliberately does not ship a backend. This page is the evidence behind that gap: what the
contract actually demands, what this deployment can actually provide, and what has to become true
before a backend is chosen. It decides nothing. `docs/deployment.md` states the target shape;
this page states which rung of it the present hardware is on, and why.

Everything here is measured read-only on the single k3s node, or read out of upstream source. No
backend was downloaded, built, provisioned or configured to produce it, and no bucket exists.

## The three facts that come before the comparison

**1. The free space is not there.** `df` reports 240 G available on `/`. `/` is an ext4 sparse
VHDX — `D:\WSL\Ubuntu\rootfs\ext4.vhdx`, 879.2 GB — on a SATA SSD whose Windows partition has
**51.9 GB free**. `/pool`, every PVC, the k3s datastore, `/var/backups/k3s` and the container
image store all live inside that one file, and collectively they can grow by about 52 GB before
the host disk stops them. The separate NVMe (F:, 333.4 GB free) is the backup target, budgeted at
200 GiB. A 320 GiB object-store reservation does not fit beside it. Capacity planning for this
lane must use the 52 GB number, not the 240 G one.

**2. Server-side encryption is the wrong layer here.** The key design requires
digest → key derivation → *then* the encryption boundary, with HMAC-derived blob ids for the
classes carrying ambient audio and 7-decimal coordinates, precisely so that a store operator
cannot confirm whether a guessed recording is present. A store that encrypts server-side has
already seen the plaintext and the plaintext-derived key. SSE-KMS therefore does not satisfy
`docs/data-governance.md:106-113` for those classes, and its presence or absence is close to
irrelevant when comparing backends. The encryption that matters is client-side, and it is not
written yet.

**3. MinIO community is unmaintained.** `minio/minio`'s README opens with "THIS REPOSITORY IS NO
LONGER MAINTAINED"; the community edition is source-only with no published binaries, legacy
binaries receive no updates, and the community documentation URLs redirect to AIStor, which
requires a licence. That is a live consideration for the process holding the only copy of
irreplaceable evidence on a machine with no dedicated operator.

Against those, this repository's own doctrine already answers the tier question:
`docs/deployment.md:12` places a single machine on a local filesystem object store;
`:54-56` specifies it as a content-addressed directory with atomic temp-file-then-rename writes,
behind an S3-shaped application interface "so the move to MinIO or cloud object storage is
configuration-only"; `docs/cost-capacity.md:199` prescribes "local MinIO/filesystem" below 100
nodes and, in the same sentence, "take tested backups; do not add Kafka". Replicated
S3-compatible storage first appears at `deployment.md:79`, which begins at three machines.

## What the contract demands

| # | Demand | Where it lives |
| --- | --- | --- |
| D1 | Conditional create-if-absent on the pointer: absent → committed, present with the same blob → replay, present with a different blob → conflict | `commit_pointer` |
| D2 | A fencing epoch when D1 is unavailable, so a resurrected writer is refused at commit | `Lease.epoch`, `_lease_is_live` |
| D3 | Published keys are immutable by mechanism — `put_immutable(if_absent=False)` raises | `backend.py` |
| D4 | The importer credential has no `delete_object`; staging is the only deletable prefix | module docstring |
| D5 | Read-after-write for a new key, because verification re-reads what was staged | `stage.py` |
| D6 | Range GET, so a 392 MB model and a 480 KB clip share one code path and bytes never flow through a worker (`docs/api-boundaries.md:88-90`) | `get_range` |
| D7 | Prefix listing for GC and audit only — resume replays the local ledger, never a bucket listing | `list_prefix` |
| D8 | Leases with TTL, holder and a monotonic epoch | `acquire_lease` |
| D9 | Three credentials: importer, reader, custodian | key design; today every job, including the `*-check` verifiers, mounts `/pool` rw |
| D10 | The digest compared on readback is the one recorded at ingest, never recomputed from the same read | `ledger.jsonl`, `index.jsonl` |

D10 is why a backend-supplied ETag or checksum is never the authority, and therefore never a
reason to prefer one backend over another.

## Measured filesystem semantics

Thirteen properties, probed on both candidate roots, each scratch tree created and removed inside
a single command.

| Property | `/` (ext4) | `/mnt/f` (9p/DrvFs → NTFS) |
| --- | --- | --- |
| `O_CREAT\|O_EXCL` refuses an existing key (D1) | yes | yes |
| `os.replace` over an existing file | yes | yes |
| directory `fsync` | yes | call returns |
| hardlink, symlink, xattr, `flock` | yes | yes |
| case-sensitive paths | yes | **no** |
| mode `0600` honoured (D9) | yes | **no** |
| create + `fsync`, 4 KiB | 16.18 ms | 4.03 ms |
| `listdir`, 69 entries | 0.08 ms | 2.48 ms |
| read-after-write of a new key (D5) | yes | yes |

Three readings matter more than the table:

* **Case-insensitivity on NTFS.** Today's keys are lowercase hex, so nothing collides. It does mean
  that if the store ever lives there, "keys are lowercase-hex and case-insensitively unique" stops
  being an implementation detail and becomes an invariant needing a test — and that shortening keys
  with base32 or base64 would be a data-loss change rather than an optimisation.
* **Mode bits are ignored on DrvFs.** The importer/reader/custodian split cannot be enforced by
  POSIX permissions there. On a deployment whose known weakness is that every job mounts the pool
  read-write, giving up the mechanism that would fix it is a real cost.
* **`fsync` that is four times faster than local ext4 for the same operation is not a fast disk.**
  It is evidence that `fsync` over 9p is not reaching a write barrier. The local adapter's
  durability rests on `write` → `fsync` → `close`; on F: that guarantee should be treated as
  unproven until someone demonstrates otherwise.

`/` passes all thirteen, at an honest 16 ms. Across roughly 9,400 corpus files that is about two
and a half minutes of `fsync`, comfortably inside the import lane's ten-minute budget.

## Comparison

| Criterion | Local directory on `/` | Local directory on F: | MinIO (community / AIStor) | SeaweedFS | Garage | Cloud S3 | Postgres blobs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D1 conditional put | probed | probed | in the API, unverified here | **verified in upstream source** (`If-None-Match` → precondition failed) | unverified | documented | only via SQL constraints |
| D3 immutability mechanism | by construction | by construction | needs bucket versioning | needs verification | **no object versioning upstream, so no object lock** | yes | by grant, not mechanism |
| D9 role split | expressible (modes honoured) | **not expressible** | strongest option (per-prefix IAM) | policy model exists | per-key-per-bucket only | yes | DB roles |
| Key custody | no new secret | no new secret | adds a root credential to a plain Secret, which the manifest backup dumps in cleartext | same | same | same + cloud IAM | DB credential |
| Backup/restore | it is a directory; the pool backup design covers it unchanged | directory, but on the backup device itself | backing up a live store needs a second tool and a restore that rebuilds server state | same | same | different problem | folds evidence into the DB, contra `deployment.md:40` |
| Capacity | ~52 GB real headroom | 333 GB less the 200 GiB backup budget | inherits either, plus internal overhead | same | same | unlimited, off-site | inherits `/` |
| Local CI | already green, no new dependency, `python:3.12-slim` already on the node | same code | service container from a source build | service container | service container | credentials in CI | the ephemeral-Postgres job could be reused |
| Deployment burden | a PVC and a directory | a hostPath and a mandatory sentinel | a service, a credential, an upgrade duty | same | same | breaks the standalone boundary | reuses an existing service |
| Boundary lint | already merge-clean | same | adapter cannot import `boto3` inside `hear/` | same | same | the `aws` rule is merge-blocking | clean |
| Future migration | the documented on-ramp: the Protocol is the shim | same | migrating off later is the same work, done twice | — | re-import required, no lock | egress | export problem |

Two notes on the ranking. MinIO wins exactly one criterion — D9 — and it wins it clearly; that is
the criterion that should reopen this question. And if an S3 server is mandated, SeaweedFS is the
better-evidenced pick: its conditional-put path is verifiable in source and its upstream is
maintained. Garage is excluded on its own documentation, which states it does not support object
versioning.

## Gaps that belong to the importer, not to any backend

Reading the merged scaffold surfaced four things that block a live run whichever backend is
chosen. All four were fixed while the Protocol still had a single implementation, which was the
point of fixing them then: each one changes the Protocol, and changing it once is cheaper than
changing it after an adapter exists. None of this selects, provisions or configures a backend.

| Gap | What it was | What closed it |
| --- | --- | --- |
| Bodies were whole objects in memory | `put_staged(bytes)` and a single `read()`; the perch model is 392 MB | `hear/objectstore/streaming.py`: a payload is a `ChunkSource` (a factory of chunk iterators), digests accumulate in flight, and `put_staged_stream`/`put_immutable_stream`/`iter_range` are the payload calls. `tests/test_objectstore_streaming.py` imports a 64 MiB object under an 8 MiB `tracemalloc` ceiling. |
| No encryption existed | envelope, per-(tenant, class) KEK and HMAC blob ids were design only | `hear/objectstore/crypto.py`: an injected `KeyProvider`/`Cipher` boundary. The **default refuses**, so a restricted class is quarantined `key_provider_unavailable` rather than published in the clear; with a provider, the blob id is `HMAC-SHA256(K_tenant_index, plaintext digest)` and the plaintext digest exists only inside the sealed metadata. |
| No republish path | the rollback ladder's "republish the previous generation" rung had no mechanism | `commit_pointer(..., expect_generation=...)` plus immutable per-generation records under `hear/v1/ptrgen/`. The open tail is the only object written twice, at most once per (class, partition) per run, and `superseded_by` is *derived* from the next generation rather than written back onto an immutable record. |
| `acquire_lease` was read-then-write | two racers could be handed the same epoch, which fences neither | acquisition is now an `O_CREAT|O_EXCL` claim on an epoch-named record: one winner, and the loser is told `None`. `LocalDirBackend(conditional_put=False)` models the store that has no conditional put — its writes really do lose an update, and a commit without a live fencing lease is refused outright. |

What is still **not** closed, and is not this lane's to close: `head()` still carries no digest, so
verification costs a second read (it is a streamed read now, not a buffered one); there is no
capability probe against a real store, because there is no store; and nothing here has been run
against anything but synthetic fixtures.

## Where an S3 adapter may live

`tools/recoupling_guard.py` scans `hear/` and `modules/`, and its `aws` rule refuses the imports
`boto3`, `botocore`, `aiobotocore`, `s3transfer`, `awscli`, `awscrt` and the AWS credential and ARN
literals; `tools/check_contract_layout.py` refuses the same names from the contract surface. Both
are merge-blocking. A boto3-based adapter therefore cannot sit in `hear/objectstore/` beside the
Protocol it implements without relocating to `tools/` or amending a gate.

There is also a loophole worth naming before it is discovered by accident: the `minio` Python SDK
is not in the banned list, so importing it from `hear/` passes the guard today while contradicting
the rule's stated reason — that domain code must be unit-testable without a cluster. If an S3
backend is ever chosen, that is a decision to take deliberately, not a lint result to lean on.

## What has to be true before any backend is deployed

1. The pool has a restore-tested backup. Unchanged, and still the hard blocker; nothing below
   matters until it passes.
2. ~~Staging streams instead of buffering~~ — done: proven against a fixture 1,024 chunks long,
   under a memory ceiling a buffered body cannot pass.
3. ~~The encryption boundary exists in code~~ — the *boundary* does, and it refuses by default. A
   **key provider and a reviewed AEAD still have to be chosen and wired**: the only cipher in the
   repository is `HmacCtrCipher`, which declares `production_ready = False`, and the only provider
   is `InMemoryTestKeyProvider`, whose material is a test seed that never reaches disk.
4. The capacity reservation is renegotiated against the 52 GB figure rather than the 240 G one.

## The open questions, stated as choices

* **Where should the shadow store live** — the ext4 root, sharing a disk with the data it shadows
  and with the datastore backup, or the second NVMe, which is the backup target and whose `fsync`
  durability is unproven?
* **Is per-prefix IAM worth a service?** It is the one thing a filesystem directory cannot give,
  and the one thing that would justify an S3 server on a single node.
* **If an S3 server is wanted, which upstream** — an unmaintained community MinIO, a licensed
  AIStor, or a maintained alternative that no document in this repository currently names?
* **Who owns the custodian credential?** Still unanswered, and still not a question measurement can
  answer.
* **How large is the reservation, really?** The shadow import is a few gigabytes after dedupe. The
  long-horizon number does not fit on this hardware; one of the two has to move.

Until those are answered, the contract stays backend-agnostic and the local directory adapter stays
what it says it is at the top of its own module: not a production backend.
