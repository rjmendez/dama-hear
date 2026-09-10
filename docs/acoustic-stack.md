# The acoustic classification stack

**Status: design.** Nothing in section 5 is built. Every number is either measured — with the
date and the command — or marked ASSUMPTION or UNMEASURED. Where the design was revised because
a critique falsified a premise, the falsified premise is kept and struck through rather than
quietly removed, because the reasoning that produced it will otherwise be produced again.

Audience: an operator building this over weeks, one stage at a time, who needs to know what
ships first, what each stage costs in node hearing and operator hours, what it refuses to claim,
and which stages cannot start until a phone release, an RTK survey, or a microphone that does
not exist yet arrives.

---

## 0. Three live failures found while writing this

Both were found by running the design's own proposed instruments against the fleet at
**2026-09-09 05:20 UTC**. Both are silent. Both report `ok`. They are at the top of this document
because the first stage of the plan is to fix them, not to add anything.

### 0.1 ⚠️THE SCENE LANE HAS BEEN DEAD FOR 3.27 HOURS AND THE DRAIN REPORTS `ok`

The nodes rebooted onto `fw 7f84d29` at ~04:10 UTC (uptime 4271 / 4225 / 4177 s on
nyquist / mach / rankine at 05:20:46 UTC). That firmware writes the scene descriptor to a
**dated** file — commit `4dbfe26`, "daily files with the oldest rolled off". The drain does not
know:

    tools/hear_drain.py:112   SCENE_FILES = ("scene.csv", "scene-prev.csv")

So every run since the reboot has fetched the **frozen legacy** `scene.csv` and ingested nothing:

| | `scene.csv` on the card | `scene-20260909.csv` on the card | drain verdict |
|---|---|---|---|
| nyquist | 20,751,993 B, **unchanged** since the 04:39 survey | 2,003,040 B (was 1,560,300 B), **growing** | `ok +0 scene`, 8467 rows → 8466 dup |
| mach | 20,270,034 B, **unchanged** | 2,220,166 B, growing | `ok +0 scene`, 8605 rows → 8604 dup |
| rankine | **absent** | 1,605,608 B, growing | `ok +0 scene`, "`scene.csv absent`" |

Pool state: 82,225 scene rows, `by_node` = `{mach: 37146, nyquist: 45079}` — **rankine has never
contributed a single scene row** — and the newest row is `1788919477.9` =
**2026-09-09T02:04:37.9Z, 3.27 h stale** at a wall clock of 05:20:46Z. The nodes have been
writing a row every 1.024 s that whole time.

The cost is paid twice. The scene descriptor is the corpus (`hear_drain.py:109`: "*`dets.csv`
only exists where the impulse gate fired, so a pool built from it alone can hold nothing but
impulsive events*"), so the highest-volume product in the fleet is being discarded — **and** the
drain is still spending a 2 MB `/sd` fetch per node per run to fetch it, on the endpoint that
never calls `audio_pump()`, which is roughly 100 % audio loss for the duration of the transfer.
It is spending node deafness to collect duplicates of a file that stopped changing.

`hear-drain-check` is the closest thing to a catch and it does not catch it either — it reports
`rankine ok ... unfetched UNKNOWN ... (the node served no scene.csv to measure)`. UNKNOWN, not
failed. **A node that serves no scene file at all is indistinguishable from a healthy one.**

**Fix (S0.1).** Prefer the newest `scene-YYYYMMDD.csv` when present; keep `scene.csv` /
`scene-prev.csv` as the legacy path; and make "no scene file of any name" a **failure**, not an
UNKNOWN. This is a change to `tools/hear_drain.py` and its `--check` assertion, not to any
manifest.

### 0.2 THE RING-WALL-SPAN HEALTH CHECK IS FIRING RIGHT NOW ON TWO OF THREE NODES

Section 4 proposes ring wall span as the replacement for `drop_s`/`drop_samples`. It works, and
it is not quiet:

    ratio = (raw.to_utc_us - raw.from_utc_us) / (raw.cap_samples / fs)

| node | ratio | `drop_s` | `drop_samples` |
|---|---|---|---|
| nyquist | **1.09148** | 47 | 453,760 (28.4 s) |
| mach | **1.11629** | 42 | 386,816 (24.2 s) |
| rankine | 1.00224 | 24 | 89,088 (5.6 s) |

nyquist and mach have each lost roughly 9–12 % of the last four minutes of audio. rankine, on
the same firmware and the same AP, has lost 0.2 %. The obvious difference is that nyquist and
mach are the two nodes carrying a 20 MB legacy `scene.csv` that the drain re-fetches 2 MB of,
four times an hour, over `/sd`. That correlation is strong and the mechanism is read from source
(`/sd` has no `audio_pump()` in its write loop) — but ⚠️**this is INFERRED**, not established:
I did not instrument the drain pod against the ratio, and rankine also has a much smaller card
and no `scene.csv` to fetch at all, so two variables move together.

⚠️**The denominator is the trap.** `/status`'s `raw` block spans `cap_samples` (3,840,000 =
240.0 s nominal). `/audio` spans `addressable_samples` (3,584,000 = 224.0 s nominal, after the
16 s overwrite guard). Pairing `/audio`'s span with `cap_samples` gives 0.933 and **can never
reach 1.02**, which is a check that cannot fail. Paired correctly, `/audio` agrees with
`/status`: nyquist's `/audio` span was 245.958 s over 224.0 s = **1.0980** against `/status`'s
1.0915. Either endpoint is a valid instrument; **each must be divided by its own sample count.**
Any implementation must carry a self-test that a known-healthy node reads within ~0.003 of 1.000,
so a wrong-denominator implementation fails immediately instead of reading permanent green.

### 0.3 ⚠️THE CLIP LANE — 478 CLIPS DESTROYED, AND NOT ONE HAD EVER LEFT A NODE

> Operator page for the shipped pipeline, and the list of what it does NOT establish:
> **`docs/clip-pipeline.md`**. Read that before quoting anything out of `clips/tags.jsonl`.

Measured across the fleet on **2026-09-09**. Every node writes 4.0 s WAVs (1.0 s pre-trigger +
3.0 s post, 16 kHz 16-bit mono, 128,044 B) into `/clips` against a 6,291,456 B budget — exactly
49 files — and evicts oldest-by-name when it is full. nyquist wrote 274 and evicted ~225; mach
wrote 102 and evicted ~53; rankine wrote 249 and evicted ~200. **625 written, ~478 destroyed, 0
collected.** `tools/hear_drain.py` contained zero mentions of clips, and the `/ls` handler
hardcoded `SD.open("/")`, so nothing could even enumerate them.

Two things made the loss invisible rather than loud:

  `fetch_sd` reads a clip as ABSENT   it requires the body to start with `b"node"` or `b"utc_us"`.
                                      A WAV starts with `b"RIFF"`, so a present 128,044 B clip
                                      came back as "the node does not have it". Hence `fetch_clip`.
  200 is not proof of a file          `/sd?file=/clips` answers **200 with a 0-byte body**. The
                                      magic and the length are checked, not the status code.

Discovery ships through the `clip` column of `dets.csv`, not through `/ls?dir=`: **we may not
flash**, and `det_flush` refuses to write a detection's row until its clip has resolved, so a
name in `dets.csv` is a clip that already landed. `/ls?dir=` is compile-only and becomes a second
candidate source at the next reflash — it closes exactly one case, a clip that outlives the dets
file that named it.

⚠️**THE ORDERING CONSTRAINT.** *Draining clips must precede any budget increase. A bigger
`CLIP_BUDGET_B` without collection just evicts faster.* Today the card holds 49 and the node
destroys the 50th; raising the budget to 196 without a drain does not save a single clip. It
changes *which* 478 are destroyed and how long each survives before it is destroyed anyway, while
consuming SD space and lengthening `clip_evict_worse_than`'s directory scan. **The node is not
the archive; the pool is.** Until something collects, every byte of budget is a byte of delay
before the same loss. The permitted sequence: land collection → observe ≥ 7 days of
`clips_deferred_by_cap == 0` and `clips_cap_hit == false` across all three nodes, read off the
heartbeat *ring* and not off one run → only then raise `CLIP_BUDGET_B` **and**
`--clip-max-per-node` in the same change, because the cap and the budget are one number in two
places. `activeDeadlineSeconds` goes in **with** the clip lane for the same reason: the clip
margin *is* the schedule margin.

Fetch is **strictly sequential**. The ESP32 serves one client at a time and refuses the rest
rather than queueing — `/ls` answers in 35–118 ms idle, degrades to 7.3 s during a large transfer,
and is refused outright mid-request — so a second CronJob or a thread pool would convert a slow
run into a refused one. Order is oldest-first by `(boot, sample)`, which is by **eviction risk**:
the flashed fleet evicts plain FIFO (prefix histogram over 370 live names is `{'ny': 370}` — no
node writes the `%02u-` priority prefix), so oldest-first is most-at-risk-first. `prio` is
recorded when the name carries it and is never read for ordering.

The index (`clips/index.jsonl`) is the durable record and the audio is a cache: `prune()` deletes
WAVs and never index lines. Each row is appended **as its clip resolves**, never buffered to the
end of the pass: `activeDeadlineSeconds: 780` makes a mid-pass kill a designed event, and a run
that kept the audio and lost the ledger let the next run's 404 write a false
`evicted_before_fetch` over bytes sitting on the PVC. A refusal line is written for **every** 404
and every bad body, so the census of what was destroyed is countable from the pool rather than
reconstructed by diffing, and `clips_seen == fetched + already_held + already_gone + gone +
probed_404 + refused + deferred_by_cap` is an assertion in `drain_clips`, not a hope. One 404 is
**not** an eviction: `night_node.ino`'s `/sd` answers 404 for any failed `SD.open`, descriptor
exhaustion included, so it takes `CL.CONFIRM_404` consecutive ones before the terminal row. A run that never reached the node reports
`clips_unknown` with its own reason — **never `clips_gone: 0`**.

### 0.4 THE TAG LANE — YAMNet over the collected clips, suspended until somebody listens

`tools/hear_tag.py` reads `clips/index.jsonl`, opens the WAVs on the PVC and appends
`clips/tags.jsonl`. It **touches no node** — the ordering constraint above is why: the ESP32
serves one client at a time and refuses the rest, so a second workload reaching a card converts
hear-drain's slow run into a refused one. `deploy/k8s/hear-tag.yaml` ships `suspend: true`.

**The model.** YAMNet as TFLite under `ai-edge-litert`, not under TensorFlow: bit-identical
scores at 12.1 ms/clip in 82 MB RSS from a 146 MB venv, against 14.3 ms in 903 MB from a 1.4 GB
venv, and the PVC is the binding constraint. All 64 of YAMNet's mel bins sit below 8 kHz, so the
16 kHz ceiling costs it nothing. **BirdNET is not built** — zero birds across nine real clips,
neotropical hypotheses at the confidence floor, 1 s of every 4 discarded, CC BY-NC-SA weights.
**PANNs/CNN14 is not built** — 32 kHz wanted, 17 % of its filterbank on guaranteed zeros, a
measured 14 % confidence loss on the same A/B, 311 ms and 1.5 GB RSS. The goal is **event
triage**, not species ID: YAMNet has `Bird`, `Owl`, `Hoot`, `Chirp` and stops.

⚠️**NORMALISATION IS THE WHOLE FAILURE MODE.** Measured 2026-09-09 on two clips pulled off
nyquist:

| clip | raw | RMS-normalised to −20 dBFS |
|---|---|---|
| `nyquist-db21acd5-1082421378` (−56.9 dBFS) | `Silence 0.406 \| Speech 0.183 \| Animal 0.147` | `Animal 0.307 \| Cricket 0.204 \| Speech 0.198` |
| `nyquist-db21acd5-1082530195` (−62.1 dBFS) | `Silence 0.723 \| Animal 0.055 \| Fowl 0.042` | `Animal 0.464 \| Wild animals 0.331 \| Bird 0.226` |

Clips are recorded at −49 to −62 dBFS, so this is the whole corpus. A pipeline that skips
`normalise()` **exits 0 forever and tags every clip Silence**.

⚠️**THE RATE IS ASSERTED, NEVER RESAMPLED.** YAMNet neither validates nor resamples its input, so
a wrong rate degrades silently. The header must be within ±64 Hz of 16 000 or the clip is
REFUSED into a counted bucket — mach shipped a whole boot headed 22 624 Hz, which is a condition
on the card today. A resampler would launder a firmware defect into plausible-looking tags.

⚠️**WEIGHTS ARE PINNED BY sha256 AND THE TOOL NEVER FETCHES THEM.** `yamnet.tflite`
(16 096 668 B, `141fba1c…`) and `yamnet_class_map.csv` (14 096 B, `cdf24d19…`, pinned to commit
`dfffd623`) live on the PVC at `/pool/models/yamnet`. The CronJob preamble fetches them once with
Python — not `wget` or `curl`, neither of which exists in `python:*-slim` — and
`hear_tag --verify-weights` runs **unconditionally and fatally** before any tagging. Hashing the
file is the point: the existing numpy guard tested only that a directory existed, which enforces
nothing. A mismatch exits **2**, distinct from "tagging is behind".

**What a row carries.** Every class above `SCORE_FLOOR` (a storage bound, *not* an operating
point), `max_unstored_score` so the discarded tail is a number rather than an absence, the
1024-d embedding (free from the same forward pass, and the only fixed-axis record that survives
`prune()`), both sample rates side by side, the model sha256, and `provenance: "model"` with
`claim.usable_as_training_label: false`.

⚠️**A TAG IS NOT A LABEL.** No scene- or sketch-based model may be trained on these. Doing so
would measure whether a 20-band descriptor can reconstruct what a full-fidelity model already
decided, which is not correctness — and this project has that exact failure on file: all 35 dama
ant models were trained on circular self-labels. `hear/tags.py` ships a **read-only**
`scene_overlap()` query and nothing more. It reports `basis: "utc"` for an anchored clip, and its
sample-counter fallback is off by default and marks itself `weak` when used, because neither
scene.csv nor dets.csv carries a boot id and `sample` restarts at 0 every boot.

**THE PHASE-3 GATE.** `hear-tag` stays suspended until all three hold: (1) ≥ 300 `stored` rows
across all three nodes over ≥ 3 consecutive days; (2) a human has **listened to ≥ 30 of them**,
including at least one from mach, and written what they heard into
`docs/clip-calibration-<date>.md`; (3) `--max-silence-frac` is set from **that measured
distribution**. Until (3), the check prints `silence REPORT … NOT GATED` and cannot fire. Three
models agreeing on "Dog" is corroboration, not ground truth, and nobody has yet heard one clip
this fleet recorded.

---

## 1. Three premise corrections

These reshape the architecture and are not editorial.

**1.1 There is no radio.** `grep -E 'lora|meshtastic|sx126|RadioLib|rf95|mqtt|PubSub|WiFiUDP|
HTTPClient'` across `firmware/night_node/`, `firmware/puc_node/`, `firmware/hear_poc/` and
`firmware/lib/hear_platform` returns a comment about the nRF52840 and `SPI.begin(SD_SCK, ...)` —
the SD card. A node **ships nothing**. It is a single-client `WebServer` that gets polled. The
"radio carries 172 B sketches, WiFi carries audio" split is real in `docs/uplink.md`,
`docs/architecture.md` and `hear/wire.py`, and exists nowhere on a node. `hear/wire.py`'s v2
frame and `hear/node/telemetry.py`'s 14 B frame have **no firmware producer**.

Consequence: `hear/backend/` (receive → decode → attribute → associate → solve → publish, 75
tests) decodes radio frames for a radio that does not exist. It is not the path to central
classification and must not be wired as one.

**1.2 Pull-audio and 48 kHz are on disjoint hardware.** From `hear/nodeclass.py`, enumerated
live:

| class | `fs_hz` | `mic_count` | `raw_retain_s` | note |
|---|---|---|---|---|
| `xiao-s3-pps` | 16000 | 1 | **240.0** | the only class with a ring |
| `xiao-s3-i2s` | 48000 | 1 | 80.0 | "Planned I2S variant. **Not built.**" |
| `puc-pps` / `puc-ntp` | 48000 | 2 | **0.0** | |
| `gotchi-phone` | 44100 | 1 | **0.0** | |

**Every byte the central stack can retrospectively pull today is 16 kHz, band-limited to 8 kHz.**
This is the binding constraint on central classification — bigger than model choice — and it is
what gates §5's stage S1.

**1.3 Pulling deafens the node and the node cannot see it.** Measured on rankine
(2026-09-09, transport survey), using ring wall span as the instrument:

| endpoint | bytes | wall | ring excess | claimed by `drop_samples` | under-report |
|---|---|---|---|---|---|
| `/sd` (scene tail) | 1,392,463 | 10.10 s | +11.1 s (~100 % lost) | 0.12 s | **92×** |
| `/perf?mb=2` | 2,097,152 | 16.03 s | +17.7 s (~100 %) | 0.70 s | **25×** |
| `/audio?dur=30` | 960,170 | 7.01 s | +1.7 s (~24 %) | 0.49 s | **3.5×** |

Mechanism, read from source: only the `/audio` handler calls `audio_pump()` between chunks
(`night_node.ino:2712-2721`); `/sd` (2181) and `/perf` (2217) do not. Even `/audio` caps
catch-up at `AUDIO_PUMP_MAX` = 6 blocks = 96 ms, the I2S DMA depth — past that "*the samples are
already gone*". So the loss fraction on `/audio` is **link-speed dependent by construction**:
24 % at rankine's 137 kB/s, 57.5 % measured on mach at ~71 kB/s effective.

The counter is structurally blind (`night_node.ino:2989-3040`): the audit differences the two
most recently **seen** PPS edges, so a stall spanning N > 1 edges is charged **one** second, and
if the surviving delta clears `0.97 × 16000` it is charged **zero**.

**Consequence for the whole design: the transport budget is denominated in seconds of node
deafness per hour, not in bytes.** Bandwidth is not the constraint — three parallel `/audio`
pulls measured 375.9 kB/s aggregate, near-linear, per-node rates unchanged from solo. The
constraint is one ESP32 core and its pull duty cycle.

---

## 2. The split

> **Every sensor emits a cheap, continuous, event- or interval-keyed descriptor. Nothing streams
> audio. The central stack pulls raw audio only retrospectively, only inside a ring window, only
> when a trigger has already decided it wants it. Sensors emit; oxalis serves.**

| tier | emits continuously | holds locally | central pulls on demand |
|---|---|---|---|
| xiao nodes (nyquist, mach, rankine) | 172 B sketch per gate event; 20×4 scene row every 1.024 s | 240 s PSRAM raw ring (3,840,000 samples, 7.68 MB); SD at 20.81 MB/day measured | `GET /audio?from=&dur=` → WAV. **224 s addressable**, **`max_dur_s` = 30** |
| hugbot | 172 B sketch on `dama/hugbot5000/acoustic_sketch` (fleet broker, mTLS) + per-board bearing cone on `audio_bearing` | 8 s ESP ring, PPS-anchored **on the Pi only** | **nothing** |
| puc | BirdWeather/BirdNET detections upstream (station 4066) | nothing | **nothing** |
| phones | 172 B sketch on `dama/<node>/acoustic_sketch` | `AudioCaptureRing`, `raw_retain_s = 0` | **nothing** |

**The ring is the architectural licence.** 224 s of retrospective slack means a central
classifier may take up to ~3 minutes to decide it wants audio. That is the entire reason a
battery node is allowed to stay dumb. A four-minute pull is 8–10 paced requests, 57.4 s wall,
167 kB/s effective (measured, rankine), and costs that node ~14 s of its own capture.

**Budget, per node, enforced as a token bucket:** charge *actual transfer seconds × that node's
own measured loss fraction*, where the loss fraction is re-derived from the ring-wall-span
instrument rather than assumed. At the fast end (rankine, 24 %) a 60 s/hour `/audio` allowance
costs 14.4 s/h of deafness (0.40 %); at the slow end (mach, 57.5 %) the same allowance costs
34.5 s/h (0.96 %). Quote both ends; do not quote 24 % as a fleet constant.

**⚠️The puller is not the only spender and must not be accounted alone.** The drain currently
spends 104–156 s/hour/node on `/sd` — 2.9–4.3 % of every day — which is 3–11× the puller's
ceiling depending on which node you measure. One per-node budget object, read by both, or the
stated ceiling is not a ceiling. (And per §0.1 the drain is currently spending all of it to
collect duplicates.)

**Rejected: continuous raw streaming.** Not on bandwidth — 32 kB/s/node against 376 kB/s
aggregate is 26 % occupancy. On measured capture loss: at that duty the node loses 24–57 % of
what it is streaming, the returned WAV is contiguous in **write order, not in time**, and nothing
in the WAV body marks the splice. You would be streaming half a stream and the node would report
that it was fine.

**Rejected: putting puc in a data path.** No ring, no SD, no `/audio`, no `/sd`, no sketch, no
scene, GPS fix 0, PPS not wired. Its `/mic` is a **GET that calls `pdm_i2s.end()` then
`begin()`** — a read-shaped request that reconfigures the I2S peripheral. Never poll it. puc's
value is entirely as a label source (§6.3).

---

## 3. The pipeline on k3s

Almost nothing here is new infrastructure. It is already running.

```
nodes  ──HTTP pull──▶ hear-drain (CronJob, LIVE — FIX per §0.1)  ─┐
phones ──MQTT───────▶ dama-sketch-corpus (Deployment, LIVE)       ├─▶ hear-pool PVC (LIVE)
hugbot ──MQTT───────▶ (same dama/+/acoustic_sketch wildcard)      ─┘        │
                                                                            ▼
                                             hear-score   (S0b — BLOCKED, §3.2)
                                                                            │
                                                                            ▼
                                             hear-puller  (S1 — gated on §5)
                                                                            ▼
                                             hear-embed   (S1 — gated on §5)
```

### 3.1 ⚠️The repo manifest and the live CronJob have diverged in BOTH directions

Measured 2026-09-09:

    live   args: --pool /pool/corpus --node nyquist= --node mach= --phone-corpus /pool/sketch_corpus --node rankine=
    repo   args: --pool /pool/corpus --node nyquist= --node mach= --node rankine=

`kubectl apply -f deploy/k8s/hear-drain.yaml` as it stood would have **deleted `--phone-corpus`
from the live drain** — the written-whole hazard the repo documents for ConfigMaps, applied to a
CronJob spec. Another session added rankine to the live object within the last hour; a third
session added `--phone-corpus`. **This document's only manifest change is to add
`--phone-corpus` back to the repo file** so the file is a superset of the live object and
applying it is safe. Verify the live args again before applying — they have moved twice tonight.

Retire the earlier "S0 = add rankine to the drain" item: rankine is already in the live CronJob,
and per §0.1 what it actually needs is a filename fix, not a node-list entry.

### 3.2 ⚠️`hear-score` cannot ship compliantly today, and this is a blocker not a detail

`deploy/k8s/gen_configmap.py` is the fleet's stated safety mechanism — import-closure `check()`
plus a `dama-hear/commit` provenance stamp. It **cannot emit a second ConfigMap**:

- `main()` hardcodes `name: hear-drain-code`;
- `FILES` is a fixed 7-entry list documented as "*the import closure of `tools/hear_drain.py`
  and nothing else*";
- `check()` only walks `hear.*` imports — it is blind to `modules.*`;
- and the half of the safety property that makes the keys **importable** — the
  `volumeMounts` → `subPath` block — is **hand-written** in `hear-drain.yaml` (lines 64-70,
  duplicated at 135-141), not generated.

`hear-score` needs `modules/supersonic/classify.py` and `model_sketch_15.json`, neither of which
is a `hear.*` module. Routing them through this generator would mean injecting them into the
**drain's** ConfigMap. That is how `dama-sketch-corpus-code` ended up in the cluster with no
`dama-hear/commit` annotation and no `app` label: there was no compliant path, so someone
hand-applied one.

**Therefore `hear-score` has no manifest in this repo yet, deliberately.** Its prerequisite is a
generalization of `gen_configmap.py` to take `(name, FILES, mount_root)` and emit **both** the
ConfigMap **and** the matching `volumeMounts` block, with `check()` extended to walk `modules.*`.
That is ~40 lines and it is S0c. Writing a `hear-score` manifest before it exists would ship the
exact omission this section criticises.

### 3.3 The deployment idiom, and the one exception

`python:3.13-slim` + a pinned `pip install --target /pool/pylib`, as `hear-drain` and
`dama-sketch-corpus` already do. No Dockerfile, no CI, no registry, for what the fleet treats as
a small script.

The exception is `hear-embed` (S1), which needs a built image. ⚠️**There is no build path to the
in-cluster registry today.** `10.43.116.193:5000` exists (`registry` namespace, NodePort 30050)
and `dama-bridge-rust` / `ant-mirror-daemon` pull from it, but the only kaniko builder in the
`dama` namespace is `ant-trainer-image-rebuild`, which builds `dama-gotchi/training/Dockerfile`
and pushes to **AWS ECR**. Naming the local registry as `hear-embed`'s home names a destination
with no producer. S1 must include either a second kaniko CronJob targeting the local registry or
an explicit host `docker build && push` with the Dockerfile checked into this repo.

### 3.4 ⚠️`hear-score` must not write the pool

`grep -n 'flock\|fcntl\|lockf' hear/pool.py tools/hear_drain.py` returns **nothing**. Writes are
plain `open(path, "a")` (records `:257`, ledger `:262`) and `gzip.open(..., "at")` (scene
`:492`), and the dedup key set is seeded by one directory scan per `Pool` instance (`:228`), so
two live instances each miss the other's writes. There are already **two** writers — the drain
CronJob and whatever backfill another session runs — and concurrent gzip appends corrupt members
rather than failing loudly.

Verdicts go to a **separate `/pool/verdicts` store**, keyed by the pool record's existing content
`key`. `hear-score` reads the pool and never writes it. If anything ever must write the pool
concurrently, an `flock` around `_append` and the scene gz path is a prerequisite, not a
follow-up.

### 3.5 Not extended

`dama-acoustic-guard` (17 d) is a plausibility watchdog over `dama/+/acoustic_env`,
`dama/+/acoustic_ranging`, `dama/hugbot5000/acoustic_rangedoppler` and `.../acoustic_rir`. It
validates phone **ranging**; it does not classify sound and does not touch sketches or scene
rows. Different topics, different job. Leave it alone. Likewise `dama-strid-band-validity` is a
false friend — its "stridulation" is an IMU gyro band, not an insect.

---

## 4. The instruments, and what the stack refuses to claim

`hear/nodeclass.py`'s `require_arrival` / `require_band` / `timing_budget_m` are the intended
gate at the top of the stack. ⚠️**Two of the refusals below cannot be enforced by it as written**,
and that is recorded here rather than designed around:

- **There is no hugbot / ESP class in the registry.** Enumerated live, `CLASSES` holds exactly
  `gotchi-phone`, `puc-ntp`, `puc-pps`, `xiao-s3-i2s`, `xiao-s3-pps`. The fleet's only multi-mic
  array has no entry.
- **`NodeClass` has no mic-spacing / aperture field at all**, and `can_bear()` returns `True` on
  `mic_count >= 2` alone. So it returns `True` for `puc-ntp` — whose aperture appears nowhere in
  this repo — and it **cannot compute a spatial-Nyquist ceiling**. That is precisely how the
  deployed `hugbot-esp-doa.service` came to run `--f-hi 20000` against a 4501 Hz limit, 4.4×
  over.

Prerequisite for calling `nodeclass` the gate: add `mic_spacing_m`, derive
`spatial_nyquist_hz = c / (2 · spacing)`, make `can_bear(f_lo, f_hi)` take a band and **refuse
when spacing is unset** rather than returning `True`, and register a hugbot ESP class
(48 kHz, 2 mics/board, 0.0381 m, PPS-anchored on the Pi). Until then, do not describe
`nodeclass` as the enforcement point in any other document.

The system refuses to:

1. **Emit an arrival time from a class that cannot support one.** Exclude, never degrade. An
   NTP node averaged into a PPS solve returns a plausible residual and a metre of common-mode
   error.
2. **Emit a bearing** from any single-mic node; above 4501 Hz on hugbot (`c/2d`, 38.1 mm,
   operator caliper 2026-09-04); or in robot-frame azimuth at all —
   `array_geometry.RING_SLOT_TO_ARRAY` pins only slot 0. Publish
   `observable="azimuth_cone_only"`, `front_back_ambiguous=true`,
   `geometry_scale_verified=false`, and the `band_hz` actually correlated.
3. **Trust `drop_s` / `drop_samples`** as evidence a node heard anything (3.5× / 25× / 92×
   under-report; §1.3). Health keys on ring wall span (§0.2) with its own denominator and a
   healthy-node self-test.
4. **Trust `X-Audio-Clipped`.** `dur` is clamped to `AUDIO_MAX_S` **before** the clip check, so
   a `dur=120` request returns 30 s labelled `clipped: none`. Carry `X-Audio-Samples`, and carry
   `X-Audio-Fs-Hz` **per file** — `fs_clean` was observed at 15,912.4 and 16,042.7 Hz on
   requests seconds apart, and the WAV header cannot represent it.
5. **Pool across band axes.** `pool._geom_str()` keys on `(bands, slices, f_lo_hz, f_hi_hz)` and
   refuses the sketch's `[300, 20000]` against the scene's `[62.5, 7812.5]`. That guard is
   correct and must not be relaxed.
6. **Score a cross-rate frame without `layout=fixed`.** Measured cost of getting it wrong on
   identical audio: AUC 0.9473 → 0.9141.
7. **Claim species-level insect ID.** Field SOTA is macro F1 0.56–0.58 on a curated corpus
   (InsectSet459). 16 kHz puts most katydids physically outside the data
   (`modules/bioacoustic/detect.py`: ultrasonic 15–60 kHz "*here to be refused, not to be
   used*"). Ship presence and chorus intensity; state the ceiling in the same sentence.
8. **Score a sketch as calibrated across nodes** — see §6.1, which is the sharpest constraint in
   this document.
9. **Ship a gate that cannot fire.** Three known instances: `AudioRing.gap_frames` on hugbot's
   ESP ring (always 0 against 20.0–22.1 % measured fabricated columns, because `esp_ring_feed`
   pads inside `ring.write()` and never calls `write_silence`); the ring-wall-span check with
   the wrong denominator (§0.2); and `drop_samples` itself.

### 4.1 ⚠️`holdout_envelope` and `alarm_reproducibility` cannot be wired to S0

The earlier draft said they go into "every scoring path from day one". That is not buildable and
would have been the fourth check that cannot fail. Both take `fit: FitFn, score: ScoreFn`
(`hear/validate.py:82-83, 663, 731`) and work by **refitting** — `holdout_envelope` to measure
exceedance spread across cut points, `alarm_reproducibility` to Jaccard two independent fits of
the same corpus. `model_sketch_15.json` is a frozen supervised logistic that is never refit and
carries no covariance. There is nothing to hold out and no second fit to compare; wiring them in
yields a `Refused` or a vacuous constant.

They are the right machinery for the **S1/S2 probes**, which are genuinely fit, and that is the
case they were ported for. Reserve them there, and **state the failure action** — which
exceedance or Jaccard value blocks a verdict from publishing, and where that number is written.
A validator whose output nothing gates on is indistinguishable from no validator.

S0's instruments are the ones the shipped code actually supports:

- **refusal counts by reason**, published, not logged. `score_sketch` raises `SketchMismatch` on
  a layout or band mismatch; over `testdata/sketch_golden.json` it scores 3 of 9 and refuses 6 on
  `layout='nyquist'`. Live pool rows are currently fine (`layout: "fixed"`, `fs_stated_by:
  "frame"`; `valid_bands: 15` on the 16 kHz node history, **20** on phone rows and on node rows
  from the 48 kHz sketch build), but a scorer that silently skips refusals reports "all clear"
  identically whether the corpus is healthy or has gained a legacy row. A nonzero
  unstated-layout count is an **alert**.
- **the `needs_label()` 0.35–0.65 band rate**, against the 14 % `classify.py:138` measured over
  1014 events. A band rate that departs from 14 % says the score distribution moved.
- **the score distribution** against the 228-event training set.
- **pool anchoring**: 474 of 1809 sketches (**26.2 %**) carry `utc_us = 0`. Those cannot name an
  `/audio` window in principle and belong in an explicitly non-pullable lane.

---

## 5. Stages

Each stage states what it costs, what it is blocked on, and what it is allowed to claim when
it lands.

### S0 — ships first, useful alone, needs no new hardware, no GPU, no phone release

Three items, in order. All are repairs.

**S0.1 — fix the scene lane.** Per §0.1. Prefer the newest `scene-YYYYMMDD.csv`; keep the legacy
names; make "no scene file of any name" a check **failure**. Re-verify by watching
`by_node` gain a `rankine` partition and `last_utc_s` track wall clock.
*Cost:* one file, `tools/hear_drain.py`. *Claim on landing:* the scene corpus is live on three
nodes, not two, and is not 3 h behind.

**S0.2 — stop paying for duplicates, and reconcile the manifest.** Once S0.1 lands, the dated
file is small and growing; the 2 MB tail against a 20 MB frozen file is pure waste. Cut the
drain's cadence to match its actual reach-back rather than running `*/15` against a 2.41 h tail
(99.99 % redundant by construction — measured `8467 rows → +0 new` every run). Add
`--phone-corpus` to `deploy/k8s/hear-drain.yaml` (**done in this change**) and re-check the live
args before applying, because they moved twice while this was written.
*Cost:* a manifest edit and a schedule change. *Recovers:* on the order of 3 % of every node's
hearing per day, at zero data cost. *Claim:* none — this is a cost reduction, not a capability.

**S0.3 — generalize `gen_configmap.py`.** Per §3.2. `(name, FILES, mount_root)` → ConfigMap +
`volumeMounts`; `check()` walks `modules.*`. ~40 lines. This unblocks every later stage and is
the only reason `hear-score` is not in this change.

**S0b — `hear-score`, once S0.3 lands.** A small worker on the existing `hear-pool` PVC that
reads `/pool/corpus`, runs `classify.score_sketch` with `model_sketch_15.json` over every sketch
record, writes verdicts to a **separate** `/pool/verdicts` store (§3.4), publishes
`dama/hear/verdict`, and publishes the four S0 instruments in §4.1 alongside — **refusals by
reason included**.

Why this and nothing else first: the classifier is already trained, already validated, and was
verified scoring 7/7 live nyquist frames with 0 refusals. **It has no caller.** Roughly 900
otherwise-orphaned tests acquire a consumer. But see §6.1 before deciding what a verdict means.

### S1 — `hear-puller` + `hear-embed`. ⚠️GATED, and the gate is not a formality

`hear-puller` issues budgeted `GET /audio` inside the 224 s window; `hear-embed` runs Perch 2.0
on the GPU and writes width-tagged embeddings. **Two things must be true before S1 starts, and
neither is true today.**

**Gate 1 — the trigger must be inside the ring window.** ⚠️A verdict computed from
`/pool/corpus` is 4–5× too late to address the audio that produced it: the drain is `*/15`
(900 s) plus 53–78 s of job wall time, against a 223.995 s addressable ring. S0.2 makes it
*worse*. **The pull trigger must be driven from `/detections`** — the live 128-deep RAM ring,
~600 B/event, ~35 ms measured, including still-`PENDING` clips — polled well inside 224 s, with
`hear-drain` left doing archival only. Then the cadence cut in S0.2 is free rather than
self-defeating. State the end-to-end latency as a number against 223.995 s. Unanchored sketches
(26.2 %) route to a lane that is explicitly not pullable.

**Gate 2 — the sample-rate penalty must be measured, not assumed.** Perch 2.0 is 32 kHz native;
every pullable byte is 16 kHz band-limited to 8 kHz (§1.2). Upsampling into a 32 kHz model
presents **measured silence above 8 kHz as if it were measurement** — the same operation §4.6
forbids one layer down, where it cost 0.9473 → 0.9141 on identical audio.

⚠️The experiment the earlier draft nominated to price this — *"embed hugbot's 48 kHz ring native
vs decimated-to-16-and-upsampled"* — **is unrunnable**. §2's own table says hugbot serves
nothing; `wt-anom/wiring.json` declares `ring-pull-capture` `island: true`, "ON-REQUEST AND
DELIBERATELY DISABLED", with no systemd unit on either host. There is no other 48 kHz source
with retention anywhere in the fleet (`puc-*` 0.0, `gotchi-phone` 0.0, `xiao-s3-i2s` not built),
and the 2026-09-05 training clips are 0.5 s against Perch's 5 s window.

Two runnable substitutes, in cost order:
- **puc's BirdWeather clips** — 48 kHz, 9 s granularity, already being recorded on this
  property. Embed native vs decimated-to-16-and-upsampled and measure the probe AUC gap. Costs
  no firmware, no node deafness, no operator time beyond an API pull.
- **a one-off operator-supervised hugbot capture written to a file** — not a served endpoint, not
  a re-enabled pull path.

Until one of those produces a number, S1 may not ship a probe on upsampled 16 kHz. And
"per-rate heads, not one pooled head" is **vacuous while exactly one pullable rate exists** —
reinstate the rule when a second appears.

**Gate 3 — the GPU is real but the capacity figure is wrong.** `nvidia.com/gpu: 32` allocatable
is `pattern: "*"` × `timeSlicing.replicas: 16` over **two physical cards** (verified:
`kube-system/nvidia-device-plugin-config`, `allocatable = 32`). The survey's `nvidia-smi` inside
the vLLM pod found a **RTX 2080 Ti** (11,264 MiB, 560 MiB used) and a **RTX 4070 Ti**
(12,282 MiB, **11,453 MiB used** by vLLM at `--gpu-memory-utilization=0.80`). Time-slicing gives
**no VRAM isolation and no device affinity**: a pod requesting `nvidia.com/gpu: 1` lands on
either card, and landing on the 4070 Ti leaves it ~800 MiB. "31 free replicas" is a scheduling
count, not headroom. Size against the 2080 Ti's ~10.7 GB and **pin** — split the plugin config
into named resources, or move vLLM to a named device. The 2080 Ti is Turing / sm_75; verify the
chosen Perch runtime loads on it (the model card names TF 2.20.rc0; an ONNX mirror exists,
fidelity unvalidated) **before** sizing an image.

### S2 — labels, runnable in parallel with S1, and cheaper than any of it

1. **Cover mach's mic for ten minutes.** Its reported +15.5 dB electrical noise floor decides
   whether it is a classification node at all. Ten minutes of operator time, and it should
   precede model selection, not follow it.
2. **Harvest BirdWeather station 4066** (⚠️**rotate the flash-recovered token first — it is a
   live third-party credential sitting in a backup file**). Time-align detections to a
   neighbouring node's ring. This is the same pattern as `dama-rtk-labeler`: a
   better-instrumented sensor labels a worse-instrumented one, continuously and for free. It is
   the single highest-leverage item in this document. ⚠️**How many detections exist, over what
   span, at what confidence, is UNMEASURED** — and that number decides whether a site bird probe
   is trainable this month or next year. Measure it before planning around it.
3. **Buy an RTL-SDR v3 (~$25) and run `dump1090`.** ADS-B turns every overflight into a
   timestamped, typed, altitude- and range-tagged positive **and produces the negatives**, which
   is the expensive half. This is AeroSonicDB's actual contribution — the method, not the data.
   `grep -E 'adsb|ads-b|dump1090|readsb|tar1090|1090'` across both trees returns nothing but one
   false positive on a test class name: no ADS-B receiver exists in the fleet today.

### S3 — blocked on a phone release. Not a code gap

Producer (`AcousticSketch.kt`), transport (`dama/+/acoustic_sketch`), lander
(`dama-sketch-corpus`, deployed, subscribed, TLS connection succeeded) and sink (`hear-pool`) are
all live and correct. The worker logs `NO SKETCHES: ... 0 messages in 2001 s since start
(subscription is up; the fleet is not publishing)`. The pool reports `phone_utc_trusted:
{true: 0, false: 0, not_stated: 0}` and `by_source: {node: 1809}`.

Root cause: dama-gotchi `cfca6aec` (#1115) landed 2026-09-07 19:40 and `git tag --contains` is
**empty**; the latest release is v0.9.180, dated 2026-09-05. **Ship an APK containing
`cfca6aec` and the phone lane lights up with no further work in this repo.**

### S4 — blocked on the RTK survey. No TDoA product ships before it

Node GNSS positions disagree by 4–17 m under canopy, and that is the dominant error term — ahead
of timing quality, which is 24–28 ns. `hear/solve/placement.py` already measured why: shifting a
track ±6 m past three **same-side** nodes changed the TDoAs by **0.000 ms**, and past straddling
nodes by up to 117 ms. Geometry beats timing. Sound crosses the 3.5-acre site's 119 m square
equivalent in 347 ms.

The association cross-check `|dt_i − dt_j| ≤ 2d/c` only discriminates below ~0.35 m spacing, so
it works **inside hugbot's array and nowhere else on the property**.

### S5 — blocked on hardware that does not exist

- **48 kHz with a ring.** `xiao-s3-i2s` is "Planned. Not built." — and even built, the ICS-43434
  is 50 Hz–15 kHz, low-passed above 24 kHz, so at 48 kHz **the microphone binds, not Nyquist**.
- **Ultrasonic katydids (15–60 kHz) and bats (192–384 kHz).** Not reachable at any sample rate
  on that part. ⚠️Stated precisely so nobody reopens it: `hear/sketch.py:41` already encodes
  96 kHz (flag 8) and 192 kHz (flag 9), so **the wire format is not the blocker** — the
  microphone and the ADC are, and no firmware change reaches this.
- **puc bearing.** §2 lists puc as bearing-capable in principle (two mics, shared PDM clock,
  which makes a *relative* bearing independent of its ±1.1–3.1 m NTP bound). ⚠️It needs firmware
  that nobody has written — puc has no ring, no SD, no `/audio` — and an **aperture that appears
  nowhere in this repo**, so its aliasing ceiling cannot even be stated. This belongs in S5, not
  in an inventory of present capability.

---

## 6. Models: the cascade, and the numbers it is allowed to quote

A cheap always-on triage in front of an expensive identifier. Explicitly, and with two large
caveats.

**Stage 0 — triage: `modules/supersonic/model_sketch_15.json`.** A 15-band logistic over 172 B.
`b = −46.4987`, 120 weights, `min_fs_hz = 16000`. Cost is a dot product per event; at the
corpus's 2.91 events/h/node it is free.

⚠️**Quote the right numbers.** The artifact says `auc_nested_grouped_cv = 0.96648`. The earlier
draft said "0.9588 (0.9705 after the onset fix)" and **both are the wrong artifact**: 0.95895 is
`model.json`'s `auc_grouped_cv` — the **six hand-feature** model, grouped not nested — and
0.9705/0.97281 is the **20-band** `model_sketch.json`, which carries `min_fs_hz = 32000.0` and
**cannot run on this fleet at all**. The number that applies to nyquist/mach/rankine — a model
fitted on 48 kHz rig clips scoring 16 kHz audio — is the measured cross-rate transfer **0.9473**.

> **Stage 0 quotes 0.9473 in situ on the 16 kHz nodes, and 0.9665 as its corpus CV. Nothing
> else.** (`classify.py`'s and `README.md`'s docstrings still carry the stale 0.9588/0.9634 pair
> against the artifacts' 0.9665/0.9728; fix those or this error re-imports itself.)

Note also that `nfft = 256` is a fixed **sample** count: 5.33 ms / 187.5 Hz bins at 48 kHz vs
16 ms / 62.5 Hz bins at 16 kHz. `layout=fixed` equalises band **edges**, not the analysis window.
0.9473 already accounts for that; do not correct for it twice.

### 6.1 ⚠️THE MODEL CONSUMES ABSOLUTE dB AND THE FLEET IS NOT CALIBRATED

This is the sharpest constraint in the document and it applies to S0 as deployed.

The feature is `q/2 + ref_db`, and the artifact's own note says `ref_db` is "*the single
strongest term this project has measured*". `sum(w) = 0.595903`. Therefore the model's **entire**
p = 0.1 → p = 0.9 range is `2·ln(9) / 0.5959` = **7.37 dB of `ref_db`**.

No cross-node level calibration exists anywhere in this repo. `hear/node/telemetry.py` says so
outright: the level is "*NOT calibrated to absolute SPL: that needs a reference the field does
not have*". Measured tonight on identical hardware and identical firmware, mach's ambient is
**3.04 dB** above nyquist's — `+1.81` in `z`, **41 % of that entire range**, from nothing but a
microphone. The reported +15.5 dB mach floor would be `+9.24`. The 90.31 dB normalisation error
the hugbot emitter work found would be `+53.8` — a sigmoid pinned at 1.0 on every event forever.

**Any uncalibrated per-node gain offset above ~7 dB turns the triage into a constant, per node,
in either direction — and a saturated classifier is indistinguishable from a confident one.**
This is a fleet-wide property of an absolute-dB model, not a hugbot special case.

Three admissible responses; pick one before S0b publishes a verdict anyone acts on:
1. **Estimate a per-node `ref_db` offset from co-heard events** and publish it alongside every
   verdict. The first gunshot heard by hugbot and a node simultaneously is that measurement.
2. **Run Stage 0 on shape only.** `train_sketch.py` already measured the cost: **0.9450** with
   level removed vs 0.9634 with it — 1.8 points to buy immunity from an unbounded, silent,
   uncalibrated bias. On current evidence this is the right trade for a fleet-wide triage.
3. **Refuse to score any node with no calibration record**, and say so per node in the published
   output rather than emitting a number with an unstated offset.

### 6.2 ⚠️Stage 0 is a GUNSHOT model behind an IMPULSE gate. Three of four targets are structurally silent

`classify.py`'s own docstring: P(gunshot) from 228 events, one afternoon, one range, one rifle,
all labelled supersonic CRACK — "*Retrain before trusting it somewhere else.*"

Upstream of it, the only thing that produces a sketch at all is `hear/node/detect.py`'s
**broadband amplitude gate**. Measured live tonight — and ⚠️**this has changed since the survey**:
the floor is no longer 800. All three nodes report `floor: 200`, `floor_source: "file"`,
`floor_default: 800`, `floor_saved: 200.0` — someone lowered it via `POST /gate` and it
persisted. So:

| node | ambient | thr | thr over ambient |
|---|---|---|---|
| nyquist | 13.0 | 200 | **23.7 dB** |
| mach | 19.5 | 200 | **20.2 dB** |
| rankine | 14.1 | 200 | **23.0 dB** |

Against `modules/bioacoustic/detect.py`'s 12.081 h night capture (1450 `health.csv` rows), the
lowered floor is 16.8 dB over median ambient (28.95) and **7.5 dB over ambient p95** (84.11) —
much closer than the 28.8 / 19.6 dB that module measured at floor 800. So the *level* argument
against the gate is now marginal rather than hopeless. **The other two arguments are unchanged by
any floor:**

- **Band masking.** All 48 in-run detections that night peaked in **mel band 0** (312.5–500 Hz),
  and band 0 exceeded the mean of bands 16–19 (the four entirely above 4 kHz) by a **median
  24.6 dB**. Low-frequency rumble sets the envelope; the insect band is masked before the
  threshold is consulted.
- **Crest factor.** It is an amplitude detector. A sustained tonal source raises the mean without
  producing the peak the gate looks for.

**Consequence, stated plainly: birds, insects and aircraft never produce a sketch, so Stage 0
never scores them, so no pull is triggered, so the identifier never sees them — and the symptom
is a low event rate and a green pipeline.** Live corroboration: 2 detections per node in
~64 min on 3.5 wooded acres at night.

Therefore:

- **Do not call Stage 0 "fleet-wide triage". Call it the supersonic pull trigger.** State
  per-target recall for birds/insects/aircraft as an **explicit open number**, currently
  unmeasured and believed near zero.
- **Give bioacoustics its own trigger.** `modules/bioacoustic/detect.py`'s `TonalGate` is a
  complete 51-test streaming detector built for exactly this question — band-limited power,
  spectral flatness / envelope autocorrelation, minimum duration — and it needs a **caller and
  labels, not a rewrite**.
- **Use the ungated scene row** (20×4, every 1.024 s, 82,225 rows already pooled) as the
  substrate for sustained and diurnal sources. It is the only ungated representation the fleet
  produces.
- ⚠️**The gate floor and the cost budget are the same knob**, and the earlier draft treated them
  in two different sections. "2.91 events/h, free forever" silently assumes a floor that excludes
  three of four targets. Re-derive the Stage 0 cost and the audio budget at the event rate a
  bioacoustic-capable trigger would produce — and derive any new floor **from an envelope
  distribution on that node's own quiet capture**, never from a median, and never validated
  against the same samples it came from.

### 6.3 Stage 1 — Perch 2.0, chosen on licence

32 kHz, 5 s window, EfficientNet-B3, ~12M params, **1536-d** embeddings, Apache-2.0. BirdNET
V2.4 is comparable in the few-shot benchmark ("Perch and BirdNET 2.3 obtain similar
performance"), but its **models** are CC BY-NC-SA 4.0 and ShareAlike plausibly follows every
probe onto a fleet that also ships an Android APK. Take BirdNET's answers, not its weights.

Drop the 1536 × 14,795 classification head (~91 MB fp32 — **larger than the trunk**); embeddings
only. ⚠️**Exclude Perch's insect head by name in code**: on 19 UK Orthoptera species it measured
macro F1 0.071, macro AUC **0.454 — below chance**. "Insecta appears in the training-data table"
is not evidence the head works.

**Stage 3 is an oracle, not a model:** BirdNET via puc's existing BirdWeather feed.

### 6.4 The representations must not be unified

- **Sketch** (172 B, 20×8, `[300, 20000]` Hz, event-gated) — the **index** and the pull trigger.
  Three producers, one byte contract, golden vectors on both sides. It is ~1/1700th of what
  Perch eats and must never try to feed a classifier.
- **Scene** (20×4, `[62.5, 7812.5]` Hz, ungated, 1.024 s) — the **context**: ambient state,
  diurnal drift, node health.
- **Embeddings** (1536-d Perch) — the **payload**, produced centrally, never on a node.

⚠️**Do not route embeddings through `tools/hear_bridge.py`.** Four consumers hard-code
`len(e) == 1024`; three drop non-matching records silently and `audio_anomaly_score.py` returns
`0.0` / "not anomalous", which the bridge's own docstring calls "*worse than dropping because it
looks like an answer*". **Live foot-gun: BirdNET V2.4 embeddings are also 1024-d**, so they would
pass that guard and be scored against a YAMNet-fitted covariance with no error anywhere; Perch's
1536-d would be silently dropped. Neither outcome is acceptable. Every embedding record carries
`dim`, `model`, `model_version`, `fs_native`, `fs_source`, and the consumer **dispatches on width
before anything else**. (`hear_bridge.py`'s own named consumers — `audio_embed_archiver.py`,
`cluster_audio_embed.py`, `audio_ant.py` — were deleted in hugbot PR #375. It should be
retargeted or retired deliberately.)

---

## 7. What each tier contributes, and why flattening them destroys it

- **xiao nodes** — GPS-PPS time (tAcc 24–28 ns, 0 glitches, PPS spread 6–9 µs) and the **only
  240 s retrospective ring in the fleet**. They are the fleet's clock and its memory. They are
  single-mic and **structurally cannot bear**.
- **hugbot** — the **only co-located multi-mic array**, 38.1 mm intra-mic, and therefore the only
  bearing that can be cross-checked. Capped at 4501 Hz. hugbot **emits, never serves**: battery
  robot, flaky wifi, a Pi at 84 °C with a `CPUQuota` on every audio unit.
- **puc** — a **live BirdNET station** (4066): the fleet's free label source. Not a data-plane
  node (§2). Not a bearing source until S5 (§5).
- **phones** — mobility, and the **only operator labels in the fleet**, which is the entire
  argument for the shared byte format. 25.1 ms clock (8.6 m): useful for presence and labels,
  never for TDoA.

---

## 8. Rejected, with the measurement

- **YAMNet as an embedding backbone for anything new.** 361 of 1024 dimensions hold **exactly
  zero variance** and 43.2 % of directions are pinned by the covariance floor
  (`hear/validate.py`), against the scene corpus's rank 80/80 and 0 % floored. The hugbot
  detector built on it measured mean AUC **0.624** and caught **0 %** of gaussian noise matched
  to the corpus; two independent fits of the same corpus agreed on which rows alarm at
  **Jaccard 0.155**; on a drift-free interleaved control it detected **0.31 %** — *below* its own
  0.5 % false-alarm rate. Its own author calls it a corpus reader, not an alarm. It has published
  ~691k messages to **zero subscribers**. **Port the validation machinery; never the model.**
  If a small AudioSet tagger is genuinely wanted, EfficientAT `mn10_as` is strictly better on
  every axis (MIT, 4.88M params, mAP 47.1).
- **AST / PaSST / BEATs.** 87–90M params for ~1 mAP over `mn10_as` at 4.88M. Decisive: the
  measured few-shot result is that AudioSet-trained embeddings **lose** to bird-trained
  embeddings on all six bioacoustic datasets tested, including bats and marine mammals
  (VGGish scored 0.04 top-1 on marine mammals). Three of four targets are bioacoustic.
- **PULSE as shipped.** 96 kHz UK field recordings — 2× the fleet's best rate, 6× its usable
  microphone bandwidth — 19 UK species, macro F1 0.212 (0.338 with active labelling), and the
  preprint's weights URL is a literal `XXX` placeholder. Take the **method** (BirdNET
  distillation + BYOL + active labelling) and the **finding** (Perch fails on Orthoptera).
- **`hear/backend/`** as the path to central classification. It decodes radio frames; there is no
  radio (§1.1).
- **A third audio-pull protocol** alongside `GET /audio` and `dama/colony/audio/pull`. Pick one.
- **Feeding the sketch to Perch or BirdNET.** BirdNET wants 144,000 samples, Perch 160,000; the
  sketch is 20×8 int8 over 33–44 ms. The sketch's job is to decide **which four seconds are
  worth 128 kB of WiFi**. Keep the split.
- **Re-keying the pool** so hugbot rows read `source='hugbot'`. `source` is an argument to
  `key()`, the content address already written into every stored row; changing it re-keys the
  whole MQTT corpus and duplicates every record. hugbot rows carry `source='phone'` and are read
  on the phone's `utc_trusted` ladder. **Document the consequence; do not "fix" it.**

---

## 9. Open numbers — unmeasured, and what each one blocks

| unmeasured | blocks | cost to close |
|---|---|---|
| per-node `ref_db` offset across the fleet | whether any Stage 0 verdict means anything (§6.1) | one co-heard event, or accept 0.9450 shape-only |
| mach's +15.5 dB floor: electrical or environmental | whether mach is a classification node | **10 minutes** — cover the mic and log |
| BirdWeather 4066 detection count / span / confidence | whether a site bird probe is trainable this month | one API pull |
| Perch/BirdNET accuracy on 16 kHz-sourced audio | all of S1 (§5, Gate 2) | one puc-clip A/B |
| per-node RSSI on nyquist/mach/rankine | the transport curve — `night_node.ino` never calls `WiFi.RSSI()` and `/status` has no wifi block; the only RSSI on the property is puc's −77 dBm | a firmware field |
| whether the 24 %→57.5 % `/audio` loss curve is linear in link speed | whether the token bucket's charge model is right | two more nodes' worth of points |
| the 3.27 h scene stall's historical extent | how much corpus has already been lost this way | a row-rate gap analysis over 82,225 pooled rows |
| whether the drain causes the 1.09/1.12 ring excess | whether S0.2 actually recovers what §0.2 implies | instrument the ratio across a drain run with no other traffic |
| the 474 unanchored sketches (26.2 %): boot rows or an ongoing gap | any TDoA work | partition by boot |
| powerline-easement broadband emission in the aircraft band | the false-positive floor on the one easy target | one quiet capture near the easement |
| hugbot's ESP acoustic→ADC group delay | hugbot arrival times — must be **omitted**, not zeroed | an external reference |
| whether the 2080 Ti (sm_75) runs the chosen Perch runtime | S1 image sizing | one container run |

---

## 10. Ledger

Measurements taken while writing this document, 2026-09-09 05:19–05:21 UTC, read-only, from the
k3s host. Nothing was applied, flashed, or POSTed.

    curl -s http://172.16.100.{105,116,50}/status
      fw 7f84d29, class xiao-s3-pps, uptime_s 4271 / 4225 / 4177
      gate floor 200 (floor_source "file", floor_default 800, floor_saved 200.0), thr 200
      ambient 13.0 / 19.5 / 14.1  ->  thr over ambient 23.7 / 20.2 / 23.0 dB
      e_max_win 44.6 / 42.0 / 47.3
      raw span/(cap_samples/16000) = 1.09148 / 1.11629 / 1.00224
      acq drop_s 47 / 42 / 24, drop_samples 453760 / 386816 / 89088

    curl -s http://172.16.100.105/audio
      span_s 240.0, cap_samples 3840000, addressable_samples 3584000, max_dur_s 30
      from/to = 245.958 s over 224.0 s addressable = 1.0980

    curl -s http://172.16.100.{105,116,50}/ls
      nyquist scene.csv 20751993 B (frozen), scene-20260909.csv 2003040 B (growing)
      mach    scene.csv 20270034 B (frozen), scene-20260909.csv 2220166 B (growing)
      rankine NO scene.csv,                  scene-20260909.csv 1605608 B (growing)

    curl -s http://172.16.100.{105,116,50}/detections   ->  2 / 2 / 2

    kubectl logs -n dama hear-drain-29815500-bnhcn
      nyquist/mach/rankine all "ok +0 sketch, +0 scene"; 8467 -> 8466 dup; rankine "scene.csv absent"
    kubectl logs -n dama hear-drain-check-29815517-lpgpb
      all four lanes "ok"; nyquist and rankine "unfetched UNKNOWN"
      pool: 82225 scene rows, by_node {mach 37146, nyquist 45079}, last_utc_s 1788919477.9
            = 2026-09-09T02:04:37.9Z (3.27 h stale)
            1809 sketches, 1335 anchored / 474 not, by_source {node: 1809}
            phone_utc_trusted {true 0, false 0, not_stated 0}

    kubectl get cronjob hear-drain -n dama -o jsonpath=...
      live args carry --phone-corpus AND --node rankine=; repo file carried rankine only

    kubectl get cm nvidia-device-plugin-config -n kube-system
      pattern "*", timeSlicing replicas 16; node allocatable nvidia.com/gpu 32  ->  2 physical

    python3 - modules/supersonic/*.json
      model_sketch_15.json  auc_nested_grouped_cv 0.96648  min_fs_hz 16000  b -46.4987
                            120 weights, sum(w) 0.595903, |w|max 0.1093
                            p=0.1 -> p=0.9 spans 7.37 dB of ref_db
      model_sketch.json     auc 0.97281  min_fs_hz 32000   (UNUSABLE on this fleet)
      model.json            auc_grouped_cv 0.95895          (six hand features)

    python3 - hear/nodeclass.py
      CLASSES = {gotchi-phone, puc-ntp, puc-pps, xiao-s3-i2s, xiao-s3-pps}
      no hugbot class; no mic-spacing/aperture field; can_bear() == (mic_count >= 2)

    grep -n 'flock|fcntl|lockf' hear/pool.py tools/hear_drain.py   ->  no matches
    grep -n 'scene\.csv' tools/hear_drain.py:112
      SCENE_FILES = ("scene.csv", "scene-prev.csv")
