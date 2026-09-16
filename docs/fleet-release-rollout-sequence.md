# Fleet release and rollout sequence

Dated **2026-09-15T18:45Z**. **Nothing in this document has been executed.** It creates, handles,
generates and transports **no token**, cuts and publishes **no release**, flashes and reboots
**no node**, and changes **nothing in the cluster**. Every number below was read, not assumed:
node `/status` and `/log` over HTTP GET, `gh release`/`gh api`/`gh run` reads, read-only `kubectl
get`/`logs`, and a fresh clone of `origin/main` at `a7060fb` (`v0.1.6-9-ga7060fb`).

This is the **sequence**, not the gate. The gate is
[release-v0.1.6-readiness.md](release-v0.1.6-readiness.md) and it is still the authority on *what a
release must prove about itself*. This document is what happens in what order, with what
dependencies, once the one open operator decision — `HEAR_ADMIN_TOKEN` scope and custody
([0006](decisions/0006-admin-token-provisioning-policy.md)) — is made. It also records the parts of
that runbook the fleet has already overtaken, because a sequence written against a stale picture is
worse than none.

---

## 0. What is already true (measured 2026-09-15T18:41–18:45Z)

The readiness runbook was written as a *pre-cut* document. Since it was written, the tag was cut,
the release was published, and five of six nodes were flashed. Sequencing from here must start
from that, not from §7 of that runbook.

### 0.1 The release exists

| Fact | Evidence |
|---|---|
| `v0.1.6` is tagged at `ffd2054` | `git log -1 v0.1.6`; `ffd2054` is **not** an ancestor of today's `main` HEAD `a7060fb` — nine commits have landed since |
| `v0.1.6` is published | `gh release view v0.1.6` — created 17:27:28Z, published 17:38:30Z, author `github-actions[bot]` |
| All **three** variants shipped | `hear_node-xiao-s3-pps`, `hear_node-esp32s3-i2s-gps`, `hear_node-esp32s3-i2s-gps-qspi`, each with `.bin`, `-bootloader.bin`, `-partitions.bin`, `-merged.bin`, `.elf` |
| The 48 kHz claim is in the published manifest | downloaded `release-manifest.json`: all three variants carry `capture_profile.fs_acquisition_hz: 48000` (`fs_nominal_hz` 16000 × `decimation` 3) |
| The secret-free claim is in the published manifest | `build.image_class: "unprovisioned"`, `compiled_in_credentials: false`, `provisioning_required: ["node_id","wifi","admin_token","push_token"]` |
| The release carries **no SBOM and no attestation** | the asset list contains no `release-sbom.cdx.json` and no `release-provenance.intoto.jsonl`; `gh api repos/rjmendez/dama-hear/attestations/sha256:<app digest>` returns **404** |

The provenance work (#228, `a7060fb`) merged **after** the tag. `v0.1.6` is therefore a genuine,
self-verifying, secret-free release with **no signature and no bill of materials**. §2 sequences
that debt; it cannot be repaired in place.

### 0.2 The rollout already happened

Live `/status` at 18:41Z, unauthenticated reads only:

| Node | `fw` | `auth.push` | `auth.admin` | clock | mic probe | `raw.span_s` | `sys.heap_min` | `loop_max_ms` |
|---|---|---|---|---|---|---|---|---|
| `nyquist` | **v0.1.6** | `configured`, `src: nvs`, `last_code: 202` | `configured`, `src: nvs`, `ota_recovery_open: false` | LOCKED | `normal`, attempts 1, settle 30 ms | 80.0 | 56 200 | 546 |
| `mach` | **v0.1.6** | same, `202` | same | LOCKED | `normal`, 1, 30 ms | 80.0 | 56 108 | 84 |
| `kasami` | **v0.1.6** | same, `202` | same | HOLDOVER | `normal`, 1, 21 ms | 80.0 | 39 472 | 796 |
| `ageev` | **v0.1.6** | same, `202` | same | HOLDOVER | `normal`, 2, 50 ms | 80.0 | 63 228 | 827 |
| `gold` | **v0.1.6** (`-qspi`) | same, `202` | same | HOLDOVER | `normal`, 2, 50 ms | **0.0** | **19 312** | 966 |
| `rankine` | **v0.1.5** | **no `auth` block at all** | — | LOCKED | `normal` | 80.0 | 78 560 | 1 140 |

Five consequences, each of which changes the sequence:

1. **The admin token has already been provisioned to five nodes** (`auth.admin.configured: true,
   src: "nvs"` everywhere except `rankine`). ADR 0006's "no token exists yet, so neither option has
   a migration debt — this is the cheapest moment the decision will ever have" **is no longer
   true.** The decision is now a *migration* decision. See §1.
2. **`gold` was flashed with the quad asset** despite the runbook's §7 `NO-GO`. It landed
   correctly — `sys.psram_bus: "quad"`, `sys.psram_fault: false`, `psram: 1 807 356` free — and it
   still has **no raw ring**: `raw.span_s 0.0`, `/audio` returns `{"ring":false,...,"addressable":
   false}`. That is exactly the condition PR **#230** exists for, and #230 is **not in v0.1.6**.
   `gold` cannot get a ring without a **new tag**. See §7.
3. **`gold`'s `heap_min` is 19 312 B**, below the > 20 000 B acceptance line in
   [fleet-hardware-remediation-2026-09-15.md](fleet-hardware-remediation-2026-09-15.md) §R1, at
   ~53 min uptime. It is a standing watch item, not a pass.
4. **The mic settle fix is merged and working.** `cb0f4ce` ("bounded mic probe settle window") is
   an ancestor of `v0.1.6`. `ageev` and `gold` report `attempts: 2, settle_ms: 50` with
   `mic_state: normal` — the second read is the one that succeeded. The runbook's §7 warning about
   `ageev`'s "latched mic-selftest false positive … until the unmerged settle fix lands" and the
   "additional standing gates: the unmerged mic boot-probe settle fix" line are **both stale**;
   the false positive is closed on live hardware, with the diagnostic fields to prove *how* it was
   reached. See §8.
5. **`rankine` is the only node left on the old lineage** and it is the sole remaining cause of the
   split build. See §6.

### 0.3 The fleet-level state

| Fact | Evidence |
|---|---|
| `hear-drain-check` exits 1 on a split fleet | job `hear-drain-check-29824937` (24 min old): `⚠️FLEET IS SPLIT across 2 builds: v0.1.5, v0.1.6`, listing `nyquist v0.1.6`, `mach v0.1.6`, `rankine v0.1.5`. The `mach` half of the split is **closed**; `rankine` alone keeps it open |
| The drain path is healthy | `hear-drain-29824950` completed; `nyquist`/`mach` returning rows, `rankine`/`gold`/`kasami`/`ageev` `ok +0` with `cursor-v1`, 0 lost |
| Durable soak has a **new** T0 | `~/bridge-soak/T0-20260915T182622Z/report.md`, 18:26:22Z, **all 14 criteria pass**; outbox 9 244 records, `pending 0`, `failed 0`, `/state` 32.4 MB of a 64 MB budget; bridge pod 24 min old, `restarts=0`; this is the authoritative "T0 prime" recorded by `phase2-soak-day1-review` |
| Soak node coverage is **cumulative, not live** | `node_coverage()` reads `select device_id, count(*) from durable_records group by 1` over the whole ledger, so `all_nodes_covered … present 6` includes `rankine`'s 475 records from **before** it went `401`. `coverage_not_regressed` compares presence, not freshness, so a silent node keeps passing until 30-day retention prunes it. Treat six-node coverage as an **unearned pass** until §6 closes |
| No pool backup exists in the cluster | no `backup` CronJob in `dama`; PR #227 is open and every CronJob in it is `suspend: true`. The only backups on disk are the ConfigMap snapshots under `~/dama-hear-cm-backups-20260914/` |

---

## 1. D1 — the decision that still gates everything, restated honestly

ADR 0006 asks for one choice: **A, per-node admin tokens** (recommended there) or **B, one
fleet-wide token**. §0.2 changes the cost of both, and the ADR must be updated to say so before it
is accepted.

**What is already on the hardware:** five nodes hold an admin token in NVS. Today's tooling
(`gen_secrets.read_push_config()`, `flash.py:admin_token()`) can only resolve a **single bare
`HEAR_ADMIN_TOKEN`**, so whatever was provisioned is, by construction, **the same value on all five
nodes**. The fleet is in shape **B** — reached by omission, which is the exact failure mode ADR 0006
was written to stop.

| | **B — ratify fleet-wide** | **A — migrate to per-node** |
|---|---|---|
| Code changes | none | the five changes in ADR 0006 §"Changes required after the decision", as one PR, before any node is touched |
| Node work | none | **six USB visits** (5 provisioned + `rankine`), because there is no network path that writes NVS — `PROV` lines are read only from the USB CDC port |
| Blast radius accepted | one captured node (T3) = fleet-wide control-plane compromise; these are outdoor, physically reachable nodes | one node |
| Rotation cost | six USB visits | six USB visits (identical) |
| Sequence impact | §3 becomes a *custody and record-keeping* lane only | §3 becomes a re-enrollment lane and gates §4, §5 and §7 |
| Honest statement required | "the fleet shares one admin secret, deliberately, and one stolen node costs a full re-enrollment" | "the shared value provisioned on 2026-09-15 is retired and must be treated as burned" |

**Neither option is executable by this document.** The output of D1 is a written decision: ADR 0006
moves from `proposed` to `accepted`, with §"The choice that is not mine to make" replaced by the
chosen option, **plus a new paragraph recording that five nodes were provisioned before the
decision** and what that means for the option chosen.

**If A is chosen, the currently-provisioned value must be treated as a secret that is already
shared beyond its intended scope**, retired at re-enrollment rather than reused per node. It must
not be split, re-derived from, or used as one of the per-node values.

---

## 2. Dependency graph

```
                      D1  admin token scope (OPERATOR, blocking)
                       |
         +-------------+--------------------------------+
         |                                              |
   (A) tooling PR: gen_secrets/flash/enroll        (B) custody record only
         |    per ADR 0006 "changes required"           |
         +-----------------+----------------------------+
                           |
                           v
                  L3  NVS provisioning / re-enrollment order  <---- physical access
                           |
   #230 (gold ring tiers) -+-> L2  next release cut  ---> L1 provenance attestation
        [OPEN, CI green]   |        (v0.1.7)                    (closes the v0.1.6 debt)
                           |            |
                           |            v
                           |     L4 canary + mixed-version staging
                           |      nyquist -> mach -> kasami -> ageev -> gold
                           |            |
                           |            +--> L5 nyquist failback receipt (must be FIRST flash)
                           |            |
                           |            +--> L7 gold ring acceptance (needs #230 IN the tag)
                           v
                  L6 rankine USB recovery  <---- PHYSICAL ACCESS (independent of D1 only
                           |                     for the data-backup half)
                           v
                  L10 telemetry / drain / 48 kHz acceptance
                           |
                           v
                  L11 durable soak: six-node coverage becomes honest
```

Edges that are **not** in the graph, deliberately:

* `gold`'s ring does **not** depend on D1 — it depends on #230 being in a **tag**. But *installing*
  any new tag on `gold` depends on D1, because `flash.py --release` gates on `auth.admin`.
* `rankine`'s **data backup** does not depend on D1. Its **re-enrollment** does.
* The durable soak does not depend on the release at all; the release depends on *not* disturbing
  the soak window. See §11.

---

## 3. L3 — NVS provisioning order and verification, without disclosing a token

Applies to re-enrollment under A, to `rankine` under either option, and to any future board.

**Order.** One node at a time, in the §4 order, and never in parallel: two `enroll.py` sessions
cannot be told apart in a `PROV` failure, and a half-written record reboots the node.

1. `rankine` first if and only if the operator is already at the node with USB (§6) — it is the only
   node that is *currently* unprovisioned, so it is the only one whose enrollment cannot make
   anything worse.
2. Otherwise `nyquist` → `mach` → `kasami` → `ageev` → `gold`, the same order as §4, so that a
   provisioning defect is discovered on the node with the most evidence behind it.

**Steps (not executed here).** Per ADR 0006 §Decision item 4, injection is USB enrollment only:

```sh
python3 firmware/hear_node/enroll.py <node> /dev/ttyACM0 --class <board-class> --no-flash
```

**Verification, entirely from unauthenticated reads** — none of these disclose, echo or confirm a
token value:

| Check | Field | Pass |
|---|---|---|
| Record loaded | `/status` `prov.loaded` | `true` |
| Push credential present | `auth.push.configured` / `.src` | `true` / `"nvs"` |
| Admin credential present | `auth.admin.configured` / `.src` | `true` / `"nvs"` |
| Recovery valve closed | `auth.admin.ota_recovery_open` | `false` |
| Backend accepts it | `auth.push.last_code` after one heartbeat | 2xx (the fleet reads `202` today) |
| The cache agrees | node's Redis key reappears with a live TTL | present |
| The ledger agrees | the node's `device_id` count in the bridge outbox increases **within the sample window** | increasing |

The last row is the one the current tooling does *not* give you: soak coverage is cumulative
(§0.3), so a node that stopped publishing still reads as covered. Compare counts between two
snapshots rather than trusting `all_nodes_covered`.

**Non-disclosure rules, unchanged and binding** (ADR 0006 §Threat model T2; readiness §5):

* `~/.hear_push` is mode `600`, operator machine only, never committed, never pasted anywhere —
  including into an agent transcript.
* Token values never appear on a command line. `flash.py` sends `X-Hear-Auth` as a **header**.
* `enroll.py` prints a **masked** summary; the `PROV` line stores fields hex-encoded with a CRC.
* `/status` reports `configured`, `src` and `ota_recovery_open` and never a value, length or hash.
  **That is the whole verification surface, and it is sufficient** — nothing in this sequence needs
  to read a token back.
* A `401` in `auth.push.last_code` is a **loud stop**, not a retry: it is the `rankine` signature.

---

## 4. L4 — canary and mixed-version staging

The staging order does not change, but its *content* does: every node except `rankine` is already
on `v0.1.6`, so this order now applies to the **next** tag (§5), not to `v0.1.6`.

| Order | Node | Why it is here | Gate before the next node is touched |
|---|---|---|---|
| 1 | `nyquist` | canary **and** the failback-receipt node (§5). LOCKED, full stack, most evidence | the §5 receipt is complete, and `/status` returns to `fw` = new tag, `auth.*.src: nvs`, `last_code` 2xx |
| 2 | `mach` | second LOCKED node; proves the canary was not node-specific | `hear-drain-check` build table shows `mach` on the new tag; no new `label_rejects` growth |
| 3 | `kasami` | HOLDOVER. ⚠️ its PSRAM bus mode has **still never been scanned** and it is absent from `NODE_PSRAM_MODES`, so it takes the class default. It currently holds `raw.span_s 80.0` on the octal image, which is *evidence it is octal* but not a scan | boot does not assert on PSRAM; `raw.span_s` stays 80.0 — **any drop is a stop**, and #230's tiers are explicitly designed so a node holding 80 s cannot lose it |
| 4 | `ageev` | HOLDOVER, healthy, mic probe now self-explaining | `mic_state: normal` with `attempts`/`settle_ms` reported; `raw.span_s` stays 80.0 |
| 5 | `gold` | quad `-qspi` asset only; the ring acceptance is §7 | `sys.psram_bus: quad`, `psram_fault: false`, and **`raw.want_s` > 0 with `raw.span_s` > 0** — which is only possible if #230 is in the tag |
| — | `rankine` | **excluded from OTA, always**, until §6 completes | n/a |

**Mixed-version rules while the staging runs:**

* A split fleet is **expected and tolerated** mid-rollout; `hear-drain-check` will exit 1 for the
  duration and that is not a new fault. What is *not* tolerated is finishing the rollout without
  recording which node is on which tag.
* `tools/fleet.py --require-one-build` treats an unreachable node as inconclusive rather than
  silently excluding it (`fix-fleet-check-ignores-unreachable-nodes`). Do not "fix" a red check by
  making a node unreachable.
* Arrivals from different builds are **not comparable**. Any TDoA or latency conclusion drawn
  across a mixed window is void; say so in the artifact rather than discarding the window.
* Never revert a provisioned node to a tokenless asset (`v0.1.2`–`v0.1.5`). That is how `rankine`
  was lost.

---

## 5. L5 — the deliberate `nyquist` failback receipt

**Still unexercised.** The per-image failback is implemented and unit-covered, and the v0.1.6
rollout did **not** take the receipt — `rankine` and `gold` both report `boot_try 0`, `healthy yes`,
`img_state 2`, i.e. images that marked themselves healthy normally. The uniformity gate stays open.

**It must be the first flash of the next tag, on `nyquist`, before any other node is touched.**
Taking it later means taking it on a fleet that has already committed to the image.

Procedure and expected proof are in readiness §8 and are not restated here. The additions this
sequence makes:

* **Precondition:** record `/ota` (`running`, partition address, `boot_try`, `healthy`,
  `img_state`), `/status` (`fw`, `auth`, `prov`) and the Redis TTL **before** the flash. `gold`
  currently runs `app1 @ 0x340000` and `rankine` runs `app0 @ 0x010000`; the partition a node
  reverts *to* is only meaningful against its own recorded start.
* **The receipt now also proves the token survives a partition flip.** Since §0.2, the credentials
  are in NVS, outside both OTA slots — `auth.push.last_code` returning 2xx *after* the revert is
  the second half of the receipt, and a `401` there is a stop for the whole fleet.
* **Do not intervene for ~6 minutes** (3 unhealthy boots × ≥90 s). Intervening early destroys the
  evidence and can strand the node.
* A receipt missing the `/log` `BOOT GUARD:` line or the partition-address change **is not a
  receipt**.

---

## 6. L6 — `rankine`: exclusion, data backup, USB recovery

`rankine` is `v0.1.5`, has **no `auth` block**, pushes `401`, and is `/update`- and `/reboot`-locked.
It is excluded from every OTA step above, in every branch of D1. Any OTA attempt is a no-op that
burns rollout attention.

**Recovery is physical and is owned by `recover-rankine-usb`.** The full procedure — data pull and
receipt, custody handoff, the USB session, acceptance, validation, no-go conditions and the soak
consequence — is [rankine-usb-recovery.md](rankine-usb-recovery.md). Sequence, dependency-ordered:

1. **Back the node's data up first, before anything is written.** `rankine` is one of the three
   SD-card nodes and the drain log shows it still serving files; `sd 80` in the build table is its
   current card state. A reflash that loses unpulled clips loses them permanently — the pool is the
   only other copy. Pull via the existing drain path, not by hand.
2. **Record the pre-recovery receipt**: `/status`, `/ota`, `/log` tail, and the last timestamp
   `rankine` appears in the bridge outbox (currently 475 records, all before it went `401` at
   ~14:25Z).
3. **Enroll over USB** (§3) — this is the *only* way credentials reach it, and under D1=A it is
   also its per-node token provisioning. `rankine` is the one node where A costs nothing extra: it
   has to be visited regardless.
4. **Install a credentialed image.** Which tag depends on §2: if the next tag is close, install it
   and let `rankine` join at the fleet version; if not, `v0.1.6` is installable and correct for a
   `xiao-s3-pps` node, and closes the split immediately.
5. **Verify** with the §3 table *plus* `/audio` returning a 48 000 Hz WAV header and the drain path
   reading rows again.

**What closes when `rankine` closes:** `hear-drain-check` returns to exit 0 (it is now the sole
cause of the split), the six-node soak coverage becomes *earned* rather than inherited from the
ledger's history, and the tokenless-asset incident is fully retired.

**What must not happen:** do not silence `hear-drain-check`, do not add `rankine` to an ignore
list, and do not claim uniformity while it is out. A deliberately excluded node is a *recorded*
exception, not an invisible one.

---

## 7. L7 — `gold`: quad variant and ring sizing

Current state, measured: quad image installed and correct (`psram_bus: quad`, `psram_fault: false`,
1 807 356 B free PSRAM), **ring absent** (`raw.span_s 0.0`, `/audio` `ring:false`,
`addressable:false`), `heap_min` 19 312 B, `i2s.measured_hz` 15 997.7 (the post-decimator scene
lane; acquisition is 48 kHz), GPS `fix 1` with 4–8 satellites and `utc HOLDOVER`, no SD card.

Dependency chain, strictly ordered:

1. **#230 merges** (`fix/praw-small-psram-tiers`, open, 17/17 CI green). It extends `PRAW_TIERS_S`
   to `{80,60,45,30,20,15,10}`, chosen at runtime from the largest free SPIRAM block, keeps the
   ≥30 s tiers byte-identical so no peer can lose span, and reserves `DET_RING_MIN` slots below
   30 s so a small part cannot buy `/audio` by pushing the detection ring onto the internal heap.
   It also adds `sys.psram_total` and `raw.want_s` to `/status` — neither is present on any node
   today, which is itself the proof that no live image carries the tiers.
2. **A new tag is cut** containing it (§2 and §9). `gold` cannot be fixed by re-flashing `v0.1.6`:
   the asset it needs does not exist yet.
3. **`gold` is flashed last** in the §4 order, with the `-qspi` asset only. `board_profiles.
   release_variant_refusal()` refuses the octal asset on it and vice versa; do not force either.
4. **Acceptance** (`gold-quad-flash-readiness` owns the go/no-go; the full non-executing package —
   release gate, provisioning handoff, ring and heap thresholds, `/audio` validation, rollback — is
   [gold-release-flash-acceptance.md](gold-release-flash-acceptance.md)):
   * `raw.want_s` > 0 and `raw.span_s` > 0 and stable — expected landing 10 s or 15 s on a 2 MiB
     part; `raw.want_s` names the tier, so **do not assume a number**;
   * `/audio` reports `ring: true` and `addressable: true` with a 48 000 Hz header;
   * `sys.psram_total` = 2 097 152;
   * `psram_fault: false`, boot log shows `quad bus (as built)` and **no** `psram FAULT`;
   * `sys.heap_min` > 20 000 B after ≥ 1 h uptime — **today it is 19 312 B at 53 min and that is a
     no-go as it stands**; the detection-ring reservation in #230 is what is expected to move it,
     and if it does not, stop rather than shipping a node that is one TLS handshake from a panic;
   * `sys.loop_max_ms` < 1 000 after ≥ 1 h (966 today — marginal, watch it).
5. **Only then** is a `gold` microphone conclusion possible at all (remediation §R5). Until
   `/audio` is addressable there is no PCM to judge, and the summary statistic currently says
   `mic_state: normal` after a 2-attempt / 50 ms settle.

`gold` also remains out of TDoA on the #102 capture-path offset, independently of all of the above.

---

## 8. L8 — the I2S/mic settle diagnostic change

**Closed, and the sequence should stop carrying it as a gate.** `cb0f4ce` replaced the single
latched 768-sample read with `mic_probe_settle()`: a bounded settle window that reads, classifies
and accepts the first healthy verdict, paying the window only when the microphone is still not
answering, and reporting `selftest.mic_stats.attempts` and `settle_ms` so "healthy on the first
read" and "healthy after the window" are distinguishable.

It is an ancestor of `v0.1.6` and the live evidence is unambiguous: `ageev` and `gold` report
`attempts: 2, settle_ms: 50` with `mic_state: normal`; `nyquist`/`mach` report `attempts: 1,
settle_ms: 30`; `kasami` reports `attempts: 1, settle_ms: 21`. The cold-start false positive that
drove the `ageev` mic-replacement plan is gone, and the reason it is gone is visible in the
telemetry rather than inferred.

Two consequences for this sequence:

* Readiness §7's `ageev` row and its "standing gates: the unmerged mic boot-probe settle fix" line
  are **stale** and should not be treated as open work. (They are left unedited here to keep this
  document isolated from PR #230, which edits the same section.)
* `attempts`/`settle_ms` are now **acceptance fields** in §4 and §10: a node that starts reporting
  a *rising* `attempts` or a `settle_ms` at the budget ceiling is degrading, even while
  `mic_state` still says `normal`. That is the signal the old firmware could not produce.

---

## 9. L1 + L2 — provenance debt, and the next release cut

### 9.1 The v0.1.6 provenance debt (L1)

`v0.1.6` has a manifest but no SBOM and no attestation (§0.1). The repository's own rules decide
what may be done about it:

* **The tag may not be re-cut.** `release_manifest.py` binds the attestation to the tag and
  readiness §9 states it directly: never re-cut the same tag.
* **`v0.1.6` is still installable, by design.** `verify_declared_sbom()` treats a manifest with no
  `sbom` block as a *pre-SBOM release and leaves it alone*, and the attestation check only runs when
  a bundle is supplied. The fleet is not stranded, and the installers are not lying to the
  operator — `flash.py --verify-signature` reports an attestation that is not there, and says so.
* **Revocation is not warranted.** `release_revocations.json` is for a release that fails
  verification or is mis-built. `v0.1.6` verifies against its own manifest; it merely predates the
  provenance assets. Revoking it would strand the five nodes running it with no benefit.
* **The debt is closed forward**, at the next tag, which will be the first release built by the
  post-#228 `release.yml` and therefore the first to publish `release-sbom.cdx.json` (CycloneDX
  1.6) and `release-provenance.intoto.jsonl` (keyless SLSA v1 via GitHub OIDC, **no repository
  secret, no signing key**), both covered by `SHA256SUMS`.
* **Record the gap in the v0.1.6 release notes** — an edit to the notes is not a re-cut and does
  not touch an asset. One sentence: this release predates the SBOM/attestation change; verify it
  with `release_manifest.py verify` against the published manifest; the next release is signed.

### 9.2 Pre-cut checks for the next tag (L2)

Unchanged from readiness §6 in substance; what follows is the delta this sequence adds.

| Check | Pass condition | Delta |
|---|---|---|
| Prerequisites are ancestors | `#186 c2ca757`, `#191 dc75e60`, `#196 3f27c10` — all three verified ancestors of today's HEAD | unchanged; **#212 and #213 are now both MERGED**, so readiness §1's "open work that is not a prerequisite" is stale and the "ship with or without #213" decision is **resolved by the merge** |
| #230 in the tag | `git merge-base --is-ancestor <#230 merge commit> HEAD` | **new, and it is what the tag is for** (§7) |
| Three variants build | `firmware` matrix green, including `[quad psram]` | unchanged; `gold` now *depends* on the qspi asset rather than merely being offered it |
| 48 kHz manifest claim | `fs_acquisition_hz == 48000` for all three variants, derived offline | unchanged |
| Secret-free assets | no `secrets.h` anywhere; manifest `image_class: unprovisioned`, `compiled_in_credentials: false`, `provisioning_required` all four | unchanged, and it held for v0.1.6 — verified in the **published** manifest |
| SBOM present and bound | `release-sbom.cdx.json` published, manifest `sbom` block matches its bytes, SBOM describes the same artifacts/toolchain | **new — this is the debt being closed** |
| Attestation present and covering | `release-provenance.intoto.jsonl` published; `release_manifest.py verify --attestation` (structure/subjects, **not** the signature) then `gh attestation verify --signer-workflow .../release.yml` (the cryptographic check) | **new**; a 404 from `gh api .../attestations/sha256:<digest>` is a fail |
| Coordinates | `coord-guard` + `tests/test_no_site_coordinates.py` | unchanged — a published asset set is public forever |
| Clean tree | `git status --porcelain` empty | unchanged; the manifest refuses a dirty source commit at install time |
| Offline verify of the **published** release | `release_manifest.py verify --dist dist --tag <tag> --source-root .` before any node is touched | unchanged |

No SLSA *level* may be claimed, and the build is not claimed to be bit-reproducible.

---

## 10. L10 — telemetry, drain and 48 kHz acceptance

Per node, after any flash, and as the fleet-level exit criteria:

| Layer | Check | Pass |
|---|---|---|
| Node | `/status` `fw` | the intended tag, on the intended variant |
| Node | `auth.push.last_code` | 2xx within one heartbeat cycle (`202` today) |
| Node | `auth.*.src` | `"nvs"` on every node, `ota_recovery_open: false` |
| Node | `/audio` WAV header | **48 000 Hz** (`i2s.nominal_hz: 16000` is the post-decimator scene lane and is **not** the acquisition rate) |
| Node | `raw.span_s` / `raw.want_s` | unchanged or better per §4; 80.0 on the octal nodes, > 0 on `gold` after §7 |
| Node | `selftest.mic_stats` | `mic_state: normal`, `attempts` not rising across boots, `settle_ms` below the budget ceiling |
| Node | clock | `time.valid: true`; LOCKED or HOLDOVER with `discontinuity_flags` explained (`16` = the HOLDOVER flag the three I2S/GPS nodes carry today) |
| Cache | Redis node key | present with a live TTL |
| Ledger | bridge outbox `device_id` count | **increasing between two snapshots**, not merely present (§0.3) |
| Fleet | `hear-drain-check` | exit 0 — achievable only after §6 |
| Fleet | `hear-drain` | rows advancing on the SD nodes, `cursor-v1` reporting `0 lost` on the ringed nodes |
| Pool | scene/sketch geometry | `20x4@62.5-7812.5` unchanged; any change is a contract event, not a rollout detail |

---

## 11. L11 — impact on the durable soak

The Phase 2 soak is a **14-day continuous-evidence** clock with an authoritative T0 prime at
**2026-09-15T18:26:22Z**, all 14 criteria passing. The rollout interacts with it in four ways, and
all four are sequencing constraints rather than blockers:

1. **A flash is a gap.** Each node reboot stops its heartbeats for the flash window. `ledger_fresh`
   (newest record within 120 s) is a *fleet-level* criterion and survives one node rebooting, but
   `record_growth` at T+24h grades total volume against ~52 000 records/day ±20 %. Five sequential
   flashes inside one 24 h window is roughly an hour of missing node-minutes — inside tolerance,
   but only if it is recorded. **Record every flash window in the milestone notes** so a growth
   dip has an explanation attached rather than an investigation.
2. **Do not flash inside a milestone sample.** Take the T+24h, T+7d and T+14d samples *before* any
   flash that day, never during. `rollout_settled` and `pod_restarts_zero` grade the bridge, not
   the nodes, so a node flash cannot fail them — but a sample taken mid-flash under-counts, and the
   baseline comparison is the whole point.
3. **Six-node coverage is currently unearned** (§0.3): `rankine` passes on history. `phase2-soak-
   day1-review` already records a **five-node coverage expectation while `rankine` is USB-locked**
   — keep it that way, and only claim six when §6 has closed and `rankine`'s count is observed
   *increasing*.
4. **Do not `kubectl apply deploy/k8s/hear-mqtt-bridge.yaml`** during the soak; the in-repo file is
   the mTLS/8883 variant and does not describe the live plaintext Deployment.
   `plaintext_mqtt_preserved` fails loudly if it happens, and the TLS cutover is a separate
   sequenced workstream. The same applies to PR #233 (`hear-mqtt-bridge` ConfigMap restart): it is
   a bridge change, and landing it mid-soak restarts the pod and resets `pod_restarts_zero` and the
   window. Sequence it either before a T0 re-baseline or after T+14d — not between milestones.

---

## 12. Data backups and preservation

| Data | Where it lives | State today | Rule for this sequence |
|---|---|---|---|
| Node SD clips/records | on-node card (`nyquist`, `mach`, `rankine`) | `rankine` `sd 80`, pulling; `gold`/`kasami`/`ageev` are ring-only (`sd: false`) | **Pull before a USB reflash** (§6). An OTA preserves the card; a USB erase does not |
| Ring audio | node PSRAM | volatile by definition | never a backup target; a reboot loses it and that is expected |
| Bridge durable outbox | `hear-mqtt-bridge-state` PVC, 5 Gi, 13.4 MB / 32.4 MB of `/state` | Bound, `pending 0`, `failed 0` | read-only during the soak; the tool opens it `mode=ro` with `query_only=ON` and cannot prune |
| Heartbeat receiver ledger | `hear-heartbeat-state` PVC, 5 Gi | Bound, `pending_records 0` | same |
| Pool corpus `/pool/corpus` | `hear-pool` PVC, 5 Gi | 111 395 sketches, 1 720 535 scene rows, ~201 MB | **no backup mechanism is live.** PR #227 is open and every CronJob in it is `suspend: true`; `phase3-pool-backup-g0-execution` is blocked on passphrase custody. Do not treat the pool as backed up |
| Live ConfigMaps | cluster | snapshots in `~/dama-hear-cm-backups-20260914/` | take a fresh snapshot before any ConfigMap-touching PR (#233, #227) lands |
| Annotations SQLite | `hear-annotate` | backed up via the SQLite backup API in the `rollout-annotate-hardening` procedure | unchanged; not on this sequence's path |

**The honest statement:** the corpus is the only copy of the fleet's history and it is not backed
up. That does not block the rollout — a firmware rollout does not write the pool — but it must not
be described as "safe to proceed because we can restore".

---

## 13. Stop and rollback gates

| Trigger | Gate | Action |
|---|---|---|
| `auth.push.last_code` is `401` after any flash | **fleet stop** | treat as fleet-wide until disproved; this is the `rankine` signature |
| `flash.py --release` refuses pre-write | per-node stop | correct behaviour; provision per §3. **Never** `--allow-admin-lockout` during a rollout |
| Node unreachable after flash | wait ~6 min | let 3 unhealthy boots run; then read `/ota` and `/log`. Intervening early destroys the receipt and can strand the node |
| Still unreachable after the failback window | per-node stop | it faulted before `setup()` returned; USB recovery, like `rankine` |
| §5 receipt incomplete (no `BOOT GUARD:` line, or no partition-address change) | **fleet stop** | the failback is unproven; do not roll further |
| PSRAM assert at boot (esp. `kasami`) | per-node stop | scan the board, record it in `NODE_PSRAM_MODES` with evidence. Do not force the other variant |
| `raw.span_s` drops on a node that held 80 s | **fleet stop** | #230's tiers are designed to make this impossible; if it happens the premise is wrong |
| `gold` `heap_min` ≤ 20 000 B after ≥ 1 h | `gold` no-go | ship the asset, do not accept the node |
| `hear-drain-check` still exit 1 after `rankine` returns | **fleet stop** | the split was not the only cause; re-diagnose |
| Published manifest fails offline `verify` | do not flash | record in `release_revocations.json` via a merged PR, fix, re-cut a **new** tag |
| `gh attestation verify` fails or misses an asset (next tag onward) | do not flash | treat as compromised until disproved; revoke as above |
| Soak criterion regresses at a milestone | pause the rollout, not the soak | the soak is the longer clock; a paused rollout costs nothing |
| Rollout abandoned mid-way | allowed | record which node is on which tag. **Never** revert a provisioned node to a tokenless asset |

---

## 14. The sequence

Both D1 branches, written out. **None of this is executed by this document.**

**Phase 0 — decide and record (no hardware).**
0.1 Operator chooses A or B (§1). 0.2 ADR 0006 moves to `accepted`, amended to record that five
nodes were provisioned on 2026-09-15 before the decision. 0.3 Custody entries are created in the
password manager (`hear-admin/<node-id>` or `hear-admin/fleet`). 0.4 The v0.1.6 release notes gain
the provenance-gap sentence (§9.1).

**Phase 1 — software, no nodes.**
1.1 (A only) the ADR 0006 tooling PR: `gen_secrets.read_push_config()` + `admin_token_for()`,
`flash.py:admin_token(node)`, `enroll.py` refusing a tokenless enrollment without a spelled-out
opt-out, `gen_secrets` `__main__`, and `tests/test_admin_token_scope.py`. 1.2 #230 merges. 1.3 The
next tag is cut and published, with SBOM and attestation (§9.2), and verified offline **and**
cryptographically before any node is touched.

**Phase 2 — `rankine`, physical, parallel to Phase 1 from step 2.1.**
2.1 Pull `rankine`'s SD data through the drain path. 2.2 Record the pre-recovery receipt. 2.3 USB
enroll (§3) — under A this is its per-node provisioning. 2.4 Install a credentialed image. 2.5
Verify per §3 and §10. 2.6 Confirm `hear-drain-check` returns to exit 0.

**Phase 3 — (A only) re-enrollment, physical.**
3.1 Six USB visits in §4 order, verifying each with §3 before moving on. 3.2 The pre-decision shared
value is retired, not reused.

**Phase 4 — staged rollout of the new tag.**
4.1 `nyquist`, **taking the §5 failback receipt as part of the flash**. 4.2 `mach`. 4.3 `kasami`
(PSRAM watch). 4.4 `ageev`. 4.5 `gold` with the `-qspi` asset, then §7 acceptance. Each step
satisfies §3 and §10 before the next begins; any §13 trigger stops it.

**Phase 5 — close.**
5.1 Record the per-node tag table. 5.2 Confirm six-node coverage is *earned* (counts increasing,
not inherited). 5.3 Fold the flash windows into the soak milestone notes. 5.4 Re-open
`gold-quad-flash-readiness` and `recover-rankine-usb` only if their acceptance did not pass.

---

## 15. Open operator decisions

1. **D1 — admin token scope and custody** (§1). Blocking. Now a *migration* decision, not a
   greenfield one.
2. **When the next tag is cut** (§9.2) — it is what `gold` is waiting for, and it is also what
   closes the provenance debt. Cutting it before Phase 3 (under A) means `gold` gets the ring at a
   flash it will need anyway; cutting it after means one fewer node visit.
3. **Whether `rankine` receives `v0.1.6` or the next tag** (§6.4). `v0.1.6` closes the split sooner;
   the next tag avoids flashing it twice.
4. **When PR #233 lands** relative to the soak milestones (§11.4). It restarts the bridge pod.
5. **Pool backup custody** (`phase3-pool-backup-g0-execution`) — not on this path, but §12's honest
   statement stands until it is resolved.

---

## 16. Provenance of this document

Written in a fresh clone of `rjmendez/dama-hear` at `origin/main` `a7060fb` (`v0.1.6-9-ga7060fb`).
Evidence collected 2026-09-15T18:41–18:45Z: unauthenticated HTTP `GET /status`, `/ota`, `/log`,
`/audio` on all six nodes; `gh release view/download` of the published `v0.1.6` metadata assets
(`release-manifest.json`, `build-info.json`, `SHA256SUMS`, `release-manifest.schema.json`);
`gh api .../attestations/...`; `gh run list --workflow release.yml`; `gh pr view` for #212, #213,
#227, #230, #232, #233; read-only `kubectl get pods/cronjob/pvc` and `kubectl logs` in `dama`; and
the existing soak reports under `~/bridge-soak/`.

**Nothing was created, flashed, published, applied, restarted or written to a node or the cluster.**
No token was created, requested, read back, printed or transported; the only credential-adjacent
fields consulted are the boolean `configured`/`src`/`ota_recovery_open` reports that `/status`
publishes to anyone on the LAN by design.
