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

The rules come from `files/phase3-key-design/PHASE3-OBJECT-KEY-DESIGN.md` and
`files/phase3-import-plan/PHASE3-OBJECT-IMPORT-PLAN.md`. Nothing here imports `hear.pool`,
`hear.clips` or `hear.tags`: the object store takes `pool.key`/`clip_key`/`tag_key` as *inputs*
and must never re-derive an identifier those modules already wrote into rows that exist.
"""
