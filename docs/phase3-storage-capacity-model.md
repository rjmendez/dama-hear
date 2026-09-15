# Phase 3 storage capacity model (single-node profile)

**Date:** 2026-09-15. **Scope:** a non-provisioning capacity model for the Phase 3 object-store
import and the `dama/hear-pool` backup that gates it.

**Nothing was provisioned, mounted, copied or deleted to produce this document.** Every number
below comes from read-only inspection: `df`, a directory listing of the Windows volume, and
read-only `kubectl exec` (`du`, `find`, `wc`, and a read-only Python scan of `corpus/ledger.jsonl`
and `corpus/clips/index.jsonl`) inside a pod that already mounts `/pool`. No PVC was created, no
quota was applied, no storage class was changed, no archive was written, no byte of `/pool` was
copied out, and no retention was executed.

This document answers the three questions
[object-store-backend.md](object-store-backend.md) left open — where the shadow store lives, how
large the reservation really is, and how the 320 GiB G5 reservation is reconciled with the 200 GiB
backup budget — and it answers them **conditionally**. Section 12 lists exactly which measurements
must be validated before any number here is treated as a commitment.

---

## 1. The answer in one page

| Decision | Value | Conditional on |
| --- | --- | --- |
| Backups own `F:` **exclusively** | **140 GiB** budget, warn 98 GiB (70 %), refuse 126 GiB (90 %) | §5, §9 |
| Restore/drill scratch on `F:` | **40 GiB**, separate accounting from the budget | §8 |
| Shadow object store lives on `/` (ext4, `local-path-retain` PVC `hear-objectstore`) | **24 GiB** reservation, warn 16.8 GiB, stop 21.6 GiB | §6, §7 |
| Import staging/work dir (own PVC, never `/pool`, never the store) | **4 GiB** | §6.4 |
| G5 (import plan §10.3) | **320 GiB → 24 GiB**, and re-scoped from "the store" to "a bounded shadow import" | §7.3 |
| Backup budget (`pool-backup-restore.md` §8) | **200 GiB → 140 GiB** | §5.4 |
| Total committed on `F:` | 180 GiB of 333.3 GiB free | §2 |
| Total committed on `/` | 28 GiB of **51.4 GiB of real host headroom** | §2 |

**The single most important statement in this document:** with `corpus/raw/` retained forever and
growing at the measured 1.1–2.4 GB/day, *no* reservation on either device is sustainable. A backup
chain over an uncapped source reaches 379 GiB in 90 days (§5.2) and exhausts `F:` outright; a
shadow store that tracks an uncapped source exhausts the host's 51.4 GiB of real headroom in about
three weeks (§7.2). Source-side dedupe plus a `raw/` retention policy are therefore **preconditions
of the capacity plan, not a Phase F cleanup**. The numbers above are sized for the post-dedupe,
retained world and are explicitly stop-gated (§9) so that entering it early fails closed instead of
filling a disk.

---

## 2. Measured substrate (2026-09-15 18:45 UTC, read-only)

| Fact | Value | How observed |
| --- | --- | --- |
| `/` (`/dev/sdc`, ext4) | 1007 GiB total, 283.4 GB (263.9 GiB) apparent free | `df -B1` |
| `/` is a sparse VHDX | `D:\WSL\Ubuntu\rootfs\ext4.vhdx`, **944.29 GB (879.5 GiB) already allocated** | directory listing of `D:` |
| `D:` (Windows disk 0) | 999.67 GB, **55.16 GB (51.4 GiB) free** | `df -B1 /mnt/d` |
| `C:` (disk 1) | 270.3 GB (251.8 GiB) free | `df -B1 /mnt/c` |
| `F:` (disk 2) | 1000.19 GB, **357.9 GB (333.3 GiB) free** | `df -B1 /mnt/f` |
| `/` inodes | 5.34 M of 67.1 M used (8 %) | inventory §2 |
| `F:` throughput | 162 MB/s write, 203 MB/s read (`dd`, 512 MiB, `oflag=direct`, deleted in the same command) | backup plan §2.4 |

**The apparent 263.9 GiB of free space on `/` is not a capacity.** The VHDX is already 879.5 GiB
and can only grow by the 51.4 GiB that `D:` still has. Any growth on `/` — `/pool`, the shadow
store, container images, the k3s datastore, every other PVC — draws on that same 51.4 GiB. The
import plan's "backoff if node free space < 40 G" (§10.2) is measured against the wrong number and
is corrected in §9.

## 3. Measured source (same snapshot, read-only)

| Class | On-disk | Files / rows | Note |
| --- | --- | --- | --- |
| `/pool` total | 15 GiB | — | includes 4.7 G of vendored `pylib*`, never in scope |
| `corpus/` | 9825 MiB | — | in scope |
| `corpus/raw/` | **8917 MiB** | 5626 files | authoritative, **no retention** |
| `corpus/tdoa/` | 327 MiB | — | derived |
| `corpus/scene/` | 192 MiB | — | authoritative, already gzipped |
| `corpus/clips/` | 152 MiB | index 15 MiB, tags ~130 MiB, **0 `.wav` files present** | see §3.2 |
| `corpus/scores/` | 145 MiB | — | derived |
| `corpus/records/` | 90 MiB | — | authoritative |
| `models/` | 492 MiB | ~40 files | reproducible, unpinned |
| `sketch_corpus/` | 21 MiB | — | derived |
| `corpus/ledger.jsonl` | — | **5588 rows** | ingest-time SHA-256 per source file |
| `corpus/clips/index.jsonl` | — | **20 057 rows** | durable clip record |

### 3.1 Dedupe, re-measured

A read-only replay of `ledger.jsonl` gives the post-dedupe footprint directly:

| Metric | 2026-09-15 18:45 UTC | Inventory (earlier the same day) |
| --- | --- | --- |
| Ledger rows | 5588 | 5359 |
| **Distinct SHA-256** | **2300** | 2176 |
| Total ingested bytes | 10.796 GB | — |
| **Duplicate bytes** | **6.377 GB (59.1 %)** | 5.92 GB |
| **Distinct bytes** | **4.419 GB** | ~4.4 G |
| Bytes by kind | `dets.csv` 5.33 GB, `mqtt.jsonl` 2.74 GB, `scene.csv` 2.71 GB, `detections.json` 0.007 GB | — |

The duplicate fraction is **stable at ~59 %** across two independent measurements taken hours
apart, which is what makes it safe to use as a planning constant: **content-addressed import stores
0.41 × the bytes the file layer stores.** The cause is structural (the drain re-archives whole
`dets.csv`/`scene-*.csv` tails every 15 minutes), so the ratio holds as long as the drain behaves
this way.

### 3.2 An observation that changes the clip term (diagnosed after the fact — see §3.2.1)

`corpus/clips/**` currently contains **zero `.wav` files**. `index.jsonl` records 3932 `stored`
rows, 1966 of which carry `audio_pruned_at`, plus 1961 `deferred_by_cap` and 6003
`evicted_before_fetch`. The earlier inventory measured 1823 WAVs / 847 MiB present under a 2 GiB
cap with **0 rows pruned**; hours later the cap has pruned and the class is empty on disk while
1961 fetches are being deferred by that same cap.

This lane does not diagnose it — the capacity consequence is what matters here:

* the audio term of every model below is **0 today**, and
* it is **+1.9 GB** the moment stored audio is present again (3932 rows × 480 044 B),

so both are carried as a named conditional, and no reservation is sized as if audio were
permanently absent. That a 2 GiB cap appeared to empty a 0.74 GiB store while deferring new fetches
was recorded here as a correctness question for the clip lane; §3.2.1 answers it.

### 3.2.1 The answer: an operator privacy prune, not the 2 GiB cap

A read-only follow-up (fresh clone, live cluster read only, replay of `clips/index.jsonl` and
`corpus/heartbeat.json`) found the clip audio was deleted deliberately, on operator instruction not
to retain voice audio, and the clip lane was then switched off. **The 2 GiB cap never bound.**

* All 1966 `audio_pruned_at` values are one identical timestamp, `2026-09-15T18:27:15.225627Z` —
  a single `prune()` call, not a cap grinding the class down.
* The live `hear-drain` CronJob was patched out of band to `--clip-max-per-node 0` and
  `--clip-store-max-b 0`. `clips.prune()` with `max_bytes = 0` deletes every WAV by construction;
  `--clip-max-per-node 0` then skips `drain_clips()` entirely (`tools/hear_drain.py`), so nothing
  refills the class and nothing prunes it now either.
* The heartbeat `clips_recent` ring shows a normal clip run at 18:20:47Z (clips fetched, 0
  deferred) and `"the clip lane is disabled (--clip-max-per-node 0)"` for every run from 18:26:08Z
  onward.
* The 1961 `deferred_by_cap` rows are **not rising and not related**: the index is append-only and
  never compacted, the newest deferral pre-dates the prune, and the reasons are `deadline` (1595)
  and `count` (366) — never `disk`. Deferral was binding on `rankine` for transport reasons well
  before this change.

Capacity consequence, corrected: the audio term is **0 for as long as the clip lane stays
disabled**, and the **+1.9 GB conditional applies only if audio retention is deliberately turned
back on**. It is a policy state, not a pending anomaly.

Two states now disagree and the disagreement is the live risk: the repo manifest
`deploy/k8s/hear-drain.yaml` still carries `--clip-max-per-node 96` and no `--clip-store-max-b`, and
the CronJob's `last-applied-configuration` carries `--clip-max-per-node 13`. A routine
`kubectl apply` of the manifest would silently resume retaining raw audio.

### 3.3 Growth

`corpus/raw/` bytes by file mtime day (read-only `os.stat` walk, no file opened):

| Day | Files | MB | Day | Files | MB |
| --- | ---: | ---: | --- | ---: | ---: |
| 2026-09-08 | 178 | 109 | 2026-09-12 | 238 | 403 |
| 2026-09-09 | 506 | 410 | 2026-09-13 | 636 | 1664 |
| 2026-09-10 | 903 | 806 | 2026-09-14 | 1430 | **3418** |
| 2026-09-11 | 957 | 821 | 2026-09-15 (18.75 h) | 778 | 1706 (≈ 2184/day) |

Two growth rates are carried through every model, because the series is too short and too
non-stationary to justify one:

| Scenario | Rate | Basis |
| --- | --- | --- |
| **G-low** | **1.09 GB/day** | 7-day mean |
| **G-high** | **2.42 GB/day** | last 3 days, extrapolating 09-15 |

Post-dedupe (§3.1) these become **0.45 GB/day** and **0.99 GB/day** of genuinely new bytes. The
difference between the file-layer rate and the content rate is the entire economic case for Phase 3.

---

## 4. The model

Capacity is modelled as seven terms, each with its own device, its own growth law, and its own stop
condition. Nothing is a single number, because the three consumers (source, backup, shadow) fail at
different times for different reasons.

```
T1 source pool        /  = S0 + r·t                      (uncapped today)
T2 backup chain       F: = Σ retained L0 + Σ retained L1  (function of S(t) and retention depth)
T3 shadow blobs       /  = 0.41·S0 + 0.41·r·t + models + segments
T4 shadow metadata    /  = objects × ~8 KiB              (pointers, metadata docs, fanout)
T5 staging/work       /  = bounded, ≤ 4 GiB, transient
T6 restore capacity   F: = 1 × expanded chain            (never on /, never on /pool)
T7 safety margin      both = 2× verify/repair + 10 % filesystem reserve
```

Compression ratios used for T2 are the sampled measurements from the backup plan §2.6 (zlib-1 over
the first ≤20 MiB of three files per class): `raw/*dets.csv` 0.367, `raw/*scene*.csv` 0.345,
`raw/*health.csv` 0.346, `records` 0.275, `scores` 0.077, `scene/*.jsonl.gz` 0.998 (store as-is),
WAV 0.563. Models are assumed 0.90 (mostly incompressible weights). **These are samples, not a
whole-corpus measurement** (§12.1).

---

## 5. T2 — backup capacity on `F:`

### 5.1 Level-0 today

| Term | On-disk | ×ratio | Compressed |
| --- | ---: | ---: | ---: |
| `raw/` | 8917 MiB | 0.367 | 3272 MiB |
| `scene/` | 192 MiB | 1.00 (stored as-is) | 192 MiB |
| `tdoa/` | 327 MiB | 0.30 | 98 MiB |
| `scores/` | 145 MiB | 0.077 | 11 MiB |
| `records/` | 90 MiB | 0.275 | 25 MiB |
| `clips/` (index + tags, no audio) | 152 MiB | 0.30 | 46 MiB |
| `models/` | 492 MiB | 0.90 | 443 MiB |
| `sketch_corpus/` | 21 MiB | 0.30 | 6 MiB |
| **Level-0 total** | **10.07 GiB** | | **≈ 4.0 GiB** |

This matches the backup plan's 4.2 GiB estimate, which included 0.5 GiB of clip audio that is
currently absent. Add **+1.1 GiB** compressed to every level-0 figure below if audio returns (§3.2).

**Encrypted-archive overhead is not a capacity term.** `gpg --symmetric --compress-algo none` over
an already-gzipped stream adds a header of a few hundred bytes per *generation*, not per file: for a
4 GiB archive that is < 0.00001 %. The real cost of encryption is elsewhere — ciphertext is
incompressible, so no downstream layer can recover space, and a per-generation archive cannot be
delta'd. Both are already assumed by this model.

### 5.2 The chain, over an uncapped source

GFS as proposed in `pool-backup-restore.md` §8 (28 × L1, 4 × weekly L0, 3 × monthly L0), with L1
sized as 7 days of new bodies plus ~0.3 GB per run of re-stored append-only streams:

| Horizon | G-low chain | G-high chain | G-high L0 alone |
| --- | ---: | ---: | ---: |
| 30 days | 67.5 GiB | 106.1 GiB | 28.8 GiB |
| 60 days | 123.4 GiB | 230.2 GiB | 53.6 GiB |
| 90 days | 190.4 GiB | **379.1 GiB** | 78.4 GiB |
| 180 days | 425.1 GiB | 900.2 GiB | 152.9 GiB |

**The proposed 200 GiB budget is breached between day 55 (G-high) and day 95 (G-low), and `F:`
itself is exhausted by day 90 under G-high.** The backup plan's own "8–10 weeks" estimate is
confirmed and, at the higher rate, optimistic.

### 5.3 The chain, in the world Phase 3 creates

With source-side dedupe (0.41×, §3.1) and a 90-day `raw/` retention, level-0 stops growing once the
retention window fills, and a leaner GFS (1 monthly, 2 weekly, 12 × L1 ⇒ RPO still 6 h over 3 days)
gives a steady state:

| Configuration | Steady-state L0 | Steady-state chain |
| --- | ---: | ---: |
| Uncapped, GFS(3,4,28), G-high | — (unbounded) | 379 GiB @ 90 d, 900 GiB @ 180 d |
| Dedupe only, GFS(3,4,28), G-high | — (unbounded) | 176 GiB @ 90 d, 389 GiB @ 180 d |
| Dedupe + 90-day retention, lean GFS(1,2,12), G-high | 78.4 GiB | **≈ 108 GiB** |
| Dedupe + 90-day retention, lean GFS(1,2,12), G-low | 34.4 GiB | **≈ 55 GiB** |

### 5.4 Recommended budget

**140 GiB hard cap on `F:`, warn at 98 GiB (70 %), refuse a new generation at 126 GiB (90 %).**

* It covers the recommended steady state (108 GiB, G-high) with 30 % headroom.
* It leaves 193 GiB of `F:` free, of which 40 GiB is claimed by restore scratch (§8) and the rest is
  deliberate slack on a device with no second copy.
* It is **below** the 200 GiB previously proposed, which is the point: 200 GiB sized a chain over an
  uncapped source, which §5.2 shows is not a plan but a countdown. A lower cap that fails closed
  earlier is safer than a higher cap that is reached while the source is still accelerating.
* Retained fail-closed rules from the backup plan are unchanged and are load-bearing here: refuse to
  start when `target_free_bytes < 2 × expected_archive_bytes`; never delete a generation another
  retained generation names as parent; never delete the newest L0 or the newest restore-tested
  generation.

---

## 6. T3–T5 — the shadow object store

### 6.1 Blob bytes

| Term | Bytes | Basis |
| --- | ---: | --- |
| `raw` blobs | **4.42 GB** | distinct ledger bytes (§3.1), 2300 blobs |
| Stream segments (`records`, `scene`, `scores`, `tdoa`, `tags`, `index`, `ledger`) | ≈ 0.95 GB | on-disk sizes, no dedupe assumed |
| Model files | 0.52 GB | 492 MiB, ~40 files |
| Clip audio | **0 today / +1.89 GB conditional** | §3.2 |
| **Total payload** | **≈ 5.9 GB today, 7.8 GB with audio** | |

### 6.2 Metadata, pointers and filesystem overhead

From the import plan §10.3 and the key design: ~4.4 k blobs, ~7.6 k pointers, one metadata document
per pointer. On ext4 with a 4 KiB block size, every small object costs a full block plus an inode,
and `blob_key`'s two-level hex fanout adds directories:

| Term | Count | Unit | Total |
| --- | ---: | ---: | ---: |
| Pointer docs | 7.6 k | 4 KiB block | 30 MiB |
| Metadata docs | 7.6 k | 4 KiB block | 30 MiB |
| Blob block rounding | 4.4 k | ~2 KiB mean waste | 9 MiB |
| Fanout directories | ~4.6 k | 4 KiB | 18 MiB |
| Run ledger (append-only JSONL, ~9 rows/object) | — | — | ~20 MiB |
| **Overhead total** | | | **≈ 110 MiB** |

Planning constant: **≈ 8 KiB of overhead per stored object**, dominated by block granularity, not by
document content. Inode pressure is negligible (15 k of 61.8 M free).

**Encryption overhead at the object layer is also negligible**: `hear/objectstore/crypto.py` appends
a 32-byte tag per sealed stream and derives the nonce rather than publishing it, so the ciphertext
term is 32 B × ~12 k objects ≈ 0.4 MiB, plus a wrapped-key reference inside each metadata document
(already counted in the 4 KiB block). Encryption's capacity effect is again indirect: sealed bodies
are incompressible, so a shadow store that is itself backed up contributes at ratio 1.0, not 0.367
(§8.2).

### 6.3 Shadow total

`5.9 GB payload + 0.12 GB overhead ≈ 6.0 GB today`, `≈ 8.0 GB` with audio present.

### 6.4 Staging and work dir

Staging is transient and already bounded by the import plan: work-dir footprint ≤ 2 GiB, ≤ 4
concurrent PUTs (≤ 2 above 100 MB), 1 MiB streaming chunks with an 8 MiB resident cap
(`hear/objectstore/streaming.py`). The largest single object is the 392 MB model. **Reserve 4 GiB
on its own PVC** — double the stated bound, because a staged object plus its verify read-back plus a
partially-published segment can coexist, and because `put_staged` must never spill into either
`/pool` or the store's own reservation.

---

## 7. Where the shadow store goes, and how big the reservation is

### 7.1 Device

`/` (ext4, PVC `hear-objectstore` on `local-path-retain`), per the backend audit's recommendation.
Three reasons are capacity reasons rather than preference:

1. **`F:` is committed to backups.** Putting the shadow on `F:` would put a copy of the evidence on
   the same device as the only backup of that evidence — the failure domain the backup exists to
   escape — and would consume the budget in §5.4.
2. **POSIX mode and ownership exist on ext4 and do not exist on `F:`** (drvfs/NTFS mounts
   `uid=1000,gid=1000`), and the importer/reader/custodian role split is expressed with directory
   modes.
3. **`C:` is not a candidate** without a new VHDX or a new mount, which is a provisioning act this
   lane may not perform. It is, however, the obvious answer to §13's "add a device" (251.8 GiB free).

### 7.2 The constraint that decides the size

The shadow store draws on `D:`'s **51.4 GiB**, shared with `/pool` itself:

| Consumer on `/` | Rate | 51.4 GiB is gone in |
| --- | --- | --- |
| `/pool` alone, G-high, uncapped | 2.42 GB/day | **≈ 21 days** |
| `/pool` + a shadow tracking it post-dedupe | 3.41 GB/day | **≈ 16 days** |
| `/pool` + shadow, G-low | 1.54 GB/day | ≈ 36 days |

A shadow store that continuously tracks an uncapped source is therefore **not fundable on this
host**, at any reservation. What *is* fundable is a **bounded shadow import**: a one-shot import of
a frozen manifest (import plan Phase A), verified, then either extended under a retention policy or
deleted (rollback = delete the shadow).

### 7.3 Recommended reservation, and the G5 reconciliation

**`hear-objectstore` PVC: 24 GiB, alert at 16.8 GiB (70 %), importer stops at 21.6 GiB (90 %) used —
and, before either, at the host gate in §9.2, which binds first on this machine.**

| Term | GiB |
| --- | ---: |
| Payload today (§6.3) | 5.5 |
| Clip audio, conditional (§3.2) | 1.8 |
| Overhead @ 8 KiB/object (§6.2) | 0.2 |
| Re-import / repair headroom (1 × payload incl. audio; a failed class is re-staged, not overwritten in place) | 7.3 |
| Growth allowance, ≈ 10 days post-dedupe @ G-high | 9.2 |
| **Reservation** | **24** |

G5 in the import plan reads "≥ 2 × post-dedupe plus 90 days of growth ≈ 320 G". That number is
**unsatisfiable**: 320 GiB does not exist on `/` (51.4 GiB real), and on `F:` it collides with the
backup budget (320 + 200 = 520 GiB > 333.3 GiB free). The reconciliation is not to shrink the number
and keep the meaning; it is to **change the meaning**:

| Was | Becomes |
| --- | --- |
| One reservation covering the store and 90 days of growth | Two reservations on two devices with two owners: backups 140 GiB on `F:`, shadow 24 GiB on `/` |
| Sized for continuous operation | Sized for a **bounded shadow import** — continuous operation is a separate decision that requires §13's precondition |
| 320 GiB | **24 GiB**, with the 90-day growth term replaced by an explicit stop condition (§9) |
| Alert at 70 % of the reservation | Unchanged, plus a *host* free-space gate that the reservation cannot express (§9.2) |

`local-path` does not enforce PVC requests — `/pool` reports the whole 1007 GiB filesystem and is
already ~3 × its declared 5 Gi. **A reservation on this cluster is an accounting statement, not an
enforcement mechanism.** It is therefore stated as a number the importer itself enforces (§9), not
as a quota the platform will apply.

---

## 8. T6 — failure and recovery capacity

Recovery needs space that no steady-state model accounts for, and it needs it at the worst moment.

| Recovery action | Space needed | Where it must land |
| --- | --- | --- |
| Restore a chain for a drill or an incident | 1 × expanded corpus (**10.1 GiB today**, 78 GiB of L0 at 90 d under G-high) | **`F:` restore scratch**, or PVC `backups/hear-pool-restore` on `local-path-retain` |
| Verify a restore (V2–V5) | +0 (streamed), but a second expanded copy if the operator diffs trees | `F:` |
| Re-import a failed shadow class | ≤ 1 × payload | inside the 24 GiB reservation (§7.3) |
| Roll back a shadow import | −(store), releases space | — |
| Rebuild derived classes from authoritative ones | ≈ 0.9 GB transient | `/` |

**Restore must not be the event that fills `D:`.** A 10 GiB restore fits in the VHDX's reusable
slack today; a 78 GiB restore does not fit in 51.4 GiB at all. So: **restore drills default to
`--restore-to` on `F:`**, and **40 GiB of `F:` is reserved for restore scratch outside the 140 GiB
budget**. The restore-isolation rules are unchanged — never into `/pool`, never a pod that mounts
`hear-pool`, refuse a non-empty target.

### 8.2 Backing up the shadow store

If the shadow store is itself backed up (it is a directory, so the same CronJob design covers it),
add its payload at **ratio 1.0**, not 0.367 — sealed bodies do not compress. That is +6–8 GiB per
level-0, i.e. **+42–56 GiB** on the lean chain in §5.3, which does not fit under the 140 GiB budget
alongside the source. Recommendation: **do not back up the shadow during the shadow phase.** It is
by definition a second copy of data whose authority is `/pool`, and `/pool` is what the budget
protects. Revisit only when the store becomes authoritative, at which point the source-side backup
term shrinks correspondingly.

---

## 9. Alerts and stop conditions

Thresholds are stated against the number that actually constrains the system — **physical free
space on the backing Windows volume** — because the filesystem-level number is fiction (§2).

### 9.1 Backup lane (`F:`)

| Condition | Threshold | Action |
| --- | --- | --- |
| Budget used | ≥ 98 GiB (70 % of 140) | warn; review retention depth |
| Budget used | ≥ 126 GiB (90 %) | refuse new generation after pruning; check job fails loud |
| `target_free_bytes` | < 2 × expected archive | refuse to start (already specified) |
| `F:` free | < 60 GiB | refuse; restore scratch is no longer fundable |
| Sentinel `/target/.hear-backup-target` | absent | refuse before writing a byte (already specified) |
| Level-0 size | > 1.5 × previous L0 | warn: growth regime changed, re-run this model |

### 9.2 Import lane (`/`)

| Condition | Threshold | Action |
| --- | --- | --- |
| `D:` physical free | **< 25 GiB** | importer refuses to start |
| `D:` physical free | **< 15 GiB** | importer stops mid-run, closes `partial`; page |
| Shadow store used | ≥ 16.8 GiB (70 % of 24) | warn |
| Shadow store used | ≥ 21.6 GiB (90 % of 24) | stop, close `partial`, do not stage further objects |
| Staging PVC used | ≥ 3 GiB of 4 | abort the current class |
| Node load1 > 8 | — | skip the run (unchanged) |
| Source read rate | > 40 MB/s sustained | throttle (unchanged) |
| Wall clock | > 10 min | hard stop, clean `partial` (unchanged) |

> **Correction to the import plan §10.2.** "Backoff if node free space < 40 G" reads `/`, which
> reports 263.9 GiB and will keep reporting a comfortable number right up until the VHDX cannot
> grow. It must read `D:` physical free, and 40 GiB is 78 % of everything that device has. The
> replacement is the 25 GiB / 15 GiB pair above.

### 9.3 Source lane (`/pool`) — the conditions that gate Phase 3 itself

| Condition | Threshold | Action |
| --- | --- | --- |
| `raw/` growth | > 3.5 GB/day for 2 consecutive days | capacity review; re-run this model before any import |
| `/pool` total | **> 20 GiB** | **block the Phase 3 import** until `raw/` retention exists |
| `D:` physical free | < 20 GiB | operational incident: the node, not just hear, is at risk |
| Clip class | `deferred_by_cap` rising **while the clip lane is enabled** and stored audio is 0 | investigate before sizing any audio term (§3.2.1); today the lane is off by policy |
| Duplicate fraction | drifts below 40 % | the 0.41 × planning constant is invalid; re-measure |

---

## 10. Schedule and throughput

Capacity and schedule interact here in one direction only: a run that overruns its window competes
with the 15-minute drain, and starving the drain loses node data that cannot be re-fetched.

| Lane | Window | Bytes | At measured throughput |
| --- | --- | --- | --- |
| Level-0 backup | Sun 02:02, `activeDeadlineSeconds: 1800` | read 10.1 GiB, write 4.0 GiB | read + SHA-256 ≈ 2–4 min, write ≈ 25 s at 162 MB/s ⇒ **≈ 3–5 min** |
| Level-1 backup | `32 3,9,15,21 * * *` | ≈ 0.3–0.6 GB | **≈ 1 min** |
| Import run | `2,32 * * * *`, ≤ 10 min hard stop | ≤ 40 MB/s × 600 s = **≤ 24 GB/run** theoretical | first full import ≈ 10.1 GiB read ⇒ **~2–3 runs**, resumable by ledger |
| Restore drill | quarterly | 1 × chain | decrypt+expand ≈ 5 min + verify 10–20 min, target ≤ 60 min |

**No network term exists.** Every path is local (PVC on the node, `hostPath` to a local SSD); there
is no egress, no bucket transfer and no bandwidth cost in this profile. If the store ever moves
off-host, the 4.4 GB post-dedupe payload plus 0.45–0.99 GB/day becomes the transfer budget, and this
section must be rewritten.

Level-0 at 90 days under G-high (78 GiB compressed, ~213 GiB read) no longer fits the 1800 s
deadline at 162 MB/s plus hashing. **The backup window is a second, independent reason `raw/` needs
retention** — and a stop condition of its own: if a level-0 exceeds 20 minutes, the schedule, not
just the budget, has to change.

---

## 11. Reservation summary

| Device | Free | Committed | Purpose | Remaining |
| --- | ---: | ---: | --- | ---: |
| `F:` (disk 2) | 333.3 GiB | **140 GiB** | backup budget (GFS chain) | |
| `F:` | | **40 GiB** | restore/drill scratch | 153.3 GiB unallocated |
| `/` on `D:` (disk 0) | **51.4 GiB real** | **24 GiB** | `hear-objectstore` shadow PVC | |
| `/` on `D:` | | **4 GiB** | import staging PVC | 23.4 GiB for `/pool` growth, images, every other PVC |
| `C:` (disk 1) | 251.8 GiB | 0 | not used; the candidate device if §13 is taken up | |

The 23.4 GiB left on `D:` after the shadow commitments is **≈ 10 days of `/pool` growth at G-high**.
That is the number to argue with, and it is why §9.3 blocks the import at 20 GiB of `/pool` rather
than waiting for a device to fill.

---

## 12. What must be validated before these numbers are commitments

Every recommendation above is conditional on measurements this lane could take read-only. The
following were **not** measured, and each one moves a number:

1. **Whole-corpus compression ratios.** §4's ratios are samples of ≤20 MiB from three files per
   class at zlib-1. A real level-0 dry run is the only way to know the archive size; a 10 % error on
   the `raw/` ratio is ±0.9 GiB per level-0 and ±7 GiB on the lean chain.
2. **The first real level-0.** Runtime, `files_unstable`, and actual `bytes_out` are unknown until
   G0 executes, and G0 is blocked on policy input (passphrase custody, `F:` ownership), not on code.
3. **The clip-class anomaly (§3.2).** Answered in §3.2.1: an operator privacy prune with the cap set
   to 0, followed by the clip lane being disabled. The audio term is 0 while that policy stands, and
   +1.9 GB only if audio retention is turned back on.
4. **Growth stationarity.** Eight days of data, spanning a fleet that was still being expanded, does
   not establish a rate. G-low and G-high differ by 2.2 × and by 189 GiB at the 90-day chain.
5. **Segment count and size.** The ~7.6 k pointer / ~4.4 k blob counts come from the import plan's
   estimate, not from a built manifest. The freeze manifest (import plan Phase A) is what turns the
   8 KiB/object constant into a measurement.
6. **Post-dedupe growth.** 0.41 × is measured on the *existing* corpus. Whether new bytes dedupe at
   the same rate depends on drain behaviour that may change with the cursor work.
7. **`local-path` enforcement.** Assumed absent (it is, today). If a quota mechanism is ever added,
   the stop conditions in §9.2 become redundant rather than load-bearing.
8. **Real `F:` write behaviour under a 4 GiB archive.** The 162 MB/s figure is a 512 MiB `dd` with
   `oflag=direct`; drvfs behaviour on a multi-GiB streamed write through a container is not the same
   experiment.

---

## 13. Decisions still owed to an operator

1. **Does `raw/` get a retention policy, and what is it?** 90 days is assumed throughout §5.3 because
   it is the only assumption that produces a steady state. This is a governance decision
   (`docs/data-governance.md`), not a storage one, and the capacity plan is downstream of it.
2. **Is the shadow import bounded or continuous?** This model funds a bounded import. Continuous
   shadow operation on this host requires either source-side retention landing first, or a device.
3. **Is a device added?** `C:` has 251.8 GiB free and no commitments. A new VHDX or mount there is
   the cheapest way to make every number in this document comfortable, and it is a provisioning act
   that was deliberately not performed here.
4. **Is the shadow store backed up?** §8.2 recommends not during the shadow phase. That is a
   durability trade, and it belongs to whoever owns R6.
5. **Clip audio retention (§3.2.1).** Audio retention is currently off by operator instruction,
   applied only to the live CronJob. Whether that decision is encoded in
   `deploy/k8s/hear-drain.yaml` — so an apply cannot revert it — is an operator decision, and the
   audio term of this model follows it.

---

## 14. Evidence

| Claim | Source |
| --- | --- |
| Device geometry, VHDX size, free space | `df -B1` on `/`, `/mnt/c`, `/mnt/d`, `/mnt/f`; listing of `D:\WSL\Ubuntu\rootfs\ext4.vhdx`, 2026-09-15 18:45 UTC |
| Pool class sizes, file counts | read-only `du -sm` / `find` via `kubectl exec` into the pod that already mounts `/pool` |
| Dedupe ratio, bytes by kind | read-only Python replay of `corpus/ledger.jsonl` (5588 rows) |
| Clip outcomes, prune counts | read-only Python replay of `corpus/clips/index.jsonl` (20 057 rows) |
| `raw/` growth by day | read-only `os.stat` walk of `corpus/raw/`; no file opened |
| Compression ratios, `F:` throughput, snapshot-primitive absence | [pool-backup-restore.md](pool-backup-restore.md) §2.4, §2.6 |
| Object and pointer counts, throttles, G5 as written | Phase 3 object-store import plan (§10.2, §10.3) |
| Backend choice, the 52 GB finding, G5/budget collision | [object-store-backend.md](object-store-backend.md) §0.1, §5.1 |
| Encryption overhead | `hear/objectstore/crypto.py` (`TAG_BYTES = 32`, derived nonce), `hear/objectstore/streaming.py` (1 MiB chunks, 8 MiB resident cap) |
| R6, retention/erasure coupling | [migration-risk-register.md](migration-risk-register.md) R6; `docs/data-governance.md` |

No coordinate, node position, node name or audio content appears in this document.
