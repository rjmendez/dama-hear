# v0.1.6 release readiness runbook

Dated 2026-09-15. **Nothing here has been executed.** This document is the pre-cut gate for the
first release after the `rankine` tokenless-OTA incident. It states what must be merged, what the
release must prove about itself, what the published assets are and are not allowed to do, and the
per-node rollout order with its stop conditions.

Read [ota-release-credentials.md](ota-release-credentials.md) first: it is the incident record and
the credential contract. This document is the release procedure built on top of it.

## 0. Why v0.1.6 exists

The 48 kHz acquisition migration is **architecturally complete and proven fleet-wide** — every
deployed node acquires at 48 kHz and the published `v0.1.5` manifest already declares
`fs_acquisition_hz: 48000` for all three variants. v0.1.6 is **not** a sample-rate rollout.

v0.1.6 exists for exactly one reason: `v0.1.2`–`v0.1.5` assets were built without `secrets.h`, so
`HEAR_PUSH_TOKEN` and `HEAR_ADMIN_TOKEN` compiled to empty strings. `rankine` was flashed with
`v0.1.5` and became backend-mute (every heartbeat `401`) **and** OTA-locked (`/update` and
`/reboot` fail closed). No published release can be rolled to the rest of the fleet until a tag
exists that carries the NVS credential path. The uniformity gate is therefore blocked on a
**release**, not on firmware capability.

The nodes still on the older lineage (`gold`, `ageev`, `kasami`, `nyquist`, `mach`) predate the
endpoint-auth change and still accept a tokenless `/update`, so they remain remotely recoverable
today. That window is what makes an ordered rollout possible; it closes for each node the moment
it takes an auth-aware image.

## 1. Required merged prerequisites

| # | State at cut | Why it is required |
|---|---|---|
| #186 | **merged** `c2ca757` | Clip ring capacity: the firmware SD FIFO/drain arithmetic the fleet is expected to run. |
| #191 | **merged** `dc75e60` | `flash.py` sends `X-Hear-Auth` and refuses to flash an image with no admin token. Without it the operator tool itself cannot install v0.1.6. |
| #196 | **merged** `3f27c10` | Push/admin credentials live in the `hear_prov` NVS record outside both OTA slots; compiled-in tokens are copied to NVS at boot; `/status` gains the `auth` block; `flash.py --release` and `deploy_gate` gate on it. **This is the fix the release is for.** |

Verify at the candidate commit, not from memory:

```sh
git merge-base --is-ancestor c2ca757 HEAD && echo "#186 in"
git merge-base --is-ancestor dc75e60 HEAD && echo "#191 in"
git merge-base --is-ancestor 3f27c10 HEAD && echo "#196 in"
```

### Open work that is *not* a prerequisite

* **#212** (`fix/release-auth-safety`) — the release-packaging half of the same incident. It is
  currently `CONFLICTING`/`DIRTY` against `main` and overlaps `#196`, which already landed. It is
  **not** required for v0.1.6 and must not block the cut; if it lands first, re-run §3 and §4
  against the new `release.yml` before tagging.
* **#213** (`fix-release-token-provisioning-escalation`) — makes `/update` the single recovery
  valve when *no* admin token is configured at all, while keeping `/reboot`, `/format`, `/gate`
  writes and hardware probes fail-closed. This is the belt-and-braces answer to the rankine class.
  It is **optional for the cut but strongly preferred**: without it, a v0.1.6 image installed
  outside `flash.py` on an unprovisioned node still strands that node. Decide explicitly, in
  writing, whether v0.1.6 ships with or without #213 — do not let it land silently mid-rollout.

**No other open PR is a prerequisite.** Phase 1/2/3/4 work (#198–#211) is design, tests and
backend and does not ship in a firmware asset.

## 2. Release artifact variants

Three `hear_node` variants, one per board class / PSRAM bus mode, plus the metadata set:

| Asset stem | Board class | PSRAM | Target nodes |
|---|---|---|---|
| `hear_node-xiao-s3-pps-v0.1.6` | `xiao-s3-pps` | octal | XIAO ESP32-S3 Sense nodes |
| `hear_node-esp32s3-i2s-gps-v0.1.6` | `esp32s3-i2s-gps` | octal | `nyquist`, `mach`, `rankine`, `ageev`, `kasami` (class default) |
| `hear_node-esp32s3-i2s-gps-qspi-v0.1.6` | `esp32s3-i2s-gps` | **quad** | `gold` only (8 MB flash / 2 MB quad PSRAM) |

Each stem publishes `.bin`, `-bootloader.bin`, `-partitions.bin`, `-merged.bin` and `.elf`, plus
`build-info.json`, `release-manifest.json`, `release-manifest.schema.json`,
`release-sbom.cdx.json`, `release-provenance.intoto.jsonl` and `SHA256SUMS` (§6,
[release-provenance.md](release-provenance.md)).

**The quad variant is a build requirement of the cut, not a follow-up.** `gold` has quad PSRAM;
the octal class image leaves it with `psramFound() == false` and no raw ring.
`board_profiles.release_variant_refusal()` refuses to install the octal asset on it and vice versa,
so a release that omits the `-qspi` stem leaves `gold` with no installable asset at all. The
`firmware / build hear_node (esp32s3-i2s-gps) [quad psram]` matrix job must be green on the
candidate commit before the tag is pushed.

Building the quad asset is **not** the same as deciding `gold` is ready to receive it — see §7.

## 3. Manifest validation: `fs_acquisition_hz = 48000`

`release.yml` runs `release_manifest.py generate` then `verify` inside the release job, so a
manifest that does not match the tree fails the release before publication. The 48 kHz claim is
derived, not asserted: `capture_profile()` reads `FS_NOMINAL` from the board header and `DECIM`
from `hear_node.ino` and publishes `fs_acquisition_hz = FS_NOMINAL * DECIM`.

Confirmed on the candidate commit `60f07c0`, offline, from the tree alone:

| Variant | `fs_nominal_hz` | `decimation` | `fs_acquisition_hz` |
|---|---|---|---|
| `hear_node-xiao-s3-pps` | 16000 | 3 | **48000** |
| `hear_node-esp32s3-i2s-gps` | 16000 | 3 | **48000** |
| `hear_node-esp32s3-i2s-gps-qspi` | 16000 | 3 | **48000** |

Reproduce without building or downloading anything:

```sh
python3 - <<'PY'
import sys, pathlib
sys.path.insert(0, "firmware/hear_node")
import release_manifest as rm, board_profiles as bp
for cls, mode in [("xiao-s3-pps","octal"), ("esp32s3-i2s-gps","octal"), ("esp32s3-i2s-gps","quad")]:
    p = rm.capture_profile(pathlib.Path("."), cls)
    assert p["fs_acquisition_hz"] == 48000, (cls, mode, p)
    print(bp.release_stem(cls, mode), p["fs_nominal_hz"], p["decimation"], p["fs_acquisition_hz"])
PY
```

`i2s.nominal_hz = 16000` in `/status` is the **post-decimator scene lane**, not a 16 kHz fleet.
Anyone reading `16000` as the acquisition rate is reading the wrong field; the acquisition rate is
`fs_acquisition_hz` in the manifest and the WAV header served by `/audio`.

After publication, verify the *published* manifest offline before any node is touched:

```sh
python3 firmware/hear_node/release_manifest.py verify --dist dist --tag v0.1.6 --source-root .
```

## 4. Public-artifact auth contract

This is the contract the release notes must state in these terms, because it is the part operators
got wrong last time.

**A published v0.1.6 asset is an unprovisioned image.** It contains no Wi-Fi credentials, no node
identity, no push token and no admin token. Compiling fleet credentials into a downloadable
artifact would publish them to anyone who can fetch the release, so this is deliberate and
permanent.

| | Unprovisioned (as downloaded) | Fleet-ready (after enrollment) |
|---|---|---|
| Node identity | MAC-derived | NVS `hear_prov` node id/class |
| Wi-Fi | none; comes up as its own AP | NVS network list |
| Backend push | unauthenticated → `401`, node is backend-mute | NVS `ptoken`, `auth.push.last_code` 2xx |
| `/update`, `/reboot` | fail closed (see #213 for the recovery valve) | NVS `atoken`, authenticated |
| `/status` `auth` block | `configured: false` | `configured: true`, `src: "nvs"` |

**Therefore: OTA of a public v0.1.6 asset onto an unenrolled node is explicitly unsupported.** The
only supported paths are USB enrollment (`enroll.py`) for a new or unprovisioned board, and
`flash.py --release v0.1.6` for a node whose live `/status` already proves NVS credentials.
`flash.py --release` refuses before writing a single byte otherwise, and that refusal is a feature,
not an obstacle to work around.

## 5. Admin-token provisioning (operator-selected; nothing generated here)

**This runbook does not choose, generate, print, store or transport any token value.** The admin
token is an operator decision and the only remaining decision that gates the rollout: `~/.hear_push`
on the operator machine currently carries `HEAR_PUSH_TOKEN` only, so `HEAR_ADMIN_TOKEN` must be
**chosen** and provisioned — there is nothing to recover.

The policy, threat model, key lifecycle, lost-token behaviour and the exact tooling changes each
option implies are worked out in `docs/decisions/0006-admin-token-provisioning-policy.md`, which
recommends **per-node** tokens and likewise generates nothing. This section states the decision;
that ADR states what follows from it.

Required decision before §7 begins:

* Is the admin token **fleet-wide or per-node**? Per-node limits blast radius; fleet-wide is one
  secret to rotate. The NVS record supports either.
* Where does the authoritative copy live (password manager / operator secret store)? `~/.hear_push`
  is a working copy, not the authority.
* What is the rotation trigger and procedure?

Local secret handling rules:

* `~/.hear_push` is a `KEY=value` file, mode `600`, on the operator machine only. Never committed,
  never pasted into an issue, PR, log, chat or agent transcript.
* `enroll.py` reads it and writes only a **masked** summary; it never prints values, and the NVS
  `PROV` line stores fields hex-encoded with a CRC.
* Token values never appear on a command line (they would land in shell history and `ps`).
  `flash.py` sends the admin token in the `X-Hear-Auth` **header**, never a query string.
* A token must never be committed, and must never be compiled into anything published.

Provisioning path per node, over USB, once:

```sh
python3 firmware/hear_node/enroll.py <node> /dev/ttyACM0 --class <board-class> --no-flash
```

Then confirm on the live node, before that node is considered provisioned:

* `prov.loaded: true`
* `auth.push.configured: true`, `auth.push.src: "nvs"`
* `auth.admin.configured: true`, `auth.admin.src: "nvs"`
* after one heartbeat cycle, `auth.push.last_code` is 2xx (a `401` here is a loud stop)
* the node's key reappears in Redis with a live TTL

Nodes already running a credentialed local build migrate without USB: their compiled-in tokens are
copied into NVS at boot, so one `flash.py <node> <ip>` from this tree (or `enroll.py --no-flash`)
followed by a reboot moves them to `src: "nvs"`.

## 6. Pre-cut release checks

Run all of these on the candidate commit in a clean clone. Every one is offline; none touches a
node.

```sh
python3 -m pytest -q \
  tests/test_release_manifest.py tests/test_firmware_release_workflow.py \
  tests/test_release_image_provenance.py tests/test_release_provenance_attestation.py \
  tests/test_flash_ota_auth.py tests/test_nvs_enrollment.py tests/test_enrollment_gate.py \
  tests/test_firmware_admin_auth.py tests/test_boot_failback_per_image.py \
  tests/test_shared_boot_failback.py tests/test_clip_header_rate.py
```

Green on `60f07c0`: **129 passed**, before the provenance suite was added; re-run the block above on the candidate commit and record the new count.

| Check | How | Pass condition |
|---|---|---|
| Prerequisites merged | `git merge-base --is-ancestor` (§1) | #191 and #196 are ancestors of the tag commit |
| 48 kHz manifest claim | §3 snippet | `fs_acquisition_hz == 48000` for all three variants |
| Quad variant builds | `firmware` workflow matrix | `[quad psram]` job green |
| All six firmware builds | `firmware` workflow | `hear_node` ×3, `puc_node`, `hear_poc`, `path_test`, `sense_bringup` green |
| Generated headers current | `ci.yml` "generated headers are current" | no diff after regeneration |
| Contract freeze | `ci.yml` "contract layout and frozen baseline" | frozen baseline matches the tree |
| Recoupling boundary | `ci.yml` "adapter conformance and recoupling boundary" | green |
| Coordinates | `coord-guard.yml` + `tests/test_no_site_coordinates.py` | **no real-world coordinates in tree or history** — this gates the tag; a published asset set is public forever |
| Working tree clean | `git status --porcelain` | empty; the manifest records dirty state and a dirty cut is not reproducible |
| Manifest self-verify | `release_manifest.py verify` in `release.yml` | the release job fails the release, not the fleet |

**Provenance, SBOM and signature.** The manifest is the provenance record: it binds tag, commit,
dirty state, FQBN, esp32 core `3.3.11`, `arduino-cli` `1.5.1`, build flags, board header,
partition table, capture profile and a per-artifact SHA-256, with a published JSON schema.
`build-info.json` records the toolchain and states `credentials: none compiled in`.

The SBOM and attestation gap this section used to record is closed by
[release-provenance.md](release-provenance.md), which is the design, the procedure and the
failure/revocation handling in full. In short, a release now also publishes:

* `release-sbom.cdx.json` — CycloneDX 1.6, derived from the same tree as the manifest and
  deterministic, covering the binaries, the source closure, the vendored `firmware/lib` libraries
  and the toolchain pins, and stating what it does **not** enumerate (the esp32 board package's
  contents);
* `release-provenance.intoto.jsonl` — a SLSA v1 provenance attestation over every published file,
  signed keyless through the release job's GitHub OIDC identity. **No signing key exists**:
  `release.yml` reads no repository secret, and the bundle is published as an asset so
  verification needs no GitHub credential.

`SHA256SUMS` is written last and covers all of it. Pre-cut checks:

```sh
python3 -m pytest -q tests/test_release_provenance_attestation.py
python3 firmware/hear_node/release_manifest.py verify --dist dist --tag v0.1.6 --attestation
gh attestation verify hear_node-esp32s3-i2s-gps-v0.1.6.bin --repo rjmendez/dama-hear \
  --signer-workflow rjmendez/dama-hear/.github/workflows/release.yml \
  --bundle release-provenance.intoto.jsonl
```

The offline `verify --attestation` checks structure and subject coverage only — it does not check
the signature, and says so. `gh attestation verify` is the cryptographic check.

⚠️Still do **not** claim a SLSA *level* for v0.1.6: the release carries SLSA v1 provenance from
GitHub's hosted builder, and a level is an audit conclusion, not a field. And the build is not
claimed to be bit-reproducible — the inputs are pinned, the outputs are attested.

## 7. Rollout order and per-node no-go gates

Do not start until §5's token decision is made and §6 is green. One node at a time. Each node must
satisfy the §5 confirmation list *and* pass `deploy_gate` before the next node is touched.

| Order | Node | Asset | Gate / stop condition |
|---|---|---|---|
| 0 | **`rankine`** | — | **EXCLUDED from OTA.** It is already on tokenless `v0.1.5`: pushes `401`, no Redis key, `/update` and `/reboot` fail closed. It is **USB-recovery only** and is owned by the separate `recover-rankine-usb` task. Any attempt to OTA it is a no-op that wastes the rollout window. |
| 1 | **`nyquist`** | `esp32s3-i2s-gps` octal | Canary **and** the rollback-receipt node (§8). LOCKED GPS, normal pipeline, full stack — it is the node with the most evidence behind it. Stop the entire rollout if its receipt does not come back. |
| 2 | **`mach`** | `esp32s3-i2s-gps` octal | The reason this step exists: `mach` is on `v0.1.4-5-g2355270`, the sole cause of the split build behind `hear-drain-check` exit 1. Upgrading it to v0.1.6 ends the split. Gate: drain check returns to exit 0 after it. |
| 3 | **`kasami`** | `esp32s3-i2s-gps` octal | HOLDOVER/normal. ⚠️ `kasami`'s PSRAM bus mode has **never been scanned** and it is deliberately absent from `NODE_PSRAM_MODES`, so it takes the class default. If its boot asserts on PSRAM, stop and scan it rather than guessing a variant. |
| 4 | **`ageev`** | `esp32s3-i2s-gps` octal | Its GPS/time gate is met (`time.valid: true`, HOLDOVER). It carries a **latched mic-selftest false positive** that survives any reflash until the unmerged `selftest_mic_probe` settle fix lands — do not read it as new damage and do not let it be "fixed" by reflashing. |
| 5 | **`gold`** | `esp32s3-i2s-gps` **`-qspi`** quad | **NO-GO for this rollout.** `gold` has 2 MiB PSRAM against a 2.88 MiB minimum ring, so it currently reports `psram=0` with no raw ring and no PCM proof. Building the quad asset (§2) is required; *installing* it needs a separate ring-sizing decision owned by `gold-quad-flash-readiness`. Ship the asset, do not flash the node. |

Additional standing gates, all of which are out of this task's scope and none of which the rollout
may silently absorb: the unmerged mic boot-probe settle fix, and #102 capture-path offset, which
still blocks `gold`/`kasami`/`ageev` from contributing TDoA regardless of firmware version.

## 8. Deliberate rollback receipt (`nyquist`)

The per-image failback is implemented and unit-covered (57 tests) but **has never been exercised on
live hardware**. The uniformity gate is not met until one live receipt exists. Take it on
`nyquist`, deliberately, as step 1 of the rollout — not as an accident later.

Mechanism, from `hear_boot.{h,cpp}`: the running image is marked healthy only after
`HEAR_BOOT_HEALTHY_MS` (30 s) **and** `reachable`. An image that is up but unreachable past
`HEAR_BOOT_UNHEALTHY_MS` (90 s) force-reboots so the counter can advance;
`HEAR_BOOT_MAX_TRIES` (3) unhealthy boots flip the boot partition back and restart. `proven_ok` is
held in RTC memory together with the partition address it was earned on, so it does not transfer
across a partition change. The failback deliberately does **not** rely on the bootloader's rollback
feature.

Procedure:

1. Record the pre-flash receipt: `/ota` (`running`, partition address, `boot_try`, `healthy`,
   `img_state`), `/status` (`fw`, `auth`, `prov`), and the current Redis heartbeat TTL.
2. Flash v0.1.6 with `flash.py --release v0.1.6`.
3. **Withhold `proven_ok`**: keep the node from being `reachable` (STA unassociated) for three boot
   cycles. This is the whole experiment — do not let it associate and mark itself healthy.
4. Let the counter run: 3 unhealthy boots at ≥90 s each, roughly 5 minutes.
5. Restore reachability and collect the post-revert receipt.

Expected proof — all of it, not a subset:

* `/log` contains `BOOT GUARD: 3 boots without reaching healthy -- reverting to <label>`.
* `/ota` shows `running` on the **other** partition label at a **different** address than step 1.
* `/status` `fw` reports the **previous** build, not `v0.1.6`.
* `boot_try` resets and `healthy` returns to `yes` once the reverted image is up and reachable.
* `auth.push.last_code` returns 2xx and the node's Redis key reappears with a live TTL — NVS
  credentials survived the partition flip, which is the second thing this test proves.
* The node is reachable on `/status`, `/ota` and `/audio` with a 48000 Hz WAV header, i.e. the
  revert returned it to service and did not cost the 48 kHz property.

A receipt missing the `/log` line or the partition-address change is **not** a receipt; it is a
node that quietly marked itself healthy, and the gate stays open.

## 9. Failure and rollback procedures

| Failure | Response |
|---|---|
| `flash.py --release` refuses pre-write | Correct: the node cannot prove NVS Wi-Fi/push/admin. Provision per §5. Never pass `--allow-admin-lockout` to get past it during a release rollout. |
| Post-flash `auth.push.last_code` is `401` | Stop the rollout. The token in NVS is not the one the backend accepts. This is the rankine signature; treat one `401` as fleet-wide until disproved. |
| Node unreachable after flash | Do nothing for ~6 minutes: the failback needs 3 unhealthy boots to revert. Then check `/ota` and `/log`. Intervening early destroys the evidence and may strand the node. |
| Node still unreachable after the failback window | It hung before `setup()` returned or faulted in a global constructor — the failback cannot save either. USB recovery, like `rankine`. |
| PSRAM variant mismatch asserted at boot | `board_profiles` refused for a reason. Do not force the other asset; scan the board and record it in `NODE_PSRAM_MODES` with its evidence. |
| `hear-drain-check` still exit 1 after `mach` | The split build was not the only cause. Stop and re-diagnose; do not continue to `kasami`. |
| Published manifest fails offline `verify` | Do not flash anything from that release. Record it in `firmware/hear_node/release_revocations.json` (a PR, merged first — deleting the release does not reach an operator's cached copy), fix, re-cut with a **new** tag. Never re-cut the same tag: the attestation binds it. |
| `gh attestation verify` fails, or the bundle does not cover an asset | Stop. Do not flash. Treat it as a compromised or mis-built release until disproved, and revoke as above. |
| Rollout must be abandoned mid-way | The fleet tolerates a mixed v0.1.5/v0.1.6 lineage (it is in one today). Stop where you are, record which node is on which tag, and do **not** revert provisioned nodes to a tokenless asset. |

## 10. Release notes requirements

The generated notes already cover the asset table and the enroll/flash commands. v0.1.6's notes
must additionally state, explicitly:

1. **What this release fixes**: the `v0.1.2`–`v0.1.5` tokenless-asset defect, named, with the
   symptom (`401` pushes, fail-closed `/update` and `/reboot`) so an operator recognises it.
2. **Unsupported behaviour, stated as unsupported**: OTA of a public v0.1.6 asset onto a node that
   is not already enrolled with NVS push and admin credentials. It will produce a backend-mute
   node, and — without #213 — an OTA-locked one. `flash.py --release` refuses it by design.
3. **The supported paths**: USB `enroll.py` for a new board; `flash.py --release v0.1.6` for an
   enrolled node.
4. **Credential statement**: these assets contain no Wi-Fi credentials, no node identity and no
   tokens, and never will.
5. **Verification**: the offline `release_manifest.py verify` command, and that all three variants
   declare `fs_acquisition_hz: 48000`.
6. **Provenance**: manifest, `release-sbom.cdx.json` (CycloneDX 1.6) and a keyless SLSA v1
   attestation bundle, all covered by `SHA256SUMS`, with the `gh attestation verify` command
   spelled out and the limits stated — no SLSA level claimed, build not bit-reproducible, board
   package pinned by version rather than enumerated (§6, [release-provenance.md](release-provenance.md)).
7. **`rankine` is excluded** from OTA and is USB-recovery only.

## 11. Open decisions blocking the cut

1. **Admin token policy** (§5) — fleet-wide vs per-node, authority of record, rotation. Operator
   decision; nothing else in this runbook can proceed without it.
2. **Ship with or without #213** (§1) — the tokenless-OTA recovery valve. Preferred: with.
3. **`gold` ring sizing** (§7) — required before `gold` is flashed, not before the release is cut.
