# `dama/hear-pool` Backup and Restore Plan (G0 for Phase 3)

Written 2026-09-15 against `origin/main` at `1e028ea` and a **read-only** inspection of the live
host and cluster. Design and acceptance criteria only — no code and no cluster object ships with
this document.

**Nothing was changed.** No backup was taken, no storage was mounted, no live `/pool` byte was
copied out, no cluster object was created/edited/deleted, no host configuration was touched. The
only write anywhere was a 512 MiB `/dev/zero` throughput probe written to `F:\` and deleted in the
same command (§2.4); it touched no project data. No coordinate, node position or audio content
appears in this document.

This plan closes risk-register **R6** ("the Phase 3 import source has no backup, snapshot or
replica", `docs/migration-risk-register.md:49`), whose exit gate is *"a restorable `/pool` backup
exists and has been test-restored before the first import byte is written"*. It is also the **G0**
gate of `PHASE3-OBJECT-IMPORT-PLAN.md`.

---

## 1. Decision in one paragraph

There is no snapshot primitive anywhere in this stack — no CSI snapshot API, no LVM, no btrfs/ZFS,
no second node, and the filesystem is ext4 inside a Windows VHDX. The only viable mechanism is a
**pull-based, file-level, self-contained generational archive**: a CronJob in namespace `backups`
that mounts PVC `dama/hear-pool` **read-only**, streams each selected file through SHA-256 →
`tar` → gzip → AES-256, and writes one encrypted archive plus a JSONL manifest and a signed receipt
to a **hostPath on `F:\`, a different physical SSD from the one holding the data**. Weekly level-0,
6-hourly level-1 (new/changed files only), restore-tested into an isolated PVC that no `hear-*`
workload can reach. Consistency comes from the pool's own write discipline (append-only prefixes,
`tmp`+`os.replace`) plus the ingest-time digests already in `ledger.jsonl`/`index.jsonl`, not from
quiescing the pipeline. Estimated level-0 ≈ **4.2 GiB** compressed, ≈ 3–5 min, level-1 ≈ 1.5 GiB/day.

---

## 2. Measured environment (all read-only)

### 2.1 Cluster

| Fact | Value | How observed |
| --- | --- | --- |
| Node | `desktop-bvrdk4j`, single, control-plane, k3s `v1.34.6+k3s1` | `kubectl get nodes -o wide` |
| OS | Ubuntu 24.04.4, kernel `6.6.123.2-microsoft-standard-WSL2+` — **WSL2**, not bare metal | same |
| CPU / RAM | 28 vCPU, 62 GiB | `nproc`, `free -g` |
| StorageClasses | `local-path` (default), `local-path-retain` (Retain), `dama-models-sc` — **all** `rancher.io/local-path` | `kubectl get sc` |
| Snapshot API | **absent** — `volumesnapshotclass` is not a server resource; only `k3s.cattle.io/v1 ETCDSnapshotFile` exists | `kubectl api-resources \| grep -i snapshot` |
| PVC `dama/hear-pool` | RWO, `local-path`, **5 Gi requested**, PV `pvc-bb88674c-4a2d-4556-ae98-203f15b31a16`, **`reclaimPolicy: Delete`** | `kubectl get pvc/pv -o jsonpath` |
| Host path | `/var/lib/rancher/k3s/storage/pvc-bb88674c-…_dama_hear-pool` (root-only; `sudo` needs a password) | `kubectl get pv`, `sudo -n` |
| Existing backups | ns `backups`: `k3s-datastore-backup` (`0 */6`, sqlite `.backup`, 28 generations), `k3s-manifest-backup` (`15 0`, dumps `secret -A` **in cleartext**), both to hostPath `/var/backups/k3s` = 533 M | `kubectl -n backups get cronjob -o yaml`, `du -sh` |
| Cron minutes already used in `dama` | 0,4,7,11,12,15,17,19,22,26,27,30,34,37,38,41,42,45,47,49,52,56,58 | `kubectl -n dama get cronjob` |

`/var/backups/k3s` is on the same ext4 filesystem, the same VHDX and the same physical disk as
`/pool`. **The cluster today has no backup that survives the loss of one device.**

### 2.2 The filesystem, and the constraint nobody has written down yet

```
/dev/sdc  ext4  1007G  714G used  243G avail  rw,discard,errors=remount-ro   (mount propagation: private)
inodes: 5,342,402 / 67,108,864 used (8%)
```

That ext4 filesystem is a **sparse VHDX file on a Windows volume**:

| Fact | Value |
| --- | --- |
| Backing file | `D:\WSL\Ubuntu\rootfs\ext4.vhdx`, **943,754,051,584 B ≈ 879 GiB allocated** |
| `D:` | Windows disk **0**, Samsung SSD 860 EVO 1TB, 932 G, **880 G used, 52 G free (95 %)** |
| `C:` | Windows disk **1**, Samsung SSD 970 EVO Plus 1TB, 248 G free |
| `F:` | Windows disk **2**, Samsung SSD 970 EVO Plus 1TB, **334 G free** |
| Disk health | all three `Healthy` |
| BitLocker | `C:`, `D:`, `F:` all **FullyDecrypted, ProtectionStatus Off** |

Consequences that change earlier planning:

1. **`/pool`'s real headroom is not 243 GiB.** Roughly 165 GiB is space already allocated inside the
   VHDX and reusable (the filesystem is mounted `discard`), and beyond that only **52 GiB** of real
   `D:` capacity remains before the VHDX cannot grow. At the measured `raw/` growth of 3.4 GB/day
   this is weeks, not months, and the failure lands on *every* workload on the node, not just hear.
   **The G5 "reserve 320 G" gate in `PHASE3-OBJECT-IMPORT-PLAN.md` is not satisfiable on `D:` and
   must be re-pointed at a different volume.**
2. **A backup written anywhere under `/` is not a backup.** Data, k3s datastore, existing backups
   and the VHDX all share disk 0. The target must be `C:` or `F:`.
3. **Nothing is encrypted at rest.** `docs/data-governance.md:108` requires storage, backups and
   removable media to be encrypted; archive-level encryption is therefore mandatory, not optional.

### 2.3 Tooling actually present

| Present | `tar` (GNU), `rsync`, `gzip`, `pigz`, `gpg`, `openssl`, `sha256sum`, `dd`, `python3.12`, `kubectl`, `k3s`, `helm` |
| --- | --- |
| **Absent** | `restic`, `borg`, `zstd`, `age`, `sqlite3` CLI, `pv`, `cpio`, `mc`, any CSI snapshotter, Velero, LVM/ZFS/btrfs tooling |

`sudo` requires a password, so **every** read of `/pool` must go through a pod that mounts the PVC
(this is how the Phase 3 inventory was measured, and how this plan's job works).

### 2.4 Is `F:` reachable and fast enough from a pod?

* `k3s server` (pid 326) sees `/mnt/c`, `/mnt/d`, `/mnt/f` in its own mountinfo → a `hostPath`
  volume under `/mnt/f` resolves.
* Precedent in-cluster: `infra/nova-bullshit-detector-…` already `hostPath`-mounts
  `/mnt/c/Users/…/workspace` and completed successfully. drvfs `hostPath` works.
* Throughput, measured once and cleaned up: `dd` 512 MiB `oflag=direct` → **162 MB/s write**,
  **203 MB/s read** on `/mnt/f`. Ample for a 4.2 GiB archive.
* Mount options are `uid=1000,gid=1000`: a root container writes as the desktop user, and **NTFS
  cannot carry POSIX ownership or mode**. This is exactly why the payload is a `tar` archive
  (metadata travels *inside* the archive) and not an `rsync` mirror.
* Syncthing is installed for the Windows user and `F:\` carries a stale `.stfolder`. Its config was
  last written in 2022, lists only `C:\…\Sync`, `Pictures` and `~`, and does **not** list `F:`. It
  is dormant — but ambient audio and precise coordinates must never land inside a consumer sync
  share, so re-verifying this is a named G0 pre-check (G0.1c).

### 2.5 What is being protected (measured 2026-09-15, read-only `kubectl exec`)

| Path | Size | Files | Class |
| --- | --- | --- | --- |
| `corpus/raw/**` | 8.4 G | 5449 | authoritative, immutable once written |
| `corpus/clips/**` (1834 WAV + `index.jsonl` + `tags*.jsonl`) | 856 M | — | authoritative; **WAVs are prunable** |
| `corpus/tdoa/**` | 292 M | — | derived |
| `corpus/scene/**` (`.jsonl.gz`) | 190 M | — | authoritative, append-only, multi-member gzip |
| `corpus/scores/**` | 143 M | — | derived |
| `corpus/records/**` | 88 M | — | authoritative, append-only |
| `corpus/ledger.jsonl` | 3.0 M | 1 | authoritative index + **ingest-time SHA-256 per source file** |
| `corpus/state/`, `heartbeat.json` | 868 K | 8 | operational; `state/node_positions.json` is **location-sensitive** |
| `corpus/annotations.sqlite3` | 172 K | 1 | authoritative, **live WAL** |
| `models/**` | 492 M | — | reproducible but unpinned in-pool |
| `sketch_corpus/**` | 21 M | — | derived |
| **Backup scope total** | **≈ 10.5 GiB**, 9381 corpus files + models | | |
| `pylib*` | 4.7 G | — | **excluded**: vendored `site-packages`, pip-reproducible |

Sensitivity: 5 s ambient outdoor audio (1834 clips) and 7-decimal GPS in `raw/**-health.csv` and
`state/node_positions.json`. Any copy of this pool is a location-disclosing artifact.

### 2.6 Compressibility, measured (zlib level 1, first ≤20 MiB of 3 files per class)

| Class | Ratio | Class | Ratio |
| --- | --- | --- | --- |
| `raw/*dets.csv` | 0.367 | `records/*.jsonl` | 0.275 |
| `raw/*scene*.csv` | 0.345 | `scores/*.jsonl` | 0.077 |
| `raw/*health.csv` | 0.346 | `scene/*.jsonl.gz` | **0.998 — already gzipped, store as-is** |
| `clips/**.wav` | 0.563 | | |

Projected level-0 archive: raw 2.95 G + clips ≈ 0.50 G + scene 0.19 G + tdoa ≈ 0.09 G + scores
0.011 G + records 0.024 G + models ≈ 0.44 G + sketch/ledger/state ≈ 0.01 G ≈ **4.2 GiB**.

---

## 3. Options assessed

| Option | Verdict | Why |
| --- | --- | --- |
| CSI volume snapshot | **Impossible** | No `VolumeSnapshotClass`/`VolumeSnapshot` resource on the API server; `local-path` is not a CSI driver. |
| Storage-layer replication (Longhorn/Ceph) | **Rejected now** | Requires a second node or a second device *and* a migration of the live PVC — a far larger, destructive change than the gate needs. Correct long-term answer; not a G0. |
| LVM / btrfs / ZFS snapshot | **Impossible** | `/` is plain ext4 inside a VHDX; no volume manager, no snapshot-capable fs, no tooling installed. |
| Windows VSS / copy the VHDX | **Rejected** | Copying an 879 GiB VHDX to hold 10.5 GiB of data is absurd, `D:` has 52 G free, and a live-copied VHDX gives a crash-consistent ext4 image with no per-file verification path. |
| `restic`/`borg` repository | **Rejected for G0** | Neither is installed; introducing a repository format that must itself be understood, versioned and restored adds a dependency to the one mechanism that must work when everything else is broken. Revisit once dedupe economics dominate (§8). |
| `rsync` mirror to `F:` | **Rejected as the primary** | NTFS-over-9p cannot store POSIX ownership/mode; hardlink-based generations (`--link-dest`) are unreliable on drvfs; a mirror has no generations, so a corrupting write propagates on the next run. Retained only as an optional *secondary* plain mirror of the ≤1 MiB `state/`+`ledger` set. |
| k3s etcd/datastore snapshot | **Not applicable** | Backs up object definitions, not PVC contents — this is precisely the existing gap. |
| NAS / off-site | **Not available** | `/mnt/syno-shared`, `/mnt/syno-malicious`, `/mnt/e` are empty directories, nothing mounted; no CIFS/NFS mount exists. Listed as a §13 follow-up, because two SSDs in one chassis is not off-site. |
| **Generational encrypted `tar` archives to `F:` (chosen)** | **Chosen** | Uses only tooling that exists, is restorable with `gpg`+`tar` and nothing else, is self-contained per chain, verifies against digests the pool already computes, needs no cluster mutation beyond adding two CronJobs, and lands on a different physical SSD. |

---

## 4. Design

### 4.1 Components (all additive; nothing existing is modified)

| Object | Purpose |
| --- | --- |
| `tools/hear_pool_backup.py` | One-pass backup: walk → per-file SHA-256 → `tar` → gzip → AES-256 → archive + manifest + receipt. Also `--restore` and `--verify`. Stdlib only (`tarfile`, `gzip`, `hashlib`, `sqlite3`, `json`) plus one `subprocess` call to `gpg`. |
| CronJob `backups/hear-pool-backup-l0` | Weekly level-0. |
| CronJob `backups/hear-pool-backup-l1` | 6-hourly level-1 (new/changed files only). |
| CronJob `backups/hear-pool-backup-check` | Staleness + target-health gate; **fails loud** (resilience invariant 4, `docs/resilience.md`). |
| ConfigMap `backups/hear-pool-backup-code` | Generated by `deploy/k8s/gen_configmap.py` — the only supported way to ship code into a pod in this repo. |
| ServiceAccount `backups/pool-backup` | **No RBAC rules at all**; the job needs none. |
| Secret/Infisical reference | AES-256 passphrase, §6. |

Job pod shape, copied from the proven `hear-*-check` pattern (`deploy/k8s/hear-drain.yaml`):
`image: python:3.13-slim`, `restartPolicy: Never`, `backoffLimit: 0`, `concurrencyPolicy: Forbid`,
`requests cpu 500m/memory 512Mi`, `limits cpu 4/memory 2Gi`, `activeDeadlineSeconds: 1800`.

Volumes:

```yaml
- name: pool                      # THE POOL IS READ-ONLY. This is the whole point.
  persistentVolumeClaim: { claimName: hear-pool, readOnly: true }
  # ... and on the container: { mountPath: /pool, readOnly: true }
- name: target
  hostPath: { path: /mnt/f/hear-backup, type: Directory }   # Directory, never DirectoryOrCreate
- name: work
  emptyDir: { sizeLimit: 2Gi }    # sqlite snapshot staging only; never on /pool
```

> ⚠️ **`type: Directory` is not sufficient on its own.** If `F:` ever fails to mount, `/mnt/f` still
> exists as an empty directory *on the ext4 root*, and `hostPath` would happily write the backup
> onto the very disk it exists to escape — while inflating the VHDX that has 52 G of headroom. The
> job therefore **must** verify a sentinel file `/target/.hear-backup-target` containing a fixed
> UUID and the string `F:` before it writes a single byte, and exit non-zero if it is absent.
> Sentinel-before-write is a hard requirement, not a nicety.

`hear-pool` is RWO and already bound to workloads on the same (only) node, so an additional
read-only mount schedules without contention.

### 4.2 Target layout on `F:`

```
F:\hear-backup\
  .hear-backup-target                       # sentinel: uuid + volume label (created once, by hand)
  generations\<gen_id>\
      pool-<gen_id>.tar.gz.gpg              # the payload
      manifest-<gen_id>.jsonl.gpg           # one row per file (encrypted: it carries paths)
      receipt-<gen_id>.json                 # cleartext, no paths: ids, counts, digests, timings
  index.jsonl                               # append-only: one row per generation, ledger of chains
  restore-tests\<test_id>\report.json       # G0 and quarterly drill evidence
```

`gen_id = <level>-<UTC ISO basic>-<8 hex of the run uuid>`, e.g. `l0-20260920T023200Z-9f3c1a02`.
Manifests are encrypted because a path list alone discloses node names, day partitions and clip
counts. Receipts are cleartext so monitoring can read them without the key.

### 4.3 Manifest row (JSONL, one per file)

```json
{"gen_id":"l0-…","path":"corpus/raw/<node>/<stamp>-dets.csv","bytes":123456,"mtime_ns":…,
 "inode":…,"sha256":"<64 hex>","class":"raw","state":"stored",
 "digest_source":"computed|ledger|index","ledger_sha256":"<64 hex>|null","stable":true,
 "tar_member":"pool/corpus/raw/…"}
```

`state` ∈ `stored` | `unchanged` (level-1, body lives in the referenced parent generation) |
`vanished` (deleted under us mid-run, §5.3) | `unstable` (changed under us, deferred, §5.2).
Canonicalisation for any hashing of the manifest itself reuses `tools/freeze_contracts.py`'s
convention (`sort_keys`, compact separators, UTF-8) so digests are comparable with Phase 3 work.

### 4.4 Receipt (cleartext, no paths, no coordinates)

`gen_id`, `level`, `parent_gen_id`, `started_at`/`ended_at`, `files_stored/unchanged/vanished/unstable`,
`bytes_in`, `bytes_out`, `archive_sha256`, `manifest_sha256`, `manifest_rows`, `tool_version`,
`repo_commit`, `pool_stats_sha256` (digest of the `--stats` snapshot taken at freeze time, §9.3),
`target_free_bytes`, `sentinel_uuid`, and a detached `gpg --sign` signature if a signing key is
provisioned. Every receipt is appended as one row to `index.jsonl`.

### 4.5 Level-1 rule

A file is re-stored in a level-1 generation when `(path, bytes, mtime_ns, inode)` differs from the
newest generation in the chain that stored it, **or** when the file's class is append-only (those
always change, and re-storing the whole ≈ 850 MiB of JSONL/gz each run at ≈ 300 MiB compressed is
cheaper and far simpler than byte-range deltas). Everything else — `raw/` archives, WAVs, model
files — is immutable once published, so a level-1 stores only genuinely new bodies. Projected
level-1: **≈ 1.2 GiB/day of new `raw/` + ≈ 0.3 GiB per run of re-stored streams ≈ 1.5 GiB/day.**

Cross-generation body dedupe by SHA-256 was deliberately **rejected**: it would make restoring
generation *N* require reading arbitrary earlier archives, i.e. it trades the one property a backup
must never trade (each chain restores with `gpg` + `tar` and nothing else) for ≈ 40 % space, on a
volume with 334 G free. Revisit only if §8's budget is breached.

---

## 5. Consistency boundaries

`/pool` is live: `hear-drain` runs every 15 min, five other lanes run on top of the hour. **The
pipeline is not quiesced and no CronJob is suspended** — suspending drain drops node data that
cannot be re-fetched (`hear/clips.py`: an evicted clip "cannot be re-fetched from anywhere"), which
would make the backup itself a data-loss event. Consistency is derived per class instead.

### 5.1 Append-only streams — prefix rule

`ledger.jsonl`, `clips/index.jsonl`, `clips/tags*.jsonl`, `records/**.jsonl`, `scores/**.jsonl`,
`tdoa/arrivals/**` are opened `"a"` and only ever grow (`hear/pool.py:427,432`, `hear/clips.py:364`,
`tools/hear_score.py:676`). `scene/<day>/<node>.jsonl.gz` is `gzip.open(..., "at")`
(`hear/pool.py:761`), i.e. appended gzip **members**. Therefore **any prefix of these files is a
valid, self-consistent earlier state**. The job:

1. `open()`, `fstat()` → freeze `length` and `inode`;
2. read exactly `length` bytes (never to EOF) while hashing, feeding `tarfile.addfile` with the
   frozen size;
3. `fstat()` again — if `inode`/`dev` changed (rotation), mark `unstable` and defer;
4. record the frozen `length` and digest in the manifest.

Restore-side repair, applied by `--restore` and asserted by G0.8: trim the tail to the last `\n`
for JSONL, to the last complete gzip member for `.jsonl.gz`, and record any trimmed byte count in
the restore report. A trim > 0 is normal; a trim that removes a *complete* row is a bug.

### 5.2 `raw/` archives — the one non-atomic writer

`tools/hear_drain.py:1047-1053` (`archive()`) writes a raw archive with a plain `open(p,"wb")` +
`write()` — **no `tmp` + `os.replace`**, unlike every other writer in the lane
(`hear_drain.py:732,1329,1525,2363`, `hear_tag.py:1536`, `hear_score.py:600`, `hear_tdoa.py:455`).
A raw archive can therefore be observed partially written, and a pod killed mid-write leaves a
truncated file in the pool permanently.

Mitigation in the backup (not a fix to the pool — out of scope here, raised in §13):

* re-`fstat()` after reading; if `bytes`/`mtime_ns` moved, mark `unstable`, do **not** store a torn
  body, and let the next generation pick it up;
* skip any file whose `mtime` is younger than 120 s (drain's write window) in level-1 runs;
* where `ledger.jsonl` already records the ingest-time `sha256` for that body, compare — a mismatch
  is recorded as `unstable` with `digest_source: "ledger"` and a non-zero `unstable` count in the
  receipt. Persistent `unstable` on the same path across three generations is a check failure.

### 5.3 Pruned clips

`hear/clips.py:458-492` prunes WAVs (`os.remove`, line 481) before each fetch when the 2 GiB cap is
exceeded, and sweeps `*.tmp` (line 426). A file can vanish between `walk` and `open`. `ENOENT` is
**expected**, recorded as `state: "vanished"`, and never fatal; `*.tmp` files are excluded by
pattern. A `vanished` count is informational, not an error — but a `vanished` path that a previous
generation stored is *preserved* in the older archive, which is the desired behaviour (index rows
are never deleted; §8 governs when the audio finally ages out of the backups too).

### 5.4 `annotations.sqlite3` — live WAL

`tools/hear_annotate/server.py:238-244` opens it with `PRAGMA journal_mode=WAL`. A raw copy of the
triple is a torn database. The job uses `sqlite3.Connection.backup()` (the SQLite backup API) to
write a consistent copy into the `work` emptyDir, runs `PRAGMA integrity_check` on the copy, records
its row counts per table, and only then adds the **copy** to the archive.

> Constraint to resolve in implementation: reading a WAL database requires creating/writing the
> `-shm` file, which a fully read-only mount forbids. Two acceptable shapes, in order of preference:
> **(a)** a second, narrow volumeMount that is read-write **only** for the three
> `corpus/annotations.sqlite3*` paths via `subPath`, while `/pool` stays read-only; **(b)** open
> `file:/pool/corpus/annotations.sqlite3?immutable=1` read-only, valid only while no writer holds
> the DB, and fall back to (a) on `SQLITE_READONLY`. Whichever is used, G0.7 must pass on a restore
> taken while `hear-annotate` is running. At 172 KiB the copy is free.

### 5.5 Models and small state

`models/**` are static files (write-once directories); `state/*.json` and `heartbeat.json` are
`tmp`+`os.replace`, hence atomically observable. No special handling. `pylib*` is excluded by an
explicit allowlist of roots (`corpus/`, `models/`, `sketch_corpus/`) — an allowlist, not a denylist,
so a new 4 GiB vendored directory cannot silently enter the backup.

---

## 6. Encryption and key custody

* **Cipher.** `gpg --symmetric --cipher-algo AES256 --digest-algo SHA512 --s2k-mode 3
  --s2k-count 65011712 --compress-algo none --batch --passphrase-file <fd>` over the already-gzipped
  stream. `gpg` is present on the host; in `python:3.13-slim` it is installed at job start
  (`apt-get install -y --no-install-recommends gnupg`) exactly as `k3s-datastore-backup` already does
  `apk add sqlite`. **The install is fail-closed**: no `gpg`, no run, non-zero exit. Hardening
  follow-up: bake an image into the in-cluster registry (`registry` namespace exists) so the backup
  does not depend on egress at 02:32.
* **Why symmetric.** Restore must work with exactly two inputs — the archive and a passphrase — with
  no keyring state to lose. Public-key mode would let backups be written without the ability to read
  them, which is attractive, but doubles the custody problem at the moment the gate is trying to
  prove restorability. Revisit after G0.
* **Custody.** The passphrase lives in **Infisical** (`infisical` namespace is running, backed by its
  own Postgres) and is injected as an env-var-sourced file at job start. **An offline copy is
  mandatory** and must live outside this host entirely (sealed paper or a separate device). Rationale:
  Infisical's Postgres is a PVC on the same disk as everything else — key and data share a failure
  domain, which is the one failure mode that turns "encrypted backup" into "no backup".
* **Do not put the passphrase in a plain `Secret` in namespace `backups` without a change**, because
  `k3s-manifest-backup` dumps `kubectl get secret -A -o yaml` in cleartext to `/var/backups/k3s`
  every night. If a `Secret` is used anyway, that job must be amended to exclude it. (The existing
  cleartext-secret dump is a pre-existing finding in its own right; raised in §13.)
* **Key rotation.** New passphrase ⇒ new chain: rotate only at a level-0 boundary, record
  `key_ref` (a non-secret label, e.g. `pool-backup-2026Q3`) in every receipt, and keep the previous
  passphrase in custody until the last generation encrypted under it has aged out.
* **Blast radius.** Archives are readable by anyone with the passphrase; NTFS and drvfs give no
  access control (`uid=1000` for everything). The encryption *is* the access control.

---

## 7. Schedule

Minutes 2 and 32 are the only minutes free of every `hear-*` cron; the `backups` namespace uses
minutes 0 and 15.

| Job | Schedule | Purpose |
| --- | --- | --- |
| `hear-pool-backup-l0` | `2 2 * * 0` (Sun 02:02) | level-0 full |
| `hear-pool-backup-l1` | `32 3,9,15,21 * * *` | level-1 incremental → **RPO ≤ 6 h** |
| `hear-pool-backup-check` | `2 */3 * * *` | staleness, target health, budget, chain integrity |

Budgeted runtimes (read ≈ 10.5 GiB from a local SSD, SHA-256 ≈ 0.5–1 GB/s/core, zlib-1 ≈ 60–100 MB/s
/core across 4 workers, write 4.2 GiB at 162 MB/s): **level-0 ≈ 3–5 min, level-1 ≈ 1 min.**
`activeDeadlineSeconds: 1800` with `concurrencyPolicy: Forbid`. Overlap with a drain run is
tolerated by design (§5); the job never blocks a writer because it holds no locks and mounts
read-only.

---

## 8. Capacity, retention and the closed loop with Phase 3

Current: level-0 ≈ 4.2 GiB, level-1 ≈ 1.5 GiB/day, and `raw/` grows **3.4 GB/day, uncapped**.

Retention (GFS, applied by the backup tool itself, never by hand):

| Tier | Keep | Approximate cost today |
| --- | --- | --- |
| Level-1 | 28 most recent (7 days) | ≈ 10.5 GiB |
| Weekly level-0 | 4 | ≈ 17–25 GiB |
| Monthly level-0 | 3 (first level-0 of each month) | ≈ 15–30 GiB |
| **Budget on `F:`** | **200 GiB hard cap, alert at 70 %** | 334 GiB free today |

Fail-closed rules: refuse to start if `target_free_bytes < 2 × expected_archive_bytes`; refuse to
delete a generation that any retained generation names as a `parent_gen_id`; never delete the newest
level-0 or the newest successfully restore-tested generation; deletion is by whole generation
directory, logged to `index.jsonl` with the reason.

**The honest number:** at +1.2 GiB/day of compressed level-0 growth, a level-0 reaches ≈ 40 GiB in a
month and the 200 GiB budget is consumed in roughly **8–10 weeks**. This backup buys the runway for
Phase 3, and Phase 3 (dedupe of the 5.92 GB of byte-identical `raw/` archives — 59 % of the class —
plus the retention policy `raw/` has never had) is what makes it sustainable. The two are a loop, and
this plan is the end that must exist first. Re-size the budget at Phase F.

Privacy/retention coupling: `docs/data-governance.md:114,120` requires deletion to reach backups and
derived copies. Generation expiry is the mechanism — an erasure request is satisfied when every
generation containing the item has aged out, or, for a hold/erasure that cannot wait, by re-cutting a
level-0 and expiring the chain that contains the item. Record the intent in the erasure receipt.

---

## 9. Restore

### 9.1 Isolation (non-negotiable)

Restore **never** writes to `/pool`. Target is a **new** PVC `backups/hear-pool-restore` on
StorageClass **`local-path-retain`**, mounted at `/restore`, in a pod that **does not mount
`hear-pool` at all**. The restore tool refuses to run if its target path resolves to a device/inode
already occupied by `/pool`, if `/restore` is non-empty, or if `--i-understand` is absent.
Verification compares restored bytes against the manifest and against `ledger.jsonl`/`index.jsonl`
digests *carried inside the archive* — never against the live pool. This also prevents a restore
from being the event that fills `D:`: a 10.5 GiB restore fits in the reusable VHDX space, but a
restore-to-`F:`-then-inspect mode (`--restore-to /target/restore-scratch`) is provided for large or
repeated drills.

### 9.2 Procedure

1. Read `index.jsonl` on `F:`; choose `gen_id`. Chain = that generation plus its `parent_gen_id`
   ancestors back to the level-0.
2. Fetch the passphrase from custody (Infisical, or the offline copy).
3. `sha256sum` each archive and manifest, compare with the receipt → mismatch aborts, loudly.
4. `gpg --decrypt` → `gunzip` → `tar -x` each archive **oldest first**; later generations overwrite
   earlier paths; files marked `unchanged` resolve to the ancestor that stored them; files marked
   `vanished`/`unstable` in the newest generation retain the newest stored body and are listed in the
   report.
5. Apply tail repair (§5.1) and record trimmed bytes per file.
6. Run the verification ladder (§9.3). Emit `restore-tests/<test_id>/report.json`.

### 9.3 Verification ladder

| Level | Check | Fails when |
| --- | --- | --- |
| **V1 archive** | archive/manifest `sha256` == receipt; `gpg` and `gzip` streams terminate cleanly | any bit rot on `F:`, truncated write |
| **V2 object** | per-file `sha256` == manifest, for **100 %** of files; file count and total bytes match | tar/restore bug, silent corruption |
| **V3 cross-reference** | restored `raw/` bodies match `ledger.jsonl` `sha256`; restored WAVs match `index.jsonl` `sha256` (1823/1823 rows carry one) | a manifest that faithfully recorded a corrupt body |
| **V4 structural** | every JSONL line parses; every `.jsonl.gz` decompresses fully; `PRAGMA integrity_check` on `annotations.sqlite3` == `ok` and per-table row counts match the receipt | torn tail not repaired, bad sqlite snapshot |
| **V5 semantic** | `python tools/hear_drain.py --pool /restore/corpus --stats` reproduces the stats snapshot taken inside the backup job at freeze time (`pool_stats_sha256`) | a restore that is byte-plausible but semantically short |

V5 runs `--stats` only, never `--check` (which reaches out to nodes over HTTP). It needs
`hear/pool.py` and friends, shipped by the same ConfigMap bundle.

**RPO** = 6 h (level-1 cadence). **RTO** = decrypt+expand ≈ 5 min + verify ≈ 10–20 min; the target is
**≤ 60 min** end to end, measured and recorded by G0.10.

---

## 10. G0 acceptance test — explicit pass/fail

G0 is **passed** only when every item below is recorded with a date, a `gen_id`, and an artifact
path under `restore-tests/`, and the evidence is summarised in the repo. Until then Phase 3 import
stays blocked and R6 stays open.

| # | Test | Pass condition |
| --- | --- | --- |
| **G0.1a** | Target independence | Sentinel exists at `F:\hear-backup\.hear-backup-target`; `F:` is Windows disk 2, the data VHDX is on disk 0; both `Healthy` |
| **G0.1b** | Sentinel enforcement | With the sentinel renamed (on a scratch path, not the live one), the job **exits non-zero and writes nothing** |
| **G0.1c** | No consumer sync | No sync agent (Syncthing/OneDrive/Dropbox) has `F:\hear-backup` in scope; re-checked and recorded on the day of the first run |
| **G0.2** | Level-0 completes | One level-0 generation with `files_unstable == 0` (or every `unstable` path stored cleanly in the next generation), receipt written, `index.jsonl` row appended |
| **G0.3** | Isolation | Restore pod's spec mounts **no** `hear-pool` volume; restore PVC is a new `local-path-retain` claim; restore refuses a non-empty target |
| **G0.4** | V2 object-level | 100 % of manifest rows verify by `sha256`; counts and bytes match; **zero** mismatches |
| **G0.5** | V3 cross-reference | 100 % of restored `raw/` bodies present in `ledger.jsonl` and 1823/1823 WAVs match their `index.jsonl` digests |
| **G0.6** | V5 semantic | `--stats` over the restored pool reproduces `pool_stats_sha256` exactly |
| **G0.7** | Live-WAL sqlite | Snapshot taken while `hear-annotate` is **running**; `integrity_check == ok`; per-table row counts match the receipt |
| **G0.8** | Prefix/tail rule | Every restored JSONL line parses; every `.jsonl.gz` fully decompresses; trimmed bytes per file < one row, and the count is recorded |
| **G0.9** | Key custody | Restore performed using a passphrase fetched fresh from custody by an operator with **no write access to `/pool`**; an offline copy is confirmed to exist off-host |
| **G0.10** | RPO/RTO | Backup age at restore ≤ 6 h; measured RTO ≤ 60 min; both in the report |
| **G0.11a** | Negative: corruption | One byte flipped in a **copy** of an archive ⇒ restore fails at V1, loudly, and never produces a partial "successful" tree |
| **G0.11b** | Negative: no key | Restore without the passphrase fails closed with a distinguishable error |
| **G0.11c** | Negative: vanished file | A file removed between walk and read (reproduce with a synthetic tree) is recorded `vanished`, not silently missing, and does not fail the run |
| **G0.12** | Source untouched | Job pod spec has `readOnly: true` on the pool volume and mount; `ledger.jsonl` size/mtime and `/pool` file count identical before and after; drain/tag/score/tdoa heartbeats show no gap across the window |
| **G0.13** | Repeatability | **Two** consecutive chains (a level-0 and a level-1 on top of it) restore-tested independently; a quarterly drill is scheduled and named |

Phase 3 import may begin only after **G0.1–G0.13 all pass**, and the import plan's G5 (capacity
reservation) is re-pointed away from `D:` in light of §2.2.

---

## 11. Monitoring and failure modes

`hear-pool-backup-check` (`2 */3 * * *`) fails — and a failed Job is the alert — when any of:
newest receipt older than **8 h** (level-1) or **9 days** (level-0); sentinel missing; target free
space below the alert threshold or below 2× the next expected archive; newest receipt's
`archive_sha256` does not match the file on disk (rolling scrub: N generations per run, full sweep
weekly); `files_unstable > 0` for the same path in three consecutive generations; `index.jsonl`
chain broken (a retained generation names a missing parent); no restore test in 90 days. It also
writes `heartbeat.json` next to `index.jsonl`, mirroring the pool's own heartbeat convention.

| Failure mode | Detection | Response |
| --- | --- | --- |
| `F:` not mounted / drive detached | sentinel check | Job exits non-zero before writing; check job alerts |
| `hostPath` silently lands on ext4 | sentinel check (the reason it exists) | as above |
| `D:` fills (52 G free, VHDX cannot grow) | node disk alert; check job records `/` free | **Highest-severity standing risk**; drives Phase 3 dedupe priority |
| `F:` fills | budget gate + 70 % alert | Retention prunes; refuse-to-start below 2× |
| Silent bit rot on NTFS | V1 rolling scrub | Restore from an older generation; re-cut level-0 |
| Passphrase lost | G0.9 custody check; quarterly drill | Offline copy; rotate only at level-0 boundaries |
| Backup job never scheduled | staleness gate | Alert at 8 h |
| Torn `raw/` archive (§5.2) | `unstable` count; ledger digest mismatch | Deferred to next generation; persistent case escalates |
| Restore pollutes `/pool` | design: no pool mount in restore pod + refusal checks | — |
| `apt-get`/registry unreachable at run time | fail-closed `gpg` presence check | Pre-baked image (follow-up) |
| Secrets leaked via manifest backup | §6 | Keep the passphrase in Infisical, not a plain `Secret` |
| Whole-site loss (fire/theft) | not covered | §13: off-host copy is a follow-up, not a G0 |

---

## 12. What this plan deliberately does **not** do

* Does not suspend, scale or modify any `hear-*` workload; does not quiesce the pipeline.
* Does not write to `/pool` — ever, in any mode.
* Does not change the `hear-pool` PVC, its StorageClass or its `reclaimPolicy` (though see §13).
* Does not dedupe, prune, retain or delete anything in `/pool`; that is Phase F, after G0.
* Does not move the object store, choose a backend, or touch Phase 3 key/import work.
* Does not require root on the host (`sudo` needs a password; everything goes through a pod).

---

## 13. Follow-ups raised by this work (each separable, none blocking G0)

1. **`D:` has 52 GiB of headroom for a VHDX that backs every PVC on the node.** This is a
   node-level outage risk, not a hear risk, and it invalidates the import plan's 320 G reservation.
2. **`raw/` archives are written non-atomically** (`tools/hear_drain.py:1047-1053`) while every other
   writer in the lane uses `tmp` + `os.replace`. One-line-ish fix; removes a torn-file class.
3. **`hear-pool` PV is `reclaimPolicy: Delete`.** One `kubectl delete pvc` destroys the evidence.
   `local-path-retain` exists and is unused (`docs/migration-risk-register.md:55` already says this
   about R7's PVC). Moving requires a recreate, so it is its own task.
4. **`k3s-manifest-backup` writes every cluster Secret in cleartext** to `/var/backups/k3s` on an
   unencrypted disk, 15 generations deep.
5. **`*-check` jobs mount `/pool` read-write** although they only verify — the same `readOnly: true`
   this plan uses on the backup job applies to them.
6. **No off-host copy.** Two SSDs in one chassis survive a disk failure, not a fire, theft or a
   ransomware event that reaches the Windows user account. An encrypted archive is already safe to
   ship; a NAS or off-site target is the natural next step (`/mnt/syno-*` exist as empty directories,
   suggesting a NAS once was or was intended to be mounted).
7. **Models have no digest manifest in-pool**; the backup records one, which partially closes the
   gap Phase 3's key design also names.

## 14. Open questions

1. **Level-0 day/hour** — `2 2 * * 0` assumes the node is up on Sunday at 02:02 local. WSL2 is not a
   server: if the desktop sleeps or Windows reboots, a missed weekly is a real gap. Should
   `startingDeadlineSeconds` + a catch-up-on-boot path be added? (Recommended: yes,
   `startingDeadlineSeconds: 7200` and a check-job escalation at 9 days.)
2. **Passphrase owner** — who holds the offline copy, and where is it physically? Unanswerable from
   inside the cluster; must be named by a person before G0.9 can pass.
3. **`F:` vs `C:`** — `F:` (334 G free, disk 2) is recommended over `C:` (248 G free, disk 1, OS
   volume). Is `F:` used by anything else that could reclaim the space? The stale `.stfolder` says
   its history is not fully known here.
4. **sqlite mount shape** — §5.4 option (a) (narrow `subPath` rw) or (b) (`immutable=1` read-only);
   (a) is recommended and must be confirmed against the actual `hear-annotate` write pattern.
5. **Retention vs erasure** — how fast must an erasure request reach backups? §8 assumes
   "on generation expiry"; `docs/data-governance.md:120` may require faster for a legal request.

---

## 15. What is implemented, and what it deliberately still refuses to do

Added 2026-09-15: `tools/hear_pool_backup.py`, `deploy/k8s/hear-pool-backup.yaml`,
`deploy/k8s/hear-pool-backup-code.yaml` (generated) and `tests/test_pool_backup.py`.

**This is scaffolding, and no backup of the live pool exists yet.** §14.2 (who holds the offline
passphrase copy) and §14.3 (`F:` ownership) are unanswered, and a scheduled job that writes
archives nobody can prove is decryptable would be worse than the gap it closes: it would look
like a backup. So every path is fail-closed by construction rather than by operator discipline.

| Guard | Where | Exercised by |
| --- | --- | --- |
| Dry-run is the default; `--execute` is required before any byte is written | `run_backup`, `run_restore` | G0.2b |
| No passphrase is created, derived, defaulted, logged or stored — only a path handed in from outside, streamed to `gpg` on a pipe fd | `require_key`, `encrypt`, `redact` | G0.9, G0.9b |
| A `--pool` that resolves to `/pool` is refused before anything is read | `refuse_live_pool` | G0.12 |
| Sentinel-before-write: `.hear-backup-target` must exist and its uuid must match `--sentinel-uuid` | `read_sentinel` | G0.1a, G0.1b |
| Target must not share a device with the source (non-synthetic sentinels) | `refuse_same_device` | §2.2 |
| Plaintext archives are reachable only on a sentinel marked `synthetic`, for fixtures | `require_key` | G0.9 |
| Allowlisted roots, `pylib*`/`*.tmp` excluded, live WAL triple never read as a file | `select`, `snapshot_sqlite` | G0.2 |
| Frozen-prefix reads; `unstable`/`vanished` recorded, never fatal, never torn | `build_payload` | G0.8, G0.11c |
| Canonical JSONL manifest, digest recorded in the receipt | `manifest_bytes` | G0.6 |
| Cleartext receipt carries counts and digests, never a path or a coordinate | `_receipt_safe` | G0.9 |
| Whole generation published by `os.replace`; failure removes the staging directory and appends no index row | `run_backup` | G0.11d |
| Restore requires `--i-understand`, an empty target, and refuses `/pool` or the source tree | `refuse_unsafe_restore_target` | G0.3 |
| Restore drill mounts no `hear-pool` volume; destination is a fresh `local-path-retain` claim | manifest | G0.3b |
| Every CronJob ships `suspend: true`, `readOnly: true` on the pool volume *and* mount, `hostPath type: Directory` | manifest | G0.12 |
| No passphrase value in any shipped manifest; no `secretKeyRef` in this namespace | manifest | G0.12b |

`tests/test_pool_backup.py` runs the whole ladder on synthetic fixtures built in `tmp_path`: a
miniature pool with each class the plan treats differently, a live-WAL SQLite database with an
open writer, a torn JSONL tail, multi-member gzip, a pruned clip and a flipped archive byte.

### 15.1 Two deviations from §4, both forced

1. **Namespace `dama`, not `backups`.** A PVC is only mountable from its own namespace, and
   `hear-pool` is in `dama`; the job the plan describes could not have read the volume it exists
   to back up. Isolation comes from the restore job mounting no pool volume, not the namespace.
2. **SQLite is staged, then snapshotted.** §5.4 offers a narrow read-write `subPath` or
   `immutable=1`. Neither is used: `sqlite3.connect("file:…?mode=ro")` *creates* `-shm` next to
   the source, which both writes to a pool this job promises not to write to and fails outright
   on a read-only mount. The triple is copied into the job's own `emptyDir`, the copy is opened
   read-write, and the backup API produces the archived artifact from it. A torn copy is caught
   by `PRAGMA integrity_check` and fails the run.

### 15.2 Exactly what is needed before `--execute` may run against the live pool

Until all four are recorded in this document, the CronJobs stay suspended and G0 stays open:

1. **A named passphrase holder and a physical location for the offline copy** (§14.2). Without
   this, G0.9 cannot pass by definition, and an encrypted archive is a data-loss event in waiting.
2. **A decision that `F:` is owned by this system**, with the stale Syncthing `.stfolder`
   re-checked on the day of the first run (§14.3, G0.1c), and the sentinel uuid written by hand
   into `F:\hear-backup\.hear-backup-target` and into `hear-pool-backup-config`.
3. **A custody delivery path for `/custody/passphrase`** that is not a plain `Secret` in `dama`,
   because `k3s-manifest-backup` dumps every Secret in cleartext nightly (§6, §13.4).
4. **The erasure-latency answer** (§14.5), because it decides whether generation expiry is a
   sufficient deletion mechanism or retention must be re-cut on request.

Retention/GFS enforcement (§8) and the `--stats` V5 semantic ladder (§9.3) are **not implemented**:
both act on generations that do not exist yet, and V5 needs the pool modules shipped alongside the
tool, which would make the one thing that must work when everything else is broken depend on the
rest of the tree. Both land with the unsuspend commit.
