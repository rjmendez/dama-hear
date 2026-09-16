# `rankine` USB recovery and data preservation

Dated **2026-09-15**. Written in a fresh clone of `origin/main` at `62641da`
(`v0.1.6-23-g62641da`).

**Nothing in this document has been executed.** It does not touch `rankine`, does not flash,
reboot, enroll or power-cycle any node, does not cut, re-cut or edit a release, does not apply,
patch, restart or create anything in the cluster, and does not generate, request, read back,
print, transport or reference any secret value. Every credential-adjacent field named below is one
of the booleans `/status` publishes to anyone on the LAN by design (`configured`, `src`,
`ota_recovery_open`, `last_code`).

This is the package the `recover-rankine-usb` lane is owed:
[fleet-release-rollout-sequence.md](fleet-release-rollout-sequence.md) §6 names the lane and gives
it five lines. This document is the procedure, its acceptance evidence, its refusal conditions, and
the one thing it is **not** allowed to decide.

---

## 0. Evidence base, and how stale it is

Two kinds of fact appear below and they are not interchangeable.

**In-tree facts** — re-verified against `62641da` while writing this, and true of the code as it
stands: tool behaviour, refusal conditions, endpoint semantics, board resolution, ancestry.

**Live facts** — **not re-measured here.** `rankine` was not probed, because probing it is not
required to write the procedure and the brief forbids touching it. Every live number is quoted
from the last read-only sweep on record, **2026-09-15T18:41–18:45Z**, published in
[fleet-release-rollout-sequence.md](fleet-release-rollout-sequence.md) §0.2/§0.3. Treat them as a
**starting hypothesis to re-confirm at the node**, not as the state you will find. §3.1 re-reads
all of them before anything is written.

### 0.1 `rankine`, as last measured

| Field | Value (2026-09-15T18:41–18:45Z) | Consequence |
|---|---|---|
| `fw` | `v0.1.5` | the only node not on `v0.1.6`; the sole remaining cause of the split-build page |
| `auth` block | **absent from `/status` entirely** | it is running an image old enough to predate the block; nothing about its credential state can be read remotely |
| backend push | `401` since ~14:25Z | unauthenticated; the ledger has been receiving nothing from it since |
| `/update`, `/reboot` | refuse | no usable admin token; OTA is not a path (§2) |
| clock | `LOCKED` | healthy reference node; it is not broken, it is **locked out** |
| `selftest.mic_state` | `normal` | no hardware suspicion |
| `raw.span_s` | `80.0` | full octal PSRAM ring — this is the number a regression is measured against |
| `sys.heap_min` | 78 560 B | the healthiest headroom in the fleet |
| `sys.loop_max_ms` | 1 140 | above the < 1 000 line used for `gold`; a watch item, not a blocker |
| OTA slot | `app0 @ 0x010000`, `boot_try 0`, `healthy yes`, `img_state 2` | record it before the flash (§3.1); the partition a node reverts *to* is only meaningful against its own recorded start |
| storage | SD card, `sd 80` in the drain build table | one of the three SD nodes; **the card is the only copy of what has not been drained** |
| drain | `ok +0` with `cursor-v1`, `0 lost` | serving files; the path this recovery preserves data through |
| ledger | 475 records, all **before** it went `401` | the six-node soak coverage it satisfies is history, not liveness (§8) |
| IP / class | `172.16.100.50`, `xiao-s3-pps` | from `deploy/k8s/hear-drain.yaml:90`; class is the `board_profiles.DEFAULT_BOARD_CLASS` |

### 0.2 What this document corrects in the existing record

Four things the current docs say are no longer true at `62641da`, and each one changes a step.

1. **`flash.py --release` cannot be used on a board on USB.** `flash.py:344` dies with *"a release
   goes over the air; a board on USB is enrolled with enroll.py"*. §6 of the rollout sequence lists
   "enroll over USB" and "install a credentialed image" as steps 3 and 4; on this tree they are
   **one command** — `enroll.py … --release <tag>` writes the image *and then* the NVS record, in
   that order, in one session (`enroll.py:349–371`). Splitting them is not possible with these
   tools and attempting it wastes the visit.
2. **#230 is merged.** `78e4683` ("size the raw ring for a 2 MiB PSRAM part") is an ancestor of
   today's `main` and is **not** an ancestor of `v0.1.6`. The rollout sequence §7 still describes it
   as `[OPEN, CI green]`. The practical effect here is that the *next tag* is now cuttable on its
   own merits, which changes the §5 image choice from "wait for an open PR" to "an operator picks a
   date".
3. **`v0.1.6` predates the provenance work.** #228 merged at `a7060fb`, after the `v0.1.6` tag at
   `ffd2054`. `v0.1.6` publishes no `release-sbom.cdx.json` and no
   `release-provenance.intoto.jsonl`. An `enroll.py --release v0.1.6` therefore verifies against
   `SHA256SUMS` on the legacy path and **`--verify-signature` has nothing to verify**. Any tag cut
   from today's `main` is built by the post-#228 `release.yml` and is the first that can be
   installed with a cryptographic check (§5).
4. **ADR 0006's "no token exists yet" is stale.** Five nodes were provisioned on 2026-09-15 before
   the decision was taken. `rankine` is the *only* unprovisioned node left, which makes it the one
   node whose enrollment cannot make anything worse — and the one node whose enrollment commits it
   to whichever shape D1 has (or has not) settled on. §4.1 is the refusal that follows.

---

## 1. Scope

**In scope:** pulling `rankine`'s data before anything is written to it; the custody handoff that
puts an operator at the node without moving a secret; the USB/serial recovery itself; the
acceptance evidence that proves enrollment took; the functional validation of 48 kHz acquisition,
heartbeat, admin auth and streaming afterwards; the conditions under which the work stops.

**Out of scope, deliberately:**

* **Deciding to do it.** Recovering `rankine` mid-soak is a fleet-composition change. §8 states what
  it costs and why it needs its own decision. This document does not take that decision, does not
  imply it has been taken, and does not authorise the work.
* **Cluster actions.** `kubectl create job --from=cronjob/hear-drain …` is the intended way to
  flush an SD tail on demand and it **alters the cluster**. It is named in §3.3 so that an operator
  who holds that authority knows the option exists; it is not part of this procedure.
* **Cutting a tag.** §5 states which tag is installable and what each choice costs. Cutting one is
  §9.2 of the rollout sequence and a separate act.
* **`gold`'s ring, `kasami`'s bus scan, D1 itself.** Neighbouring lanes. They are referenced where
  they gate a step and otherwise left alone.

---

## 2. Why USB, and why there is no network path

The reason is layered, and every layer has to fail before USB is unavoidable — all of them do.

1. **`/update` refuses.** The endpoint-auth change made `/update`, `/reboot`, `/format` and
   `/gate` POST require `X-Hear-Auth`. `rankine` holds an admin token whose value is not in the
   operator's custody, so every privileged route refuses everyone, `flash.py` included.
2. **The tokenless recovery valve does not help here.** `/update` is left open **only when the node
   has no admin token at all** (`docs/ota-release-credentials.md` §Mechanism). That valve was added
   *because of* `rankine` and arrived after it. A node that has a token nobody can present is
   precisely the state the valve does not cover.
3. **NVS is not network-writable.** The `PROV` record is read **only from the USB CDC port**
   (`enroll.py:exchange()` → `PROV?`/`PROV`/`PROV OK` over `serial.Serial`). There is no HTTP route
   that writes it, by design. Credentials reach a node over a cable in front of an operator or not
   at all.
4. **OTA writes the app slot only.** Even if `/update` were open, an OTA cannot write NVS; a
   credential-free release image installed over the air on an unenrolled node produces a node that
   boots and pushes `401` — which is the condition being fixed, reproduced.

So: `rankine` needs a **physical visit with a USB cable**. Nothing in this document, and nothing in
the tree, shortens that. Every OTA attempt against it is a no-op that burns rollout attention, and
it stays excluded from every OTA step of every rollout until this closes.

---

## 3. Data pull, before anything is written

**Rule: nothing is written to `rankine` until this section has produced a receipt.**

### 3.0 What survives and what does not

| Data | Survives the USB recovery? | Note |
|---|---|---|
| SD card contents | **Yes.** `arduino-cli upload` writes flash, not the card | but see the `dets.csv` roll below — surviving is not the same as findable |
| NVS `hear_prov` record | rewritten, deliberately | that is the point of the visit |
| PSRAM raw ring (80 s) | **No** | volatile; a reboot loses it. Never a backup target; expected loss |
| In-RAM detection ring | **No** | same |
| `dets.csv` | survives, but **rolls to `dets-prev.csv` on reflash** | `tools/hear_drain.py:16`. Whichever of the pair was not fetched this run is the one that rolls off next — **always fetch both** |
| `scene-YYYYMMDD.csv` family | survives | names are **discovered** from `/ls`, not assumed (`hear_drain.py:137`); `scene.csv`/`scene-prev.csv` are the old-firmware fallback only |
| `health.csv` | survives | tailed from a watermark, not fetched whole (`hear_drain.py:181`) |
| Clips (`/clips/*.wav`) | survive until the node's own eviction takes them | fetched sequentially, budget-capped, indexed in `clips/index.jsonl` so a clip is never re-asked and a destroyed one never re-probed |

The eviction is the clock that matters: on 2026-09-09 the fleet had written 625 clips and ~478 had
already been destroyed by the node's own 49-clip cache before anything left a node. A clip not
pulled before the visit may simply not be there after it.

### 3.1 Pre-recovery receipt (read-only, no writes)

Take this **first**, and keep it — §7's acceptance is a comparison against it, not against the
numbers in §0.1.

```sh
IP=172.16.100.50
curl -s "http://$IP/status"                  -o rankine-status-pre.json
curl -s "http://$IP/ota"                     -o rankine-ota-pre.json
curl -s "http://$IP/ls"                      -o rankine-ls-pre.json
curl -s "http://$IP/audio"                   -o rankine-audio-pre.json     # bare: "what is retrievable?"
curl -s "http://$IP/log"    | tail -n 400    >  rankine-log-pre.txt
curl -s "http://$IP/detections"              -o rankine-detections-pre.json
```

Record from them, explicitly, because each is a later comparison:

* `node`, `fw`, `prov.*`, and **whether an `auth` block exists at all**;
* `raw.span_s` (expected `80.0` — this is the regression line), `sys.heap_min`, `sys.loop_max_ms`;
* `selftest.mic_state`, `mic_stats` if present;
* `time.state`, `time.valid`, `discontinuity_flags`;
* `/ota`: `running` partition **and its address**, `boot_try`, `healthy`, `img_state`;
* `/ls`: every file name and size — this is the manifest the pull is checked against;
* the last timestamp `rankine` appears in the bridge outbox (475 records as last read, all
  pre-`401`), read from an existing soak snapshot rather than by touching the cluster.

**No authenticated request appears anywhere in this section, and none is needed.**

### 3.2 The pull

Use the existing drain path. It archives raw bytes before parsing them
(`<pool>/raw/rankine/<utc>-<file>`), dedupes, discovers dated scene partitions, fetches both
detection files, and checks identity from `/status` before ingesting anything it served — a
swapped DHCP lease would otherwise file another node's detections under `rankine`.

```sh
python3 tools/hear_drain.py \
  --pool ~/hear-pool \
  --node rankine=172.16.100.50 \
  --json | tee rankine-drain-prerecovery.json
```

Then, from the report and the pool:

* `fetched` counts per file, and **`dets-prev.csv` absent is normal** — it exists only after a roll
  (`hear_drain.py:550`);
* clips: `fetched` / `destroyed` / `deferred` / `lost`, each refusal counted by reason. **A non-zero
  `deferred` is not a failure but it is unfinished business**: re-run until it settles, or record
  the number in the receipt and accept the loss explicitly;
* `0 lost` on the `cursor-v1` path;
* the raw archive exists under `~/hear-pool/raw/rankine/` and its byte counts agree with the
  `/ls` sizes from §3.1.

Two operational cautions, both measured behaviours of the node:

* **`/sd` is effectively single-client.** A second client during a large `/sd` transfer is refused
  outright (`hear_drain.py:653`). Do not run this by hand inside the scheduled `hear-drain`
  CronJob's window; pick a gap, or let the scheduled run do the work.
* **`200` is not proof of a file.** `/sd?file=/clips` answers `200` with a 0-byte body
  (`hear_drain.py:1127`). Judge the pull by bytes and by the `/ls` manifest, never by status code.

### 3.3 The cluster-side alternative (named, not performed)

An operator who holds cluster authority may prefer to flush the tail on demand with
`kubectl -n dama create job --from=cronjob/hear-drain hear-drain-prerecovery-rankine`. **That
alters the cluster and is out of scope here.** If it is not run, simply wait for the next scheduled
`hear-drain` to report `rankine ok` and take that run's report as the receipt.

### 3.4 Pull acceptance (all must hold before §6 begins)

| Check | Pass |
|---|---|
| `/ls` manifest from §3.1 | every listed file has a corresponding archived object in the pool |
| `dets.csv` **and** `dets-prev.csv` | both attempted; a missing `-prev` is recorded as normal, not as a gap |
| dated scene family | discovered from `/ls`, not assumed; every discovered partition fetched |
| clips | `deferred` = 0, or the residual number is written into the receipt and explicitly accepted |
| `lost` | 0 |
| identity | the drain's `/status` read-back names `rankine` (a mismatch refuses that node and the run continues without it — treat as a **stop**, not a warning) |
| receipt | §3.1 files and the drain JSON are stored together, outside the node, before the cable goes in |

---

## 4. Operator custody handoff

The visit needs an operator holding credentials. **The handoff moves none of them.**

### 4.1 Precondition the handoff cannot paper over

`rankine`'s enrollment is the act that gives it an admin token, so it inherits whatever shape D1
(ADR 0006) has. With today's tooling, `gen_secrets.read_push_config()` and `enroll.py` resolve a
**single bare `HEAR_ADMIN_TOKEN`** — so enrolling `rankine` now necessarily provisions the same
fleet-wide value the other five hold. That is option **B, reached by omission**, which is the exact
failure mode ADR 0006 exists to stop.

**Therefore:**

* If **B** is chosen (or is being deliberately continued and recorded), proceed.
* If **A** is chosen, the ADR 0006 tooling PR must merge **before** this visit, or the visit buys a
  node that must be re-enrolled later. `rankine` is the one node where A costs nothing extra — it
  has to be visited regardless — but only if the tooling exists when the operator is standing there.
* If D1 is **undecided**, that is a **no-go** for enrollment (§7.1). The data pull in §3 is
  unaffected and may proceed on its own; it depends on no decision.

### 4.2 What is handed over

Non-secret, every item. This is the whole packet:

| Item | Form |
|---|---|
| Node identity | `rankine`, class `xiao-s3-pps`, `172.16.100.50` |
| Pre-recovery receipt | the §3.1 files and the §3.2 drain report, by path |
| Image decision | the tag chosen in §5, and whether `--verify-signature` is required for it |
| D1 status | "A, tooling merged at `<sha>`" / "B, ratified on `<date>`" — a state, not a value |
| Custody entry **name** | `hear-admin/rankine` (A) or `hear-admin/fleet` (B) — the **name only** |
| Expected `/status` shape afterwards | the §7 table |
| Stop conditions | §9, in full |
| Return artefacts | the §7 and §8 evidence, the post-recovery `/status`, and the flash window timestamps |

### 4.3 What is never handed over, written down, or transmitted

* Any token value: push, admin, or Wi-Fi PSK. Not in a ticket, a chat message, a PR, a commit, a
  CI log, a screenshot, or an agent transcript. An accidental disclosure is a rotation trigger
  (ADR 0006 §Logging and redaction) and the rotation is not optional.
* The contents of `~/.hear_push` (mode `600`, operator machine only, never the authority, never
  synchronised) or `~/.wifi`.
* The `PROV` line bytes. It carries the values hex-encoded with a CRC — **that is framing, not
  protection**. Enroll only on a machine and a cable the operator controls.
* A token as a command-line argument or a query string. `flash.py` sends it as a header
  (`X-Hear-Auth`) specifically so it cannot land in shell history or `ps`. The firmware also
  accepts it as a query argument (`tests/test_firmware_admin_auth.py:92`) — **do not use that
  form**; it lands in logs.

⚠️ **The one disclosure surface in the tooling itself.** Without `--ap-pass` or `--no-ap-pass`,
`enroll.py` *generates a fallback-AP password and prints it once* (`enroll.py:319`, "record this —
it is not shown again"). That value is a real secret and it appears on the operator's terminal.
Handle it one of three ways, chosen **before** the visit:

* `--no-ap-pass` — the node derives one from its own MAC. Unique per node, not operator-chosen,
  not secret-strength. **The default recommendation here**, because it creates nothing to transport.
* Let it generate — then it goes **straight into the password manager** and nowhere else. Never
  into the receipt, the PR, the ticket, or a transcript.
* `--ap-pass <value>` — **avoid**: a value on the command line is shell history and `ps`, which is
  what ADR 0006 §Decision item 4 forbids for exactly this reason.

### 4.4 Handoff checklist

Sign-off fields, all non-secret: pull receipt stored ☐ · D1 state recorded ☐ · tag chosen and
verified offline ☐ · custody entry name agreed ☐ · AP-password handling chosen ☐ · stop conditions
read ☐ · soak decision recorded as taken-or-deferred (§8) ☐.

---

## 5. Which image goes on

`rankine` is class `xiao-s3-pps`, absent from `board_profiles.NODE_PSRAM_MODES`, so it takes the
class default **octal** and the release stem `hear_node-xiao-s3-pps`.
`board_profiles.release_variant_refusal()` must return empty for it; if it does not, **stop** — the
board is not what the table says it is, and that is a scan, not a flash.

| Option | What it buys | What it costs |
|---|---|---|
| **`v0.1.6`** | closes the split-build page immediately; the same tag the other five run; installable and verifiable against its own manifest | **no SBOM, no attestation** (§0.2 item 3) — `--verify-signature` has nothing to check; and `rankine` must be flashed again (OTA, no visit) when the next tag lands |
| **The next tag** | first release built by the post-#228 `release.yml`: `release-sbom.cdx.json` + keyless SLSA `release-provenance.intoto.jsonl`, both covered by `SHA256SUMS`; carries #230; `rankine` is flashed once | the tag does not exist yet; the visit waits on a cut that is somebody's decision, and the split page stays open until then |

**The brief's standing constraint:** the next tag going onto `rankine` should use the provenance
workflow. Read strictly, that favours waiting for the next tag. Read against the split page it also
allows `v0.1.6` now on the explicit understanding that the *next* tag `rankine` receives — the one
after this recovery — is a provenance tag installed with `--verify-signature`. **Either reading is
defensible and the choice is an operator's** (rollout sequence §15.3). This document does not make
it; it refuses only the third option, which is installing a provenance-capable tag *without*
checking it.

Whichever is chosen, verify it **offline, before the visit**, with the release downloaded and no
node present:

```sh
python3 firmware/hear_node/release_manifest.py verify --dist dist --tag <tag> --source-root .
# next tag only, once SBOM and attestation exist:
python3 firmware/hear_node/release_manifest.py verify --dist dist --tag <tag> --attestation
python3 firmware/hear_node/release_sbom.py       verify --dist dist --tag <tag>
gh attestation verify hear_node-xiao-s3-pps-<tag>.bin \
  --repo rjmendez/dama-hear \
  --signer-workflow rjmendez/dama-hear/.github/workflows/release.yml \
  --bundle release-provenance.intoto.jsonl
```

⚠️ **A check that could not run is never a check that passed.** `gh` missing is an error, not a
skip. `enroll.py` without `--verify-signature` prints `signature NOT checked` — that string is
accurate and must be read, not glossed.

---

## 6. The USB recovery procedure

### 6.1 Bench prerequisites

* `arduino-cli` with the esp32 core (the same toolchain `flash.py` uses) and `pyserial`.
* `~/.wifi` and `~/.hear_push` present on the operator machine, mode `600`.
* `gh` on `PATH` if the chosen tag is a provenance tag (§5).
* The chosen release verified offline (§5) **before** the cable goes in.
* The §3 pull receipt in hand.
* **One node at a time.** Two concurrent `enroll.py` sessions cannot be told apart in a `PROV`
  failure, and a half-written record reboots the node.

### 6.2 Getting the board into the bootloader

`rankine` is running this firmware, so the upload can reset it into its bootloader over the USB
serial line by itself. A board that is wedged, or running something else, has to be put there by
hand: **hold BOOT while plugging it in**.

Under WSL2 the board re-enumerates during the upload and on every reboot, so attach it with
auto-reattach or the port vanishes mid-session:

```sh
usbipd attach --wsl --busid <id> --auto-attach
```

### 6.3 The one command

```sh
python3 firmware/hear_node/enroll.py rankine /dev/ttyACM0 \
  --class xiao-s3-pps \
  --release <tag> \
  --no-ap-pass \
  [--verify-signature]        # provenance tags only; see §5
```

Variant: if the board already runs a **secret-free release image** and only the record is missing,
`--no-flash` writes the record alone and touches no binary. On the last measured state `rankine`
runs `v0.1.5`, which *is* a release image, so `--no-flash` is a legitimate minimal-change option —
it fixes the credentials without changing the firmware, and leaves the split page open. Choosing it
is the §5 decision in a third form; record which was used.

**What the command does, in order** (each step is a refusal point, not a warning):

1. Reads `~/.wifi` and `~/.hear_push`, builds the `PROV` line, prints a **masked** summary —
   `push=yes|no admin=yes|no`, never a value.
2. Resolves the FQBN from the **node**, not just the class, and refuses a release whose variant does
   not match the board (`release_variant_refusal`).
3. Verifies the release: revocation list → manifest hashes and `image_class: unprovisioned` →
   SBOM if declared → attestation if declared → signature only with `--verify-signature`.
   **Declared-and-missing is a refusal**, overridable only by `--allow-unattested`, which then says
   so in its own output.
4. `arduino-cli upload` writes the image.
5. `PROV?` handshake over CDC; prints the *before* state (`src`, `node`, `nets`, `fw`).
6. Sends the hex-encoded, CRC'd `PROV` record in 64-byte chunks; requires `PROV OK` within 20 s.
   **`PROV ERR` is a stop** — §9.
7. Waits for the node to reboot **on its NVS record** and join Wi-Fi; dies if it comes back as its
   own access point (joined no network).
8. Reads live `/status` and runs `deploy_gate.status_reasons()` plus
   `deploy_gate.auth_reasons(require_nvs_credentials=True)`. Any reason is a failure and the
   command says which.

**Never** pass `--allow-admin-lockout` (that is `flash.py`'s escape hatch and it exists to be
refused during a rollout). **Never** re-run a failed enrollment blind; read the error first.

---

## 7. Post-recovery acceptance

### 7.1 Enrollment acceptance — entirely from unauthenticated reads

None of these discloses, echoes or confirms a token value. **That is the whole verification
surface, and it is sufficient.**

| Check | Field | Pass |
|---|---|---|
| Identity | `/status` `node` | `rankine` |
| Image | `/status` `fw` | the tag chosen in §5, on the `xiao-s3-pps` variant |
| Record loaded | `prov.loaded` | `true` |
| Push credential | `auth.push.configured` / `.src` | `true` / `"nvs"` |
| Admin credential | `auth.admin.configured` / `.src` | `true` / `"nvs"` |
| Recovery valve closed | `auth.admin.ota_recovery_open` | **`false`** — `true` means the node is OTA-open to the LAN and must not be left that way |
| Backend accepts it | `auth.push.last_code` after one heartbeat | 2xx (the fleet reads `202`) |
| Cache | the node's Redis key | present, with a live TTL |
| Ledger | the node's `device_id` count in the bridge outbox | **increasing between two snapshots** — presence alone is history (§8) |

### 7.2 48 kHz acquisition

⚠️ **The trap, and it is easy to fail into.** `/status` `i2s.nominal_hz` is `16000` and the
`X-Audio-Fs-Hz` response header is also ≈16 kHz — both are the **post-decimator scene lane**
(`DECIM` = 3), not the acquisition rate. The **WAV header inside the body** is written at
`fs_timebase() × DECIM` (`hear_node.ino`, `wav_header(hdr, nout * DECIM * 2, lrint(fsu * DECIM))`)
and is the acquisition rate. **Read the RIFF header, not the JSON field, not the response header.**

```sh
IP=172.16.100.50
curl -s "http://$IP/audio" -o audio-window.json          # ring, span_s, addressable, from/to utc
FROM=$(python3 -c "import json;d=json.load(open('audio-window.json'));print(d['from_utc_us']+3000000)")
curl -sD audio-headers.txt "http://$IP/audio?from=$FROM&dur=10" -o rankine-post.wav
python3 -c "import wave;w=wave.open('rankine-post.wav');print(w.getframerate(),w.getnchannels(),w.getnframes())"
```

| Check | Pass |
|---|---|
| bare `/audio` | `ring: true`, `addressable: true`, `span_s` ≈ **80.0** (not lower than §3.1) |
| WAV `getframerate()` | **48000** (± the measured timebase) |
| `X-Audio-Clipped` | `none` for a window taken inside the reported range |
| `X-Audio-Samples` | consistent with `Content-Length` = `44 + samples × DECIM × 2` |
| `/status` `i2s.measured_hz` | ≈16 000 on the scene lane with a sane `ppm`; this is *not* the 48 kHz claim |
| published manifest | `capture_profile.fs_acquisition_hz: 48000` for `hear_node-xiao-s3-pps` in the tag that was installed |

### 7.3 Heartbeat

| Check | Pass |
|---|---|
| `auth.push.configured` / `.src` | `true` / `"nvs"` |
| `auth.push.last_code` | 2xx within one heartbeat cycle. **`401` is a loud stop, not a retry — it is the `rankine` signature** (§9) |
| Redis node key | reappears with a live TTL |
| Bridge outbox | `rankine`'s `device_id` count **increases** between two snapshots taken minutes apart |
| Clock | `time.valid: true`; `LOCKED` (its last measured state) or `HOLDOVER` with `discontinuity_flags` explained |

### 7.4 Admin auth — proved without sending the token

Prove it **fail-closed**. Do not prove it by succeeding.

```sh
# offline, no node, no secret:
python3 -m pytest tests/test_firmware_admin_auth.py -q

# live, unauthenticated, and inert:
curl -si -X POST "http://$IP/gate"   | head -n 1     # expect 401
curl -s     "http://$IP/gate"        | head -c 200   # expect 200 JSON: GET is unauthenticated by design
```

`HEAR_REQUIRE_AUTH()` runs **before** `/gate` POST looks at any argument, so an argless
unauthenticated POST returns `401` and changes nothing — that is why `/gate` is the safe probe.

⚠️ **Do not probe admin auth with `/reboot`, `/format` or `/update`.** A `401` from them would be
the same evidence, and a success would be a reboot, a wiped card, or a flash. There is no reason to
take that risk when `/gate` answers the same question.
⚠️ **Do not send the token to check that it works.** `auth.admin.configured: true`,
`src: "nvs"`, `ota_recovery_open: false` is the positive proof, and it discloses nothing.

### 7.5 Streaming and the drain path

| Layer | Check | Pass |
|---|---|---|
| `/audio` windowed | a 10 s window inside the reported range | `200 audio/wav`, `Content-Length` = `44 + samples × DECIM × 2`, body length matches the header, `X-Audio-Clipped: none` |
| `/audio` out of range | a window the ring does not hold | `416` with `X-Audio-Clipped: all` — a refusal, never a silently shortened file |
| `/sd` | `hear_drain.py` post-recovery run | files fetched, byte counts non-zero, `0 lost` on `cursor-v1` |
| `/sd` concurrency | one client at a time | a second client during a large transfer is refused; do not overlap with the CronJob window |
| `dets.csv` roll | after the reflash | `dets.csv` has rolled to `dets-prev.csv`; the post-recovery drain fetches **both** and the pre-recovery archive still holds the pre-roll bytes |
| Fleet | `hear-drain-check` | exit **0** — achievable only once `rankine` is back on the fleet build |

⚠️ If `hear-drain-check` still exits 1 after `rankine` returns on the fleet tag, the split was
**not** the only cause. Stop and re-diagnose; do not silence the assertion and do not add `rankine`
to an ignore list. `tools/fleet.py --require-one-build` treats an unreachable node as
*inconclusive* rather than excluding it, and that behaviour must not be "fixed".

---

## 8. Consequence for the Phase 2 soak baseline — **an open decision, not a step**

**This is the part of the package that is not a procedure.**

### 8.1 What the soak currently believes about `rankine`

`bridge_soak_evidence.py:node_coverage()` reads `select device_id, count(*) from durable_records
group by 1` over the **whole ledger**. `EXPECTED_NODES` includes `rankine`, and `rankine` has 475
records from before it went `401`. So `all_nodes_covered` reports six-node coverage **from
history**. `coverage_not_regressed` compares *presence*, not freshness, so a silent node keeps
passing until 30-day retention prunes it.

The T0 prime at **2026-09-15T18:26:22Z** passed all 14 criteria on that basis, and
`phase2-soak-day1-review` already records a **five-node coverage expectation while `rankine` is
USB-locked**. The honest statement is that six-node coverage today is an **unearned pass**.

### 8.2 Why recovering `rankine` is a fleet-composition change

Recovering it does not merely fix a node. It changes the population the baseline was measured over:

* **`record_growth`** grades total volume at T+24h against ~52 000 records/day ±20 %, relative to a
  baseline captured with **five** nodes publishing. A sixth node resuming pushes volume across that
  boundary. The growth number either side of the recovery is **not comparable**, and a band that is
  ±20 % is not wide enough to absorb a node.
* **`all_nodes_covered`** flips from an unearned pass to an earned one — an improvement in meaning
  with no change in verdict, which is exactly the kind of silent semantic shift that makes a later
  reader trust the wrong window.
* **`ledger_fresh`** is fleet-level and survives one node's flash window, but the flash window is a
  gap in `rankine`'s contribution and must be **recorded in the milestone notes** so a dip has an
  explanation attached rather than an investigation.
* Nothing in this recovery touches the bridge, so `pod_restarts_zero`, `rollout_settled` and
  `plaintext_mqtt_preserved` are unaffected **by the node work itself**.

### 8.3 The decision this document does not take

> **Recovering `rankine` during the Phase 2 soak window is a fleet-composition change to a
> running 14-day evidence clock. It requires its own explicit, recorded operator decision. This
> document does not take it, does not imply it has been taken, and does not authorise the work.**

The options, stated so the decision can be made rather than discovered:

| Option | Effect on the soak | Cost |
|---|---|---|
| **Defer until after T+14d** | baseline untouched; the 14-day window stays comparable end to end | the split-build page stays open for the rest of the window, and `rankine`'s SD tail keeps ageing against clip eviction |
| **Recover and re-baseline T0** | composition change is absorbed by a new, honest baseline over six live nodes | the 14-day clock **restarts** |
| **Recover mid-window, keep the clock** | the split closes now; coverage becomes earned | `record_growth` across the boundary is **void** and must be annotated as such in the milestone notes, with the exact flash window recorded |

Independently of which is chosen, two sequencing rules hold:

1. **Never inside a milestone sample.** Take T+24h / T+7d / T+14d **before** the recovery on that
   day, never during. A sample taken mid-flash under-counts, and the baseline comparison is the
   whole point.
2. **Do not claim six-node coverage until it is observed increasing**, not merely present (§7.1).

---

## 9. Stop, no-go and rollback conditions

### 9.1 No-go before anything is written

| Trigger | Why |
|---|---|
| §3.4 pull acceptance incomplete | a reflash that loses unpulled clips loses them permanently; the pool is the only other copy |
| D1 undecided, or **A** chosen and the ADR 0006 tooling PR not merged | the enrollment would provision the fleet-wide shape by omission (§4.1) |
| The §8 soak decision not recorded as taken-or-deferred | the recovery would be an unrecorded change to a running evidence clock |
| Chosen tag fails the offline `release_manifest.py verify` | do not flash; record in `release_revocations.json` via a merged PR, fix, and cut a **new** tag — never re-cut one |
| Chosen tag is in `release_revocations.json` | `enroll.py` refuses before the first HTTP request; do not work around it |
| `release_variant_refusal()` non-empty for `rankine` | the board is not what the table says; scan it, do not force either variant |
| `gh` unavailable while `--verify-signature` was required | a check that could not run is not a check that passed |
| A second `enroll.py` session is open on any node | a `PROV` failure would be unattributable |

### 9.2 Stop during the session

| Trigger | Action |
|---|---|
| `PROV ERR this image has compiled-in credentials` | the board is running a locally-built image, not a release image. Flash a release image first (`--release`), then enroll. Do not retry `--no-flash` |
| any other `PROV ERR`, or no `PROV OK` within 20 s | stop and read it. Do not re-send blind — a half-written record reboots the node |
| "rebooted on its NVS record but joined no network" | the node is its own access point. Fix `~/.wifi` and re-enroll **before leaving the site**; a node in AP mode is unreachable from the LAN |
| `arduino-cli upload` fails | the board is not in its bootloader — hold BOOT while plugging in; under WSL2 confirm `usbipd --auto-attach` is still holding the port |
| the live readiness gate fails after enrollment | `enroll.py` names the failing reasons; fix at the node. Leaving with a failing gate is how a second visit gets booked |

### 9.3 Stop after the session

| Trigger | Gate | Action |
|---|---|---|
| `auth.push.last_code` = `401` | **fleet stop** | this is the `rankine` signature; treat as fleet-wide until disproved |
| `auth.admin.ota_recovery_open` = `true` | node untrusted | the node is OTA-open to the LAN; re-enroll before leaving |
| `raw.span_s` < the §3.1 value (80.0) | **stop** | a ring regression on a node that held a full ring contradicts the tier design; the premise is wrong |
| `sys.heap_min` collapses (compare against 78 560 B) or `loop_max_ms` rises materially above 1 140 | node no-go | do not accept the node; investigate before it is counted as recovered |
| `selftest.mic_state` not `normal`/`quiet` | separate investigation | **do not pre-judge it as a hardware fault** — mis-reading a settling probe as a dead microphone is the mistake `docs/fleet-hardware-remediation-2026-09-15.md` exists to undo |
| `hear-drain-check` still exit 1 | **fleet stop** | the split was not the only cause; re-diagnose. Do not silence it, do not add an ignore entry |
| unreachable after the flash | wait ~6 min | let three unhealthy boots run (3 × ≥90 s); then read `/ota` and `/log`. Intervening early destroys the boot-guard evidence and can strand the node. Still unreachable ⇒ USB again, at the node |

### 9.4 Rollback, stated honestly

**There is no "restore `rankine` to v0.1.5" rollback, and offering one would be a lie.** Its
pre-state is *worse* than any outcome of this procedure: no fleet credential, `401` pushes,
privileged routes locked, excluded from telemetry. Nothing is being traded away.

What that means concretely:

* **Never revert a provisioned node to a tokenless asset** (`v0.1.2`–`v0.1.5`). That is precisely
  how `rankine` was lost, and repeating it on a now-enrolled `rankine` re-creates the original
  incident.
* The real rollback is **abandonment, recorded**: leave `rankine` excluded, keep the exclusion
  visible, and keep `hear-drain-check` red. *A deliberately excluded node is a recorded exception,
  not an invisible one.*
* If the enrollment took but the image is wrong, the node is now **OTA-reachable** and the fix is
  `flash.py rankine 172.16.100.50 --release <tag>` from the bench — no second visit. That is the
  asymmetry worth noticing: the visit's real product is the NVS record, not the image.
* The on-node boot guard reverts a panicking image to its previous slot by itself. `/status` `fw`
  is the only field that separates "flashed" from "flashed, panicked and rolled back"; identity
  alone does not.

---

## 10. Open items this package hands back

1. **The §8 soak decision.** Defer / re-baseline / annotate. Owner: operator. Blocking for the
   recovery, not for the data pull.
2. **D1 — admin token scope** (ADR 0006). Blocking for enrollment (§4.1). Now a *migration*
   decision: five nodes are already provisioned.
3. **`v0.1.6` or the next tag** (§5). Blocking for the image, not for the visit's timing.
4. **Whether to record the provenance gap in the `v0.1.6` release notes.** Not this document's to
   make; noted because `rankine` is the last node that could receive `v0.1.6` and would inherit it.

---

## 11. Provenance of this document

Written in a fresh clone of `rjmendez/dama-hear` at `origin/main` `62641da`
(`v0.1.6-23-g62641da`). In-tree claims were verified by reading, at that commit:
`firmware/hear_node/enroll.py`, `flash.py`, `board_profiles.py`, `release_manifest.py`,
`hear_node.ino` (`/audio`, `/gate`, `/sd`, `/ls`, `/update`, `/reboot` handlers),
`tools/hear_drain.py`, `tools/bridge_soak_evidence.py`, `tests/test_firmware_admin_auth.py`,
`deploy/k8s/hear-drain.yaml`, `docs/ota-release-credentials.md`, `docs/release-provenance.md`,
`docs/decisions/0006-admin-token-provisioning-policy.md`,
`docs/fleet-hardware-remediation-2026-09-15.md` and
`docs/fleet-release-rollout-sequence.md`; plus `git merge-base --is-ancestor` for `78e4683` (#230)
against `HEAD` and against `v0.1.6`. The `fix/survey-rankine` branch was checked and is an
**ancestor of `main`** — it holds no unmerged Rankine recovery content and nothing here rests on it.

**Live node state was not re-measured.** `rankine` was not contacted, probed, flashed, rebooted or
powered. No release was cut, re-cut or edited. Nothing was applied, patched, created, restarted or
deleted in the cluster. No credential was generated, requested, read back, printed, transported or
referenced.
