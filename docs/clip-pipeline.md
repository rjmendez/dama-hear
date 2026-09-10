# The clip pipeline

Detection audio, from the node's SD card to the pool, and — once a human gate opens — to a set of
model-generated tags. This is the operator page for what actually ships. The design argument is in
`docs/acoustic-stack.md` §0.3 and §0.4.

---

## ⚠️WHAT THIS PIPELINE DOES NOT ESTABLISH

**Read this before quoting a single number out of `clips/tags.jsonl`.**

**1. These are model-generated labels. Nobody has listened to this audio.** Every score in
`tags.jsonl` carries `"provenance": "model"` as a literal, and `"claim": {"usable_as_training_label":
false}`. Those fields are there so the refusal travels with the data instead of living only on this
page. A YAMNet score is a hypothesis about a 4-second clip. It is not an observation, it is not an
annotation, and three models agreeing is corroboration, not ground truth.

**2. The audio is 16 kHz, so everything above 8 kHz is simply not present.** Bat calls, most insect
stridulation detail, and the upper half of many bird songs are outside the recording, not merely
missed by the model. A confident "no bird" from this pipeline is a statement about 0–8 kHz.

**3. YAMNet does event triage, not species identification.** Its 521 classes go as far as `Bird`,
`Owl`, `Hoot`, `Chirp` and stop. It is structurally incapable of "Barred Owl vs Great Horned Owl".
Asking it for species produces a confident answer to a question it cannot represent. BirdNET, which
can represent that question, was evaluated and **refused**: zero birds across nine real clips,
neotropical hypotheses at the confidence floor, and CC BY-NC-SA weights.

**4. The scores are stored whole and unthresholded on purpose.** Every class above
`SCORE_FLOOR = 0.01` is kept, plus `max_unstored_score` so the discarded tail is a number rather than
a silence. There is no top-1 label anywhere in the store. Choosing a threshold is the consumer's job,
made against a human-verified set that **does not exist yet**.

**5. No model output may be used as a training label.** Not for a scene model, not for a sketch
model, not for an ant. Training features against the tagger's own output measures "can a 20-band
descriptor reconstruct what a full-fidelity model already decided" — which is not correctness. This
project already has that exact failure on file: all 35 dama ant models trained on circular
self-labels. `hear/tags.py` ships a **read-only** `scene_overlap()` query and no export path, and
that is deliberate.

**6. What the collection lane DOES establish is separate, and it is solid.** A clip in
`clips/index.jsonl` with `outcome: "stored"` and a matching `sha256` is a measured fact: these bytes
came off that node, at that name, at that time. That part depends on no model being right, which is
exactly why it ships alone and first.

---

## 1. The problem this exists for

Measured on the live fleet 2026-09-09:

| node | clips written | clips evicted |
|---|---|---|
| nyquist | 274 | ~225 |
| mach | 102 | ~53 |
| rankine | 249 | ~200 |
| **fleet** | **625** | **~478** |

**Not one clip had ever left a node.** Each node writes 4.0 s WAVs (1.0 s pre-trigger + 3.0 s post,
16 kHz 16-bit mono, 128,044 B) into `/clips` under a 6,291,456 B budget — exactly 49 clips
(`6291456 / 128044 = 49`, confirmed live: all three nodes report `budget_left_clips 0` and
`6291456 - 17300 = 6274156 = 49 × 128044`). The 50th evicts the oldest.

Two things kept them there. The `/ls` handler hardcoded `SD.open("/")` and ignored every argument,
so it listed root only and clip names — which embed a boot id and a millis counter — were
undiscoverable. And `tools/hear_drain.py` contained zero mentions of clips.

⚠️**A bigger `CLIP_BUDGET_B` would not have saved one clip.** Without collection it changes *which*
478 are destroyed and how long each survives before being destroyed anyway. See §7.

---

## 2. The lanes

```
node /clips ──/sd?file=──> hear-drain ──> clips/<day>/<node>/*.wav
     dets.csv ──/sd?file=──┘    │                clips/index.jsonl
     /ls?dir= ─────────────────┘                        │
                                                        │  (PVC only, never a node)
                                          hear-tag ─────┴──> clips/tags.jsonl
```

**Collection — `hear-drain`, every 15 min, shipping.** Clip names come from the `clip` column of
`dets.csv`; `det_flush` refuses to write a dets row until its clip has resolved, so a name in
dets.csv is a clip that already landed. Fetches are **strictly sequential**: the ESP32 serves one
client at a time and *refuses* the rest rather than queueing, so a thread pool or a second CronJob
converts a slow run into a refused run.

**Tagging — `hear-tag`, suspended.** Touches the PVC and never a node. See §6 for the gate.

---

## 3. What lands on the PVC

| path | pruned? |
|---|---|
| `clips/<day>/<node>/<basename>.wav` | **yes** — 2 GiB cap, `unanchored` first (by mtime), then oldest UTC day |
| `clips/index.jsonl` | **never** |
| `clips/tags.jsonl` | **never** |

⚠️**Audio is the only prunable thing.** The index and the tags are the durable record; the WAV is a
cache of something the node already destroyed. A pruned row keeps every field and gains
`audio_pruned_at`. `<day>` is the UTC day when the clip is anchored and the literal `unanchored`
when it is not — never an invented zero.

⚠️**`unanchored` is pruned FIRST.** An anchored clip carries `utc_us`, `t_start/t_end_utc_s` and a
`record_key` that joins it to its sketch and its scene rows; an unanchored one joins to nothing.
Sorting it last paid the whole cap out of the joinable clips and protected the least recoverable
ones. Within `unanchored` the order is file mtime — a measured arrival time, not a guessed day.
Abandoned `<basename>.wav.<pid>.tmp` part-files are swept on every `prune()` call, cap or no cap:
`_audio_files` filters on `.wav`, so a part-file left by an `activeDeadlineSeconds` kill was
invisible to the cap and nothing ever removed it.

Each index row carries **both** rate readings side by side: `fs_hz` from the dets CSV and
`wav_header_fs_hz` from the file. They disagree in the field — mach shipped an entire boot headed
22624 Hz — and that disagreement has to be visible, not averaged away.

Identity is `clip_key = sha256("clip\x1f<node>\x1f<boot>\x1f<sample>")[:32]` — the **name**, which is
unique by construction and known before the bytes are. `sha256` of the body is stored as an
integrity field, never as identity: a content hash collides two clips of the same silence and drops
a real timestamped event.

---

## 4. Running the drain

```
--clip-max-per-node   49            0 disables the clip lane entirely
--clip-deadline-s     120
--clip-store-max-b    2147483648    2 GiB audio cap
--max-clips-deferred  0             --check fails above this over the window
--max-clips-lost      -1            report, never fail
```

`--max-clips-lost` defaults to `-1` because 666 already-destroyed clips are still named in current
dets.csv files. A gate armed today would fire on that backlog and be muted on day one, which is
worse than no gate. Set it deliberately after 7 days of measured distribution.

The CronJob carries `activeDeadlineSeconds: 780`. Runs already take 218–307 s of the 900 s interval
under `concurrencyPolicy: Forbid`, so an overrun silently *skips* the next tick; the margin is
exactly two missed runs and the clip lane spends part of it.

⚠️`deploy/k8s/hear-drain.yaml` is applied **whole** and must be a superset of the live object.
`--phone-corpus /pool/sketch_corpus` was added to the live CronJob by hand once already. Check
before every apply:

```
kubectl get cronjob hear-drain -n dama -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].args}'
```

### Every way a clip can fail to arrive

Nothing falls into a default. `clips_seen == fetched + already_held + already_gone + gone +
probed_404 + sum(refused) + deferred_by_cap` is an `assert` in `drain_clips`, not a hope.

| counter | means |
|---|---|
| `clips_fetched` | 128,044 B RIFF stored |
| `clips_already_held` | index says `stored`; not re-fetched |
| `clips_already_gone` | index says `evicted_before_fetch`; **not re-probed** |
| `clips_gone` | `CL.CONFIRM_404` consecutive 404s — newly confirmed destroyed |
| `clips_probed_404` | 404 once, **not yet terminal** — the firmware answers 404 for any failed `SD.open` |
| `clips_refused[...]` | `short` / `not_riff` / `empty` / `http_%d` / `transport` / `bad_name` / `store_*` |
| `clips_deferred_by_cap` | still on the card; retried next run |
| `clips_cap_hit` / `clips_cap_reason` | `count` / `deadline` / `disk` |
| `clips_unknown` + `clips_reason` | **the run measured nothing** |

⚠️`clips_unknown` starts **True** and is cleared only by a completed pass. A run that never reached
`/status` has measured no clips and says so; it never reports `clips_gone: 0`. The negative cache
matters: without it the drain re-probes 321 already-dead nyquist names every 15 minutes forever.

These reach the gate through a 64-entry ring in the heartbeat, not through the last run — `check`
runs at `17 * * * *` and the drain at `*/15 * * * *`, so a per-run field is four runs stale before
the gate reads it.

---

## 5. Two fetch traps worth naming

⚠️**`fetch_sd()` cannot fetch a clip, and fails by reporting it ABSENT.** It sniffs the body for
`b"node"` or `b"utc_us"`; a WAV starts with `RIFF`, so a present 128,044 B clip reads as missing.
Proven against a live node. `fetch_clip()` exists for this and nothing else.

⚠️**200 is not proof of a file.** `/sd?file=/clips` returns 200 with a zero-byte body (measured), so
length and the `RIFF` magic are checked, never assumed.

**Fetch order is by eviction risk, never priority.** Oldest-first by `(boot, sample)`. On the flashed
fleet eviction is plain FIFO oldest-by-name — a prefix histogram over 370 live names is `{'ny': 370}`,
i.e. no `%02u-` field at all — so oldest-first is most-at-risk-first. On the checkout firmware
eviction is lowest-priority-first, where the high-priority clip is the one that *survives*, so
fetching by priority would spend the cap on what is least likely to disappear. `prio` is recorded
when the name carries it and is never read for ordering.

### `/ls?dir=` — compiled, not flashed

`firmware/night_node/night_node.ino` gained an optional `dir` argument on `/ls`: streamed,
`..`-rejecting, 404 on a non-directory, capped at `LS_MAX_ENTRIES 256` with an explicit
`! truncated at <n> entries` marker. **It compiles and stops there. No node has been flashed.**

The cap is not sized to 49, because `clip_budget_left` is a RAM counter reset full every boot with
**no startup rescan of `CLIP_DIR`** — eviction only binds once *this boot's* counter is spent, so the
real ceiling is free SD space (~155 files on a ~19 MiB-free card), not 49.

`dir` is deliberately **not** restricted to `/clips`. `/sd?file=` already opens any absolute path
with no authentication, as do `/reboot`, `/update` and `/log`. Filename-guessing was accidental
obscurity, never a security boundary. This is a decision, not an oversight.

Nothing in the shipping pipeline depends on it. `ls_candidates()` runs on **every** drain today at
zero extra requests, reading the root listing the drain already fetches — and correctly returns `[]`,
because root holds `clips` as a *directory* entry (`d clips  0 B` in `tests/fixtures/ls_nyquist.txt`),
so there is no clip name in it to find. Point `_ls_sizes(..., dir="/clips")` at a node running this
handler and the same function starts returning names. Dets and `/ls` are unioned by `clip_key` with
**dets winning**, since a dets row carries `utc_us`, `fs_hz`, `trigger` and the parent record key and
a directory listing carries none of them. What survives from `/ls` is exactly the clips dets no
longer names.

⚠️`LsListing.truncated_at` exists because the `! truncated` line starts with `!`, so the `- ` parse
drops it like any other unparseable line — and a **partial** listing would otherwise be
byte-indistinguishable from a complete one.

---

## 6. The tagger, and the gate it is behind

`deploy/k8s/hear-tag.yaml` ships with `spec.suspend: true` on **both** CronJobs (the check job too —
a check for a suspended tagger would fail on "no heartbeat" every hour from day one and train an
operator to ignore it). The unsuspend condition is written onto the object as
`dama-hear/unsuspend-gate`. All three must hold:

1. `clips/index.jsonl` holds **≥ 300** `outcome: "stored"` rows across all three nodes over ≥ 3
   consecutive days.
2. A human has **listened to ≥ 30 of them**, including at least one from mach, and written what they
   heard into `docs/clip-calibration-<date>.md`.
3. `max_silence_frac` is set from the **measured** distribution in that calibration set. Threshold
   from the envelope, never a single sample.

Until 3 is set, `check_tags` runs report-only and says so on its own output line.

**Model: YAMNet as TFLite under `ai-edge-litert`, not under TensorFlow.** Bit-identical scores at
12.1 ms/clip in 82 MB RSS from a 146 MB venv, against 14.3 ms in 903 MB from a 1.4 GB venv — and the
PVC is the binding constraint. All 64 of YAMNet's mel bins sit below 8 kHz, so the 16 kHz ceiling
costs it nothing.

Weights live on the PVC at `/pool/models/yamnet/`, never in a ConfigMap (16,096,668 B against a
1 MiB cap). They are **verified against a pinned sha256**, not checked for existence:

```
yamnet.tflite         16,096,668 B  141fba1cdaae842c…
yamnet_class_map.csv      14,096 B  cdf24d193e196d9e…
```

A digest mismatch exits **2**, not 1, so a monitor can tell "the model is not what it says" from
"tagging is behind".

### ⚠️Normalisation is mandatory, not an optimisation

Measured on this fleet's own audio, two real clips pulled off nyquist:

| clip | raw | tagged raw | tagged at −20 dBFS |
|---|---|---|---|
| `…-1082421378` | −56.9 dBFS | Silence 0.406 | Animal 0.307 \| Cricket 0.204 |
| `…-1082530195` | −62.1 dBFS | Silence 0.723 | Animal 0.464 \| Bird 0.226 |

Clips are recorded at −49 to −62 dBFS. **A pipeline that skips `normalise()` is green forever and
emits Silence for every clip.**

**No resampling.** `assert_rate()` refuses anything more than `FS_TOLERANCE_HZ = 64.0` from 16000.
The measured spread is 15986–16000, which is harmless; mach's 22624 Hz boot is not, and a resampler
would quietly launder it into plausible-looking tags. YAMNet does not validate its input rate and
does not resample — feeding it the wrong rate degrades **silently** to Silence.

The 1024-d embedding is stored with every tag. It is free (same forward pass), it is a fixed
16 kHz-native axis — unlike the scene.csv (20×4, 62.5–7812.5 Hz) / sketch (20×8, 300–20000 Hz) band
split that `hear/pool.py` refuses to pool across — and it is what makes any later clustering possible
without re-fetching audio the node has long since destroyed.

### Joining a clip to scene rows

`hear/tags.scene_overlap()` is read-only and reports its own strength. ⚠️`scene.csv` and `dets.csv`
carry **no boot-id column**, and both `sample` and `uptime_s` reset to 0 every boot, so a
sample-window join can silently match a *different* boot's rows. `basis` is `"utc"` whenever the clip
is anchored; the weak `"sample"` basis is **opt-in** (`allow_sample_basis=True`) and returns
`weak: True` with a `weakness` string; otherwise `basis` is `"none"` with a counted `refused` reason.
Never a silent match. A 64000-sample clip spans `64000/16384 = 3.906` scene rows, so it overlaps 4 or
5 depending on phase — never fewer.

---

## 7. ⚠️THE ORDERING CONSTRAINT

**Draining must precede any budget increase.** The node is not the archive; the pool is. Until
something collects, every byte of budget is a byte of delay before the same loss — and a larger
`CLIP_BUDGET_B` also consumes SD space and lengthens `clip_evict_worse_than`'s directory scan.

1. Land collection. Clips flow into `clips/index.jsonl`.
2. Observe **≥ 7 days** of `clips_deferred_by_cap == 0` and `clips_cap_hit == false` across all three
   nodes, read off the heartbeat ring, **not** off a single run.
3. Only then raise `CLIP_BUDGET_B` — and raise `--clip-max-per-node` **in the same change**. The cap
   and the budget are one number in two places.
4. Any reflash **re-opens the residency measurement**. The checkout's eviction is priority-first with
   a refuse-if-not-better gate, not the FIFO the live fleet runs, so high-priority clips will live
   much longer and low-priority ones much shorter than the measured FIFO median. The 45-minute
   breaking point does not carry over. Re-measure before touching the schedule.

### The budget arithmetic

A backlog is **bounded at 49 per node** however long the drain was down — the crucial difference
from scene.csv, which grows without bound. 3 × 49 = 147 clips = 18.8 MB: 112 s at the measured
168 KB/s, 164 s at 115 KB/s, and **470 s** at the 40 KB/s contended floor. 307 + 470 = 777 s of a
900 s interval is too tight, which is what `--clip-deadline-s 120` is for. That deadline buys 176
clips at 168 KB/s, 108 at 115 KB/s and **37 at 40 KB/s** — and nyquist's worst measured 15-minute
burst was **43 clips**, so on a slow link during a burst the deadline binds *below* the burst. That
is precisely why `clips_cap_hit` and `clips_deferred_by_cap` must reach the gate: a cap that binds
silently while the run reports success is the failure this codebase exists to prevent.

Steady state is 46.2 clips/h fleet-wide = 1,109/day = 142 MB/day, so the 2 GiB audio cap is about
14 days of rolling audio. Tags plus embeddings are ~5 KB/clip = ~5.5 MB/day, kept indefinitely.

---

## 8. Shipping code to the cluster

A ConfigMap is applied **whole**. `deploy/k8s/gen_configmap.py` holds `BUNDLES`, and
`tests/test_configmap_sync.py` walks it — never a hardcoded file list. After editing any shipped
module:

```
python3 deploy/k8s/gen_configmap.py hear-drain-code > deploy/k8s/hear-drain-code.yaml
python3 deploy/k8s/gen_configmap.py hear-tag-code   > deploy/k8s/hear-tag-code.yaml
```

⚠️`hear/clips.py` is in **both** bundles, as `hear/sketch.py` already was: one edit, two ConfigMaps
to regenerate. Never hand-edit a generated YAML.

⚠️**A bundle entry is only half the seam.** The `code` volume mounts file-by-file by `subPath`, so a
file added to a bundle without a matching `volumeMount` yields a ConfigMap that *has* the module and
a container that raises `ModuleNotFoundError` on a timer — green generation, green sync test, dead
workload. `test_every_bundle_key_is_mounted_by_every_container_that_uses_it` walks `BUNDLES` and
checks every manifest, so this cannot be reintroduced one workload at a time.

`TAG_CODE` deliberately excludes `pool.py`, `detsfile.py` and `scenefile.py`: the tagger walks
`clips/index.jsonl` itself, and `scene_overlap` **takes** a Pool as an argument rather than importing
one. Making the import function-local would not have been enough — the closure audit is an
`ast.walk` and finds a nested import exactly as well as a top-level one.

`ai-edge-litert==2.2.0` + `numpy==2.2.6` install into `/pool/pylib-tag`, a **separate** target from
the drain's `/pool/pylib`. The tag lane must not be able to break the drain lane's dependency
resolution; ~146 MB once is cheap insurance.

---

## 9. Deliberately not built

| | why |
|---|---|
| Training-label export, scene-row label join | circular labels; see §0 item 5 |
| BirdNET | zero birds across nine real clips, floor-confidence neotropical hypotheses, 25% of each clip discarded, CC BY-NC-SA weights |
| PANNs / CNN14 | wants 32 kHz, 17% of its filterbank on guaranteed zeros, 14% confidence loss on the same A/B, 311 ms vs 12 ms, 5.9 GB torch install |
| Resampling in the tagger | would launder mach's 22624 Hz boot into plausible tags |
| `Content-Type` fix on `/sd` | serves `.wav` as `text/csv`; real, cosmetic, and unverifiable without a flash |
| PVC resize | prune + free-space reserve bound it in code, where it is testable |

Two known-open items, both now *visible* rather than fixed: rankine's 25 `clip_why: "fail"` rows —
clips that never reached the card at all, countable from the index once this ships — and the
anchored/unanchored split of the 228,251 scene rows, which nothing here is sized from.

## Acquisition at 48 kHz, decimated to 16 kHz

The microphone runs at `FS_ACQ` (48 kHz) and everything downstream of the decimator runs at
`FS_NOMINAL` (16 kHz). Two rates, on purpose, because the two consumers want different things:

| consumer | rate | why |
|---|---|---|
| clips (`praw`, WAV) | 48 kHz | the most band this mic can legally be clocked for; Perch v2 resamples from it |
| **sketch** (`mel_impulse.h`, `aring`) | **48 kHz** | every phone in the fleet emits 48 kHz; the node was the last 16 kHz emitter |
| scene, gate, dets, timebase | 16 kHz | `mel_scene.h` is the axis of the stored scene corpus and does not move |

⚠️**A bare `FS_NOMINAL` bump would have compiled clean and corrupted the corpus.** `mel_impulse.h`
and `mel_scene.h` each hardcode a rate; running the FFT on audio at any other rate attributes every
band to the wrong frequency while the CSV keeps declaring `f_lo_hz=62.5, f_hi_hz=7812.5`.
`hear/pool.py` refuses to pool across band axes, but it keys on the *declared* axis — which would
not have changed. Two `static_assert`s tie the banks to their rates — `MELIMP_FS == FS_ACQ` and
`MELS_FS == FS_NOMINAL` — and they were verified to fire. They name **different** symbols on
purpose: one assert covering both banks would have to pick a rate, and picking either one makes
the other bank's guard a lie that still compiles.

**The filter is measured, not assumed.** `firmware/gen_decim.py` emits `decim.h`; the numbers are
taken from the **quantised** taps, because the float design and the int16 filter that runs on the
node are not the same filter:

| | |
|---|---|
| passband ripple, 62.5–7812.5 Hz | ≤ 0.08 dB |
| worst fold into that band | −63.4 dB |
| group delay | 224 acquisition samples = 4.67 ms |
| cost | 7.2 M MAC/s, ~3% of one core |

⚠️**÷3 folds more images than ÷2.** Both the k=1 and k=2 images of 16 kHz land in the used band,
so the stopband runs from 8187.5 Hz to Nyquist and every image is measured — not just the first.
The test originally checked only `fs_d − f`, which was correct for ÷2 and would have missed half
the aliasing here.

A 33-tap halfband was measured at only −7.7 dB and rejected. An earlier generator normalised
*after* quantising, pushing a 347 LSB residual onto the centre tap — a broadband impulse that
flattened the stopband to −39.0 dB. `tests/test_decim_filter.py` fails on both.

**The group delay is compensated, not ignored.** `acq_of()` converts a decimated sample index to an
acquisition index, subtracting the FIR delay, so a clip starts where the sound was and detections
are not stamped 4 ms late — which would have discarded far more than the 21 ns the GPS provides.
It saturates at 0: the first 4 ms after boot would otherwise underflow.

**Why 48 and not more.** The mic's Standard Performance Mode tops out at a 4.0 MHz clock and the
ESP32 drives PDM at fs × 64, so fs ≤ 62.5 kHz. Only ÷2 and ÷3 reach 16 kHz by an integer; 64 kHz
would be a clean ÷4 and needs 4.096 MHz, which the mic does not support. Worth noting the fleet's
current 16 kHz clocks the mic at 1.024 MHz — *below* Standard Performance's 1.1 MHz floor and above
Low-Power's 900 kHz ceiling, an unspecified gap it happens to work in. 48 kHz is the first rate
squarely inside a documented mode.

**Costs, and they are real.** The PSRAM raw ring drops from **240 s to 60 s** — a quarter of the
retrospective window. A clip is **480,044 B instead of 128,044** (3.75×), so the on-node budget
holds 13 instead of 49. That second cost only became affordable because clips are now drained every
15 minutes instead of living on the card until evicted.

**Nyquist is 24 kHz, and nothing above 10 kHz is characterised.** The datasheet's frequency
response plot ends at 10 kHz; SNR is quoted over a 20 kHz bandwidth. Response above that is
unspecified, so treat any content between 10 and 24 kHz as measured-but-uncalibrated. If it turns
out to be structured rather than noise, that is a finding, not a guarantee.

⚠️**UNVERIFIED ON HARDWARE.** The mic and SoC specs say 48 kHz is in range; no node has been
flashed. (An earlier note here cited `boards/puc.h` running PDM at 48 kHz as evidence — that was
wrong: its `FS_NOMINAL 48000` is a vendor string from a flash dump, and the only measured PDM run
on that board was at 16 kHz.) Confirm the boot line reports `PDM 48000 Hz ... -> /3 -> 16000 Hz`,
that the ring log shows the expected 60 s fallback, and that `fs_clean_hz` still settles near
16000, before trusting a night of data.
