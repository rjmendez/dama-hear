# `gold` release flash: provenance gate, acceptance and rollback — NOT EXECUTED

**Nothing in this document is executed by it.** No node is flashed, rebooted or reconfigured, no
tag is cut, no credential is read or written, and no cluster object is touched. It is the
acceptance package an operator follows *later*, by hand, when the decision to flash `gold` with a
tag containing the raw-ring tiers has actually been taken.

Scope: `gold` only. `nyquist`, `mach`, `kasami`, `ageev` and `rankine` are out of scope here and
keep the rules in [fleet-release-rollout-sequence.md](fleet-release-rollout-sequence.md) §4 and §6.

Related, and **not** restated here:
[release-provenance.md](release-provenance.md) (what a release publishes and how it verifies),
[ota-release-credentials.md](ota-release-credentials.md) and
[decisions/0006-admin-token-provisioning-policy.md](decisions/0006-admin-token-provisioning-policy.md)
(credential contract), [fleet-hardware-remediation-2026-09-15.md](fleet-hardware-remediation-2026-09-15.md)
§R1/§R4/§R5 (the three independent `gold` faults),
[fleet-release-rollout-sequence.md](fleet-release-rollout-sequence.md) §7 and §13 (where `gold`
sits in the sequence, and the fleet-level stop gates).

---

## 0. The state this package starts from

Measured `gold` state, from the rollout sequence §7 (2026-09-15T18:41–18:45Z) — **not re-measured
by this document**:

| Field | Value | Reading |
|---|---|---|
| `fw` | `v0.1.6` | pre-ring-fix. It is the tag the fleet is on |
| `sys.psram_bus` / `sys.psram_fault` | `quad` / `false` | **the quad variant is already installed and correct** |
| `psram` (free) | 1 807 356 B | a 2 MiB part, working |
| `raw.span_s` / `/audio` | 0 / `ring:false`, `addressable:false` | no ring, `/audio` is a 503 |
| `sys.heap_min` | 19 312 B at ~53 min uptime | **below the §5 floor as it stands** |
| `sys.loop_max_ms` | 966 | marginal against the §5 ceiling |
| GPS | `fix 1`, 4–8 sats, `utc HOLDOVER` | siting, tracked separately (`gold-no-gps-fix`) |
| SD | absent | there are no clips on this node; the ring is the only PCM path |

Two consequences that shape everything below.

1. **`gold` already runs the quad image.** The earlier "USB only, because the bootloader and
   partition table must change" rule in remediation §R1 was written when `gold` carried an *octal*
   image. That transition is done. The next flash changes the **app only**, so an OTA of the
   `-qspi` asset is sufficient and USB is the *recovery* path rather than the required path. Do not
   re-derive the §R1 conclusion from a stale premise.
2. **The thing `gold` is missing is code, not silicon.** `v0.1.6` predates PR #230, so its
   `PRAW_TIERS_S` stops at 30 s and no tier can fit 2 MiB. Re-flashing `v0.1.6` cannot fix this.
   A **new tag containing #230** is the only asset that changes the outcome.

---

## 1. Gate A — the release and provenance gate, before any asset reaches `gold`

`gold` may be flashed only from a tag that satisfies **all** of the following. This is the
post-#228 machinery described in [release-provenance.md](release-provenance.md); nothing here
changes it, and none of it is run by this document.

| # | Gate | How it is checked | Fail ⇒ |
|---|---|---|---|
| A1 | #230 is in the tag | `git merge-base --is-ancestor <#230 merge commit> <tag>` | **do not flash.** The asset cannot give `gold` a ring; the flash is a reboot for nothing |
| A2 | The tag is not revoked | `firmware/hear_node/release_revocations.json` does not list it. The installers check this before the first HTTP request | do not flash |
| A3 | Manifest verifies offline | `release_manifest.py verify --dist dist --tag <tag> --source-root .` | do not flash |
| A4 | SBOM published and bound | `release-sbom.cdx.json` present; `release_sbom.py verify --dist dist --tag <tag>`; the manifest `sbom` block matches its bytes | do not flash. A manifest that *declares* an SBOM which is absent is an installer refusal, not a warning |
| A5 | Attestation published and covering | `release-provenance.intoto.jsonl` present; `release_manifest.py verify --attestation` (structure and subject coverage, **offline, not the signature**) | do not flash |
| A6 | Signature verifies | `gh attestation verify <asset> --repo rjmendez/dama-hear --signer-workflow rjmendez/dama-hear/.github/workflows/release.yml --bundle release-provenance.intoto.jsonl` | **treat as compromised until disproved.** Do not flash; revoke per release-provenance §9 and re-cut a *new* tag |
| A7 | The asset is the right variant | the `-qspi` asset. `board_profiles.release_variant_refusal()` refuses the octal asset on `gold` and vice versa | do not flash, and **never force past it** |
| A8 | Assets are credential-free | manifest `image_class: unprovisioned`, `compiled_in_credentials: false`, `provisioning_required` | do not flash |
| A9 | The check actually ran | `--verify-signature` prints `verified`, never `signature NOT checked`, and `gh` missing is an error | do not flash from that machine |

**`--allow-unattested` is forbidden for `gold`.** The whole point of flashing `gold` at this tag is
that the tag is the first one with provenance; installing it unattested spends the reboot and
keeps the debt.

**On the `v0.1.6` provenance debt.** `v0.1.6` has a manifest and no SBOM/attestation, and per
rollout sequence §9.1 it is not re-cut and not revoked. That debt is closed *forward* by this tag.
It is not a reason to relax A4–A6, and it is not a reason to roll `gold` back to a pre-provenance
tag for convenience.

---

## 2. Gate B — credential-safe USB/NVS provisioning handoff

**No token value appears in this document, in any command line, in any log, in any evidence file,
or in any agent transcript.** The verification surface below is entirely unauthenticated reads
that report *presence*, never a value, a length or a hash. ADR 0006 §Threat model T2 and rollout
sequence §3 are binding and unchanged.

### B1 — what must already be true on `gold`, verified without a secret

| Check | Field (`GET /status`, unauthenticated) | Pass |
|---|---|---|
| NVS record loaded | `prov.loaded` | `true` |
| Push credential present | `auth.push.configured` / `auth.push.src` | `true` / `"nvs"` |
| Admin credential present | `auth.admin.configured` / `auth.admin.src` | `true` / `"nvs"` |
| Recovery valve closed | `auth.admin.ota_recovery_open` | `false` |
| The backend accepts it | `auth.push.last_code` after one heartbeat | 2xx (the fleet reads `202`) |

A `401` in `auth.push.last_code` is a **loud stop**, not a retry — it is the `rankine` signature
(rollout sequence §13). Stop the flash and treat it as fleet-wide until disproved.

### B2 — if any B1 row fails, the handoff is USB enrollment, at the node

Per ADR 0006 §Decision item 4, injection is USB-only. One node at a time; two concurrent sessions
cannot be told apart in a `PROV` failure, and a half-written record reboots the node.

```sh
python3 firmware/hear_node/enroll.py gold /dev/ttyACM0 --class esp32s3-i2s-gps --no-flash
```

`enroll.py` prints a **masked** summary and stores the `PROV` line hex-encoded with a CRC. Re-run
B1 afterwards; that is the whole acceptance for the handoff.

### B3 — the rules that make the flash reversible

* `~/.hear_push` is mode `600`, operator machine only, never committed, never pasted anywhere.
* Token values never travel on a command line. `flash.py` sends `X-Hear-Auth` as a **header**, read
  through the same reader `gen_secrets.py` uses, so what is sent cannot drift from what was
  provisioned.
* **Never flash `gold` with a tokenless asset**, and never `--allow-admin-lockout`. `gold` is
  reachable over the network only; an image whose admin endpoints fail closed cannot be rolled
  back over the air, which turns a reversible flash into a USB trip. That is how `rankine` was
  lost.
* The admin token must match the **running** image at the moment of the OTA, not the one being
  installed.

### B4 — physical access precondition

Do not start unless someone can reach `gold` physically within 24 h. Every residual failure mode
in §7 that the boot guard cannot catch ends at USB.

---

## 3. The flash itself — one command, no flags

Not executed here. Recorded so that the acceptance below has a defined subject.

```sh
python3 firmware/hear_node/flash.py gold <gold-ip> --release <tag> --verify-signature
```

* `--release` installs the published, credential-free `-qspi` asset; `gold`'s NVS supplies identity
  and credentials, and a node that reports no NVS record is refused (Gate B).
* Do **not** pass `--class` (it is already correct from `/status`), `--force`,
  `--allow-admin-lockout` or `--allow-unattested`.
* `flash.py` reads `/status` back afterwards and refuses to report success if the node that answers
  is not `gold`, which is the identity failure that check exists for.

Immediately before it, take a read-only snapshot (`/status`, `/ota`, `/log`, `/pins`, `/audio`,
`/detections`) and strip `pos{}` and `bssid` before any of it leaves the host. The snapshot is the
only thing that makes "did the numbers move" answerable afterwards.

---

## 4. Post-flash: quad-PSRAM verification

`sys.psram_bus` and `sys.psram_fault` already exist on `v0.1.6`, so — unlike the octal→quad
transition — **their presence is no longer proof that the new image is running.** The fields that
are new in #230 are `sys.psram_total` and `raw.want_s`; those are the ones that prove the tiers
landed.

| # | Check | Pass | Fail means |
|---|---|---|---|
| P1 | `fw` | exactly the tag from §3 | the old fw with the same `boot_id` gone ⇒ the boot guard reverted; see §7 |
| P2 | `node` / `class` | `gold` / `esp32s3-i2s-gps` | wrong identity ⇒ stop everything |
| P3 | `sys.psram_bus` | `"quad"` | `"octal"` ⇒ the wrong asset was installed; roll back |
| P4 | `sys.psram_fault` | `false` | `true` ⇒ the image's bus mode does not match the board; roll back |
| P5 | `sys.psram_total` | **2 097 152** | absent ⇒ the tag does not contain #230 and the flash was pointless; present but different ⇒ the part is not what the record says, stop and scan the board |
| P6 | boot `/log` | `quad bus (as built)`, and **no** `psram FAULT` | a FAULT line outranks every green field above it |
| P7 | `sys.psram_min` | > 300 000 B and not trending to zero across ≥4 polls | a PSRAM low-water mark near zero means the ring took the memory the web server and Wi-Fi need |

---

## 5. Post-flash: raw-ring, detection-ring and heap-margin thresholds

### 5.1 The arithmetic, so the expected tier is read rather than guessed

`FS_ACQ` is 48 kHz, int16, so a ring costs 96 000 B per second. `PSRAM_KEEP_B` is 262 144 B and is
held back on **every** tier. Tiers strictly below `PRAW_DET_RESERVE_BELOW_S` (30 s) additionally
hold back `PRAW_DET_RESERVE_B` = `DET_RING_MIN` × `sizeof(Det)` ≈ 135 kB, so a small part cannot
buy `/audio` by pushing the detection ring onto the internal heap.

| Tier | Ring bytes | Held back | Contiguous PSRAM needed | Against `gold`'s 2 097 152 B part |
|---|---|---|---|---|
| 80 s | 7 680 000 | 262 144 | 7 942 144 | no |
| 60 s | 5 760 000 | 262 144 | 6 022 144 | no |
| 45 s | 4 320 000 | 262 144 | 4 582 144 | no |
| 30 s | 2 880 000 | 262 144 | 3 142 144 | no |
| 20 s | 1 920 000 | 262 144 + ~135 k | ~2 317 312 | no — larger than the whole part |
| **15 s** | 1 440 000 | 262 144 + ~135 k | ~1 837 312 | **plausible, and tight** |
| **10 s** | 960 000 | 262 144 + ~135 k | ~1 357 312 | yes |

The tier is chosen at runtime from the **largest contiguous free SPIRAM block**, which is not the
same as total free and is not observable from today's `/status`: `psram` is reported *after* the
detection ring has been taken, and `praw` is allocated *before* it. Today's 1 807 356 B sits within
about 30 kB of the 15 s requirement, so **the landing tier is genuinely uncertain between 15 s and
10 s.** `raw.want_s` names it exactly. **Do not write a number into the acceptance and grade
against it** — `docs/REDESIGN-LESSONS.md` item 12's "~15 s on Gold/Kasami" is a recollection, not a
specification, and `kasami` measurably holds 80 s.

### 5.2 Thresholds

| # | Check | Pass | Notes |
|---|---|---|---|
| R1 | `raw.want_s` | **∈ {10, 15}**, and stable across reboots | 0 ⇒ no tier fit. Read the `praw NO PSRAM ring` line: the `/audio is disabled; capture, gate and logging are unaffected` variant reports `need` and `largest` — those two numbers are the finding. The `no usable PSRAM at all` variant is a different (P4) fault |
| R2 | `raw.span_s` | > 0, and within the node's ppm of `raw.want_s` | `span_s` divides capacity by the *measured* rate, so the two legitimately disagree slightly. Only `want_s` may be compared against the tier table |
| R3 | `raw.cap_samples` | `raw.want_s` × 16 000 (decimated domain) | 240 000 at 15 s, 160 000 at 10 s. A factor-of-3 discrepancy here is the acquisition/decimated domain confusion, not a ring fault |
| R4 | `raw.fill_pct` / `held_samples` | rising after boot, reaching 100 within `want_s` + margin | still 0 after several minutes ⇒ the writer is not running; that is a capture fault, not a ring fault |
| R5 | `raw.marks` | increasing | one `(UTC, sample)` pair per GPS second. **0 marks ⇒ the ring exists but cannot be addressed by time**, which is a clock/siting symptom, not a ring failure |
| R6 | detection ring, boot `/log` | `dets ring <n> detections, <k> kB in **PSRAM**` | `in internal RAM` is a **fail**: the reserve in #230 exists precisely to prevent it. `n` of 512 rather than 1024 is *acceptable* on the 15 s tier — the reserve is sized to `DET_RING_MIN` — but record which it was |
| R7 | `sys.heap_min` | **> 20 000 B after ≥ 1 h uptime** | baseline is 19 312 B at ~53 min, i.e. **a no-go as it stands**. This number is the headline and it must be re-measured, not inherited |
| R8 | `sys.heap_min` after serving `/audio` | still **> 20 000 B** | **new risk, and specific to this flash.** Streaming PCM allocates internal buffers that did not exist while the ring was absent. Re-read `heap_min` *after* §6, not only before |
| R9 | `heap_max_alloc` | > 40 000 B | a fragmented internal heap fails a TLS handshake long before `heap` reaches zero |
| R10 | `sys.loop_max_ms` | **< 1 000 after ≥ 1 h**, and not climbing | 966 today — marginal. A climbing value is the PSRAM-on-internal-heap signature returning |
| R11 | peers | `kasami` and `ageev` still report `raw.span_s` 80 | **fleet stop** if either drops. #230's ≥30 s tiers are byte-identical by design; a drop means the premise is wrong |

If R7 does not clear after an hour, **stop and do not accept the node**, per rollout sequence §13.
Shipping a node one allocation from a panic is worse than a node with no ring.

---

## 6. Post-flash: actual `/audio` PCM streaming validation

This is the point of the whole exercise: it is the *only* way `gold`'s microphone can be judged by
PCM rather than by a summary statistic, because this board profile has no SD card and therefore no
clips.

### 6.1 Is the ring retrievable at all

```sh
curl -s "http://<gold-ip>/audio"
```

| Field | Pass | Reading |
|---|---|---|
| `ring` | `true` | `false` ⇒ `praw` is NULL; §5 R1, not an audio fault |
| `span_s` | ≈ `raw.want_s` | |
| `cap_samples` | matches §5 R3 | decimated domain |
| `addressable_samples` | > 0 | smaller than `cap_samples` by the overwrite guard, deliberately |
| `fs_hz` | ≈ 16 000 | the **decimated** rate. It is not the acquisition rate and it is not the WAV rate |
| `addressable` | `true` | **`ring: true` with `addressable: false` is a clock finding, not a ring finding** — it means there is no PPS/UTC anchor to address the ring by. Record it, do not roll back for it |
| `from_utc_us` / `to_utc_us` | a window that advances between two polls | |

**The overwrite guard bounds what may be asked for.** It is `AUDIO_GUARD_S` (16 s) capped at a
third of the ring, so on a 15 s ring the guard is 5 s and roughly 10 s is addressable; on a 10 s
ring the guard is about 3 s and roughly 6 s is addressable. Ask for `dur` inside
`addressable_samples / fs_hz` — a larger request is clamped, and the clamp is reported in
`X-Audio-Clipped` rather than silently.

### 6.2 Fetch and inspect real PCM

```sh
curl -sD headers.txt "http://<gold-ip>/audio?from=<from_utc_us>&dur=<within the guard>" -o gold-audio.wav
```

| Check | Pass |
|---|---|
| HTTP status | `200`. `503` ⇒ no ring, or no UTC anchor — the body says which. `416` ⇒ the window asked for is not in the ring; re-read `/audio` and use its own `from_utc_us` |
| `X-Audio-Clipped` | `none`. `head`/`tail`/`both` mean the window was clamped; the file is still valid, the *span* is not what was asked for |
| `X-Audio-Fs-Hz`, `X-Audio-Samples`, `X-Audio-From-Utc-Us` | present, and consistent with the request |
| WAV header sample rate | **48 000 Hz** — `praw` is at the acquisition rate. `i2s.nominal_hz: 16000` is the post-decimator scene lane and is *not* the acquisition rate. A 16 kHz WAV header here is a defect, not a short ring |
| File length | header (44 B) + `X-Audio-Samples` × `DECIM` × 2 B, exactly |
| Content | decodes; is **not** all-zero; has structure that tracks a deliberate stimulus (a clap at a recorded UTC) |
| Repeat | a second fetch of a *different* window also returns non-zero audio |

### 6.3 What this does, and does not, decide about the microphone

* Non-zero, structured PCM that responds to a stimulus ⇒ **the microphone is proven alive.** This
  is the method that cleared `ageev` and `kasami`; trust the positive result.
* `selftest.mic_state: normal` alone is **not** this proof, and the settle-window change (`cb0f4ce`)
  makes the old latched `all_zero_samples` signature much rarer without making the summary a
  substitute for PCM. Report `mic_stats.attempts` and `settle_ms` alongside.
* `audio.env_peak`, `audio.ambient` and detection counts are **not** mic-liveness signals. `gold`
  reported a healthy `env_peak` for weeks while its selftest latched `all_zero`.
* All-zero PCM across two windows *is* a hardware finding — open it against remediation §R5, and
  it is still **not** a reason to roll the firmware back (§7).

---

## 7. Watchdog and telemetry health

### 7.1 The guards that are actually running

* `hear_boot_guard()` is the first statement of `setup()`. It counts boots in `RTC_NOINIT`; after
  `HEAR_BOOT_MAX_TRIES` (3) boots without reaching healthy it sets the other OTA partition and
  restarts — **the previous image returns with no network and no operator.**
* `hear_boot_tick()` marks the image healthy only at `HEAR_BOOT_HEALTHY_MS` (30 s) **and**
  reachable; an image that is up but unreachable past `HEAR_BOOT_UNHEALTHY_MS` (90 s) force-reboots
  so the counter can advance. Health proven by the old image does not transfer to the new one.
* `boot_wdt_arm()` covers bring-up, which the boot counter cannot: a hang produces no reset.
* Neither guard can rescue a fault *before* `setup()`. That residual is why §B4 exists.

### 7.2 Health checks, by wall clock

| When | Check | Pass |
|---|---|---|
| T+30 s | `/status` answers; `fw`, `node`, `class` | §4 P1–P2 |
| T+30 s | `/ota` | the running partition **address has changed** from the pre-flash snapshot; `boot_try 0` |
| T+30 s | `sys.reset` / `sys.postmortem` | `sw` or `poweron`; a `panic` goes straight to §8 |
| T+2 min | `/ota` `healthy` | **`yes`**. Until it does, three bad boots still auto-revert — that is the safety net working, not a delay to shortcut |
| T+5 min | `time.boot_id` | unchanged since T+30 s. A new `boot_id` is a restart; count them |
| T+5 min | `i2s.clean_s` | advancing between polls; `acq_slip` 0 |
| T+5 min | `acq.drop_s` / `drop_samples` | not growing |
| T+5 min | `sys.stream_stalls`, `sys.stream_gone`, `write_fail`, `scene.stream_skip` | 0, or unchanged from the pre-flash snapshot |
| T+5 min | `scene.rows` | advancing — the DSP path is running |
| T+5 min | `net.disc` / `net.reconn` | 0 |
| T+5 min | `auth.push.last_code` | 2xx. `401` ⇒ **fleet stop** (§B1) |
| T+5 min | `/pins` | byte-identical to the pre-flash snapshot. A different map means the wrong board profile was compiled in |
| T+20 min | `sys.heap_min`, `sys.psram_min`, `loop_max_ms` | stable across ≥4 polls, none trending down/up. The old `gold` failure was a **slow** decay, invisible in 5 minutes |
| T+20 min | `sys.chip_c` | < 70 |
| T+20 min | Redis node key | present, live TTL |
| T+20 min | bridge outbox `device_id` count | **increasing between two snapshots.** Coverage is cumulative, so "present" is not the same as "publishing" |
| T+1 h | §5 R7–R10 | the heap and loop thresholds, re-read after §6 has streamed PCM |

### 7.3 Expected, and explicitly not failures

* GPS: `fix` low or 0, few satellites, `time.state` `HOLDOVER` or `FAULT`. `HOLDOVER → FAULT` after
  a reboot is a *correct* transition — the stale anchor is gone and the node says so. A GPS
  **regression** looks different: `valid_nmea` stalling, `sentences` not advancing, or
  `pmtk_nak` > 0. Watch those three, not `fix`.
* `time.valid: false` with `sync_sigma_ns`, `anchor_age_us` and `boot_epoch_us` as literal `null` is
  the contract the heartbeat receiver requires; `gold` should be accepted despite no fix. If the
  Redis key does not appear, that is a **receiver/bridge** finding — read the bridge rejection
  reason before touching the node.
* `discontinuity_flags` of 16 is the HOLDOVER flag the I2S/GPS nodes already carry.
* `clips.*` all zero and `sd: false` — this board has no card.
* `gold` remains out of TDoA on the #102 capture-path offset, before and after.

---

## 8. Abort, failure response and rollback

### 8.1 Abort before writing a byte

Any of these, and the flash does not start:

* any Gate A row fails, or could not be run (§1 A9);
* any Gate B row fails and cannot be closed by USB enrollment at the node (§2);
* no physical access to `gold` within 24 h (§B4);
* `auth.push.last_code` is `401` anywhere in the fleet;
* the proposed asset is not the `-qspi` variant, or a refusal would have to be forced;
* `gold` is mid-panic: two `/status` 60 s apart show a changed `boot_id`;
* the link margin is thin (`net.rssi` worse than about −80 dBm) — a drop mid-upload wastes the slot;
* a Phase 2 soak milestone sample is being taken today and has not been taken yet (§9).

### 8.2 After the flash

| Symptom | Meaning | Do |
|---|---|---|
| Answers, `fw` is the *old* tag, `/ota` back on the previous partition | the image failed before 30 s; the boot guard reverted after 3 tries | **automatic — no action.** Pull `/log` for the panic. Do not reflash blind |
| No `/status` for 3 min | never joined, or the app is hung | **wait ~6 min.** 90 s unhealthy × 3 tries force a revert unaided. Intervening early destroys the receipt and can strand the node |
| Still unreachable after that window | it faulted before `setup()` returned | per-node stop; USB recovery, like `rankine` |
| `boot_id` changes repeatedly | a reset loop the counter is not catching (a hang, not a fault) | roll back over OTA **while it still answers**; if it stops, USB |
| `psram_fault: true` or `psram_bus` not `quad` | wrong asset or a dead part | roll back; escalate to a bare-board scan |
| `sys.psram_total` absent | the tag does not contain #230 | roll back or leave it; either way Gate A1 was wrong |
| `raw.want_s` 0 with the `need`/`largest` log line | no tier fit this part | **not a rollback.** Record `need` and `largest`; it is a tier-table finding |
| `heap_min` ≤ 20 000 B at ≥ 1 h | the node is one allocation from a panic | **no-go: roll back.** Do not accept the node |
| `raw.span_s` drops on `kasami`/`ageev` | #230's invariant is violated | **fleet stop**, not a `gold` action |
| `401` on a later `/update` | the token does not match the **running** image | use the provisioned value; if it is lost, USB only |

**Rollback is an OTA of the previously running, provisioned `v0.1.6` `-qspi` asset**, which is
published, verifies against its own manifest, and is what every other node is on. Rolling back
returns `gold` to a known state with **no ring and no `/audio`** — that is the point, not a
regression. Never roll `gold` back to a tokenless asset (`v0.1.2`–`v0.1.5`); that is irreversible
without USB.

**Do not roll back for:** no GPS fix, `time.state FAULT`, `raw.want_s` landing on 10 s instead of
15 s, `addressable: false` with `ring: true`, or an inconclusive microphone result. None of those
are regressions and all of them are tracked elsewhere.

---

## 9. This flash is a fleet-composition change and must be graded against the Phase 2 soak baseline

**Stated explicitly, and not executed, evaluated or scheduled by this document.**

Flashing `gold` is not a neutral maintenance action against the durable soak. It is a
**fleet-composition and configuration change** with at least four distinct effects on the evidence
the soak is collecting:

1. **A reboot is a heartbeat gap.** `ledger_fresh` is fleet-level and survives one node rebooting,
   but `record_growth` grades total volume against the baseline rate with a tolerance. The flash
   window must be **recorded in the milestone notes** so a dip has an explanation attached rather
   than an investigation.
2. **The node's published behaviour changes.** After this flash `gold` has a raw ring and a live
   `/audio` that it does not have today. That is a change in what the fleet *is*, not only in what
   version it runs, and any comparison that spans the flash is a comparison across two different
   fleets.
3. **Arrivals from different builds are not comparable.** Any TDoA, latency or detection-rate
   conclusion drawn across a window containing the flash is void; say so in the artifact rather
   than discarding the window.
4. **Coverage accounting shifts.** Node coverage in the soak is cumulative, so a node that stops
   publishing still reads as covered. Grade `gold` by comparing outbox counts between two
   snapshots, not by `all_nodes_covered`.

Therefore, **before this flash happens**, the operator must:

* establish it against the **current** Phase 2 soak baseline — the authoritative T0 prime is
  `2026-09-15T18:26:22Z` with all 14 criteria passing — and confirm that baseline is still the live
  one and has not been re-primed;
* take the T+24h / T+7d / T+14d sample **before** the flash on that day, never during it;
* decide explicitly whether the flash goes inside the current soak window or waits for a
  re-baseline. If a soak criterion regresses afterwards, **pause the rollout, not the soak** — the
  soak is the longer clock and a paused rollout costs nothing.

None of the above is a step this document performs. It is the evaluation that owns the decision of
*when*, and it sits upstream of every gate in §1–§8.

---

## 10. What is deliberately out of scope

* **`kasami`, `ageev`, `nyquist`, `mach`: do not touch** in the same operation. `kasami` is
  deliberately absent from `NODE_PSRAM_MODES` and takes the class default; its bus mode has still
  never been scanned, and forcing a variant on it would be the Ageev-class guess again.
* **`rankine`: excluded from OTA entirely** until its USB recovery completes.
* No reboot, `/format`, gate-floor write, ConfigMap edit or bridge change accompanies this flash.
* No `--release` flash of any node with an asset that predates NVS provisioning.
* The `gold` GPS sky/antenna problem (`gold-no-gps-fix`) is physical and unaffected by any
  firmware. It is not graded here.
* No SLSA *level* is claimed and the build is not claimed to be bit-reproducible.

---

## 11. Provenance of this document

Written from a fresh clone of `origin/main` at `62641da`, by reading the merged firmware
(`firmware/hear_node/hear_node.ino` `PRAW_TIERS_S` and the `setup()` allocator,
`firmware/lib/hear_platform/src/hear_boot.h`), the installer and provenance tooling, and the
existing operations documents cited in the header. **No node, cluster, credential or release was
touched, read or created in producing it.** The `gold` values in §0 are quoted from the rollout
sequence's measurements and were not re-measured.
